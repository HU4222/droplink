# DropLink — desktop (Windows)

The Windows side is a single-port HTTP server plus a small native window. It has **no third-party runtime dependencies**.

## Run

```bat
python server.py                      :: port 8848, opens the browser UI
python server.py --port 9000
python server.py --recv "D:\Shared"   :: change the receive folder
python server.py --secure             :: hardened profile (see docs/SECURITY.md)
python server.py --no-browser         :: headless, useful for scripting
```

Build the standalone executable with `python build/build_exe.py -o dist\DropLink.exe`.

## Files

| File | Role |
|---|---|
| `server.py` | HTTP server, routing, auth, file/clipboard/push APIs, QR, firewall helpers |
| `app_main.py` | Entry point for the packaged EXE: parse flags, start server, create window |
| `gui.py` | Native Win32 window and tray icon through `ctypes` |
| `qrmini.py` | Dependency-free QR matrix + PNG/ICO rendering, also the app icon |
| `web/` | The UI served to both the local window and every phone |
| `build/` | `build_exe.py` and the regression scripts |
| `启动.bat` / `放行防火墙.bat` / `安装增强组件.bat` | One-click helpers for non-technical users |

## Notes

- Config and logs live in `%LOCALAPPDATA%\DropLink`.
- Uploads stream to a hidden `.dl_*.part` and are renamed on completion, so an interrupted transfer never appears as a real file.
- Deleting from the UI goes to the Recycle Bin, never a permanent unlink.
- `--secure` is a full second profile, not a flag on top: see [`../docs/SECURITY.md`](../docs/SECURITY.md).

> **中文**：电脑端是「单端口 HTTP 服务 + 一个原生小窗口」，不依赖任何第三方运行时。`python server.py` 直接跑，常用参数有 `--port`、`--recv`、`--secure`、`--no-browser`。配置和日志写在 `%LOCALAPPDATA%\DropLink`；上传先写隐藏的 `.part` 再改名；界面里的删除一律进回收站；`--secure` 是一整套独立的加固配置，细节见 `../docs/SECURITY.md`。
