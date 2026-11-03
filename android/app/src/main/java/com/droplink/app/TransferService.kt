package com.droplink.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.Environment
import android.os.IBinder
import java.io.File
import androidx.core.app.NotificationCompat

/**
 * 前台服务：持有内置 HTTP 服务，保证退到后台后进程不被系统（尤其国产 ROM 的电池管理）杀掉。
 * 服务运行期间通知栏常驻一个"运行中"提示。
 */
class TransferService : Service() {

    companion object {
        const val CHANNEL_ID = "droplink"
        const val PORT = 8848
        const val NOTIFY_ID = 1
        @Volatile var running = false
        /** 本机界面令牌：只有 WebView 通过 ?k= 拿到；其它应用即使连上回环也读不到数据。 */
        @Volatile var localToken: String = ""
    }

    private var server: HttpServer? = null

    override fun onCreate() {
        super.onCreate()
        startForeground(NOTIFY_ID, buildNotification())
        localToken = java.security.SecureRandom().let { rnd ->
            val bytes = ByteArray(24)
            rnd.nextBytes(bytes)
            android.util.Base64.encodeToString(
                bytes, android.util.Base64.URL_SAFE or android.util.Base64.NO_WRAP or android.util.Base64.NO_PADDING
            )
        }
        server = HttpServer(this, PORT, { storageRoot() }, { recvRoot() }, localToken).apply { start() }
        running = true
    }

    /**
     * 浏览根目录：拿到「所有文件访问」权限后就是整机存储，否则退回应用私有目录。
     * 用 lambda 每次请求实时判定，用户授权后无需重启服务即可生效。
     */
    private fun storageRoot(): File {
        val granted = Build.VERSION.SDK_INT < 30 || Environment.isExternalStorageManager()
        return if (granted) Environment.getExternalStorageDirectory()
        else getExternalFilesDir(null) ?: filesDir
    }

    /** 接收目录固定为 Download/DropLink，避免文件散落在存储根（BUG-08）。 */
    private fun recvRoot(): File {
        val dir = File(Environment.getExternalStorageDirectory(), "Download/DropLink")
        if (dir.exists() || dir.mkdirs()) return dir
        val fallback = File(getExternalFilesDir(null) ?: filesDir, "DropLink")
        fallback.mkdirs()
        return fallback
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onDestroy() {
        running = false
        server?.stop()
        server = null
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun buildNotification(): Notification {
        createChannel()
        val pi = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setContentTitle("DropLink 互传")
            .setContentText("服务运行中，可接收文件")
            .setContentIntent(pi)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= 26) {
            val nm = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "DropLink 传输服务", NotificationManager.IMPORTANCE_LOW)
            )
        }
    }
}
