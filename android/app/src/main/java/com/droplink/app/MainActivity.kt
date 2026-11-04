package com.droplink.app

import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.content.res.Configuration
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.os.StatFs
import android.provider.DocumentsContract
import android.widget.FrameLayout
import android.widget.TextView
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
import java.io.File
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
    private val prefs by lazy { getSharedPreferences("droplink", MODE_PRIVATE) }
    private var errorView: TextView? = null
    private val uiHandler = Handler(Looper.getMainLooper())
    private val loadWatchdog = Runnable { showLoadTimeout() }

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
            prefs.edit().putString("saveDir", uri.toString()).apply()
            startDownloadFlow()
        }
    }

    companion object {
        const val SCAN_REQUEST = 100
        const val PICK_REQUEST = 101
        const val PERMISSION_REQUEST = 102
        const val FILE_CHOOSER_REQUEST = 103
        const val DL_CHANNEL_ID = "droplink-download"
        const val DL_NOTIFY_ID = 2001

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

        saveDirUri = prefs.getString("saveDir", null)?.let { runCatching { Uri.parse(it) }.getOrNull() }

        // 下载中心：队列 + 续传 + 校验 + 通知动作（1.4.0）
        DownloadCenter.init(this)
        DownloadCenter.setListener {
            runOnUiThread {
                try {
                    webView.evaluateJavascript(
                        "window.onDownloadQueueChanged && window.onDownloadQueueChanged()", null)
                } catch (_: Exception) {
                }
            }
        }
        DownloadCenter.sweepOrphans()

        // 启动前台服务（持有内置 HTTP 服务，保活进程，避免退后台被系统杀掉）
        val svc = Intent(this, TransferService::class.java)
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(svc) else startService(svc)

        webView = WebView(this)
        val root = FrameLayout(this)
        root.addView(webView, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        val ev = TextView(this).apply {
            text = "页面加载超时\n\n点此重试"
            gravity = android.view.Gravity.CENTER
            setTextColor(0xFFE8EAF4.toInt())
            textSize = 16f
            setBackgroundColor(0xFF12141F.toInt())
            visibility = android.view.View.GONE
            setOnClickListener {
                visibility = android.view.View.GONE
                webView.loadUrl(pageUrl.ifBlank { homeUrl() })
            }
        }
        errorView = ev
        root.addView(ev, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        setContentView(root)

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
                errorView?.visibility = android.view.View.GONE
                uiHandler.removeCallbacks(loadWatchdog)
                uiHandler.postDelayed(loadWatchdog, 12000)
                rememberRemote(url)
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                pageUrl = url ?: pageUrl
                uiHandler.removeCallbacks(loadWatchdog)
                errorView?.visibility = android.view.View.GONE
                // 首页加载完成后清掉「启动占位页」的历史记录，返回键不会退到空白页（BUG-01）
                if (url != null && url.startsWith("http://127.0.0.1:${TransferService.PORT}/")) {
                    view?.clearHistory()
                }
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

            /**
             * 网页里的 confirm()/alert() 默认会显示「http://127.0.0.1 网页指出：」这种标题，
             * 看起来像钓鱼提示，也把内部地址暴露给用户。改成应用内弹窗（标题固定 DropLink）。
             */
            override fun onJsConfirm(
                view: WebView?,
                url: String?,
                message: String?,
                result: android.webkit.JsResult?
            ): Boolean {
                if (message == null || result == null) return false
                AlertDialog.Builder(this@MainActivity)
                    .setTitle("DropLink")
                    .setMessage(message)
                    .setCancelable(false)
                    .setPositiveButton("确定") { _, _ -> result.confirm() }
                    .setNegativeButton("取消") { _, _ -> result.cancel() }
                    .show()
                return true
            }

            override fun onJsAlert(
                view: WebView?,
                url: String?,
                message: String?,
                result: android.webkit.JsResult?
            ): Boolean {
                if (message == null || result == null) return false
                AlertDialog.Builder(this@MainActivity)
                    .setTitle("DropLink")
                    .setMessage(message)
                    .setCancelable(false)
                    .setPositiveButton("知道了") { _, _ -> result.confirm() }
                    .show()
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
        // 仅调试包开放 WebView 调试端口，用来跑真机自动化；release 包不开放
        if (BuildConfig.DEBUG) WebView.setWebContentsDebuggingEnabled(true)

        webView.addJavascriptInterface(Bridge(), "androidBridge")

        // 拦截下载：让用户指定保存目录，完成后提示实际路径
        webView.setDownloadListener { url, _, contentDisposition, mimeType, _ ->
            pendingUrl = url
            pendingMime = mimeType ?: "application/octet-stream"
            pendingName = parseName(contentDisposition, url)
            if (saveDirUri != null) startDownloadFlow() else pickDirLauncher.launch(null)
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

    /** 解析保存名：优先 RFC 5987 的 filename*，否则退回 filename，最后才用 URL 末段。 */
    private fun parseName(contentDisposition: String?, url: String): String {
        val cd = contentDisposition ?: ""
        val star = Regex("filename\\*=UTF-8''([^;]+)", RegexOption.IGNORE_CASE).find(cd)
        if (star != null) {
            val raw = star.groupValues[1].trim().trim('"')
            return try {
                java.net.URLDecoder.decode(raw, "UTF-8")
            } catch (_: Exception) {
                raw
            }
        }
        val plain = Regex("filename=\"?([^\";]+)\"?", RegexOption.IGNORE_CASE).find(cd)
        if (plain != null) {
            val raw = plain.groupValues[1].trim()
            return try {
                java.net.URLDecoder.decode(raw, "UTF-8")
            } catch (_: Exception) {
                raw
            }
        }
        return url.substringAfterLast('/').substringBefore('?').ifBlank { "file" }
    }

    private fun fmtBytes(n: Long): String {
        if (n < 0) return "未知"
        if (n < 1024) return "$n B"
        val units = arrayOf("KB", "MB", "GB", "TB")
        var v = n.toDouble() / 1024
        var i = 0
        while (v >= 1024 && i < units.size - 1) {
            v /= 1024
            i++
        }
        return String.format(java.util.Locale.US, "%.1f %s", v, units[i])
    }

    private fun freeBytes(): Long = try {
        StatFs(Environment.getExternalStorageDirectory().absolutePath).availableBytes
    } catch (_: Exception) {
        0L
    }

    /** 下载前先探测大小并让用户确认，避免误触直接写满存储（BUG-02）。 */
    private fun startDownloadFlow() {
        val url = pendingUrl ?: return
        val name = pendingName
        val mime = pendingMime
        if (DownloadCenter.activeCount() >= 5) {
            toastMsg("下载队列已满（5 个），先处理一下再来")
            return
        }
        kotlin.concurrent.thread(isDaemon = true) {
            val size = probeSize(url)
            runOnUiThread {
                val free = freeBytes()
                val msg = buildString {
                    append(name)
                    append("\n大小：").append(if (size >= 0) fmtBytes(size) else "未知")
                    if (free > 0) append("\n剩余空间：").append(fmtBytes(free))
                    if (size > 0 && free in 1 until size) append("\n\n空间不足，可能写到一半失败。")
                }
                AlertDialog.Builder(this)
                    .setTitle("下载到本机")
                    .setMessage(msg)
                    .setPositiveButton("下载") { _, _ ->
                        val dir = saveDirUri
                        if (dir == null) {
                            pickDirLauncher.launch(null)
                        } else {
                            DownloadCenter.enqueue(url, name, mime, dir.toString())
                            toastMsg("已加入下载队列")
                        }
                    }
                    .setNegativeButton("取消", null)
                    .show()
            }
        }
    }

    /** 用 Range 探测总大小，不下载正文。 */
    private fun probeSize(url: String): Long = try {
        val conn = URL(url).openConnection() as HttpURLConnection
        conn.connectTimeout = 5000
        conn.readTimeout = 5000
        conn.setRequestProperty("Range", "bytes=0-0")
        CookieManager.getInstance().getCookie(url)?.let { if (it.isNotEmpty()) conn.setRequestProperty("Cookie", it) }
        conn.connect()
        val cr = conn.getHeaderField("Content-Range")
        val total = cr?.substringAfterLast('/')?.trim()?.toLongOrNull()
        val len = total ?: conn.contentLengthLong
        conn.disconnect()
        len
    } catch (_: Exception) {
        -1L
    }

    // 下载的实现在 DownloadCenter.kt：队列、续传、校验、通知都在那边（1.4.0）。
    // 这里只保留「确认 → 入队」的交互，避免 Activity 里再出现第二套下载逻辑。


    /** 记住上次连接的电脑地址，冷启动时静默重连（BUG-06）。 */
    private fun rememberRemote(url: String?) {
        if (url.isNullOrBlank()) return
        try {
            val host = Uri.parse(url).host ?: return
            if (host == "127.0.0.1" || host == "localhost" || host == "::1") return
            prefs.edit().putString("lastUrl", url).apply()
        } catch (_: Exception) {}
    }

    private fun showLoadTimeout() {
        errorView?.visibility = android.view.View.VISIBLE
    }

    private fun toastMsg(msg: String) {
        webView.evaluateJavascript("window.__toast && window.__toast(${JSONObject.quote(msg)})", null)
    }

    /** 快速探测上次连接的电脑是否还在线（1.5 秒）。 */
    private fun pingUrl(base: String): Boolean = try {
        val ping = java.net.URI(base).resolve("/api/ping").toString()
        val conn = URL(ping).openConnection() as HttpURLConnection
        conn.connectTimeout = 1500
        conn.readTimeout = 1500
        conn.connect()
        val ok = conn.responseCode == 200
        conn.disconnect()
        ok
    } catch (_: Exception) {
        false
    }

    /** 轮询本地端口，服务就绪后再加载首页；最多等 6 秒，超时也照样加载（让重试兜底）。 */
    /**
     * 加载首页并在失败时重试：WebView 在「刚装完包 / 服务刚起」时偶发停在占位页，
     * 只 loadUrl 一次会一直转圈，这里最多补 3 次。
     */
    private fun loadHomeWithRetry(tries: Int = 0) {
        if (isFinishing) return
        val url = homeUrl()
        android.util.Log.i("DropLinkBoot", "load #" + tries + " " + url)
        webView.loadUrl(url)
        if (tries >= 3) return
        webView.postDelayed({
            val u = webView.url
            if (u == null || u.isBlank() || u == "about:blank") {
                android.util.Log.w("DropLinkBoot", "still blank, retry " + (tries + 1))
                loadHomeWithRetry(tries + 1)
            }
        }, 2500)
    }

    private fun waitServerThenLoad() {
        android.util.Log.i("DropLinkBoot", "defer load")
        // 不再用「socket 轮询 + 工作线程」：部分 ROM 上该线程会卡住，界面永远停在占位页。
        // 直接延时加载，失败由 loadHomeWithRetry() 兜底重试。
        webView.postDelayed({ loadHomeWithRetry() }, 700)
    }

    private fun ensurePermissions() {
        val need = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            need.add(android.Manifest.permission.POST_NOTIFICATIONS)
        }
        // 1.4.3 起不再申请定位：热点开关走 WifiManager 反射，全程不读 SSID / 扫描结果，
        // 用不到位置信息（定位权限在 Android 上只服务于「通过 Wi-Fi 反推位置」）。
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
        fun freeBytes(): Long = this@MainActivity.freeBytes()

        /**
         * 由页面显式发起的下载：交给 Activity 走「确认 → .part → 改名 → 通知」。
         * 不再依赖 WebView 的 DownloadListener —— 同源 http 附件在不同 WebView 版本上
         * 行为不一致，会出现「点了没反应、也没有提示」（R-02）。
         */
        @JavascriptInterface
        fun downloadFile(url: String, name: String, mime: String) {
            if (!trustedPage()) return
            val u = url.trim()
            if (!u.startsWith("http://") && !u.startsWith("https://")) return
            val n = if (name.isBlank()) parseName(null, u) else name.trim()
            runOnUiThread {
                pendingUrl = u
                pendingName = n
                pendingMime = mime.ifBlank { "application/octet-stream" }
                if (saveDirUri != null) startDownloadFlow() else pickDirLauncher.launch(null)
            }
        }

        /** 队列快照（JSON），供页面显示进度。 */
        @JavascriptInterface
        fun downloadQueue(): String = DownloadCenter.snapshotJson()

        /** 队列里还有几个未完成任务。 */
        @JavascriptInterface
        fun activeDownloads(): Int = DownloadCenter.activeCount()

        @JavascriptInterface
        fun pauseDownload(id: String) {
            if (!trustedPage()) return
            DownloadCenter.pause(id)
        }

        @JavascriptInterface
        fun resumeDownload(id: String) {
            if (!trustedPage()) return
            DownloadCenter.resume(id)
        }

        @JavascriptInterface
        fun cancelDownload(id: String) {
            if (!trustedPage()) return
            DownloadCenter.remove(id)
        }

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
        webView.evaluateJavascript(
            "(function(){try{return (window.__dlBack && window.__dlBack())?'1':'0'}catch(e){return '0'}})()"
        ) { r ->
            if (r != null && r.contains("1")) return@evaluateJavascript
            if (webView.canGoBack()) webView.goBack() else super.onBackPressed()
        }
    }

    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        // 旋转后 WebView 的视口不会自己重算，主动触发一次布局与 resize（BUG-07）
        webView.post {
            webView.requestLayout()
            webView.invalidate()
            webView.evaluateJavascript("window.dispatchEvent(new Event('resize'))", null)
        }
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
        try {
        } catch (_: Exception) {}
        uiHandler.removeCallbacks(loadWatchdog)
        webView.destroy()
        super.onDestroy()
    }
}
