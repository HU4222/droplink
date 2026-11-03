# Architecture

DropLink is a two-part system that speaks one very small protocol: plain HTTP over the LAN.

```
┌──────────────────────┐          HTTP (LAN)          ┌──────────────────────┐
│  Phone               │  ───────────────────────▶   │  PC                  │
│  DropLink APK        │                             │  DropLink.exe        │
│  ├─ WebView UI       │  ◀───────────────────────   │  ├─ ThreadingHTTP    │
│  └─ (or any browser) │        JSON + files         │  ├─ web/ (UI assets) │
└──────────────────────┘                             │  └─ tray + window    │
                                                     └──────────────────────┘
```

Both platforms run the **same server design and the same web UI**, which is why behaviour and screenshots match.

## 1. Desktop side

| File | Responsibility |
|---|---|
| `app_main.py` | Entry point: parse flags, start the server, create the tray/window, open the browser UI |
| `server.py` | Everything HTTP: routing, session/pairing, file listing, upload, download, ZIP, clipboard, push, diagnostics |
| `gui.py` | Native Win32 window and tray icon via `ctypes` (no tkinter/Qt dependency) |
| `qrmini.py` | QR matrix generation and PNG/ICO rendering without Pillow or `qrcode` |
| `web/` | Single-page UI; no bundler, no framework, no CDN |

### Request lifecycle

1. `ThreadingHTTPServer` accepts a connection on port 8848 (auto-increments if busy).
2. `Handler` resolves the route in `_route_get` / `_route_post`.
3. Authentication: a `dlsess` cookie issued by `/api/login` (pairing code) or the local token when `--secure` is on.
4. The handler writes either JSON or a streamed file body. Large files are streamed in 512 KB chunks with `Range` support — memory usage stays flat regardless of file size.
5. Every response closes the connection unless it is a keep-alive static asset.

### Threading model

- One thread per request (`daemon_threads = True`), so a slow 10 GB download never blocks browsing.
- A background sweeper removes expired sessions, shares and stale `.part` files.
- Uploads write to a hidden `.dl_<random>.part` first, `fsync`, then claim the final name with an atomic rename. An interrupted upload therefore never produces a half-written file with a real name.

## 2. Android side

| File | Responsibility |
|---|---|
| `MainActivity.kt` | Hosts the `WebView`, exposes the JS bridge, handles downloads via SAF, permissions, orientation |
| `HttpServer.kt` | The same API surface as the desktop, implemented on a `ServerSocket` |
| `TransferService.kt` | Foreground service that owns the HTTP server so it survives screen-off and app switches |
| `QrScannerActivity.kt` | CameraX + ZXing scanner for the pairing QR code |
| `HotspotManager.kt` | Best-effort hotspot toggle via reflection |

The app is **not** a thin client: it runs its own server, so two phones can talk to each other, and the phone can serve files to a PC browser. The WebView loads `http://127.0.0.1:8848/` from the bundled `assets/www`.

### Why a WebView instead of native screens

The UI is ~2 500 lines of HTML/CSS/JS shared verbatim with the desktop. Shipping the same files on both sides keeps the two implementations from drifting, and a UI fix lands on both platforms at once. The native code only provides what the browser cannot: filesystem access outside the app sandbox, background service, camera scanning and notifications.

## 3. Data flow examples

**Phone → PC upload**

```
WebView picker → POST /api/upload?name=…&rel=…  (raw body)
      → server writes .dl_xxx.part → fsync → rename to final name
      → JSON {ok, saved, size} → UI moves the row into “Transfer centre”
```

**PC → phone download**

```
Tap a row → JS calls window.androidBridge.downloadFile(url, name, mime)
      → MainActivity probes size with a Range request
      → confirmation dialog → SAF target chosen once, remembered
      → GET /api/download?path=… → written as name.part → renamed → notification
```

**Sending a folder**

```
<input webkitdirectory> → for each file: rel = "folder/sub/file.jpg"
      → POST /api/upload with rel → server recreates directories, then writes the file
```

> **中文**：电脑端是「线程化 HTTP 服务 + 原生托盘窗口 + 纯手写网页界面」，手机端跑的是**同一套服务设计**和**同一份网页界面**（打包进 APK 的 assets），所以两端行为一致。大文件按 512 KB 分块流式收发并支持 Range 断点续传，内存占用与文件大小无关；上传先写隐藏的 `.part`，落盘后再原子改名，中断不会留下半截真名文件。安卓不是瘦客户端——它自己就是服务端，所以手机之间、手机对电脑浏览器都能互传；原生代码只做浏览器做不到的事：沙箱外的文件、前台服务保活、相机扫码、系统通知。
