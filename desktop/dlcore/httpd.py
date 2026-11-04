# -*- coding: utf-8 -*-
"""HTTP 传输层：服务器、请求解析、鉴权、静态资源、路由分发。

由 server.py 拆分而来（2026-11-04）。纯搬移，不改逻辑。
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import webbrowser
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .config import *
from .config import is_secure
from .fsutil import *
from .media import *
from .winenv import *
from .state import STATE
from .api import ApiMixin
from .config import _LIVE, _LIVE_LOCK, _STATIC_CACHE  # 共享缓存（原地修改）

def _static_asset(path, st, compressible):
    key = (path, st.st_mtime_ns, st.st_size)
    hit = _STATIC_CACHE.get(key)
    if hit is not None:
        return hit
    with open(path, "rb") as f:
        raw = f.read()
    gz = None
    if compressible and len(raw) > 1024:
        try:
            gz = gzip.compress(raw, 6)
            if len(gz) >= len(raw):
                gz = None
        except Exception:
            gz = None
    if len(_STATIC_CACHE) > 16:
        _STATIC_CACHE.clear()
    _STATIC_CACHE[key] = (raw, gz)
    return raw, gz



# --------------------------------------------------------------------------
# HTTP 处理
# --------------------------------------------------------------------------

class Server(ThreadingHTTPServer):
    """更高的连接队列 + 守护线程：多台手机同时连、批量并发时不丢连接。"""
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def handle_error(self, request, client_address):
        """客户端断连属于预期事件，别让它变成整段 Traceback（复测 P2-01）。

        socketserver 默认把任何异常都按未捕获异常打到 stderr，EXE 的 stderr 重定向进
        droplink.log，于是一次取消下载就写进去几十行堆栈——真正的异常会被淹没。
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            try:
                sys.stderr.write("[断连] %s:%s %s\n" % (client_address[0], client_address[1],
                                                         type(exc).__name__))
                sys.stderr.flush()
            except Exception:
                pass
            return
        super().handle_error(request, client_address)


class Handler(ApiMixin, BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"DropLink/{VERSION}"
    sys_version = ""
    # 读写超时：慢速连接（Slowloris）会在 30 秒内被断开，修复 X-07
    timeout = 30
    # 并发处理上限：再多就只能排队，避免几百条连接把线程/内存吃光
    _slots = threading.BoundedSemaphore(64)

    # ---------------- 基础设施 ----------------

    def handle(self):
        if not Handler._slots.acquire(timeout=10):
            try:
                self._send(503, b"busy", "text/plain; charset=utf-8", {"Retry-After": "5"})
            except Exception:
                pass
            self.close_connection = True
            return
        try:
            super().handle()
        finally:
            Handler._slots.release()

    def log_message(self, fmt, *args):
        pass

    def version_string(self):
        return self.server_version

    def _is_local(self):
        ip = self.client_address[0]
        return ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _host_header(self):
        h = (self.headers.get("Host") or "").strip()
        if not h or "@" in h or "/" in h or "\\" in h:
            return ""
        if h.startswith("["):
            end = h.find("]")
            return h[1:end].lower() if end > 0 else ""
        return (h.rsplit(":", 1)[0] if ":" in h else h).lower()

    def _host_ok(self):
        if not is_secure():
            return True
        h = self._host_header()
        return bool(h) and h in allowed_hosts(self.server.server_port)

    def _origin_ok(self):
        if not is_secure():
            return True
        o = (self.headers.get("Origin") or "").strip()
        if not o:
            return True
        if o.lower() == "null":
            return False
        try:
            u = urllib.parse.urlparse(o)
        except Exception:
            return False
        if u.scheme not in ("http", "https"):
            return False
        return (u.hostname or "").lower() in allowed_hosts(self.server.server_port)

    def _fetch_ok(self):
        if not is_secure():
            return True
        # 浏览器跨站请求一律拒绝；curl/原生 App 不带这个头，放行后仍要过令牌/会话
        sfs = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        return sfs in ("", "same-origin", "none")

    def _token_ok(self):
        tok = self.headers.get("X-DL-Token") or ""
        return bool(tok) and secrets.compare_digest(tok, LOCAL_TOKEN)

    def _cookie_token(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k.strip() == "dlsess":
                return v.strip()
        return ""

    def _authed(self):
        # 个人使用模式：恢复「本机请求直接放行」的旧行为
        if not is_secure() and self._is_local():
            return True
        # Host/Origin/Sec-Fetch 是硬门槛：过不了就等同于未登录（阻断 DNS 重绑定与 CSRF）
        if not (self._host_ok() and self._origin_ok() and self._fetch_ok()):
            return False
        if self._token_ok():
            return True                 # 程序自己打开的界面
        return STATE.session_valid(self._cookie_token())

    def _local_ui(self):
        """改设置 / 开程序 / 调防火墙：必须是本机、来源合法，并且已认证（令牌或配对会话）。"""
        return (self._is_local() and self._host_ok() and self._origin_ok()
                and self._fetch_ok() and self._authed())

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if is_secure():
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
            # 禁止被 iframe、禁止内联脚本执行（修复 DL-08 点击劫持与同源内联脚本）
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
                "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if getattr(self, "command", None) != "HEAD" and body:
            self.wfile.write(body)

    def _server_error(self, exc):
        """500 只回一个错误号，异常文本写日志（修复 X-08 信息回显）。

        客户端断连不是服务端错误：只记一行「[断连]」，不打错误号、不打堆栈，
        否则日志里的真错误会被取消下载这类正常事件盖住（复测 P2-01）。
        """
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            try:
                print(f"[断连] {self.client_address[0]}:{self.client_address[1]} {type(exc).__name__}")
            except Exception:
                pass
            return
        # 权限不足是「用户能不能看」的问题，不是服务端内部错误：回 403 更准确，
        # 也让降级状态下的请求有个明确回包，而不是 500 或干脆没响应。
        if isinstance(exc, PermissionError):
            try:
                return self._fail(403, "没有权限访问该位置")
            except Exception:
                return None
        eid = secrets.token_hex(4)
        try:
            print(f"[错误 {eid}] {type(exc).__name__}: {exc}")
        except Exception:
            pass
        try:
            self._fail(500, f"服务器内部错误（{eid}）", close=True)
        except Exception:
            pass

    def _json(self, obj, code=200, extra=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        headers = dict(extra or {})
        accept = (self.headers.get("Accept-Encoding") or "").lower()
        # 大目录列表很占带宽：超过 2KB 就压缩（小响应压了没意义）
        if code == 200 and len(body) > 2048 and "gzip" in accept:
            body = gzip.compress(body, 5)
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
        self._send(code, body, "application/json; charset=utf-8", headers)

    def _fail(self, code, msg, close=False, **kw):
        if close:
            self.close_connection = True
        self._json({"ok": False, "error": msg, **kw}, code)

    def _need_auth(self):
        if self._authed():
            return True
        self._json({"ok": False, "error": "需要配对码", "need_login": True}, 401)
        return False

    def _body(self, limit: int = 1 << 20) -> bytes:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return b""
        if n > limit:
            raise ValueError("请求体过大")
        data = b""
        while len(data) < n:
            chunk = self.rfile.read(min(CHUNK, n - len(data)))
            if not chunk:
                break
            data += chunk
        return data

    @staticmethod
    def _no_duplicate_keys(pairs):
        """拒绝重复键（修复 X-09：{"pin":"bad","pin":"123456"} 的解析歧义）。"""
        out = {}
        for k, v in pairs:
            if k in out:
                raise ValueError("重复字段")
            out[k] = v
        return out

    def _json_body(self):
        raw = self._body()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8", "replace"),
                          object_pairs_hook=self._no_duplicate_keys)

    # ---------------- 静态资源 ----------------

    def _serve_web(self, rel: str):
        """静态资源：ETag 协商缓存 + gzip。首次加载省 70% 传输，二次加载直接 304。"""
        rel = urllib.parse.unquote(rel)
        target = os.path.abspath(os.path.join(WEB_DIR, rel))
        # 静态资源目录是安装包自带的内部路径，这里用 os.path 自证包含关系即可。
        # 不要借用 fsutil.inside：安全基线在降级时 fail-closed，会把整站打成 404。
        root = os.path.normcase(os.path.abspath(WEB_DIR)) + os.sep
        if not (os.path.normcase(target)).startswith(root) or not os.path.isfile(target):
            return self._fail(404, "文件不存在")
        try:
            st = os.stat(target)
        except OSError:
            return self._fail(404, "文件不存在")

        etag = '"%x-%x"' % (st.st_mtime_ns, st.st_size)
        if (self.headers.get("If-None-Match") or "").strip() == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return

        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        compressible = (ctype.startswith("text/")
                        or ctype in ("application/javascript", "application/json",
                                     "application/manifest+json", "image/svg+xml"))
        if compressible and "charset" not in ctype:
            ctype += "; charset=utf-8"

        raw, gz = _static_asset(target, st, compressible)
        accept = (self.headers.get("Accept-Encoding") or "").lower()
        headers = {"ETag": etag, "Cache-Control": "no-cache", "Vary": "Accept-Encoding"}
        if gz is not None and "gzip" in accept:
            headers["Content-Encoding"] = "gzip"
            self._send(200, gz, ctype, headers)
        else:
            self._send(200, raw, ctype, headers)

    # ---------------- 文件发送（支持 Range） ----------------

    def _serve_file(self, path: str, inline=False, download_name=None):
        try:
            size = os.path.getsize(path)
        except OSError:
            return self._fail(404, "文件不存在")
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") and "charset" not in ctype:
            ctype += "; charset=utf-8"

        name = download_name or os.path.basename(path)
        ascii_name = name.encode("ascii", "ignore").decode("ascii") or "download"
        ascii_name = ascii_name.replace('"', "_").replace("\\", "_")
        # HTML / SVG 一律当附件下载：直接在服务源里打开会变成同源脚本执行（DL-08）
        if ctype.split(";")[0] in ("text/html", "image/svg+xml", "application/xhtml+xml"):
            inline = False
        disp = "inline" if inline else "attachment"
        disposition = f'{disp}; filename="{ascii_name}"; filename*=UTF-8\'\'{urllib.parse.quote(name)}'

        start, end = 0, max(0, size - 1)
        partial = False
        rng = (self.headers.get("Range") or "").strip()
        m = re.match(r"^bytes=(\d*)-(\d*)$", rng)
        if m and size > 0:
            s_txt, e_txt = m.group(1), m.group(2)
            if s_txt == "" and e_txt != "":
                back = int(e_txt)
                start = max(0, size - back)
                end = size - 1
            else:
                start = int(s_txt or 0)
                end = int(e_txt) if e_txt else size - 1
            if start >= size:
                return self._send(416, b"", ctype, {"Content-Range": f"bytes */{size}"})
            end = min(end, size - 1)
            if end < start:
                # bytes=5-1 这种倒序区间按 RFC 9110 应返回 416（修复 X-10）
                return self._send(416, b"", ctype, {"Content-Range": f"bytes */{size}"})
            partial = True

        length = end - start + 1 if size else 0
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", disposition)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD" or length == 0:
            return

        # 登记为「进行中」，让本机 App 能看到实时速率（以前只有发起方页面自己知道）
        live_id = secrets.token_hex(4)
        live = {"name": os.path.basename(path), "total": length, "sent": 0,
                "start": time.time(), "peer": self.client_address[0]}
        with _LIVE_LOCK:
            _LIVE[live_id] = live
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    buf = f.read(min(CHUNK, left))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    left -= len(buf)
                    live["sent"] += len(buf)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        finally:
            with _LIVE_LOCK:
                _LIVE.pop(live_id, None)

    def _dl_page(self, filepath: str, token: str):
        """手机扫分享二维码后，先展示下载确认页（文件名 + 大小 + 下载按钮 + 位置说明）。"""
        name = os.path.basename(filepath)
        try:
            size = os.path.getsize(filepath)
        except OSError:
            size = 0
        if size >= 1 << 30:
            human = f"{size / (1 << 30):.2f} GB"
        elif size >= 1 << 20:
            human = f"{size / (1 << 20):.1f} MB"
        elif size >= 1 << 10:
            human = f"{size / (1 << 10):.0f} KB"
        else:
            human = f"{size} B"
        esc_name = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        html = (
            "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>下载文件</title><style>"
            "*{box-sizing:border-box}body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;margin:0;"
            "background:#f4f5fb;color:#0f172a;display:flex;align-items:center;justify-content:center;"
            "min-height:100vh;padding:16px;overflow-x:hidden}"
            ".card{background:#fff;border-radius:22px;padding:34px 26px;width:100%;max-width:min(420px,100vw - 32px);"
            "box-shadow:0 12px 44px rgba(15,23,42,.10);text-align:center}"
            ".ico{width:66px;height:66px;border-radius:19px;background:linear-gradient(135deg,#4f46e5,#7c3aed);"
            "color:#fff;display:flex;align-items:center;justify-content:center;margin:0 auto 18px}"
            ".ico svg{width:30px;height:30px}"
            "h1{font-size:16px;font-weight:600;color:#64748b;margin:0 0 8px}"
            ".name{font-size:16px;font-weight:650;word-break:break-all;margin:0 0 6px;line-height:1.4}"
            ".meta{font-size:13px;color:#94a3b8;margin:0 0 26px}"
            ".btn{display:block;width:100%;padding:15px;border:none;border-radius:15px;"
            "background:linear-gradient(135deg,#4f46e5,#7c3aed);color:#fff;font-size:16px;font-weight:650;"
            "text-decoration:none;cursor:pointer}"
            ".btn:active{transform:scale(.98)}"
            ".tip{font-size:12px;color:#94a3b8;margin:18px 0 0;line-height:1.7}"
            "@media (prefers-color-scheme:dark){body{background:#0b1220;color:#e2e8f0}"
            ".card{background:#111827}.name{color:#e2e8f0}.meta,.tip{color:#7c8aa0}}"
            "</style></head><body><div class='card'>"
            "<div class='ico'><svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.2'"
            " stroke-linecap='round' stroke-linejoin='round'><path d='M12 3v11'/><path d='m7 10 5 5 5-5'/>"
            "<path d='M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2'/></svg></div>"
            f"<h1>来自电脑的文件</h1>"
            f"<p class='name'>{esc_name}</p>"
            f"<p class='meta'>{human} · 点击下方按钮下载</p>"
            f"<a class='btn' href='/dl/{token}/file'>下载</a>"
            "<p class='tip'>文件会保存到手机浏览器的「下载」目录。<br>"
            "如需改保存位置，请在浏览器菜单的「下载」设置里修改。</p>"
            "</div></body></html>"
        )
        return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

    # ---------------- 路由 ----------------

    def do_GET(self):
        try:
            self._route_get()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001
            self._server_error(exc)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        try:
            self._route_post()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001
            self._server_error(exc)

    def _parts(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        return urllib.parse.unquote(parsed.path), {k: v[0] for k, v in qs.items()}

    def _route_get(self):
        # Host / Origin 先过一遍：DNS 重绑定的请求会带着攻击者的域名，这里直接拒绝
        if not self._host_ok():
            self.close_connection = True
            return self._send(421, b"", "text/plain; charset=utf-8")
        if not (self._origin_ok() and self._fetch_ok()):
            self.close_connection = True
            return self._fail(403, "请求来源不被允许")

        path, q = self._parts()

        if path in ("/", "/index.html"):
            return self._serve_web("index.html")
        if path.startswith("/static/"):
            return self._serve_web(path[len("/static/"):])
        if path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
            png = icon_png(180)
            if png:
                return self._send(200, png, "image/png", {"Cache-Control": "max-age=86400"})
            return self._send(204, b"")
        if path == "/favicon.ico":
            png = icon_png(64)
            if png:
                return self._send(200, png, "image/png", {"Cache-Control": "max-age=86400"})
            return self._send(204, b"")
        if path == "/api/icon":
            try:
                size = int(q.get("size") or 192)
            except (TypeError, ValueError):
                return self._fail(400, "参数无效")
            size = max(48, min(512, size))
            png = icon_png(size)
            if not png:
                return self._fail(404, "未安装 Pillow")
            return self._send(200, png, "image/png", {"Cache-Control": "max-age=86400"})
        if path == "/manifest.webmanifest":
            icons = []
            if icon_png(192):
                icons = [
                    {"src": "/api/icon?size=192", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                    {"src": "/api/icon?size=512", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
                ]
            manifest = {
                # iOS Safari 17+ / 鸿蒙浏览器都读这份 manifest；
                # id 与 scope 固定，避免「添加到主屏幕」后出现两个图标
                "id": "/", "name": APP_NAME, "short_name": APP_SHORT,
                "description": "同一个 Wi-Fi 下，手机与电脑互传文件",
                "lang": "zh-CN", "dir": "ltr",
                "start_url": "/?src=pwa", "scope": "/",
                "display": "standalone",
                "display_override": ["standalone", "minimal-ui", "browser"],
                "orientation": "any",
                "background_color": "#f4f6fb", "theme_color": "#4f46e5",
                "icons": icons,
            }
            return self._send(200, json.dumps(manifest, ensure_ascii=False),
                              "application/manifest+json; charset=utf-8")
        if path == "/qr.png":
            text = q.get("text") or ""
            if len(text.encode("utf-8", "ignore")) > 512:
                return self._fail(414, "二维码内容过长")
            png = qr_png(text) if text else None
            if not png:
                return self._fail(404, "二维码不可用")
            return self._send(200, png, "image/png", {"Cache-Control": "no-store"})
        if path.startswith("/dl/"):
            rest = path[4:].strip("/")
            is_file = rest.endswith("/file")
            token = rest[:-5] if is_file else rest
            item = STATE.shares.get(token)
            if not item or item["exp"] < time.time():
                return self._fail(404, "链接已失效")
            if is_file:
                return self._serve_file(item["path"])
            return self._dl_page(item["path"], token)

        # ---- API ----
        if path == "/api/ping":
            body = {"ok": True, "ts": time.time()}
            try:
                import dlcore
                bad = dlcore.degraded_names()
                if bad:
                    body["degraded"] = bad
            except Exception:
                pass
            return self._json(body)
        if path == "/api/session":
            # 未认证不返回任何主机信息（修复 DL-05：以前未授权就能拿到 PIN/路径/内网 IP）
            if not self._need_auth():
                return None
            return self.api_session()
        if not self._need_auth():
            return None
        if path == "/api/logout":
            return self.api_logout()
        if path == "/api/sessions":
            return self.api_sessions()
        if path == "/api/list":
            return self.api_list(q)
        if path == "/api/roots":
            return self._json({"ok": True, "roots": quick_roots(), "recv_dir": STATE.recv_dir})
        if path == "/api/download":
            return self.api_download(q)
        if path == "/api/zip":
            return self.api_zip_get(q)
        if path == "/api/thumb":
            return self.api_thumb(q)
        if path == "/api/preview":
            return self.api_preview(q)
        if path == "/api/sha256":
            return self.api_sha256(q)
        if path == "/api/search":
            return self.api_search(q)
        if path == "/api/clip":
            with STATE.lock:
                items = list(STATE.clip)
            return self._json({"ok": True, "items": items})
        if path == "/api/transfers":
            with STATE.lock:
                items = list(STATE.transfers)
            return self._json({"ok": True, "items": items})
        if path == "/api/config":
            return self._json({"ok": True, "recv_dir": STATE.recv_dir})
        if path == "/api/diag":
            return self.api_diag()
        if path == "/api/networks":
            return self.api_networks()
        if path == "/api/live":
            return self.api_live()
        if path == "/api/peers":
            return self.api_peers()
        if path == "/api/inbox":
            return self.api_inbox()
        return self._fail(404, "接口不存在")

    def _route_post(self):
        if not self._host_ok():
            self.close_connection = True
            return self._send(421, b"", "text/plain; charset=utf-8")
        if not (self._origin_ok() and self._fetch_ok()):
            self.close_connection = True
            return self._fail(403, "请求来源不被允许")

        path, q = self._parts()

        if path == "/api/login":
            return self.api_login()
        if path == "/api/upload":
            return self.api_upload(q)
        if not self._need_auth():
            return None
        if path == "/api/logout":
            return self.api_logout()
        if path == "/api/sessions/revoke":
            return self.api_sessions_revoke()
        if path == "/api/clip":
            return self.api_clip_add()
        if path == "/api/clip/delete":
            return self.api_clip_del()
        if path == "/api/mkdir":
            return self.api_mkdir()
        if path == "/api/rename":
            return self.api_rename()
        if path == "/api/delete":
            return self.api_delete()
        if path == "/api/zip":
            return self.api_zip()
        if path == "/api/share":
            return self.api_share()
        if path == "/api/send":
            return self.api_send(q)
        if path == "/api/inbox/ack":
            return self.api_inbox_ack()
        if path == "/api/config":
            return self.api_set_config()
        if path == "/api/open":
            return self.api_open()
        if path == "/api/firewall/set":
            return self.api_firewall_set()
        if path == "/api/firewall/remove":
            return self.api_firewall_remove()
        if path == "/api/open-hotspot":
            return self.api_open_hotspot()
        return self._fail(404, "接口不存在")

