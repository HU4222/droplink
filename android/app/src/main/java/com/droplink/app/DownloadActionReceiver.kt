package com.droplink.app

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.widget.Toast

/**
 * 下载通知上的操作按钮（1.4.0）。
 *
 * 单独放一个静态注册的 Receiver：Activity 被回收后通知还在，按钮仍然要能点。
 */
class DownloadActionReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context?, intent: Intent?) {
        val ctx = context ?: return
        val act = intent?.action ?: return
        val id = intent.getStringExtra("id") ?: ""

        when (act) {
            DownloadCenter.ACTION_PAUSE -> DownloadCenter.pause(id)
            DownloadCenter.ACTION_RESUME -> DownloadCenter.resume(id)
            DownloadCenter.ACTION_CANCEL -> DownloadCenter.remove(id)

            DownloadCenter.ACTION_OPEN_FILE -> {
                val uri = intent.getStringExtra("uri") ?: return
                val mime = intent.getStringExtra("mime") ?: "*/*"
                try {
                    ctx.startActivity(
                        Intent(Intent.ACTION_VIEW)
                            .setDataAndType(Uri.parse(uri), mime)
                            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
                    )
                } catch (_: Exception) {
                    Toast.makeText(ctx, "没有能打开该文件的应用", Toast.LENGTH_SHORT).show()
                }
            }

            DownloadCenter.ACTION_OPEN_DIR -> {
                val uri = intent.getStringExtra("uri") ?: return
                try {
                    ctx.startActivity(
                        Intent(Intent.ACTION_VIEW)
                            .setData(Uri.parse(uri))
                            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
                    )
                } catch (_: Exception) {
                    // 部分 ROM 的文件管理器不接 tree uri：退回系统的「最近文件」
                    try {
                        ctx.startActivity(
                            Intent(Intent.ACTION_VIEW)
                                .setDataAndType(Uri.parse(uri), "vnd.android.document/directory")
                                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                        )
                    } catch (_: Exception) {
                        Toast.makeText(ctx, "打不开文件夹，请在文件管理器里查看 Download/DropLink", Toast.LENGTH_LONG).show()
                    }
                }
            }
        }
    }
}
