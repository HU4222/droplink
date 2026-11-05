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
from .config import EDITION, is_secure, is_secure_edition
from .logsafe import log, redact_ip, redact_path, safe_exc, stamp
from .fsutil import *
from .fsutil import SESSION_ABS_TTL
from .media import *
from .winenv import *
from .state import STATE
from .api import ApiMixin
from .config import _LIVE, _LIVE_LOCK, _STATIC_CACHE  # 共享缓存（原地修改）

# --------------------------------------------------------------------------
# 按版注册的路由（1.5.0 分版骨架）
# --------------------------------------------------------------------------
# 安全版预留的三个接口；本轮（骨架期）只回 501，第二轮实装。
# 关键点：个人版里这些路径压根不进分发，会落到各自末尾的 404，
# 而不是 403/501 —— 不对外暴露「这里本来有个接口」。
# 安全版预留的三个接口；本轮（骨架期）只回 501，第二轮实装。
# 关键点：个人版里这些路径压根不进分发，会落到各自末尾的 404，
# 而不是 403/501 —— 不对外暴露「这里本来有个接口」。
SECURE_EDITION_ROUTES = frozenset((
    "/api/device",
    "/api/trust", "/api/trust/pending", "/api/trust/approve",
    "/api/trust/reject", "/api/trust/revoke", "/api/trust/auto-accept",
    "/api/login/status",
    "/api/prepare-upload", "/api/upload/status",
    "/api/approve", "/api/approve/pending", "/api/approve/cancel",
))

EDITION_ROUTES = {
    "personal": frozenset(),
    "secure": SECURE_EDITION_ROUTES,
}

# 所有版本出现过的专属路径（与「当前版本是否启用」无关），
# 供 PUT/DELETE/OPTIONS 这类没有业务的方法判断该回 404 还是 501。
ALL_EDITION_ROUTES = frozenset().union(*EDITION_ROUTES.values())


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

    def __init__(self, server_address, RequestHandlerClass, context=None, scheme=None):
        super().__init__(server_address, RequestHandlerClass)
        # 安全版：监听 socket 包成 TLS（http.server 的标准做法）。
        # 必须在 bind/listen 之后包，wrap 的是同一个 fd，accept 时做握手。
        self.tls_context = context
        # 对外的协议：个人版 http、安全版 https。路由/接口拼地址一律读它，
        # 不要在业务代码里再写死 "http://"（P1-1）。
        self.scheme = scheme or ("https" if context is not None else "http")
        if context is not None:
            self.socket = context.wrap_socket(self.socket, server_side=True)

    def handle_error(self, request, client_address):
        """客户端断连属于预期事件，别让它变成整段 Traceback（复测 P2-01）。

        socketserver 默认把任何异常都按未捕获异常打到 stderr，EXE 的 stderr 重定向进
        droplink.log，于是一次取消下载就写进去几十行堆栈——真正的异常会被淹没。
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            try:
                sys.stderr.write(stamp() + " [断连] %s %s\n" % (redact_ip(client_address[0]),
                                                     type(exc).__name__))
                sys.stderr.flush()
            except Exception:
                pass
            return
        # TLS 握手/解密失败（明文打 TLS 端口、证书告警中途取消）同样只记一行。
        # 个人版包里没有 _ssl，所以按需导入：导不进来就说明不是 TLS 环境。
        try:
            import ssl as _ssl
            is_tls = isinstance(exc, _ssl.SSLError)
        except Exception:
            is_tls = False
        if is_tls:
            try:
                sys.stderr.write(stamp() + " [TLS] %s %s\n" % (redact_ip(client_address[0]),
                                                     type(exc).__name__))
                sys.stderr.flush()
            except Exception:
                pass
            return
        super().handle_error(request, client_address)


_CN_WARN_LOCK = threading.Lock()
_CN_WARNED = set()

# 个人版也要过 Origin 校验的「会改文件系统」的 POST（S-13）
_MUTATING_POSTS = {
    "/api/upload", "/api/delete", "/api/mkdir", "/api/rename",
    "/api/clip", "/api/clip/delete",
}

# 会被浏览器当成脚本/标记执行、必须强制下载的扩展名（仅安全版，S-12）
_ACTIVE_CONTENT_EXT = frozenset((
    "html", "htm", "xhtml", "svg", "svgz", "mht", "mhtml", "xml", "xsl",
))


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
        # 两版都做 DNS 重绑定防护（S-13）：Host 必须是本机名/回环/本机网卡 IP。
        h = self._host_header()
        ok = bool(h) and h in allowed_hosts(self.server.server_port)
        if ok and is_secure_edition():
            self._warn_cn_mismatch(h)
        return ok

    def _warn_cn_mismatch(self, host_header: str):
        """Host 与证书 SAN 不一致时记一行（不拒绝：局域网 IP 访问本来就常见）。"""
        sans = getattr(STATE, "cert_sans", None)
        if not sans:
            return
        host = host_header.rsplit(":", 1)[0] if ":" in host_header else host_header
        key = host.lower()
        if key in sans or key in ("127.0.0.1", "localhost", "::1"):
            return
        with _CN_WARN_LOCK:
            if key in _CN_WARNED:
                return
            _CN_WARNED.add(key)
        log("[TLS] Host %s 不在证书 SAN 内（不影响访问）" % redact_ip(host))

    def _origin_ok(self):
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
        # 两版都禁止被 iframe（S-13）；CSP / Permissions-Policy 仍只给安全版
        self.send_header("X-Frame-Options", "DENY")
        if is_secure():
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
        if getattr(self, "close_connection", False):
            # 错误回包后要关连接：先把客户端可能还在发的请求体读掉再关。
            # 否则 Windows 会在关闭带未读数据的 socket 时发 RST，客户端偶发
            # ConnectionAbortedError（加固套件的「连续 4 个坏名字」曾因此抖动）。
            #
            # 但必须封顶：慢速大包（每 0.1s 喂 8KB）会让 settimeout 永远不触发，
            # 一个连接就能长期占住一个线程。宁可对异常客户端发 RST，也不排空无底洞。
            #   上限 1 MB / 1 秒；排不完直接关（正常客户端错误回包时 body 都在 1MB 内）。
            try:
                self.connection.settimeout(0.2)
                deadline = time.time() + 1.0
                drained = 0
                while drained < 1024 * 1024 and time.time() < deadline:
                    chunk = self.rfile.read(min(8192, 1024 * 1024 - drained))
                    if not chunk:
                        break
                    drained += len(chunk)
            except Exception:
                pass

    def _server_error(self, exc):
        """500 只回一个错误号，异常文本写日志（修复 X-08 信息回显）。

        客户端断连不是服务端错误：只记一行「[断连]」，不打错误号、不打堆栈，
        否则日志里的真错误会被取消下载这类正常事件盖住（复测 P2-01）。
        """
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            try:
                log(f"[断连] {redact_ip(self.client_address[0])} {type(exc).__name__}")
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
            log(f"[错误 {eid}] {safe_exc(exc)}")
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

    def _fail(self, status, msg, close=False, why="", retry_after=None, **kw):
        """统一的失败响应。

        status   : HTTP 状态码（沿用旧位置参数，别改名，几十处调用都是位置传参）
        why      : 机器可读错误码，只有安全版会写进 JSON 的 "code" 字段；
                   个人版一个字段都不多，保持 1.4.x 客户端兼容（S-05）。
        retry_after : 秒数，给了就回 Retry-After 头（429）。
        """
        if close:
            self.close_connection = True
        body = {"ok": False, "error": msg}
        if why and is_secure_edition():
            body["code"] = why
        body.update(kw)
        extra = None
        if retry_after is not None:
            extra = {"Retry-After": str(int(retry_after))}
        self._json(body, status, extra=extra)

    def _need_auth(self):
        if self._authed():
            return True
        body = {"ok": False, "error": "需要配对码", "need_login": True}
        if is_secure_edition():
            body["code"] = "AUTH_REQUIRED"
        self._json(body, 401)
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
        # 可执行内容强制下载：直接在服务源里打开会变成同源脚本执行（S-12）。
        # 安全版扩到 9 种扩展名并统一 octet-stream；个人版保持 1.4.x 的既有行为。
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        html_like = ctype.split(";")[0] in ("text/html", "image/svg+xml", "application/xhtml+xml")
        if is_secure_edition() and ext in _ACTIVE_CONTENT_EXT:
            ctype = "application/octet-stream"
            inline = False
        elif is_secure_edition() and html_like:
            inline = False
        elif html_like:
            # 个人版保持 1.4.x 体验：HTML/SVG 直接在浏览器里打开，不强制下载（P2-1）
            inline = True
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

    def _route_other(self):
        """PUT / DELETE / OPTIONS / PATCH：没有对应业务，但版本专属路径要一致。

        个人版对这些路径仍回 404（不暴露接口存在）、安全版回 501；
        其余路径保持原有「不支持的方法」语义（501），
        避免框架默认的 501 HTML 页面被当成「路由存在」的证据。
        """
        path, _q = self._parts()
        if path in ALL_EDITION_ROUTES:
            # 版本专属路径：当前版本启用了就 501（骨架），没启用就 404
            if not self._edition_route(path):
                self._fail(404, "接口不存在", why="NO_ROUTE")
            return
        self._send(501, b"", "text/plain; charset=utf-8")

    do_PUT = _route_other
    do_DELETE = _route_other
    do_OPTIONS = _route_other
    do_PATCH = _route_other

    def _parts(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        return urllib.parse.unquote(parsed.path), {k: v[0] for k, v in qs.items()}

    def _edition_route(self, path: str) -> bool:
        """版本专属路由分发：命中并已回包返回 True，未命中返回 False。

        安全版：证书指纹、设备信任、首次配对轮询、传输前确认（prepare/approve）。
        个人版不注册这些路径，由调用方继续走到 404。
        """
        if path not in EDITION_ROUTES.get(EDITION, ()):
            return False
        read_ok = self.command in ("GET", "HEAD")
        if path == "/api/device":
            if not read_ok:
                return self._method_not_allowed()
            self._api_device()
        elif path == "/api/login/status":
            if not read_ok:
                return self._method_not_allowed()
            self._api_login_status()
        elif path == "/api/trust":
            if not read_ok:
                return self._method_not_allowed()
            self._api_trust_list()
        elif path == "/api/trust/pending":
            if not read_ok:
                return self._method_not_allowed()
            self._api_trust_pending()
        elif path == "/api/trust/approve":
            if self.command != "POST":
                return self._method_not_allowed()
            self._api_trust_approve()
        elif path == "/api/trust/reject":
            if self.command != "POST":
                return self._method_not_allowed()
            self._api_trust_reject()
        elif path == "/api/trust/revoke":
            if self.command != "POST":
                return self._method_not_allowed()
            self._api_trust_revoke()
        elif path == "/api/trust/auto-accept":
            if self.command != "POST":
                return self._method_not_allowed()
            self._api_trust_auto_accept()
        elif path == "/api/prepare-upload":
            if self.command != "POST":
                return self._method_not_allowed()
            self._api_prepare_upload()
        elif path == "/api/upload/status":
            if not read_ok:
                return self._method_not_allowed()
            self._api_upload_status()
        elif path == "/api/approve/pending":
            if not read_ok:
                return self._method_not_allowed()
            self._api_approve_pending()
        elif path == "/api/approve":
            if self.command != "POST":
                return self._method_not_allowed()
            self._api_approve_decide()
        elif path == "/api/approve/cancel":
            if self.command != "POST":
                return self._method_not_allowed()
            self._api_approve_cancel()
        else:
            self._json({"ok": False, "error": "not implemented in skeleton"}, code=501)
        return True

    def _method_not_allowed(self) -> bool:
        self._fail(405, "方法不允许")
        return True

    def _qps_ok(self, bucket: str) -> bool:
        """安全版的分接口限流；个人版恒放行，本机回环豁免（S-07）。"""
        if not is_secure_edition():
            return True
        if self._is_local():
            return True
        from dlcore.sec import ratelimit
        ok, wait = ratelimit.allow(bucket, self.client_address[0])
        if ok:
            return True
        retry = int(wait) if float(wait).is_integer() else int(wait) + 1
        self._fail(429, "请求过于频繁，请稍后再试", why="RATE_LIMITED",
                   retry_after=max(1, retry))
        return False

    def _local_only(self) -> bool:
        """设备信任相关操作只在本机可用；局域网客户端一律 403。

        额外过一遍 Host / Origin / Sec-Fetch：防止用户浏览器里打开的恶意网页
        跨站 POST 到 127.0.0.1 来「替自己点批准」。
        """
        return (self._is_local() and self._host_ok()
                and self._origin_ok() and self._fetch_ok())

    def _json_body_or_400(self):
        try:
            return self._json_body()
        except Exception:  # noqa: BLE001
            self._fail(400, "请求格式错误", close=True)
            return None

    def _api_device(self):
        """证书指纹（只读）：给「首次连接时核对指纹」用，不需要登录。"""
        try:
            from dlcore.sec import certgen
            info = certgen.current() or certgen.ensure_cert(verbose=False)
        except Exception as exc:  # noqa: BLE001
            return self._fail(503, "证书信息不可用：%s" % exc)
        return self._json({
            "ok": True,
            "edition": EDITION,
            "fingerprint": info["fingerprint"],
            "fingerprint_short": info["fingerprint_short"],
            "not_after": info["not_after"],
            "sans": info["sans"],
        })

    # ---------------- 首次配对审批 ----------------

    def _pair_gate(self, data, ip: str, ua: str):
        """安全版首次配对闸门（覆盖 ApiMixin 的默认空实现）。

        已信任设备 -> 返回 device_id，正常发会话；
        新设备     -> 只登记请求并回 202，一个字节的会话都不发；
        参数不对   -> 400。
        """
        if not is_secure_edition():
            return None
        from dlcore.sec import trust
        device_id = str(data.get("device_id") or "").strip()
        if not trust.valid_device_id(device_id):
            self._fail(400, "缺少或非法的设备标识")
            return ""
        if trust.get(device_id) is not None:
            trust.touch(device_id, ip)
            return device_id
        req = trust.request_pair(
            device_id,
            str(data.get("device_name") or "").strip(),
            str(data.get("platform") or "").strip(),
            ua, ip)
        log(f"[配对] 新设备待批准：{req['name']}（{redact_ip(ip)}）")
        self._json({
            "ok": False,
            "status": "pending_approval",
            "request_id": req["request_id"],
            "expires_in": max(0, int(req["expires_at"] - time.time())),
        }, code=202)
        return ""

    def _api_login_status(self):
        """待批准设备轮询：pending 202 / approved 200 + 会话 Cookie / rejected 403。"""
        from dlcore.sec import trust
        q = self._parts()[1]
        rid = (q.get("request_id") or "").strip()
        rec = trust.status(rid) if rid else None
        if rec is None:
            return self._fail(404, "请求不存在", why="NOT_FOUND")
        if rec["status"] == "expired":
            # 过期是明确状态，不是「查无此请求」（P2-1）
            return self._json({"ok": False, "status": "expired", "code": "GONE"}, code=200)
        if rec["status"] == "rejected":
            return self._fail(403, "本机已拒绝该设备", why="FORBIDDEN")
        if rec["status"] == "approved":
            extra = None
            token = rec.get("session_token") or ""
            if not token:
                # 批准与发出会话之间有个毫秒级窗口：这里补一次，避免客户端
                # 恰好在那一刻轮询到 approved 却拿不到 Cookie。
                token = STATE.new_session(rec["ip"], rec["ua"], device_id=rec["device_id"])
                trust.attach_session(rid, token)
            extra = {"Set-Cookie":
                     "dlsess=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict%s"
                     % (token, SESSION_ABS_TTL, "; Secure" if is_secure_edition() else "")}
            return self._json({"ok": True, "status": "approved",
                               "device": socket.gethostname()}, extra=extra)
        return self._json({
            "ok": False, "status": "pending_approval", "request_id": rid,
            "expires_in": max(0, int(rec["expires_at"] - time.time())),
        }, code=202)

    # ---------------- 信任列表 ----------------

    def _api_trust_list(self):
        if not self._local_only():
            return self._fail(403, "只有电脑本机可以查看信任列表", why="LOCAL_ONLY")
        from dlcore.sec import trust
        return self._json({"ok": True, "devices": trust.list_devices()})

    def _api_trust_pending(self):
        if not self._local_only():
            return self._fail(403, "只有电脑本机可以查看待批准设备", why="LOCAL_ONLY")
        from dlcore.sec import trust
        return self._json({"ok": True, "pending": trust.pending_list()})

    def _api_trust_approve(self):
        if not self._local_only():
            return self._fail(403, "只有电脑本机可以批准设备", why="LOCAL_ONLY")
        data = self._json_body_or_400()
        if data is None:
            return
        rid = str(data.get("request_id") or "").strip()
        from dlcore.sec import trust
        rec0 = trust.status(rid) if rid else None
        if rec0 is None:
            return self._fail(404, "请求不存在", why="NOT_FOUND")
        if rec0["status"] == "expired":
            return self._fail(410, "请求已过期", why="GONE")
        rec = trust.approve(rid)
        if rec is None:
            return self._fail(409, "该请求已处理", why="STATE_CONFLICT")
        token = STATE.new_session(rec["ip"], rec["ua"], device_id=rec["device_id"])
        trust.attach_session(rid, token)
        log(f"[配对] 已批准：{rec['name']}（{redact_ip(rec['ip'])}）")
        return self._json({"ok": True, "request_id": rid,
                           "device": rec["name"], "ip": rec["ip"]})

    def _api_trust_reject(self):
        if not self._local_only():
            return self._fail(403, "只有电脑本机可以拒绝设备", why="LOCAL_ONLY")
        data = self._json_body_or_400()
        if data is None:
            return
        rid = str(data.get("request_id") or "").strip()
        from dlcore.sec import trust
        rec0 = trust.status(rid) if rid else None
        if rec0 is None:
            return self._fail(404, "请求不存在", why="NOT_FOUND")
        if rec0["status"] == "expired":
            return self._fail(410, "请求已过期", why="GONE")
        if not trust.reject(rid):
            return self._fail(409, "该请求已处理", why="STATE_CONFLICT")
        log(f"[配对] 已拒绝请求 {rid[:6]}")
        return self._json({"ok": True, "request_id": rid})

    def _api_trust_revoke(self):
        if not self._local_only():
            return self._fail(403, "只有电脑本机可以撤销设备", why="LOCAL_ONLY")
        data = self._json_body_or_400()
        if data is None:
            return
        device_id = str(data.get("device_id") or "").strip()
        from dlcore.sec import trust
        if not trust.revoke(device_id):
            return self._fail(404, "设备不存在", why="NOT_FOUND")
        n = STATE.revoke_device_sessions(device_id)
        log(f"[配对] 已撤销设备 {device_id[:6]}，断开 {n} 个会话")
        return self._json({"ok": True, "sessions": n})

    def _api_trust_auto_accept(self):
        """每台已信任设备的「自动接受传输」开关（仅本机，落盘）。"""
        if not self._local_only():
            return self._fail(403, "只有电脑本机可以修改自动接受设置", why="LOCAL_ONLY")
        data = self._json_body_or_400()
        if data is None:
            return
        device_id = str(data.get("device_id") or "").strip()
        enabled = bool(data.get("enabled"))
        from dlcore.sec import trust
        if not trust.set_auto_accept(device_id, enabled):
            return self._fail(404, "设备不存在", why="NOT_FOUND")
        log(f"[配对] 设备 {device_id[:6]} 自动接受 = {'开' if enabled else '关'}")
        return self._json({"ok": True, "auto_accept": enabled})

    # ---------------- 传输前确认（S-03 / S-04） ----------------

    def _session_device_id(self) -> str:
        """当前 Cookie 会话绑定的 device_id（没绑定就是空串）。"""
        tok = self._cookie_token()
        if not tok:
            return ""
        with STATE.lock:
            rec = STATE.sessions.get(tok) or {}
            return str(rec.get("device_id") or "")

    def _api_prepare_upload(self):
        """阶段一：只收元数据，不落盘；返回 pending（或 auto 时的 accepted + token）。"""
        if not self._authed():
            return self._fail(401, "需要配对码", why="AUTH_REQUIRED")
        data = self._json_body_or_400()
        if data is None:
            return
        from dlcore.sec import approval, pathguard, trust
        dev_id = self._session_device_id()
        body_id = str(data.get("device_id") or "").strip()
        if body_id and dev_id and body_id != dev_id:
            return self._fail(403, "设备标识与会话不一致", why="FORBIDDEN")
        if not dev_id:
            return self._fail(403, "本次会话没有绑定设备", why="FORBIDDEN")
        dev = trust.get(dev_id)
        if dev is None:
            return self._fail(403, "这台设备未获信任", why="FORBIDDEN")
        files = data.get("files")
        if not isinstance(files, list) or not files:
            return self._fail(400, "文件列表为空")
        # 名字与 rel 的每一段都在这里先体检：脏名字根本不进审批队列（S-06）
        issues = []
        for raw in files[:200]:
            if not isinstance(raw, dict):
                issues.append("文件项格式错误")
                continue
            issues += pathguard.audit(str(raw.get("name") or ""))
            rel = str(raw.get("rel") or "").replace("\\", "/")
            for seg in [x for x in rel.split("/") if x]:
                issues += pathguard.audit(seg)
            try:
                if int(raw.get("size") or 0) < 0:
                    issues.append("文件大小无效")
            except (TypeError, ValueError):
                issues.append("文件大小无效")
        if issues:
            uniq = list(dict.fromkeys(issues))
            return self._fail(422, "；".join(uniq[:6]), why="INVALID_NAME")
        rec = approval.prepare(dev_id, dev.get("name") or "", self.client_address[0],
                               files, auto_accept=bool(dev.get("auto_accept")))
        log(f"[传输] {dev.get('name') or dev_id[:6]} 申请上传 {rec['file_count']} 个文件"
              f"（{human_size(rec['total_bytes'])}），状态 {rec['state']}")
        return self._json(dict(rec, ok=True))

    def _api_upload_status(self):
        """阶段二之前：设备轮询自己那次申请的状态，accepted 时拿逐文件 token。"""
        if not self._authed():
            return self._fail(401, "需要配对码", why="AUTH_REQUIRED")
        q = self._parts()[1]
        sid = str(q.get("session_id") or "").strip()
        from dlcore.sec import approval
        rec = approval.status(sid)
        if rec is None:
            return self._fail(404, "申请不存在", why="NOT_FOUND")
        if rec["device_id"] != self._session_device_id():
            return self._fail(403, "不能查看其它设备的申请", why="FORBIDDEN")
        if rec["state"] in ("expired", "cancelled"):
            return self._json({"ok": False, "error": "申请已过期或已取消",
                               "code": "GONE", "state": rec["state"],
                               "session_id": sid}, code=410)
        return self._json(dict(approval.view(rec, with_tokens=rec["state"] == "accepted"), ok=True))

    def _api_approve_pending(self):
        if not self._local_only():
            return self._fail(403, "只有电脑本机可以查看待确认传输", why="LOCAL_ONLY")
        from dlcore.sec import approval
        return self._json({"ok": True, "sessions": approval.pending()})

    def _api_approve_decide(self):
        """本机接受 / 拒绝；接受时给每个（或指定）文件签发一次性 token。"""
        if not self._local_only():
            return self._fail(403, "只有电脑本机可以确认传输", why="LOCAL_ONLY")
        data = self._json_body_or_400()
        if data is None:
            return
        sid = str(data.get("session_id") or "").strip()
        action = str(data.get("action") or "").strip()
        from dlcore.sec import approval
        rec0 = approval.status(sid)
        if rec0 is None:
            return self._fail(404, "申请不存在", why="NOT_FOUND")
        if rec0["state"] in ("expired", "cancelled"):
            return self._fail(410, "申请已过期或已取消", why="GONE")
        if rec0["state"] != "pending":
            return self._fail(409, "该申请已经处理过了", why="STATE_CONFLICT")
        if action == "accept":
            ids = data.get("file_ids")
            rec = approval.accept(sid, ids if isinstance(ids, list) else None)
            if rec is None:
                return self._fail(409, "该申请已经处理过了", why="STATE_CONFLICT")
            log(f"[传输] 已接受 {sid[:6]}：{len(rec['files'])} 个文件")
            return self._json({"ok": True, "state": rec["state"],
                               "session_id": sid})
        if action == "reject":
            if not approval.reject(sid):
                return self._fail(409, "该申请已经处理过了", why="STATE_CONFLICT")
            log(f"[传输] 已拒绝 {sid[:6]}")
            return self._json({"ok": True, "state": "rejected", "session_id": sid})
        return self._fail(400, "action 只能是 accept 或 reject")

    def _api_approve_cancel(self):
        """取消：电脑本机，或发起这次申请的那台设备。"""
        if not self._authed() and not self._local_only():
            return self._fail(403, "没有权限取消这次传输")
        data = self._json_body_or_400()
        if data is None:
            return
        sid = str(data.get("session_id") or "").strip()
        from dlcore.sec import approval
        rec = approval.status(sid)
        if rec is None:
            return self._fail(404, "申请不存在", why="NOT_FOUND")
        if not self._local_only() and rec["device_id"] != self._session_device_id():
            return self._fail(403, "不能取消其它设备的传输", why="FORBIDDEN")
        if rec["state"] == "expired":
            return self._fail(410, "申请已过期", why="GONE")
        if rec["state"] == "cancelled":
            return self._fail(409, "这次传输已经取消过了", why="STATE_CONFLICT")
        if not approval.cancel(sid):
            return self._fail(409, "这次传输已经处理过了", why="STATE_CONFLICT")
        log(f"[传输] 已取消 {sid[:6]}")
        return self._json({"ok": True, "state": "cancelled", "session_id": sid})

    def _upload_gate(self, q):
        """安全版上传闸门：校验 prepare 阶段下发的逐文件一次性 token。

        返回：True（个人版，沿用原逻辑）/ dict（安全版，已核销，带文件元数据）/ None（已回包）。
        """
        if not is_secure_edition():
            return True
        if not self._authed():
            self.close_connection = True
            self._fail(401, "需要配对码", why="AUTH_REQUIRED")
            return None
        from dlcore.sec import approval
        self._upload_sid = str(q.get("session_id") or "")
        self._upload_fid = str(q.get("file_id") or "")
        rec0 = approval.status(self._upload_sid)
        if rec0 is None:
            self.close_connection = True
            self._fail(404, "上传申请不存在", why="NOT_FOUND")
            return None
        if rec0["state"] == "rejected":
            self.close_connection = True
            self._fail(403, "电脑端拒绝了这次传输", why="TRANSFER_REJECTED")
            return None
        if rec0["state"] in ("expired", "cancelled"):
            self.close_connection = True
            self._fail(410, "这次传输已过期或已取消", why="GONE")
            return None
        if rec0["state"] == "pending":
            self.close_connection = True
            self._fail(409, "这次传输还不接受上传", why="STATE_CONFLICT")
            return None
        # 同一会话最多 2 个文件同时在写；第三个直接 429，不排队（S-07）
        if approval.active_uploads(self._upload_sid) >= 2:
            self.close_connection = True
            self._fail(429, "同一传输的并发上传过多", why="RATE_LIMITED", retry_after=1)
            return None
        rec, f = approval.consume(self._upload_sid, self._upload_fid,
                                  str(q.get("token") or ""))
        if rec is None:
            self.close_connection = True
            self._fail(403, f or "上传凭证无效", why="TOKEN_INVALID")
            return None
        return f


    def _route_get(self):
        # Host 先过一遍（两版）：DNS 重绑定请求会带攻击者域名，直接 421。
        if not self._host_ok():
            self.close_connection = True
            return self._send(421, b"", "text/plain; charset=utf-8")
        # Origin / Sec-Fetch：安全版所有 GET 都查；个人版保持 1.4.x 的宽松（S-13）
        if is_secure() and not (self._origin_ok() and self._fetch_ok()):
            self.close_connection = True
            return self._fail(403, "请求来源不被允许", why="BAD_ORIGIN")

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

        # ---- 版本专属路由（安全版预留；个人版不注册，继续往下落到 404）----
        if self._edition_route(path):
            return

        # ---- API ----
        if path == "/api/ping":
            body = {"ok": True, "ts": time.time(), "edition": EDITION, "version": VERSION}
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
        # 分接口限流放在鉴权前：未登录的洪水同样算数（本机豁免见 _qps_ok）
        if path in ("/api/list", "/api/search") and not self._qps_ok("list"):
            return None
        if path in ("/api/thumb", "/api/preview") and not self._qps_ok("preview"):
            return None
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
        return self._fail(404, "接口不存在", why="NO_ROUTE")

    def _route_post(self):
        if not self._host_ok():
            self.close_connection = True
            return self._send(421, b"", "text/plain; charset=utf-8")

        path, q = self._parts()
        # 安全版：所有 POST 都过 Origin/Sec-Fetch；个人版只查会改文件系统的 POST（S-13）。
        # 缺头（老 Android WebView 不发 Sec-Fetch）一律放行，见 _fetch_ok/_origin_ok。
        if (is_secure() or path in _MUTATING_POSTS) \
                and not (self._origin_ok() and self._fetch_ok()):
            self.close_connection = True
            return self._fail(403, "请求来源不被允许", why="BAD_ORIGIN")

        # ---- 版本专属路由（放在鉴权前：骨架期无论是否登录都回同一状态码）----
        if self._edition_route(path):
            return

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
        return self._fail(404, "接口不存在", why="NO_ROUTE")

