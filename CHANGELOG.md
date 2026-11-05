# Changelog

All notable changes to DropLink. Download links point at the latest release.

## v1.5.0 — 2026-11-05

**Two editions**

- The same codebase now ships two builds. `DropLink.exe` is the **personal** edition (unchanged behaviour, plain HTTP, 6-digit code). `DropLink-Secure.exe` is the **secure** edition, hardened by default and not switchable to plaintext.
- Secure edition adds: TLS 1.3 with a self-signed ECDSA P-256 certificate (refuses to start without one), device trust with on-PC approval and revocation, metadata-first transfer confirmation (nothing hits disk before you accept), per-file one-time upload tokens bound to session+file, machine-readable error codes (401/403/409/410/422/429/507), write-path hardening (traversal, UNC, ADS, reparse points, reserved names, depth > 32), per-endpoint token-bucket rate limiting, log redaction with timestamps, receive-folder quota and strict `.part` sweeping, and web hardening (Secure cookie, Origin/Sec-Fetch checks, Host allow-list, forced download for HTML/SVG).
- New `VERSION` file at the repo root is the release baseline; the build now fails if `config.VERSION`, `README` and `docs/EDITIONS.md` disagree with it.
- Build self-check refuses to package runtime state (certs, trust list, logs) or the secure modules into the personal build.

**Android 1.4.8**

- Search works again: the server returned `results` while the UI read `entries`. Also fixed IME composition and Enter-to-search.

> **中文**：1.5.0 起同一份代码出两个产物——个人版行为不变；安全版默认就是加固形态（TLS、设备审批、传输前确认、逐文件一次性 token、状态码语义、路径加固、限流、日志脱敏、配额、Web 面加固），拿不到证书直接拒绝启动。构建引入 `VERSION` 发布基线，版本漂移会直接构建失败。安卓 1.4.8 修复了搜索永远为空（字段名 `results`/`entries` 不一致）。

## v1.4.7 — 2026-11-04

**Naming**

- Device labels are role-based now: **sender / receiver**, with **peer** used where no direction is implied. No more "PC" / "phone" in the UI.
- Console banner: `receiver (local)` / `sender` addresses.

> **中文**：把「源端 / 靶端」改成更直白的「发送端 / 接收端」，并引入「对端」指代不涉及方向的另一台设备——否则会出现「发送文件到发送端」这类自相矛盾句。规则：说方向用发送/接收，说对象用对端，说自己用本机。

## v1.4.6 — 2026-11-04

**Naming & onboarding**

- Replaced "PC / phone" with role terms throughout the UI, server banner and Android strings.
- The one-time capability notice now appears on every browser (Android browser and desktop too), not just iOS / HarmonyOS.

> **中文**：全站取消「电脑 / 手机」称谓；初次使用提示从「仅 iOS / 鸿蒙」扩展到所有浏览器模式，各平台文案不同、记忆独立。

## v1.4.5 — 2026-11-04

**UI**

- The capability notice became a 5-second floating overlay with a countdown bar instead of a permanent banner, so it no longer takes first-screen space.

> **中文**：跨端提示改为 5 秒自动消失的浮层，带进度条倒计时，可手动关闭；首屏不再被占用。

## v1.4.4 — 2026-11-04

**Cross-platform (test build)**

- iPhone / iPad (Safari) and HarmonyOS browsers get the same file operations as the Android app — no install needed.
- Platform detection drives the wording of the download notice (Files app / download manager / system Downloads folder).
- iOS PWA metadata: manifest `id` / `scope` / `start_url`, plus `/apple-touch-icon.png`.
- Copy-to-clipboard falls back to `execCommand` on plain-HTTP pages, where `navigator.clipboard` does not exist.

> **中文**：iOS 与鸿蒙改用纯浏览器即可使用，功能与安卓 App 对齐（下载队列、断点续传、通知栏操作除外，属系统限制）。补了 iOS 的 PWA 元信息与图标，修了非 HTTPS 下复制失效的问题。

## v1.4.3 — 2026-11-04

**Permissions**

- Removed `ACCESS_FINE_LOCATION` / `ACCESS_COARSE_LOCATION`. They were never used: the hotspot toggle goes through `WifiManager` reflection and the app never reads SSID or scan results.
- First launch no longer asks for location.

> **中文**：删掉从未使用的位置权限（热点开关走反射，不读 SSID / 扫描结果），首启不再弹定位授权。

## v1.4.2 — 2026-11-04

**Resilience**

- The service now starts even if any `dlcore` module is missing or corrupted. Each failure is replaced by a stub module that fails closed (no disk access, no login bypass) and is reported on `/api/ping` and in a diagnostic page.
- New `--selfcheck` flag: exit 0 when all modules load, 1 when degraded.
- New `build/fault_check.py`: 7 modules × missing/corrupt, each launched as a real process.

> **中文**：除入口与加载器外，任一模块被删或写坏，服务仍能启动并说明是哪个模块出了问题；降级一律 fail-closed（拒绝访盘、拒绝免登录），绝不因为降级而多开一个口子。

## v1.4.1 — 2026-11-04

**Refactor**

- `server.py` split from 2.6k lines into a thin entry point plus `dlcore/`: `config`, `state`, `fsutil`, `winenv`, `media`, `api`, `httpd`.
- Behaviour unchanged; all four regression suites still pass.

> **中文**：把 2674 行的 server.py 按职责拆成 7 个模块，纯搬移不改逻辑，四套回归全绿。

## v1.4.0 — 2026-11-04

**Reliability**

- **Download queue** — tasks run one at a time, survive app restarts, and can be paused / resumed / removed.
- **Resume on reconnect** — every `.part` carries its task id, so a retry continues with `Range: bytes=<done>-` instead of starting over.
- **Integrity check** — `/api/sha256` plus a local hash comparison for files up to 512 MB.
- **Actionable notifications** — open file / open folder / pause / cancel directly from the shade.
- **Quieter logs** — client disconnects are one `[断连]` line instead of a full traceback.
- **Faster first paint** — the file list renders 24 rows first, then appends per frame.

> **中文**：1.4.0 做的是「可靠」。下载改成队列（串行、可暂停/继续、杀进程后仍在），断点续传按 `Range` 接着下，≤512 MB 的文件下载完做 SHA-256 校验，通知可直接打开文件或文件夹，客户端断连只记一行日志，列表首屏分批渲染。

## v1.3.1 — 2026-11-04

**Signature**

- Every build now carries the author mark `HU4222`: once in the app ("Mine" page footer), once in `--version`, and once in the executable metadata (`CompanyName` for the EXE, `app_author` for the APK). No behaviour changes.

> **中文**：1.3.1 只做一件事——按统一规范补作者署名 HU4222，共三处：应用内「我的」页底部、`--version` 输出、产物元数据（EXE 的 CompanyName、APK 的 app_author）。功能零改动。

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
