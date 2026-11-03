# DropLink — Android app

A Kotlin app that runs the *same* HTTP server and the *same* web UI as the Windows build, so it works as a client of your PC **and** as a server other devices can connect to.

## Requirements

- Android 8.0 (API 26) or newer
- JDK 17 + Android SDK 34 to build
- Gradle wrapper is included

## Build

```bash
./gradlew assembleDebug     # app/build/outputs/apk/debug/app-debug.apk
./gradlew assembleRelease   # needs DROPLINK_KEYSTORE + passwords, else debug key
```

See [`../docs/BUILD.md`](../docs/BUILD.md) for signing setup.

## Structure

| Path | Role |
|---|---|
| `app/src/main/java/com/droplink/app/MainActivity.kt` | WebView host, JS bridge, SAF downloads, permissions, orientation handling |
| `…/HttpServer.kt` | The HTTP API implemented on a `ServerSocket` |
| `…/TransferService.kt` | Foreground service owning the server; keeps transfers alive with the screen off |
| `…/QrScannerActivity.kt` | CameraX + ZXing scanner for the PC's pairing QR code |
| `…/HotspotManager.kt` | Best-effort hotspot switch (reflection, may be unavailable on newer ROMs) |
| `app/src/main/assets/www/` | The web UI, bundled so the app works with no network |
| `app/src/main/res/` | Launcher icons and themes |

## Permissions

| Permission | Why |
|---|---|
| `INTERNET`, `ACCESS_NETWORK_STATE`, `ACCESS_WIFI_STATE`, `CHANGE_WIFI_STATE` | LAN sockets, hotspot helpers |
| `CAMERA` | QR pairing scanner |
| `POST_NOTIFICATIONS`, `FOREGROUND_SERVICE(_DATA_SYNC)` | Transfer progress and keeping the server alive |
| `MANAGE_EXTERNAL_STORAGE` | Browse and receive files anywhere on the phone (Android 11+ requires the user to grant "All files access" manually) |
| `READ/WRITE_EXTERNAL_STORAGE` (maxSdk 32/28) | Legacy storage on older Android |

The local server binds `127.0.0.1:8848` and the LAN interface; files received from the PC land in `Download/DropLink`.

> **中文**：安卓端跑的是和电脑端**同一套服务**与**同一份网页界面**，所以它既能连电脑，也能自己当服务端给别的设备连。构建需要 JDK 17 + SDK 34，仓库自带 Gradle Wrapper。权限方面：网络与相机用于连网和扫码；通知与前台服务用于传输进度和保活；`MANAGE_EXTERNAL_STORAGE` 用于浏览/接收手机任意目录（Android 11+ 需要用户手动授予「所有文件访问」）；旧版权限只对低版本生效。电脑推过来的文件落在 `Download/DropLink`。
