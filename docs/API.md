# HTTP API

All endpoints are served by `desktop/server.py` and `android/HttpServer.kt` with the same semantics. Base URL is `http://<host>:8848`.

- Requests and responses are `application/json; charset=utf-8` unless stated otherwise.
- Authentication: after `POST /api/login` the server sets a `dlsess` cookie. Endpoints marked **auth** require it (or a local request). With `--secure`, local requests additionally need the `X-DL-Token` header.
- Errors look like `{"ok": false, "error": "…"}` with a matching HTTP status.

> **中文**：两端实现同一套接口，基地址 `http://<主机>:8848`；除登录和 `ping` 外都需要 `dlsess` Cookie；错误统一返回 `{"ok":false,"error":"…"}`。

## Session

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/ping` | Liveness probe, no auth |
| `GET` | `/api/session` | Device name, version, receive folder, LAN URLs, whether this client is paired |
| `POST` | `/api/login` | Body `{"pin":"123456","name":"My phone"}` → sets cookie |
| `POST` | `/api/logout` | Drops the current session |
| `GET` | `/api/sessions` | **auth** list of active sessions (local only) |
| `POST` | `/api/sessions/revoke` | **auth** revoke one or all sessions |

> **中文**：`/api/ping` 用于探活；`/api/session` 返回设备名、版本、接收目录和配对状态；登录用 `POST /api/login` 传配对码换 Cookie；会话列表与吊销只对本机界面开放。

## Files

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/list?path=…` | Directory listing (`entries[]` with name, size, mtime, kind) |
| `GET` | `/api/roots` | Quick-access roots (Desktop, Downloads, Documents, Pictures…) |
| `GET` | `/api/search?q=…&path=…` | Recursive filename search, capped at 200 hits |
| `GET` | `/api/download?path=…` | File body; supports `Range`, `inline=1`, `log=1` |
| `GET` | `/api/thumb?path=…&w=320` | JPEG thumbnail (needs Pillow) |
| `GET` | `/api/preview?path=…` | First 256 KB decoded as UTF-8/GB18030/UTF-16 → `{text, size, truncated}` |
| `POST` | `/api/upload?name=…&rel=…` | Raw body upload; `rel` recreates sub-directories |
| `POST` | `/api/mkdir` | `{"path":"…","name":"…"}` |
| `POST` | `/api/rename` | `{"path":"…","name":"new name"}` |
| `POST` | `/api/delete` | `{"paths":[…]}`, goes to the Recycle Bin |
| `POST` | `/api/zip` | `{"paths":[…]}` → streamed ZIP |
| `GET` | `/api/zip?paths=<json>` | Same, via navigation (used by the Android download bridge) |

> **中文**：文件类接口覆盖列目录、快捷目录、搜索、下载（支持 Range 断点续传与内联预览）、缩略图、文本预览（最多 256 KB）、上传（`rel` 可还原多级目录）、新建文件夹、重命名、删除（进回收站）和打包下载。打包同时提供 POST 与 GET 两个入口，GET 是为了让安卓的下载器能直接接管。

## Transfer & device

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/transfers` | Recent transfer history (deduplicated within 2 s) |
| `GET` | `/api/live` | In-flight transfer speed for the progress UI |
| `GET` | `/api/peers` | Devices that have paired |
| `GET` | `/api/networks` | Every local IPv4 with an interface label |
| `GET` | `/api/diag` | Firewall / port / network diagnostics |
| `GET` | `/api/config` | Current receive folder and port |
| `POST` | `/api/config` | `{"recv_dir":"…"}`, local only |
| `POST` | `/api/share` | `{"path":"…"}` → time-limited `/dl/<token>` link |
| `GET` | `/dl/<token>` | Download a shared file without a session |
| `GET` | `/qr.png?text=…` | QR PNG (text ≤ 512 bytes) |
| `GET` | `/api/icon?size=192` | Application icon PNG |
| `GET` | `/manifest.webmanifest` | PWA manifest |

> **中文**：传输相关接口给界面提供历史记录与实时速率；`/api/networks` 返回所有网卡地址并带标签；`/api/share` 生成带时效的分享直链；`/qr.png` 按需生成二维码，文本上限 512 字节；`/manifest.webmanifest` 让浏览器可以「添加到主屏幕」。

## Clipboard & push

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/clip` | Clipboard history (newest first) |
| `POST` | `/api/clip` | `{"text":"…"}`; whitespace-only or `——` is rejected |
| `POST` | `/api/clip/delete` | `{"id":"…"}` |
| `POST` | `/api/send` | Push a file to connected phones; `{"name":…,"targets":"all"}` |
| `GET` | `/api/inbox` | Phone side: files pushed to this device |
| `POST` | `/api/inbox/ack` | Phone side: mark a pushed file as received |

> **中文**：剪贴板接口在两端共享同一份历史，纯空白或只有分隔线的文本会被拒绝；推送接口让电脑把文件主动丢到已连接的手机，手机取件后需要回执确认。

## Local-only helpers

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/open?path=…` | Open a file or folder in Explorer |
| `POST` | `/api/firewall/set` | Add the Windows Firewall rule (prompts for UAC) |
| `POST` | `/api/firewall/remove` | Remove the rule |
| `POST` | `/api/open-hotspot` | Jump to the Windows hotspot settings page |

These are restricted to the machine itself and are disabled over the network.

> **中文**：这几个接口只在电脑本机可用（打开资源管理器、增删防火墙规则、跳转热点设置），网络请求一律拒绝。

## Example

```bash
# Pair, then list the Desktop folder and download one file
curl -c jar -X POST http://192.168.1.7:8848/api/login \
     -H 'Content-Type: application/json' -d '{"pin":"123456"}'
curl -b jar "http://192.168.1.7:8848/api/list?path=C%3A%5CUsers%5Cme%5CDesktop"
curl -b jar -o photo.jpg \
     "http://192.168.1.7:8848/api/download?path=C%3A%5CUsers%5Cme%5CDesktop%5Cphoto.jpg"
```

> **中文**：上面是一段最小可用的调用示例：先拿 Cookie，再列目录，最后按路径下载文件。
