# Changelog

All notable changes to DropLink. Download links point at the latest release.

## v1.3.0 — 2026-11-03

**Fixes**

- Text preview on the phone now shows the file content instead of `undefined` (the Android endpoint returned raw bytes while the UI expected JSON).
- The "Download" button in the phone's preview sheet works again. Root cause was a crash on tap: a thread was started twice, which threw `IllegalThreadStateException`. Downloads now go through a dedicated native bridge instead of relying on WebView's download listener.
- Disabled buttons (for example "Send" with an empty clipboard) are now visually disabled.
- Cold start no longer gets stuck on the splash screen. The old code polled the local port from a worker thread that could stall on some ROMs; loading now retries automatically.
- An interrupted upload no longer leaves a 0-byte file behind: the destination name is claimed atomically after the temp file is complete.
- The in-app logo on Android shows the real launcher icon instead of a QR code.

**Android**

- `allowBackup=false` (session cookies no longer go into cloud backups).
- Removed unused `READ_MEDIA_*` permissions.
- Debug probe (`DebugProbesKt.bin`) excluded from release builds.
- System `confirm()` dialogs no longer expose the `http://127.0.0.1` origin.
- 24-hour cleanup of stale `.part` files on startup.

**Desktop**

- Stale `.dl_*.part` files older than 24 hours are swept at startup.
- Uploads claim their final filename with an atomic rename (concurrent uploads of the same name stay separate).

> **中文**：1.3.0 修掉了手机端文本预览显示 `undefined`、点下载崩溃（线程被启动两次）、禁用按钮没有样式、冷启动卡在启动页、上传中断残留 0 字节文件、安卓页头图标显示成二维码等问题；同时关闭云备份、移除多余媒体权限、剔除调试产物。电脑端补了过期临时文件清理和上传落名的原子性。

## v1.2.0 — 2026-11-03

- Preview sheet no longer freezes after pressing Back.
- Download confirmation with size and free space; `.part` temp files; notification progress and cancel.
- Chinese filenames preserved end-to-end.
- Unknown file types open an action sheet instead of downloading silently.
- Empty clipboard entries rejected.
- Remember the last PC and reconnect on launch.
- Landscape layout fixed.
- Receive folder moved out of storage root into `Download/DropLink`.
- Transfer history no longer double-counts.
- Empty files show an explicit message.

> **中文**：1.2.0 是上一轮真机测试报告（10 个 BUG）的修复版本：返回键不再白屏、下载前有确认与进度、中文名不丢、未知类型不再静默下载、空剪贴板被拦、重启自动回连、横屏适配、接收目录独立、传输记录去重、空文件有提示。

## v1.1.2 — 2026-11-03

- Restored the pre-hardening default behaviour for personal use (local requests trusted, any folder browsable, 6-digit pairing code). The hardened profile remains available with `--secure`.

> **中文**：按用户要求把默认行为恢复到加固之前，加固模式保留为 `--secure` 开关。

## v1.1.0 — 2026-11-03

- Optional hardened profile: Host/Origin validation, local UI token, sensitive-directory blocking, path hardening (UNC / device names / alternate data streams / reparse points), 8-digit pairing code, session expiry, CSP and security headers, MOTW for executables.

> **中文**：引入可选加固模式（Host/Origin 校验、本机令牌、敏感目录屏蔽、路径加固、8 位配对码、会话过期、安全响应头、可执行文件打网络标记）。

## v1.0.0 — 2026-11-03

- First release: LAN file browser, upload/download, preview, clipboard, QR pairing, Android app.

> **中文**：首个版本，具备局域网浏览、上传下载、预览、剪贴板、扫码配对和安卓 App。
