package com.droplink.app

import android.content.Context
import android.net.wifi.WifiConfiguration
import android.net.wifi.WifiManager
import java.net.NetworkInterface

/**
 * 移动热点管理。
 *
 * Android 12（API 32）适配要点：
 * - API 26~32：反射调用 WifiManager.setWifiApEnabled 可正常开关热点（无系统权限要求）；
 * - API 33+（Android 13）：Google 收紧了该反射（返回 false 或抛异常），需改用
 *   LocalOnlyHotspot 或提示用户到系统设置手动开启。
 */
class HotspotManager(private val context: Context) {

    private val wifi: WifiManager
        get() = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager

    fun isEnabled(): Boolean = try {
        val m = wifi.javaClass.getMethod("isWifiApEnabled")
        m.invoke(wifi) as Boolean
    } catch (_: Exception) { false }

    fun setEnabled(enabled: Boolean): Boolean {
        if (enabled && !isEnabled()) {
            // 关闭 Wi-Fi，避免与热点冲突
            try { wifi.isWifiEnabled = false } catch (_: Exception) {}
        }
        return try {
            val m = wifi.javaClass.getMethod(
                "setWifiApEnabled",
                WifiConfiguration::class.java,
                Boolean::class.javaPrimitiveType
            )
            m.invoke(wifi, null, enabled) as Boolean
        } catch (_: Exception) { false }
    }

    /** 热点网卡 IP（通常 192.168.43.1 / 192.168.xx.1）。 */
    fun apIp(): String? {
        val candidates = mutableListOf<String>()
        try {
            NetworkInterface.getNetworkInterfaces()?.toList()?.forEach { nif ->
                val name = nif.name.lowercase()
                if (name.contains("ap") || name.contains("wlan") || name.contains("softap")) {
                    nif.inetAddresses.toList().forEach { addr ->
                        val ip = addr.hostAddress ?: return@forEach
                        if (!addr.isLoopbackAddress && !ip.contains(':')) candidates.add(ip)
                    }
                }
            }
        } catch (_: Exception) {}
        return candidates.firstOrNull { it.startsWith("192.168.") || it.startsWith("10.") }
            ?: candidates.firstOrNull()
    }

    fun isReflectionSupported(): Boolean = android.os.Build.VERSION.SDK_INT <= 32
}
