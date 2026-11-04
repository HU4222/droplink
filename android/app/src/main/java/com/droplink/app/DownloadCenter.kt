package com.droplink.app

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.webkit.CookieManager
import androidx.core.app.NotificationCompat
import androidx.documentfile.provider.DocumentFile
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest

/**
 * 下载中心：任务队列 + 断点续传 + 完整性校验 + 通知动作（1.4.0）。
 *
 * 为什么要单独一个类：原来的下载逻辑写在 Activity 里，一次只能跑一个任务，
 * 中断就从 0 重来，取消直接删 .part。现在把「任务」抽出来：
 *
 *  - 队列：串行执行，状态持久化到 SharedPreferences，杀进程后重开还在；
 *  - 续传：.part 文件名里带任务 id（稳定），重试时用 `Range: bytes=<已有长度>-` 接着下；
 *  - 校验：下载完成后向 /api/sha256 取服务端哈希，本地再算一遍比对（≤512 MB 才做，
 *    避免 10 GB 文件多读一遍盘）；
 *  - 取消 = 暂停（保留 .part），真正删除要显式调用 remove()。
 *
 * 状态机：queued → running → done / failed / paused，paused 可回到 queued。
 */
object DownloadCenter {

    const val ST_QUEUED = "queued"
    const val ST_RUNNING = "running"
    const val ST_PAUSED = "paused"
    const val ST_DONE = "done"
    const val ST_FAILED = "failed"

    const val ACTION_PAUSE = "com.droplink.app.dl.PAUSE"
    const val ACTION_RESUME = "com.droplink.app.dl.RESUME"
    const val ACTION_CANCEL = "com.droplink.app.dl.CANCEL"
    const val ACTION_OPEN_FILE = "com.droplink.app.dl.OPEN_FILE"
    const val ACTION_OPEN_DIR = "com.droplink.app.dl.OPEN_DIR"

    private const val PREFS = "dl_queue"
    private const val KEY = "tasks"
    private const val VERIFY_LIMIT = 512L * 1024 * 1024

    data class Task(
        var id: String,
        var url: String,
        var name: String,
        var mime: String,
        var dirUri: String,
        var size: Long = -1L,
        var done: Long = 0L,
        var state: String = ST_QUEUED,
        var err: String = "",
        var sha: String = ""
    )

    private lateinit var app: Context
    private val tasks = ArrayList<Task>()
    private var worker: Thread? = null
    @Volatile private var stopFlag = false
    private var listener: (() -> Unit)? = null

    fun init(c: Context) {
        app = c.applicationContext
        if (tasks.isEmpty()) load()
        ensureChannel()
    }

    fun setListener(l: (() -> Unit)?) {
        listener = l
    }

    private fun notifyUi() {
        listener?.let {
            try {
                it()
            } catch (_: Exception) {
            }
        }
    }

    // ---------------------------------------------------------------- 对外接口

    @Synchronized
    fun enqueue(url: String, name: String, mime: String, dirUri: String): Task {
        val id = System.currentTimeMillis().toString(36) + "-" +
                Integer.toHexString((Math.random() * 0xffff).toInt())
        val t = Task(id = id, url = url, name = name, mime = mime, dirUri = dirUri)
        tasks.add(t)
        persist()
        startWorker()
        notifyUi()
        return t
    }

    @Synchronized
    fun snapshot(): List<Task> = tasks.map { it.copy() }

    @Synchronized
    fun find(id: String): Task? = tasks.firstOrNull { it.id == id }

    @Synchronized
    fun pause(id: String) {
        val t = find(id) ?: return
        if (t.state == ST_RUNNING || t.state == ST_QUEUED) {
            t.state = ST_PAUSED
            persist()
            notifyUi()
        }
    }

    @Synchronized
    fun resume(id: String) {
        val t = find(id) ?: return
        if (t.state == ST_PAUSED || t.state == ST_FAILED) {
            t.state = ST_QUEUED
            t.err = ""
            persist()
            startWorker()
            notifyUi()
        }
    }

    /** 取消并删除 .part（真正的「不要了」）。 */
    @Synchronized
    fun remove(id: String) {
        val t = find(id) ?: return
        t.state = ST_FAILED
        t.err = "已取消"
        tasks.remove(t)
        persist()
        try {
            partOf(t)?.delete()
        } catch (_: Exception) {
        }
        cancelNotify(t)
        notifyUi()
    }

    @Synchronized
    fun clearFinished() {
        tasks.removeAll { it.state == ST_DONE }
        persist()
        notifyUi()
    }

    fun activeCount(): Int = synchronized(this) {
        tasks.count { it.state == ST_QUEUED || it.state == ST_RUNNING || it.state == ST_PAUSED }
    }

    fun snapshotJson(): String {
        val arr = JSONArray()
        for (t in snapshot()) {
            arr.put(
                JSONObject()
                    .put("id", t.id).put("name", t.name).put("size", t.size)
                    .put("done", t.done).put("state", t.state).put("err", t.err)
            )
        }
        return arr.toString()
    }

    // ---------------------------------------------------------------- 工作线程

    @Synchronized
    private fun startWorker() {
        if (worker?.isAlive == true) return
        stopFlag = false
        worker = kotlin.concurrent.thread(isDaemon = true, name = "dl-worker") {
            while (!stopFlag) {
                val t = synchronized(this) { tasks.firstOrNull { it.state == ST_QUEUED } }
                if (t == null) break
                synchronized(this) {
                    t.state = ST_RUNNING
                    persist()
                }
                notifyUi()
                try {
                    runTask(t)
                } catch (e: Exception) {
                    synchronized(this) {
                        t.state = ST_FAILED
                        t.err = humanError(e)
                        persist()
                    }
                    notifyFailed(t)
                }
                notifyUi()
            }
        }
    }

    private fun runTask(t: Task) {
        val dir = DocumentFile.fromTreeUri(app, Uri.parse(t.dirUri))
        if (dir == null || !dir.canWrite()) {
            fail(t, "无法写入保存目录（权限可能已被系统收回，请重新选择目录）")
            return
        }

        val partName = "${t.id}-${sanitize(t.name)}.part"
        var part = dir.findFile(partName)
        var start = part?.length() ?: 0L

        val conn = (URL(t.url).openConnection() as HttpURLConnection).apply {
            connectTimeout = 15000
            readTimeout = 60000
            try {
                CookieManager.getInstance().getCookie(t.url)?.let { if (it.isNotEmpty()) setRequestProperty("Cookie", it) }
            } catch (_: Exception) {
            }
            if (start > 0) setRequestProperty("Range", "bytes=$start-")
        }
        conn.connect()
        val code = conn.responseCode

        if (code == 416 && start > 0) {           // 已经下完了，只是没改名
            conn.disconnect()
            finish(t, dir, part, partName)
            return
        }
        if (code !in intArrayOf(200, 206)) {
            conn.disconnect()
            fail(t, "服务器返回 $code")
            return
        }

        if (code == 200 && start > 0) {           // 服务端不支持 Range，只能从头来
            start = 0
            try {
                part?.delete()
            } catch (_: Exception) {
            }
            part = null
        }
        val total = if (code == 206) start + conn.contentLengthLong else conn.contentLengthLong
        if (total > 0) t.size = total

        if (part == null) {
            part = dir.createFile("application/octet-stream", partName)
            if (part == null) {
                conn.disconnect()
                fail(t, "无法创建临时文件")
                return
            }
        }
        t.done = start

        val mode = if (start > 0) "wa" else "wt"
        val os = try {
            app.contentResolver.openOutputStream(part.uri, mode)
        } catch (_: Exception) {
            null
        } ?: run {
            conn.disconnect()
            fail(t, "无法写入文件")
            return
        }

        var done = start
        var last = 0L
        try {
            conn.inputStream.use { input ->
                os.use { out ->
                    val buf = ByteArray(64 * 1024)
                    while (true) {
                        if (stopFlag) break
                        val cur = find(t.id)
                        if (cur == null || cur.state != ST_RUNNING) break   // 被暂停/取消
                        val n = input.read(buf)
                        if (n < 0) break
                        out.write(buf, 0, n)
                        done += n
                        t.done = done
                        val now = System.currentTimeMillis()
                        if (now - last > 400) {
                            last = now
                            notifyProgress(t)
                            notifyUi()
                        }
                    }
                    out.flush()
                }
            }
        } finally {
            conn.disconnect()
        }

        val cur = find(t.id)
        if (cur == null || cur.state != ST_RUNNING) {
            // 暂停：保留 .part 供续传
            persist()
            notifyPaused(t)
            return
        }
        if (t.size > 0 && done < t.size) {
            fail(t, "连接中断（已下 ${fmt(done)} / ${fmt(t.size)}），可继续")
            return
        }
        finish(t, dir, part, partName)
    }

    private fun finish(t: Task, dir: DocumentFile, part: DocumentFile?, partName: String) {
        if (part == null) {
            fail(t, "临时文件丢失")
            return
        }
        // 校验：只对 ≤512 MB 的文件做，避免大文件多读一遍盘
        if (t.size in 1..VERIFY_LIMIT) {
            val remote = t.sha.ifEmpty { fetchSha(t.url) }
            if (remote.isNotEmpty()) {
                val local = sha256Of(part.uri)
                if (local.isNotEmpty() && local != remote) {
                    fail(t, "校验不通过：文件可能已损坏，请重试")
                    return
                }
                t.sha = remote
            }
        }
        val finalName = uniqueName(dir, t.name)
        val newUri = try {
            android.provider.DocumentsContract.renameDocument(app.contentResolver, part.uri, finalName)
        } catch (_: Exception) {
            null
        }
        if (newUri == null) {
            fail(t, "下载完成但重命名失败（$partName）")
            return
        }
        t.state = ST_DONE
        t.done = if (t.size > 0) t.size else t.done
        t.err = ""
        persist()
        notifyDone(t, newUri, dir.uri)
        notifyUi()
    }

    private fun fail(t: Task, msg: String) {
        t.state = ST_FAILED
        t.err = msg
        persist()
        notifyFailed(t)
        notifyUi()
    }

    // ---------------------------------------------------------------- 工具

    private fun partOf(t: Task): DocumentFile? {
        val dir = DocumentFile.fromTreeUri(app, Uri.parse(t.dirUri)) ?: return null
        return dir.findFile("${t.id}-${sanitize(t.name)}.part")
    }

    private fun sanitize(name: String): String = name.replace(Regex("[\\\\/:*?\"<>|]"), "_").take(80)

    private fun uniqueName(dir: DocumentFile, name: String): String {
        if (dir.findFile(name) == null) return name
        val dot = name.lastIndexOf('.')
        val base = if (dot > 0) name.substring(0, dot) else name
        val ext = if (dot > 0) name.substring(dot) else ""
        var i = 1
        while (i < 1000) {
            val c = "$base ($i)$ext"
            if (dir.findFile(c) == null) return c
            i++
        }
        return "$base-${System.currentTimeMillis()}$ext"
    }

    /** 从下载地址里取出 path，向服务端要 SHA-256。失败返回空串（不阻断下载）。 */
    private fun fetchSha(downloadUrl: String): String = try {
        val u = URL(downloadUrl)
        val q = u.query ?: ""
        val path = q.split("&").firstOrNull { it.startsWith("path=") } ?: ""
        if (path.isEmpty()) "" else {
            val port = if (u.port > 0) ":${u.port}" else ""
            val api = URL("${u.protocol}://${u.host}$port/api/sha256?$path")
            val c = (api.openConnection() as HttpURLConnection).apply {
                connectTimeout = 5000
                readTimeout = 15000
                try {
                    CookieManager.getInstance().getCookie(api.toString())?.let {
                        if (it.isNotEmpty()) setRequestProperty("Cookie", it)
                    }
                } catch (_: Exception) {
                }
            }
            val body = c.inputStream.bufferedReader().use { it.readText() }
            c.disconnect()
            Regex("\"sha256\"\\s*:\\s*\"([0-9a-f]{64})\"").find(body)?.groupValues?.get(1) ?: ""
        }
    } catch (_: Exception) {
        ""
    }

    private fun sha256Of(uri: Uri): String = try {
        val md = MessageDigest.getInstance("SHA-256")
        app.contentResolver.openInputStream(uri)?.use { input ->
            val buf = ByteArray(1 shl 20)
            while (true) {
                val n = input.read(buf)
                if (n < 0) break
                md.update(buf, 0, n)
            }
        }
        md.digest().joinToString("") { "%02x".format(it) }
    } catch (_: Exception) {
        ""
    }

    fun humanError(e: Exception): String {
        val m = (e.message ?: "").lowercase()
        return when {
            "reset" in m || "abort" in m -> "连接被对方中断，可能接收端已停止服务"
            "timeout" in m || "timed out" in m -> "连接超时，检查是否还在同一 Wi-Fi"
            "unable to resolve host" in m || "unknownhost" in m -> "找不到接收端地址，可能 IP 变了"
            "enospc" in m || "no space" in m -> "本机空间不足"
            else -> e.message ?: "未知错误"
        }
    }

    // ---------------------------------------------------------------- 通知

    private fun ensureChannel() {
        if (Build.VERSION.SDK_INT < 26) return
        val nm = app.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (nm.getNotificationChannel(MainActivity.DL_CHANNEL_ID) == null) {
            nm.createNotificationChannel(
                NotificationChannel(MainActivity.DL_CHANNEL_ID, "文件下载", NotificationManager.IMPORTANCE_LOW)
            )
        }
    }

    private fun notifyId(t: Task): Int = MainActivity.DL_NOTIFY_ID + (tasks.indexOfFirst { it.id == t.id }.coerceAtLeast(0) % 20)

    private fun action(action: String, t: Task, requestCode: Int): PendingIntent {
        val i = Intent(app, DownloadActionReceiver::class.java).setAction(action)
            .putExtra("id", t.id)
        val rc = ((t.id.hashCode() and 0xfff) * 8 + requestCode) and 0x7fffffff
        return PendingIntent.getBroadcast(
            app, rc, i,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
    }

    private fun base(t: Task, title: String, text: String): NotificationCompat.Builder =
        NotificationCompat.Builder(app, MainActivity.DL_CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setContentTitle(title)
            .setContentText(text)
            .setOnlyAlertOnce(true)

    private fun notifyProgress(t: Task) {
        try {
            ensureChannel()
            val pct = if (t.size > 0) ((t.done * 100) / t.size).toInt() else 0
            val b = base(t, "正在下载 ${t.name}",
                if (t.size > 0) "${fmt(t.done)} / ${fmt(t.size)}" else fmt(t.done))
                .setProgress(100, pct, t.size <= 0)
                .setOngoing(true)
                .addAction(0, "暂停", action(ACTION_PAUSE, t, 1))
                .addAction(0, "取消", action(ACTION_CANCEL, t, 2))
            (app.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager)
                .notify(notifyId(t), b.build())
        } catch (_: Exception) {
        }
    }

    private fun notifyPaused(t: Task) {
        try {
            val b = base(t, "已暂停 ${t.name}", "已下载 ${fmt(t.done)}，可继续")
                .setOngoing(false)
                .addAction(0, "继续", action(ACTION_RESUME, t, 3))
                .addAction(0, "删除", action(ACTION_CANCEL, t, 4))
            (app.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager)
                .notify(notifyId(t), b.build())
        } catch (_: Exception) {
        }
    }

    private fun notifyDone(t: Task, fileUri: Uri, dirUri: Uri) {
        try {
            val open = PendingIntent.getBroadcast(
                app, t.id.hashCode() and 0xfff,
                Intent(app, DownloadActionReceiver::class.java).setAction(ACTION_OPEN_FILE)
                    .putExtra("id", t.id).putExtra("uri", fileUri.toString())
                    .putExtra("mime", t.mime),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            val openDir = PendingIntent.getBroadcast(
                app, (t.id.hashCode() and 0xfff) + 1,
                Intent(app, DownloadActionReceiver::class.java).setAction(ACTION_OPEN_DIR)
                    .putExtra("uri", dirUri.toString()),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            val b = base(t, "下载完成", t.name)
                .setSmallIcon(android.R.drawable.stat_sys_download_done)
                .setAutoCancel(true)
                .setContentIntent(open)
                .addAction(0, "打开文件", open)
                .addAction(0, "打开文件夹", openDir)
            (app.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager)
                .notify(notifyId(t), b.build())
        } catch (_: Exception) {
        }
    }

    private fun notifyFailed(t: Task) {
        try {
            val b = base(t, "下载失败 ${t.name}", t.err)
                .setSmallIcon(android.R.drawable.stat_notify_error)
                .setAutoCancel(true)
                .addAction(0, "重试", action(ACTION_RESUME, t, 5))
                .addAction(0, "删除", action(ACTION_CANCEL, t, 6))
            (app.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager)
                .notify(notifyId(t), b.build())
        } catch (_: Exception) {
        }
    }

    private fun cancelNotify(t: Task) {
        try {
            (app.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager)
                .cancel(notifyId(t))
        } catch (_: Exception) {
        }
    }

    // ---------------------------------------------------------------- 持久化

    @Synchronized
    private fun persist() {
        try {
            val arr = JSONArray()
            for (t in tasks) {
                arr.put(
                    JSONObject().put("id", t.id).put("url", t.url).put("name", t.name)
                        .put("mime", t.mime).put("dir", t.dirUri).put("size", t.size)
                        .put("done", t.done).put("state", t.state).put("err", t.err)
                        .put("sha", t.sha)
                )
            }
            app.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString(KEY, arr.toString()).apply()
        } catch (_: Exception) {
        }
    }

    @Synchronized
    private fun load() {
        try {
            val raw = app.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY, null) ?: return
            val arr = JSONArray(raw)
            tasks.clear()
            for (i in 0 until arr.length()) {
                val o = arr.getJSONObject(i)
                val st = o.optString("state", ST_QUEUED)
                tasks.add(
                    Task(
                        id = o.optString("id"), url = o.optString("url"),
                        name = o.optString("name"), mime = o.optString("mime"),
                        dirUri = o.optString("dir"), size = o.optLong("size", -1),
                        done = o.optLong("done", 0),
                        // 上次被杀时正在下的任务，重启后回到「暂停」，由用户决定是否继续
                        state = if (st == ST_RUNNING) ST_PAUSED else st,
                        err = o.optString("err"), sha = o.optString("sha")
                    )
                )
            }
        } catch (_: Exception) {
            tasks.clear()
        }
    }

    fun fmt(b: Long): String {
        if (b < 0) return "?"
        if (b < 1024) return "$b B"
        val kb = b / 1024.0
        if (kb < 1024) return String.format("%.1f KB", kb)
        val mb = kb / 1024.0
        if (mb < 1024) return String.format("%.1f MB", mb)
        return String.format("%.2f GB", mb / 1024.0)
    }

    /** 清掉超过 24 小时的 .part（任务已不在队列里的孤儿文件）。 */
    fun sweepOrphans() {
        kotlin.concurrent.thread(isDaemon = true) {
            try {
                val saved = app.getSharedPreferences("droplink", Context.MODE_PRIVATE)
                    .getString("saveDir", "") ?: ""
                if (saved.isEmpty()) return@thread
                val dir = DocumentFile.fromTreeUri(app, Uri.parse(saved)) ?: return@thread
                val deadline = System.currentTimeMillis() - 24L * 3600 * 1000
                val live = synchronized(this) { tasks.map { "${it.id}-${sanitize(it.name)}.part" }.toSet() }
                for (f in dir.listFiles()) {
                    val n = f.name ?: continue
                    if (!n.endsWith(".part") || n in live) continue
                    if (f.lastModified() in 1 until deadline) f.delete()
                }
            } catch (_: Exception) {
            }
        }
    }
}
