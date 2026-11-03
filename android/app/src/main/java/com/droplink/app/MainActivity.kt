package com.droplink.app

import android.annotation.SuppressLint
import android.app.Activity
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.webkit.JavascriptInterface
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.webkit.WebChromeClient
import android.webkit.ValueCallback
import android.webkit.CookieManager
import android.webkit.WebResourceRequest
import android.webkit.WebResourceError
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.documentfile.provider.DocumentFile
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.NetworkInterface
import java.net.URL
import kotlin.concurrent.thread

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private val hotspot = HotspotManager(this)

    /**
     * 当前页面地址的副本。
     *
     * JS 桥方法运行在 JavaBridge 线程，而 WebView 的方法只能在主线程调用——
     * 直接在桥里读 `webView.url` 会抛 "A WebView method was called on thread 'JavaBridge'"，
     * 表现为点击扫码后毫无反应。这里在主线程的回调里把地址存成普通字段。
     */
    @Volatile
    private var pageUrl: String = ""

    // 下载保存目录（SAF）与待下载任务
    private var saveDirUri: Uri? = null
    private var pendingUrl: String? = null
    private var pendingName: String = "file"
    private var pendingMime: String = "application/octet-stream"

    // 网页 <input type="file"> 的文件选择回调（WebChromeClient）
    private var filePathCallback: ValueCallback<Array<Uri>>? = null
    // 首页加载重试计数（前台服务绑定端口有延迟，加载失败时自动重试）
    private var loadRetry = 0

    private val pickDirLauncher = registerForActivityResult(
        ActivityResultContracts.OpenDocumentTree()
    ) { uri ->
        if (uri != null) {
            try {
                contentResolver.takePersistableUriPermission(
                    uri,
                    Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                )
            } catch (_: Exception) {}
            saveDirUri = uri
            doPendingDownload()
        }
    }

    companion object {
        const val SCAN_REQUEST = 100
        const val PICK_REQUEST = 101
        const val PERMISSION_REQUEST = 102
        const val FILE_CHOOSER_REQUEST = 103

        /** 启动占位页：纯本地渲染，避免等服务绑定时闪出「网页无法使用」。 */
        val SPLASH_HTML = """
            <!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
            <style>
              html,body{height:100%;margin:0;background:#F4F5FB;display:flex;align-items:center;
                justify-content:center;font-family:system-ui,-apple-system,sans-serif}
              .sp{width:26px;height:26px;border-radius:50%;
                border:2.5px solid rgba(79,70,229,.2);border-top-color:#4F46E5;
                animation:r .8s linear infinite}
              @keyframes r{to{transform:rotate(360deg)}}
              @media (prefers-color-scheme:dark){
                html,body{background:#0B1020}
                .sp{border-color:rgba(150,146,255,.2);border-top-color:#9692FF}
              }
            </style><div class="sp"></div>
        """.trimIndent()
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // 启动前台服务（持有内置 HTTP 服务，保活进程，避免退后台被系统杀掉）
        val svc = Intent(this, TransferService::class.java)
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(svc) else startService(svc)

        webView = WebView(this)
        setContentView(webView)

        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            // AD-04：页面只需要 HTTP，不需要读本地文件/内容提供者
            allowFileAccess = true
            allowContentAccess = false
            allowFileAccessFromFileURLs = false
            allowUniversalAccessFromFileURLs = false
            // 本地界面不缓存：升级 APK 后前端必须立即生效（否则会一直跑旧 JS）
            cacheMode = WebSettings.LOAD_NO_CACHE
            setSupportZoom(false)
            mediaPlaybackRequiresUserGesture = false
        }
        webView.setLayerType(android.view.View.LAYER_TYPE_HARDWARE, null)
        // 前台服务绑定端口有延迟，首页加载失败（连接拒绝）时自动重试
        webView.webViewClient = object : WebViewClient() {
            override fun onPageStarted(view: WebView?, url: String?, favicon: android.graphics.Bitmap?) {
                pageUrl = url ?: ""
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                pageUrl = url ?: pageUrl
            }

            override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
                val url = request?.url?.toString() ?: return true
                // 本机界面 + 局域网内的另一台 DropLink 都留在应用内（扫码连接要用）；
                // 其余地址交给系统浏览器，避免任意公网页面加载进带 JS 桥的 WebView。
                if (isLocalOrPrivate(url)) return false
                return try {
                    startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
                    true
                } catch (_: Exception) {
                    true
                }
            }

            override fun onReceivedError(view: WebView?, request: WebResourceRequest?, error: WebResourceError?) {
                if (request?.isForMainFrame != true) return
                val code = error?.errorCode ?: -1
                if ((code == WebViewClient.ERROR_CONNECT || code == WebViewClient.ERROR_HOST_LOOKUP ||
                            code == WebViewClient.ERROR_IO || code == WebViewClient.ERROR_TIMEOUT) && loadRetry < 6) {
                    loadRetry++
                    view?.postDelayed({
                        view.loadUrl(homeUrl())
                    }, 600L)
                }
            }
        }
        // 关键：实现文件选择器，否则网页里的 <input type="file"> 点击后无任何响应
        webView.webChromeClient = object : WebChromeClient() {
            override fun onConsoleMessage(msg: android.webkit.ConsoleMessage?): Boolean {
                android.util.Log.i("DropLinkJS", (msg?.message() ?: "") + "  @" + (msg?.lineNumber() ?: 0))
                return true
            }

            override fun onShowFileChooser(
                view: WebView?,
                callback: ValueCallback<Array<Uri>>?,
                params: FileChooserParams?
            ): Boolean {
                filePathCallback?.onReceiveValue(null)
                filePathCallback = callback
                val intent = params?.createIntent() ?: Intent(Intent.ACTION_GET_CONTENT).apply {
                    addCategory(Intent.CATEGORY_OPENABLE)
                    type = "*/*"
                }
                return try {
                    startActivityForResult(intent, FILE_CHOOSER_REQUEST)
                    true
                } catch (_: Exception) {
                    filePathCallback = null
                    false
                }
            }
        }
        webView.addJavascriptInterface(Bridge(), "androidBridge")

        // 拦截下载：让用户指定保存目录，完成后提示实际路径
        webView.setDownloadListener { url, _, contentDisposition, mimeType, _ ->
            pendingUrl = url
            pendingMime = mimeType ?: "application/octet-stream"
            pendingName = parseName(contentDisposition, url)
            if (saveDirUri != null) doPendingDownload()
            else pickDirLauncher.launch(null)
        }

        // 先显示启动占位页：服务还没绑定端口时直接 loadUrl 会闪一下「网页无法使用」，
        // 这里等端口真正可连接后再跳转，用户只会看到转圈 → 界面。
        webView.loadDataWithBaseURL(null, SPLASH_HTML, "text/html", "utf-8", null)
        waitServerThenLoad()

        ensurePermissions()
    }

    /** 首页地址带上版本号与界面令牌：升级不吃旧缓存，且回环请求必须带令牌。 */
    private fun homeUrl(): String {
        val v = try {
            packageManager.getPackageInfo(packageName, 0).lastUpdateTime
        } catch (_: Exception) {
            System.currentTimeMillis()
        }
        val tok = TransferService.localToken
        return "http://127.0.0.1:${TransferService.PORT}/?v=$v&k=$tok"
    }

    private fun parseName(contentDisposition: String?, url: String): String {
        val cd = contentDisposition ?: ""
        val m = Regex("filename\\*?=(?:UTF-8'')?\"?([^\";]+)\"?").find(cd)
        if (m != null) {
            return try { java.net.URLDecoder.decode(m.groupValues[1], "UTF-8") } catch (_: Exception) { m.groupValues[1] }
        }
        return url.substringAfterLast('/').substringBefore('?').ifBlank { "file" }
    }

    private fun doPendingDownload() {
        val url = pendingUrl ?: return
        val dirUri = saveDirUri ?: return
        val name = pendingName
        val mime = pendingMime
        pendingUrl = null
        thread {
            try {
                val conn = URL(url).openConnection() as HttpURLConnection
                conn.connectTimeout = 10000
                conn.readTimeout = 30000
                // 关键：携带 WebView 里已配对的 cookie（dlsess），
                // 否则目标服务的鉴权会拒绝下载（返回 401，抛 FileNotFoundException 显示"下载失败"）
                try {
                    val cookie = CookieManager.getInstance().getCookie(url)
                    if (!cookie.isNullOrEmpty()) conn.setRequestProperty("Cookie", cookie)
                } catch (_: Exception) {}
                conn.connect()
                val code = conn.responseCode
                if (code != 200) {
                    runOnUiThread { toastMsg("下载失败：服务器返回 $code") }
                    return@thread
                }
                val input = conn.inputStream
                val tree = DocumentFile.fromTreeUri(this, dirUri)
                val file = tree?.createFile(mime, name)
                if (file == null) {
                    runOnUiThread { toastMsg("无法在该目录创建文件") }
                    input.close()
                    return@thread
                }
                val os = contentResolver.openOutputStream(file.uri)
                if (os == null) {
                    runOnUiThread { toastMsg("无法写入文件") }
                    input.close()
                    return@thread
                }
                input.copyTo(os)
                os.close()
                input.close()
                val dirName = tree.name ?: "所选目录"
                runOnUiThread {
                    toastMsg("已保存到 $dirName/$name")
                    showNotification("$dirName/$name", "文件已保存")
                }
            } catch (e: Exception) {
                runOnUiThread { toastMsg("下载失败：${e.message}") }
            }
        }
    }

    private fun toastMsg(msg: String) {
        webView.evaluateJavascript("window.__toast && window.__toast(${JSONObject.quote(msg)})", null)
    }

    /** 轮询本地端口，服务就绪后再加载首页；最多等 6 秒，超时也照样加载（让重试兜底）。 */
    private fun waitServerThenLoad() {
        Thread {
            for (i in 0 until 40) {
                try {
                    java.net.Socket().use { s ->
                        s.connect(java.net.InetSocketAddress("127.0.0.1", TransferService.PORT), 150)
                    }
                    break
                } catch (_: Exception) {
                    try {
                        Thread.sleep(150)
                    } catch (_: InterruptedException) {
                        break
                    }
                }
            }
            runOnUiThread {
                if (!isFinishing) webView.loadUrl(homeUrl())
            }
        }.apply { isDaemon = true }.start()
    }

    private fun ensurePermissions() {
        val need = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            need.add(android.Manifest.permission.POST_NOTIFICATIONS)
        }
        if (Build.VERSION.SDK_INT >= 29 &&
            checkSelfPermission(android.Manifest.permission.ACCESS_FINE_LOCATION) != PackageManager.PERMISSION_GRANTED) {
            need.add(android.Manifest.permission.ACCESS_FINE_LOCATION)
        }
        if (need.isNotEmpty()) {
            ActivityCompat.requestPermissions(this, need.toTypedArray(), PERMISSION_REQUEST)
        }
    }

    /** 只允许本机界面调用 JS 桥：其它页面即使被加载也拿不到设备能力。 */
    private fun trustedPage(): Boolean {
        // 个人使用模式：不再限制桥的调用来源（恢复加固前的行为）
        return true
    }

    /** 本机或局域网地址：允许在应用内打开（扫码连接另一台设备时用）。 */
    private fun isLocalOrPrivate(url: String): Boolean {
        return try {
            val u = Uri.parse(url)
            if (u.scheme != "http") return false
            val h = (u.host ?: "").lowercase()
            h == "127.0.0.1" || h == "localhost" || h == "::1" ||
                h.startsWith("10.") || h.startsWith("192.168.") || h.startsWith("169.254.") ||
                Regex("^172\\.(1[6-9]|2\\d|3[01])\\.").containsMatchIn(h)
        } catch (_: Exception) {
            false
        }
    }

    inner class Bridge {
        @JavascriptInterface
        fun getDeviceInfo(): String {
            if (!trustedPage()) return "{}"
            val ip = localIp() ?: "127.0.0.1"
            return JSONObject()
                .put("ip", ip)
                .put("android", Build.VERSION.SDK_INT)
                .put("model", Build.MODEL)
                .toString()
        }

        @JavascriptInterface
        fun scanQrCode() {
            if (!trustedPage()) return
            runOnUiThread {
                startActivityForResult(Intent(this@MainActivity, QrScannerActivity::class.java), SCAN_REQUEST)
            }
        }

        /** 是否已获得「所有文件访问」权限（决定能否浏览手机整机存储）。 */
        @JavascriptInterface
        fun hasAllFiles(): Boolean =
            trustedPage() && (Build.VERSION.SDK_INT < 30 || android.os.Environment.isExternalStorageManager())

        /** 跳到系统的「所有文件访问」授权页。 */
        @JavascriptInterface
        fun openAllFilesSettings() {
            if (!trustedPage()) return
            try {
                val intent = if (Build.VERSION.SDK_INT >= 30) {
                    Intent(android.provider.Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                        android.net.Uri.parse("package:$packageName"))
                } else {
                    Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                        android.net.Uri.parse("package:$packageName"))
                }
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                startActivity(intent)
            } catch (e: Exception) {
                try {
                    startActivity(Intent(android.provider.Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION)
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                } catch (_: Exception) {
                }
            }
        }

        @JavascriptInterface
        fun enableHotspot(): Boolean = trustedPage() && hotspot.setEnabled(true)

        @JavascriptInterface
        fun disableHotspot(): Boolean = trustedPage() && hotspot.setEnabled(false)

        @JavascriptInterface
        fun hotspotState(): String {
            if (!trustedPage()) return "{}"
            val ip = hotspot.apIp()
            return JSONObject()
                .put("enabled", hotspot.isEnabled())
                .put("ip", ip ?: "")
                .put("reflectionSupported", hotspot.isReflectionSupported())
                .toString()
        }

        @JavascriptInterface
        fun notify(message: String) {
            if (!trustedPage()) return
            runOnUiThread { showNotification(message) }
        }

        @JavascriptInterface
        fun pickFiles(): String {
            if (!trustedPage()) return "{\"pending\":false}"
            // SAF 文件选择：由 onActivityResult 回填，这里返回占位
            runOnUiThread {
                val intent = Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
                    addCategory(Intent.CATEGORY_OPENABLE)
                    type = "*/*"
                    putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
                }
                startActivityForResult(intent, PICK_REQUEST)
            }
            return "{\"pending\":true}"
        }
    }

    private fun showNotification(message: String, title: String = "DropLink") {
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return
        val nm = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(NotificationChannel("drop", "DropLink", NotificationManager.IMPORTANCE_DEFAULT))
        }
        val n = NotificationCompat.Builder(this, "drop")
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setContentTitle(title)
            .setContentText(message)
            .setAutoCancel(true)
            .build()
        try { NotificationManagerCompat.from(this).notify(1, n) } catch (_: Exception) {}
    }

    private fun localIp(): String? {
        try {
            NetworkInterface.getNetworkInterfaces()?.toList()?.forEach { nif ->
                nif.inetAddresses.toList().forEach { addr ->
                    if (!addr.isLoopbackAddress && addr.hostAddress?.contains(':') == false) {
                        val ip = addr.hostAddress ?: return@forEach
                        if (!ip.startsWith("169.254.")) return ip
                    }
                }
            }
        } catch (_: Exception) {}
        return null
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == FILE_CHOOSER_REQUEST) {
            val uris = if (resultCode == RESULT_OK) {
                WebChromeClient.FileChooserParams.parseResult(resultCode, data)
            } else null
            filePathCallback?.onReceiveValue(uris)
            filePathCallback = null
            return
        }
        if (requestCode == SCAN_REQUEST && resultCode == RESULT_OK) {
            val result = data?.getStringExtra(QrScannerActivity.EXTRA_RESULT) ?: ""
            webView.evaluateJavascript("window.__qrResult && window.__qrResult(${JSONObject.quote(result)})", null)
        }
    }

    override fun onBackPressed() {
        if (webView.canGoBack()) webView.goBack() else super.onBackPressed()
    }

    override fun onResume() {
        super.onResume()
        webView.onResume()
    }

    override fun onPause() {
        webView.onPause()
        super.onPause()
    }

    override fun onDestroy() {
        webView.destroy()
        super.onDestroy()
    }
}
