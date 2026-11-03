package com.droplink.app

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Color
import com.google.zxing.BarcodeFormat
import com.google.zxing.EncodeHintType
import com.google.zxing.qrcode.QRCodeWriter
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedOutputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileInputStream
import java.io.OutputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.URLDecoder
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/**
 * 内置 HTTP 服务：提供网页界面（静态资源）+ 文件传输 API 接口。
 * 与电脑版 server.py 接口对齐，WebView 加载 http://127.0.0.1:port/ 即可复用整套 UI。
 */
class HttpServer(
    private val context: Context,
    val port: Int,
    private val rootProvider: () -> File,
    private val localToken: String,
) {
    /** 根目录动态解析：用户授权「所有文件访问」后无需重启服务即可浏览整个存储。 */
    private val rootDir: File get() = rootProvider()

    private val pool = Executors.newCachedThreadPool()
    private var serverSocket: ServerSocket? = null
    private var running = false

    // 配对状态（8 位：与桌面端一致，暴力空间 10^8）
    @Volatile var pin = (100000..999999).random().toString()
    private val sessions = mutableMapOf<String, Long>()
    /** 正在发送的文件（供 /api/live 显示实时速率）。 */
    private class LiveStat(val name: String, val total: Long) {
        val start = System.currentTimeMillis()
        @Volatile var sent = 0L
    }
    private val liveStats = java.util.concurrent.ConcurrentHashMap<String, LiveStat>()

    private val clipItems = mutableListOf<JSONObject>()
    private val transferItems = mutableListOf<JSONObject>()
    private val shareMap = mutableMapOf<String, ShareEntry>()
    private val peers = mutableMapOf<String, JSONObject>()  // ip -> {ts, name}
    private val pending = mutableListOf<JSONObject>()       // 待接收推送
    private var sendDir: File = File(".")
    private var clipId = 0

    data class ShareEntry(val path: File, val exp: Long)

    fun start() {
        rootDir.mkdirs()
        sendDir = File(rootDir, ".droplink-send").apply { mkdirs() }
        running = true
        thread(name = "dl-http") {
            try {
                serverSocket = ServerSocket(port, 64, InetAddress.getByName("0.0.0.0"))
                while (running) {
                    val sock = try { serverSocket?.accept() } catch (_: Exception) { break }
                    sock?.let { pool.execute { handle(it) } }
                }
            } catch (e: Exception) {
                e.printStackTrace()
            }
        }
    }

    fun stop() {
        running = false
        try { serverSocket?.close() } catch (_: Exception) {}
        pool.shutdown()
    }

    private fun localIps(): List<String> {
        val out = mutableListOf<String>()
        try {
            java.net.NetworkInterface.getNetworkInterfaces()?.toList()?.forEach { nif ->
                nif.inetAddresses.toList().forEach { addr ->
                    val ip = addr.hostAddress ?: return@forEach
                    if (!addr.isLoopbackAddress && !ip.contains(':') && !ip.startsWith("169.254.")) {
                        if (ip !in out) out.add(ip)
                    }
                }
            }
        } catch (_: Exception) {}
        return out
    }

    // ---------------- 请求处理 ----------------

    private fun handle(sock: Socket) {
        try {
            sock.soTimeout = 30000
            val input = sock.getInputStream()
            val output = sock.getOutputStream()

            val requestLine = readLine(input) ?: return
            val parts = requestLine.split(" ")
            if (parts.size < 2) return
            val method = parts[0]
            val rawTarget = parts[1]
            val path = URLDecoder.decode(rawTarget.substringBefore('?'), "UTF-8")

            // 读取 header
            val headers = HashMap<String, String>()
            while (true) {
                val line = readLine(input) ?: break
                if (line.isEmpty()) break
                val idx = line.indexOf(':')
                if (idx > 0) headers[line.substring(0, idx).trim().lowercase()] = line.substring(idx + 1).trim()
            }

            val query = parseQuery(rawTarget.substringAfter('?', ""))
            val contentLength = headers["content-length"]?.toIntOrNull() ?: 0
            // 大 body 的 POST（上传/推送）走流式，不读入内存，避免 OOM；其余小 body 一次性读
            val isStreamPost = method == "POST" && (path == "/api/upload" || path == "/api/send")
            // 防御：非流式接口收到超大 body 直接拒绝，避免 ByteArray(contentLength) 撑爆内存
            if (!isStreamPost && contentLength > 64 * 1024 * 1024) {
                return send(output, 413, "application/json; charset=utf-8",
                    """{"ok":false,"error":"请求体过大"}""".toByteArray())
            }
            val body = if (contentLength > 0 && !isStreamPost) {
                val buf = ByteArray(contentLength)
                var off = 0
                while (off < contentLength) {
                    val n = input.read(buf, off, contentLength - off)
                    if (n < 0) break
                    off += n
                }
                if (off == contentLength) buf else buf.copyOf(off)
            } else ByteArray(0)

            val isLocal = sock.inetAddress.isLoopbackAddress
            val ip = sock.inetAddress.hostAddress ?: ""
            val cookie = headers["cookie"] ?: ""
            val sessToken = Regex("dlsess=([^;]+)").find(cookie)?.groupValues?.get(1) ?: ""
            // 个人使用模式：恢复加固前的「本机请求直接放行」行为
            val authed = isLocal || sessions.containsKey(sessToken)

            // 路由分发
            route(method, path, query, headers, body, input, contentLength, output, isLocal, isLocal, authed, ip)
        } catch (_: Exception) {
        } finally {
            try { sock.close() } catch (_: Exception) {}
        }
    }

    private fun readLine(input: java.io.InputStream): String? {
        val sb = StringBuilder()
        while (true) {
            val b = input.read()
            if (b == -1) return if (sb.isEmpty()) null else sb.toString()
            if (b == '\n'.code) break
            if (b != '\r'.code) sb.append(b.toChar())
        }
        return sb.toString()
    }

    private fun parseQuery(q: String): Map<String, String> {
        val map = HashMap<String, String>()
        if (q.isNotEmpty()) {
            q.split('&').forEach { kv ->
                val i = kv.indexOf('=')
                if (i > 0) map[kv.substring(0, i)] = URLDecoder.decode(kv.substring(i + 1), "UTF-8")
            }
        }
        return map
    }

    // ---------------- 路由 ----------------

    private fun route(method: String, path: String, query: Map<String, String>, headers: Map<String, String>,
                      body: ByteArray, input: java.io.InputStream, contentLength: Int, out: OutputStream,
                      isLocal: Boolean, localUi: Boolean, authed: Boolean, ip: String) {

        if (method == "GET") {
            if (path == "/api/session") {
                if (!authed) return send(out, 401, "application/json; charset=utf-8",
                    """{"ok":false,"error":"需要配对码","need_login":true}""".toByteArray())
                return sendJson(out, sessionJson(localUi, authed))
            }
            // 需鉴权（仅 /api/* 数据接口；静态首页、二维码、分享直链、登录、健康检查一律放行）
            if (!authed && path.startsWith("/api/") && path != "/api/login" && path != "/api/ping" && path != "/api/icon") {
                return send(out, 401, "application/json; charset=utf-8",
                    """{"ok":false,"error":"需要配对码","need_login":true}""".toByteArray())
            }
            if (path == "/api/list") return sendJson(out, apiList(query["path"]))
            if (path == "/api/roots") return sendJson(out, apiRoots())
            if (path == "/api/download") return apiDownload(query, headers, out)
            if (path == "/api/preview") return apiPreview(query, headers, out)
            if (path == "/api/search") return sendJson(out, apiSearch(query["q"]))
            if (path == "/api/clip") return sendJson(out, clipJson())
            if (path == "/api/transfers") return sendJson(out, transfersJson())
            if (path == "/api/config") return sendJson(out, JSONObject().put("ok", true).put("recv_dir", rootDir.absolutePath))
            if (path == "/api/diag") return sendJson(out, diagJson())
            if (path == "/api/networks") return sendJson(out, networksJson())
            if (path == "/api/peers") return sendJson(out, peersJson())
            if (path == "/api/live") return sendJson(out, liveJson())
            if (path == "/api/inbox") return sendJson(out, inboxJson(ip))
            if (path == "/qr.png") return sendQr(query["text"], out)
            if (path == "/api/icon") return sendQr("DropLink", out)
            if (path.startsWith("/dl/")) return handleDl(path, headers, out)
            if (path == "/api/ping") return sendJson(out, JSONObject().put("ok", true))
            return serveStatic(path, out)
        }

        if (method == "POST") {
            val data = try { if (body.isNotEmpty()) JSONObject(String(body, Charsets.UTF_8)) else JSONObject() } catch (_: Exception) { JSONObject() }
            when (path) {
                "/api/login" -> return apiLogin(data, out, localUi, ip)
                "/api/send" -> if (localUi) return apiSendStream(input, contentLength, query, out)
            }
            if (!authed) {
                return send(out, 401, "application/json; charset=utf-8",
                    """{"ok":false,"error":"需要配对码","need_login":true}""".toByteArray())
            }
            when (path) {
                "/api/upload" -> return apiUploadStream(input, contentLength, out, query)
                "/api/clip" -> return apiClipAdd(data, out)
                "/api/clip/delete" -> return apiClipDel(data, out)
                "/api/delete" -> return apiDelete(data, out)
                "/api/mkdir" -> return apiMkdir(data, out)
                "/api/rename" -> return apiRename(data, out)
                "/api/zip" -> return apiZip(data, out)
                "/api/share" -> return apiShare(data, out)
                "/api/inbox/ack" -> return apiInboxAck(data, out, ip)
                "/api/config" -> return sendJson(out, JSONObject().put("ok", true).put("recv_dir", rootDir.absolutePath))
            }
        }

        send(out, 404, "application/json; charset=utf-8", """{"ok":false,"error":"接口不存在"}""".toByteArray())
    }

    // ---------------- API 实现 ----------------

    /** 直接读安装包版本，避免源码里再写一份容易忘记同步的常量。 */
    private fun appVersion(): String = try {
        context.packageManager.getPackageInfo(context.packageName, 0).versionName ?: "1.1.0"
    } catch (_: Exception) {
        "1.1.0"
    }

    private fun sessionJson(localUi: Boolean, authed: Boolean): JSONObject {
        val urls = localIps().map { "http://$it:$port" }
        val obj = JSONObject()
            .put("ok", true)
            .put("authed", authed)
            .put("local", localUi)
            .put("device", android.os.Build.MODEL)
            .put("platform", "android")
            .put("app", "DropLink")
            .put("version", appVersion())
            .put("recv_dir", rootDir.absolutePath)
            .put("lan_urls", JSONArray(urls))
            .put("qr_target", urls.firstOrNull() ?: "")
            .put("qr_available", true)
            .put("thumb_available", false)
            .put("all_files", android.os.Build.VERSION.SDK_INT < 30 ||
                android.os.Environment.isExternalStorageManager())
        // 配对码只对本机可见，避免局域网内未配对设备通过 /api/session 拿到
        if (localUi) obj.put("pin", pin)
        return obj
    }

    private fun safeName(name: String): String =
        name.replace(Regex("[\\\\/:*?\"<>|]"), "_").trim().takeIf { it.isNotEmpty() && it != "." && it != ".." } ?: "_"

    private fun resolvePath(target: String?): File {
        val base = rootDir
        val t = target ?: ""
        // 前缀比较必须带分隔符，否则 /root-evil 会被误判为 /root 之内
        val root = base.canonicalPath + File.separator
        val f = File(t)
        // 绝对路径（如推送暂存目录里的文件）：校验在 rootDir 内即可
        if (f.isAbsolute) {
            val canon = try { f.canonicalPath } catch (_: Exception) { return base }
            return if (canon == base.canonicalPath || canon.startsWith(root)) f else base
        }
        // 相对路径：安全净化后拼到 rootDir
        val rel = File(base, t.replace('/', File.separatorChar))
        val canon = try { rel.canonicalPath } catch (_: Exception) { return base }
        return if (canon == base.canonicalPath || canon.startsWith(root)) rel else base
    }

    private fun entryJson(f: File): JSONObject {
        val isDir = f.isDirectory
        return JSONObject()
            .put("name", f.name)
            .put("path", f.absolutePath)
            .put("dir", isDir)
            .put("size", if (isDir) 0 else f.length())
            .put("mtime", f.lastModified() / 1000)
            .put("kind", if (isDir) "folder" else kindOf(f.name))
            .put("ext", if (isDir) "" else f.extension.lowercase())
            .put("hidden", f.name.startsWith("."))
    }

    private fun kindOf(name: String): String {
        val e = name.substringAfterLast('.', "").lowercase()
        return when (e) {
            "jpg", "jpeg", "png", "gif", "webp", "bmp", "heic" -> "image"
            "mp4", "mkv", "mov", "avi", "webm" -> "video"
            "mp3", "wav", "flac", "aac", "m4a" -> "audio"
            "pdf" -> "pdf"
            "txt", "md", "log", "json", "xml", "js", "css", "html" -> "text"
            "zip", "rar", "7z", "tar", "gz" -> "archive"
            "apk" -> "apk"
            else -> "file"
        }
    }

    private fun apiList(target: String?): JSONObject {
        val dir = resolvePath(target)
        if (!dir.isDirectory) return JSONObject().put("ok", false).put("error", "文件夹不存在")
        val arr = JSONArray()
        dir.listFiles()?.sortedWith(compareBy({ it.isDirectory }, { it.name.lowercase() }))?.forEach {
            if (!it.name.startsWith(".")) arr.put(entryJson(it))
        }
        val parent = dir.parentFile
        return JSONObject()
            .put("ok", true)
            .put("entries", arr)
            .put("path", dir.absolutePath)
            .put("parent", if (parent != null && parent.absolutePath.startsWith(rootDir.absolutePath)) parent.absolutePath else JSONObject.NULL)
    }

    private fun apiRoots(): JSONObject {
        val arr = JSONArray()
        arr.put(JSONObject().put("name", "接收目录").put("path", rootDir.absolutePath).put("kind", "folder"))
        return JSONObject().put("ok", true).put("roots", arr).put("recv_dir", rootDir.absolutePath)
    }

    private fun apiDownload(query: Map<String, String>, headers: Map<String, String>, out: OutputStream) {
        val f = resolvePath(query["path"])
        if (!f.isFile) return sendJson(out, JSONObject().put("ok", false).put("error", "文件不存在"))
        serveFile(f, query["inline"] == "1", out, headers["range"])
    }

    private fun apiPreview(query: Map<String, String>, headers: Map<String, String>, out: OutputStream) {
        val f = resolvePath(query["path"])
        if (!f.isFile) return sendJson(out, JSONObject().put("ok", false).put("error", "文件不存在"))
        serveFile(f, true, out, headers["range"])
    }

    private fun apiSearch(q: String?): JSONObject {
        val kw = q?.lowercase() ?: ""
        if (kw.isEmpty()) return JSONObject().put("ok", true).put("results", JSONArray())
        val results = JSONArray()
        fun walk(dir: File, depth: Int) {
            if (depth > 6 || results.length() > 200) return
            dir.listFiles()?.forEach {
                if (it.name.lowercase().contains(kw)) results.put(entryJson(it))
                if (it.isDirectory) walk(it, depth + 1)
            }
        }
        walk(rootDir, 0)
        return JSONObject().put("ok", true).put("results", results)
    }

    private fun apiLogin(data: JSONObject, out: OutputStream, localUi: Boolean, ip: String) {
        val pin = data.optString("pin")
        // 无论本机还是局域网，都必须给出正确的配对码；本机界面走令牌，不需要登录。
        if (pin != this.pin) {
            return sendJson(out, JSONObject().put("ok", false).put("error", "配对码不正确"))
        }
        val token = java.util.UUID.randomUUID().toString().replace("-", "")
        sessions[token] = System.currentTimeMillis()
        if (!localUi) {
            val name = data.optString("name", "手机设备").take(40)
            peers[ip] = JSONObject().put("ts", System.currentTimeMillis()).put("name", name)
        }
        val cookie = "dlsess=$token; Path=/; Max-Age=2592000; HttpOnly; SameSite=Strict"
        send(out, 200, "application/json; charset=utf-8",
            """{"ok":true,"device":"${android.os.Build.MODEL}"}""".toByteArray(),
            mapOf("Set-Cookie" to cookie))
    }

    private fun apiClipAdd(data: JSONObject, out: OutputStream) {
        val text = data.optString("text", "").take(20000)
        if (text.isEmpty()) return sendJson(out, JSONObject().put("ok", false).put("error", "内容为空"))
        clipId++
        val item = JSONObject().put("id", clipId).put("ts", System.currentTimeMillis() / 1000).put("text", text)
        clipItems.add(0, item)
        if (clipItems.size > 100) clipItems.removeAt(clipItems.size - 1)
        transferItems.add(0, JSONObject().put("id", "t$clipId").put("name", "剪贴板").put("kind", "text")
            .put("dir", "up").put("size", text.length).put("time", System.currentTimeMillis() / 1000))
        sendJson(out, JSONObject().put("ok", true))
    }

    private fun clipJson(): JSONObject = JSONObject().put("ok", true).put("items", JSONArray(clipItems))

    private fun apiClipDel(data: JSONObject, out: OutputStream) {
        val id = data.optInt("id", -1)
        clipItems.removeAll { it.optInt("id") == id }
        sendJson(out, JSONObject().put("ok", true))
    }

    private fun transfersJson(): JSONObject = JSONObject().put("ok", true).put("items", JSONArray(transferItems))

    private fun liveJson(): JSONObject {
        val now = System.currentTimeMillis()
        val arr = JSONArray()
        liveStats.values.forEach { v ->
            val el = maxOf(200.0, (now - v.start).toDouble()) / 1000.0
            arr.put(JSONObject().put("name", v.name).put("total", v.total).put("sent", v.sent)
                .put("speed", (v.sent / el).toLong()).put("elapsed", el))
        }
        return JSONObject().put("ok", true).put("items", arr)
    }

    private fun peersJson(): JSONObject {
        val arr = JSONArray()
        peers.entries.sortedByDescending { it.value.optLong("ts") }.forEach { (ip, v) ->
            arr.put(JSONObject().put("ip", ip).put("ts", v.optLong("ts") / 1000).put("name", v.optString("name", "手机设备")))
        }
        return JSONObject().put("ok", true).put("peers", arr)
    }

    private fun apiSendStream(input: java.io.InputStream, contentLength: Int, query: Map<String, String>, out: OutputStream) {
        val name = safeName(query["name"] ?: "file")
        val targetsRaw = query["targets"] ?: "all"
        val targets = targetsRaw.split(',').map { it.trim() }.filter { it.isNotEmpty() }.toMutableList()
        if (targets.isEmpty() || targets == listOf("all")) { targets.clear(); targets.add("all") }
        val id = java.util.UUID.randomUUID().toString().replace("-", "").substring(0, 12)
        val dest = File(sendDir, "${id}_$name")
        var written = 0L
        try {
            dest.outputStream().use { fos ->
                val buf = ByteArray(64 * 1024)
                var remaining = contentLength
                while (remaining > 0) {
                    val n = input.read(buf, 0, minOf(buf.size, remaining))
                    if (n < 0) break
                    fos.write(buf, 0, n)
                    written += n
                    remaining -= n
                }
            }
        } catch (e: Exception) {
            dest.delete()
            return sendJson(out, JSONObject().put("ok", false).put("error", e.message ?: "写入失败"))
        }
        val item = JSONObject()
            .put("id", id).put("name", name).put("path", dest.absolutePath).put("size", written)
            .put("targets", JSONArray(targets)).put("ack", JSONObject()).put("ts", System.currentTimeMillis())
        pending.add(item)
        sendJson(out, JSONObject().put("ok", true).put("id", id).put("name", name).put("size", written))
    }

    private fun inboxJson(ip: String): JSONObject {
        val arr = JSONArray()
        pending.forEach { item ->
            val ack = item.optJSONObject("ack") ?: JSONObject()
            if (ack.has(ip)) return@forEach
            val targets = item.optJSONArray("targets")
            val isAll = targets != null && targets.length() == 1 && targets.optString(0) == "all"
            if (!isAll && (targets == null || (0 until targets.length()).none { targets.optString(it) == ip })) return@forEach
            arr.put(JSONObject()
                .put("id", item.optString("id"))
                .put("name", item.optString("name"))
                .put("size", item.optLong("size"))
                .put("url", "/api/download?path=" + java.net.URLEncoder.encode(item.optString("path"), "UTF-8")))
        }
        return JSONObject().put("ok", true).put("items", arr)
    }

    private fun apiInboxAck(data: JSONObject, out: OutputStream, ip: String) {
        val id = data.optString("id")
        pending.forEach { item ->
            if (item.optString("id") == id) {
                val ack = item.optJSONObject("ack") ?: JSONObject()
                ack.put(ip, System.currentTimeMillis())
                item.put("ack", ack)
            }
        }
        sendJson(out, JSONObject().put("ok", true))
    }

    private fun diagJson(): JSONObject {
        val urls = localIps().map { "http://$it:$port" }
        return JSONObject()
            .put("ok", true)
            .put("urls", JSONArray(urls))
            .put("fw_rule", true)
            .put("profiles", JSONArray())
    }

    private fun networksJson(): JSONObject {
        val arr = JSONArray()
        localIps().forEach { ip ->
            arr.put(JSONObject().put("ip", ip).put("alias", if (ip.startsWith("192.168.43.")) "热点" else "网络"))
        }
        return JSONObject().put("ok", true).put("port", port).put("networks", arr)
    }

    private fun apiDelete(data: JSONObject, out: OutputStream) {
        val paths = data.optJSONArray("paths")
        if (paths == null) return sendJson(out, JSONObject().put("ok", false).put("error", "缺少路径"))
        var all = true
        var blocked = 0
        for (i in 0 until paths.length()) {
            // 严格解析：不在根目录内的路径返回 null（以前会回退成根目录，等于把整个接收目录删掉）
            val f = resolvePathStrict(paths.optString(i))
            if (f == null || f.canonicalPath == rootDir.canonicalPath) {
                blocked++
                all = false
                continue
            }
            all = all and f.deleteRecursively()
        }
        sendJson(out, JSONObject().put("ok", all).put("blocked", blocked))
    }

    /** 只允许根目录内部的路径；越界返回 null。 */
    private fun resolvePathStrict(target: String?): File? {
        if (target.isNullOrBlank()) return null
        val f = File(target)
        val root = rootDir.canonicalPath + File.separator
        val p = try {
            f.canonicalPath
        } catch (e: Exception) {
            return null
        }
        return if (p.startsWith(root)) f else null
    }

    private fun apiMkdir(data: JSONObject, out: OutputStream) {
        val name = safeName(data.optString("name"))
        val parent = resolvePath(data.optString("path"))
        val ok = File(parent, name).mkdirs()
        sendJson(out, JSONObject().put("ok", ok))
    }

    private fun apiRename(data: JSONObject, out: OutputStream) {
        val from = resolvePath(data.optString("path"))
        val to = File(from.parentFile, safeName(data.optString("name")))
        val ok = from.renameTo(to)
        sendJson(out, JSONObject().put("ok", ok))
    }

    private fun apiZip(data: JSONObject, out: OutputStream) {
        val paths = data.optJSONArray("paths") ?: return sendJson(out, JSONObject().put("ok", false))
        val zipFile = File(rootDir, "打包_${System.currentTimeMillis()}.zip")
        try {
            java.util.zip.ZipOutputStream(BufferedOutputStream(zipFile.outputStream())).use { zos ->
                fun add(file: File, base: String) {
                    if (file.isDirectory) {
                        zos.putNextEntry(java.util.zip.ZipEntry("$base/"))
                        zos.closeEntry()
                        file.listFiles()?.forEach { add(it, "$base/${it.name}") }
                    } else {
                        zos.putNextEntry(java.util.zip.ZipEntry(base))
                        file.inputStream().use { it.copyTo(zos) }
                        zos.closeEntry()
                    }
                }
                for (i in 0 until paths.length()) {
                    val f = resolvePath(paths.optString(i))
                    if (f.exists()) add(f, f.name)
                }
            }
            sendJson(out, JSONObject().put("ok", true).put("path", zipFile.absolutePath))
        } catch (e: Exception) {
            sendJson(out, JSONObject().put("ok", false).put("error", e.message ?: "打包失败"))
        }
    }

    private fun apiShare(data: JSONObject, out: OutputStream) {
        val f = resolvePath(data.optString("path"))
        if (!f.isFile) return sendJson(out, JSONObject().put("ok", false).put("error", "文件不存在"))
        val token = java.util.UUID.randomUUID().toString().replace("-", "").substring(0, 16)
        shareMap[token] = ShareEntry(f, System.currentTimeMillis() + 86400000L)
        val host = localIps().firstOrNull() ?: "127.0.0.1"
        sendJson(out, JSONObject().put("ok", true).put("url", "http://$host:$port/dl/$token"))
    }

    private fun handleDl(path: String, headers: Map<String, String>, out: OutputStream) {
        val rest = path.removePrefix("/dl/").trim('/')
        val isFile = rest.endsWith("/file")
        val token = if (isFile) rest.removeSuffix("/file") else rest
        val entry = shareMap[token]
        if (entry == null || entry.exp < System.currentTimeMillis()) {
            return send(out, 404, "text/html; charset=utf-8", "链接已失效".toByteArray())
        }
        if (isFile) return serveFile(entry.path, false, out, headers["range"])
        return sendDlPage(entry.path, token, out)
    }

    private fun apiUploadStream(input: java.io.InputStream, contentLength: Int, out: OutputStream, query: Map<String, String>) {
        val name = safeName(query["name"] ?: "file")
        // 文件夹传输：rel 为相对接收目录的多级路径，逐段净化后创建
        val rel = (query["rel"] ?: "").replace('\\', '/')
        val parts = rel.split('/').filter { it.isNotBlank() && it != "." && it != ".." }.map { safeName(it) }
        val dir = if (parts.isEmpty()) rootDir else File(rootDir, parts.joinToString(File.separator))
        if (!dir.exists() && !dir.mkdirs()) {
            return sendJson(out, JSONObject().put("ok", false).put("error", "无法创建目录"))
        }
        if (!dir.canonicalPath.startsWith(rootDir.canonicalPath)) {
            return sendJson(out, JSONObject().put("ok", false).put("error", "只能上传到接收目录"))
        }
        val dest = uniqueFile(dir, name)
        val tmp = File(dir, ".dl_${System.nanoTime()}.part")
        try {
            var written = 0L
            tmp.outputStream().use { fos ->
                val buf = ByteArray(64 * 1024)
                var remaining = contentLength
                while (remaining > 0) {
                    val n = input.read(buf, 0, minOf(buf.size, remaining))
                    if (n < 0) break
                    fos.write(buf, 0, n)
                    written += n
                    remaining -= n
                }
                fos.flush()
                fos.fd.sync()          // 落盘后再改名，避免中断留下半截文件
            }
            if (written != contentLength.toLong() || !tmp.renameTo(dest)) {
                tmp.delete()
                return sendJson(out, JSONObject().put("ok", false).put("error", "文件不完整"))
            }
            transferItems.add(0, JSONObject().put("id", "u${System.currentTimeMillis()}").put("name", name)
                .put("dir", "down").put("size", written).put("time", System.currentTimeMillis() / 1000)
                .put("kind", kindOf(name)))
            sendJson(out, JSONObject().put("ok", true).put("name", name).put("size", written))
        } catch (e: Exception) {
            tmp.delete()
            sendJson(out, JSONObject().put("ok", false).put("error", e.message ?: "上传失败"))
        }
    }

    /** 同名文件自动加序号，避免覆盖（与电脑端 unique_path 行为一致）。 */
    private fun uniqueFile(dir: File, name: String): File {
        var f = File(dir, name)
        if (!f.exists()) return f
        val dot = name.lastIndexOf('.')
        val base = if (dot > 0) name.substring(0, dot) else name
        val ext = if (dot > 0) name.substring(dot) else ""
        var i = 1
        while (f.exists() && i < 1000) {
            f = File(dir, "$base ($i)$ext")
            i++
        }
        return f
    }

    // ---------------- 静态与响应工具 ----------------

    private fun serveStatic(path: String, out: OutputStream) {
        val rel = when {
            path == "/" -> "www/index.html"
            path.startsWith("/static/") -> "www/" + path.removePrefix("/static/")
            path == "/manifest.webmanifest" -> "www/manifest.webmanifest"
            else -> null
        }
        if (rel == null) return send(out, 404, "text/plain; charset=utf-8", "Not Found".toByteArray())
        try {
            val bytes = context.assets.open(rel).use { it.readBytes() }
            val ctype = when {
                rel.endsWith(".html") -> "text/html; charset=utf-8"
                rel.endsWith(".js") -> "application/javascript; charset=utf-8"
                rel.endsWith(".css") -> "text/css; charset=utf-8"
                rel.endsWith(".json") || rel.endsWith(".webmanifest") -> "application/json; charset=utf-8"
                else -> "application/octet-stream"
            }
            // 禁止 WebView 吃旧缓存：升级 APK 后前端必须立即生效
            send(out, 200, ctype, bytes, mapOf("Cache-Control" to "no-cache, no-store, must-revalidate"))
        } catch (_: Exception) {
            send(out, 404, "text/plain; charset=utf-8", "Not Found".toByteArray())
        }
    }

    private fun serveFile(f: File, inline: Boolean, out: OutputStream, rangeHeader: String?) {
        val ctype = mime(f.name)
        val disp = if (inline) "inline" else "attachment"
        val name = f.name.replace("\"", "_")
        val disposition = "$disp; filename=\"$name\"; filename*=UTF-8''${java.net.URLEncoder.encode(name, "UTF-8")}"
        val size = f.length()

        // 断点续传：解析 Range（bytes=start-end / start- / -suffix）
        var start = 0L
        var end = size - 1
        var partial = false
        if (size > 0 && !rangeHeader.isNullOrBlank() && rangeHeader.startsWith("bytes=")) {
            val spec = rangeHeader.removePrefix("bytes=").split(",")[0].trim()
            val dash = spec.indexOf('-')
            if (dash >= 0) {
                val a = spec.substring(0, dash).trim()
                val b = spec.substring(dash + 1).trim()
                if (a.isEmpty() && b.isNotEmpty()) {
                    val n = b.toLongOrNull() ?: 0L
                    if (n > 0) {
                        start = maxOf(0L, size - n)
                        end = size - 1
                        partial = true
                    }
                } else if (a.isNotEmpty()) {
                    val s0 = a.toLongOrNull() ?: 0L
                    if (s0 in 0 until size) {
                        start = s0
                        val e0 = b.toLongOrNull() ?: -1L
                        if (e0 in start until size) end = e0
                        partial = true
                    } else {
                        // 起点越界 → 416，让客户端知道该从头来
                        val msg = "range not satisfiable"
                        out.write("HTTP/1.1 416 Range Not Satisfiable\r\n".toByteArray())
                        out.write("Content-Range: bytes */$size\r\n".toByteArray())
                        out.write("Content-Length: ${msg.toByteArray().size}\r\n".toByteArray())
                        out.write("Connection: close\r\n\r\n".toByteArray())
                        out.write(msg.toByteArray())
                        return
                    }
                }
            }
        }

        val length = if (size == 0L) 0L else end - start + 1
        out.write(("HTTP/1.1 " + (if (partial) "206 Partial Content" else "200 OK") + "\r\n").toByteArray())
        out.write("Content-Type: $ctype\r\n".toByteArray())
        out.write("Content-Length: $length\r\n".toByteArray())
        out.write("Content-Disposition: $disposition\r\n".toByteArray())
        out.write("Accept-Ranges: bytes\r\n".toByteArray())
        if (partial) out.write("Content-Range: bytes $start-$end/$size\r\n".toByteArray())
        out.write("Cache-Control: no-store\r\n".toByteArray())
        out.write("\r\n".toByteArray())

        if (length <= 0) return
        val key = java.util.UUID.randomUUID().toString().take(8)
        val stat = LiveStat(f.name, length)
        liveStats[key] = stat
        try {
            java.io.RandomAccessFile(f, "r").use { raf ->
                raf.seek(start)
                val buf = ByteArray(64 * 1024)
                var left = length
                while (left > 0) {
                    val n = raf.read(buf, 0, minOf(buf.size.toLong(), left).toInt())
                    if (n <= 0) break
                    out.write(buf, 0, n)
                    left -= n
                    stat.sent += n
                }
            }
        } finally {
            liveStats.remove(key)
        }
    }

    private fun sendDlPage(f: File, token: String, out: OutputStream) {
        val name = f.name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        val human = when {
            f.length() >= 1L shl 30 -> "%.2f GB".format(f.length() / (1.0 * (1L shl 30)))
            f.length() >= 1L shl 20 -> "%.1f MB".format(f.length() / (1.0 * (1L shl 20)))
            f.length() >= 1L shl 10 -> "%d KB".format(f.length() / (1L shl 10))
            else -> "${f.length()} B"
        }
        val html = """
            <!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>
            <meta name='viewport' content='width=device-width, initial-scale=1'><title>下载文件</title><style>
            *{box-sizing:border-box}body{font-family:system-ui,sans-serif;margin:0;background:#f4f5fb;color:#0f172a;
            display:flex;align-items:center;justify-content:center;min-height:100vh;padding:16px;overflow-x:hidden}
            .card{background:#fff;border-radius:22px;padding:34px 26px;width:100%;max-width:min(420px,100vw - 32px);
            box-shadow:0 12px 44px rgba(15,23,42,.10);text-align:center}
            .ico{width:66px;height:66px;border-radius:19px;background:linear-gradient(135deg,#4f46e5,#7c3aed);color:#fff;
            display:flex;align-items:center;justify-content:center;margin:0 auto 18px}
            h1{font-size:16px;color:#64748b;margin:0 0 8px}.name{font-size:16px;font-weight:650;word-break:break-all;margin:0 0 6px}
            .meta{font-size:13px;color:#94a3b8;margin:0 0 26px}
            .btn{display:block;width:100%;padding:15px;border-radius:15px;background:linear-gradient(135deg,#4f46e5,#7c3aed);
            color:#fff;font-size:16px;font-weight:650;text-decoration:none}
            .tip{font-size:12px;color:#94a3b8;margin:18px 0 0;line-height:1.7}
            </style></head><body><div class='card'>
            <div class='ico'><svg width='30' height='30' viewBox='0 0 24 24' fill='none' stroke='white' stroke-width='2.2'
            stroke-linecap='round' stroke-linejoin='round'><path d='M12 3v11'/><path d='m7 10 5 5 5-5'/>
            <path d='M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2'/></svg></div>
            <h1>来自对方设备的文件</h1><p class='name'>$name</p><p class='meta'>$human · 点击下方按钮下载</p>
            <a class='btn' href='/dl/$token/file'>下载</a>
            <p class='tip'>文件会保存到手机浏览器的「下载」目录。</p></div></body></html>
        """.trimIndent()
        send(out, 200, "text/html; charset=utf-8", html.toByteArray())
    }

    private fun mime(name: String): String {
        val e = name.substringAfterLast('.', "").lowercase()
        return when (e) {
            "html" -> "text/html; charset=utf-8"
            "js" -> "application/javascript"
            "css" -> "text/css"
            "json" -> "application/json"
            "png" -> "image/png"; "jpg", "jpeg" -> "image/jpeg"; "gif" -> "image/gif"
            "webp" -> "image/webp"; "svg" -> "image/svg+xml"
            "mp4" -> "video/mp4"; "webm" -> "video/webm"
            "mp3" -> "audio/mpeg"; "wav" -> "audio/wav"
            "pdf" -> "application/pdf"
            "txt" -> "text/plain; charset=utf-8"
            "zip" -> "application/zip"
            else -> "application/octet-stream"
        }
    }

    private fun sendQr(text: String?, out: OutputStream) {
        val t = text ?: "DropLink"
        try {
            val hints = mapOf(EncodeHintType.MARGIN to 1)
            val matrix = QRCodeWriter().encode(t, BarcodeFormat.QR_CODE, 320, 320, hints)
            val w = matrix.width
            val bmp = Bitmap.createBitmap(w, w, Bitmap.Config.ARGB_8888)
            for (y in 0 until w) for (x in 0 until w) {
                bmp.setPixel(x, y, if (matrix.get(x, y)) Color.BLACK else Color.WHITE)
            }
            val baos = ByteArrayOutputStream()
            bmp.compress(Bitmap.CompressFormat.PNG, 100, baos)
            send(out, 200, "image/png", baos.toByteArray(), mapOf("Cache-Control" to "no-store"))
        } catch (_: Exception) {
            send(out, 500, "text/plain", "qr error".toByteArray())
        }
    }

    private fun sendJson(out: OutputStream, obj: JSONObject) {
        // 接口一律禁缓存：否则 WebView 会把第一次的响应（比如空的 /api/live）缓存住，
        // 后续轮询永远拿到旧数据，表现为「传输中但界面没有速率」。
        send(out, 200, "application/json; charset=utf-8", obj.toString().toByteArray(),
            mapOf("Cache-Control" to "no-store, no-cache, must-revalidate", "Pragma" to "no-cache"))
    }

    private fun send(out: OutputStream, code: Int, ctype: String, body: ByteArray, extra: Map<String, String> = emptyMap()) {
        try {
            val reason = when (code) { 200 -> "OK"; 404 -> "Not Found"; 500 -> "Error"; 403 -> "Forbidden"; 400 -> "Bad Request"; else -> "OK" }
            out.write("HTTP/1.1 $code $reason\r\n".toByteArray())
            out.write("Content-Type: $ctype\r\n".toByteArray())
            out.write("Content-Length: ${body.size}\r\n".toByteArray())
            out.write("Connection: close\r\n".toByteArray())
            extra.forEach { (k, v) -> out.write("$k: $v\r\n".toByteArray()) }
            out.write("\r\n".toByteArray())
            out.write(body)
            out.flush()
        } catch (_: Exception) {}
    }
}
