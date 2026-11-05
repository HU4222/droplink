#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DropLink 互传 —— 局域网直连文件互传（服务端入口）

1.4.1 起本文件只做三件事：解析命令行、组装模块、启动服务。
具体实现在 dlcore/ 下：

  config.py   常量与运行模式开关（--secure）
  state.py    运行期状态（会话/剪贴板/传输记录/配置）
  fsutil.py   安全基线与文件系统工具（路径校验、命名、回收站）
  winenv.py   Windows 环境与网络（管理员、防火墙、网卡、设备识别）
  media.py    二维码、缩略图、应用图标
  api.py      /api/* 的实现（ApiMixin）
  httpd.py    HTTP 传输层（Server / Handler / 路由）

依赖仍只有标准库。
"""

from __future__ import annotations

import argparse
import ctypes
import gzip
import hashlib
import html
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

from dlcore import config
from dlcore.config import *          # noqa: F401,F403  （保持 server.APP_NAME / VERSION 可用）
from dlcore.logsafe import log, mask, redact_ip, redact_path
from dlcore.state import STATE       # noqa: F401
from dlcore.fsutil import *          # noqa: F401,F403
from dlcore.media import *           # noqa: F401,F403
from dlcore.winenv import *          # noqa: F401,F403
from dlcore.httpd import Server, Handler   # noqa: F401
from dlcore.api import ApiMixin            # noqa: F401

# 容错加载结果：上面这些 import 即使某个模块缺失/损坏也一定会成功，
# 具体哪个降级了看这里（1.4.2）。
from dlcore import STATUS as MODULE_STATUS          # noqa: F401
from dlcore import degraded_names, describe, health_line, ok as module_ok  # noqa: F401


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------

def open_app_window(url: str) -> bool:
    """优先用 Edge / Chrome 的 app 模式打开，看起来像一个独立应用窗口。"""
    bases = [
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    rels = [
        r"Microsoft\Edge\Application\msedge.exe",
        r"Google\Chrome\Application\chrome.exe",
    ]
    for base in bases:
        if not base:
            continue
        for rel in rels:
            exe = os.path.join(base, rel)
            if not os.path.isfile(exe):
                continue
            try:
                subprocess.Popen(
                    [exe, f"--app={url}", "--window-size=1200,880", "--no-first-run"],
                    creationflags=0x00000008 | CREATE_NO_WINDOW,  # DETACHED_PROCESS | 不弹控制台
                    close_fds=True,
                )
                return True
            except Exception:
                continue
    try:
        webbrowser.open(url)
        return True
    except Exception:
        return False


def _redact_url(u: str) -> str:
    """日志/非交互输出里的 URL：把主机换成打码后的形式。"""
    try:
        p = urllib.parse.urlsplit(u)
        host = p.hostname or ""
        netloc = redact_ip(host)
        if p.port:
            netloc += ":%d" % p.port
        return urllib.parse.urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))
    except Exception:
        return redact_path(u)


def print_banner(port: int, urls, cert=None):
    line = "=" * 62
    scheme = "https" if cert else "http"
    # 人看的横幅在终端里显示完整信息；输出被重定向到日志/管道时自动脱敏（S-10）。
    try:
        human = bool(sys.stdout and sys.stdout.isatty())
    except Exception:
        human = False
    pin_show = STATE.pin if human else mask(STATE.pin)
    recv_show = STATE.recv_dir if human else redact_path(STATE.recv_dir)
    urls_show = urls if human else [_redact_url(u) for u in urls]
    print()
    print(line)
    print(f"  {APP_NAME}  v{VERSION}   ·   同一个 Wi-Fi，发送端与接收端直连互传")
    if cert:
        print("  安全版：TLS 已启用（自签证书，浏览器首次打开会提示，确认后继续）")
    print(line)
    print(f"  接收端本机：  {scheme}://127.0.0.1:{port}")
    if urls_show:
        print(f"  发送端打开：  {urls_show[0]}")
        for extra in urls_show[1:]:
            print(f"              {extra}")
    else:
        print("  发送端打开：  未检测到局域网地址（请先连接 Wi-Fi 或开热点）")
    print(f"  配对码  ：  {pin_show}      （发送端首次打开需输入）")
    if cert:
        print(f"  证书指纹：  {cert['fingerprint_short']}      （首次连接请在浏览器里核对）")
    print(f"  接收目录：  {recv_show}")
    print(line)
    print("  提示：发送端连同一个 Wi-Fi / 接收端热点即可，不经过互联网，不耗移动流量。")
    print("        发送端浏览器菜单里「添加到主屏幕」，可当 App 用。")
    print("        关闭此窗口或按 Ctrl+C 即停止服务。")
    print(line)
    if urls:
        art = qr_ascii(urls[0])
        if art:
            print()
            print("  发送端可以直接扫这个二维码连接：")
            print()
            for ln in art:
                print("    " + ln)
    print()


def qr_ascii(text: str):
    """把二维码渲染成终端里的字符画（半块字符，两行合并成一行）。"""
    try:
        import qrmini
        grid = qrmini.qr_matrix(text, border=2)
    except Exception:
        return None
    try:
        enc = getattr(sys.stdout, "encoding", "") or ""
        if "utf" not in enc.lower():
            return None
    except Exception:
        return None
    # 每两个模块合成一个字符行，保持方块比例
    pad = [False] * len(grid[0])
    rows = list(grid) + ([pad] if len(grid) % 2 else [])
    out = []
    for i in range(0, len(rows), 2):
        top, bot = rows[i], rows[i + 1]
        line = []
        for x in range(len(top)):
            a, b = bool(top[x]), bool(bot[x])
            line.append("█" if (a and b) else "▀" if a else "▄" if b else " ")
        out.append("".join(line))
    return out


# --------------------------------------------------------------------------
# 降级模式（1.4.2）
# --------------------------------------------------------------------------

def print_degraded_report() -> None:
    """有模块没加载起来时，在控制台把「哪个、为什么」说清楚。"""
    bad = degraded_names()
    if not bad:
        return
    line = "!" * 62
    print()
    print(line)
    print("  降级运行：%d 个模块不可用，相关功能会失效，其余功能照常" % len(bad))
    print(line)
    for item in describe():
        if item["state"] == "ok":
            continue
        label = "文件缺失" if item["state"] == "missing" else "加载失败"
        print("  dlcore/%-11s %s" % (item["name"] + ".py", label))
        if item["error"]:
            print("       %s" % item["error"])
    print("-" * 62)
    print("  修复办法：重新安装或从官方包恢复 dlcore/ 目录，然后重启。")
    print(line)


def print_refuse_degraded() -> None:
    """安全版：模块损坏时拒绝启动，并说清是哪个模块、怎么修（P0-1）。"""
    print()
    line = "=" * 62
    print(line)
    print("  安全版无法在模块损坏时降级运行，已停止启动。")
    print("  安全版的承诺是全程 TLS；降级诊断服务是明文 HTTP，不能作为退路。")
    print("-" * 62)
    for item in describe():
        if item["state"] == "ok":
            continue
        label = "文件缺失" if item["state"] == "missing" else "加载失败"
        print("  dlcore/%-11s %s  %s" % (item["name"] + ".py", label, item["error"] or "-"))
    print("  修复办法：重新安装或从官方包恢复 dlcore/ 目录，然后重启。")
    print(line)
    print()


def degraded_page() -> bytes:
    """降级诊断页：不依赖 web/ 目录，纯内联 HTML。"""
    rows = []
    for item in describe():
        good = item["state"] == "ok"
        color = "#16a34a" if good else "#dc2626"
        label = {"ok": "正常", "missing": "文件缺失", "broken": "加载失败"}.get(
            item["state"], item["state"])
        rows.append(
            '<tr><td class="m">dlcore/%s.py</td>'
            '<td style="color:%s;font-weight:600">%s</td>'
            '<td class="e">%s</td></tr>'
            % (html.escape(item["name"]), color, html.escape(label),
               html.escape(item["error"] or "—")))
    body = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>%s · 降级模式</title><style>"
        "body{margin:0;padding:28px 18px;background:#0f172a;color:#e2e8f0;"
        "font:14px/1.7 -apple-system,'Segoe UI',system-ui,sans-serif}"
        ".card{max-width:760px;margin:0 auto;background:#1e293b;border:1px solid #334155;"
        "border-radius:16px;padding:22px 22px 8px}"
        "h1{font-size:17px;margin:0 0 6px}p.sub{color:#94a3b8;margin:0 0 18px;font-size:13px}"
        "table{width:100%%;border-collapse:collapse;font-size:13px}"
        "td{padding:7px 6px;border-top:1px solid #334155;vertical-align:top}"
        "td.m{width:150px;color:#93c5fd;font-family:ui-monospace,Consolas,monospace}"
        "td.e{color:#94a3b8;word-break:break-all}"
        ".tip{margin:16px 0 6px;padding:10px 12px;background:#0b1220;border-radius:10px;"
        "color:#94a3b8;font-size:12.5px}"
        "</style></head><body><div class=\"card\">"
        "<h1>%s 处于降级模式</h1>"
        '<p class="sub">HTTP 传输模块（dlcore/httpd.py）不可用，这里只提供诊断信息，'
        "不能传文件。恢复该文件后重启即可。</p>"
        "<table>%s</table>"
        '<div class="tip">命令行可执行 <code>--selfcheck</code> 查看同样的信息；'
        "模块正常时退出码 0，有降级为 1。</div>"
        "</div></body></html>"
    ) % (APP_NAME, APP_NAME, "".join(rows))
    return body.encode("utf-8")


def start_degraded(port: int = 8848):
    """httpd 模块不可用时的最小服务：只回诊断页与 503，不碰文件系统。

    安全版不允许走到这里：降级服务是明文 HTTP，会让「TLS 不可关」的承诺失效。
    """
    if config.is_secure_edition():
        raise RuntimeError("安全版不允许降级为明文诊断服务")
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    page = degraded_page()
    payload = json.dumps({"ok": False, "degraded": True,
                          "modules": describe()}, ensure_ascii=False).encode("utf-8")

    class _Handler(BaseHTTPRequestHandler):
        server_version = "DropLink-degraded"

        def _send(self, code, body, ctype):
            if isinstance(body, str):
                body = body.encode("utf-8")
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-DropLink-Degraded", "1")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path.startswith("/api/"):
                return self._send(503, payload, "application/json; charset=utf-8")
            if path in ("/", "/index.html", "/degraded"):
                return self._send(200, page, "text/html; charset=utf-8")
            return self._send(404, "404", "text/plain; charset=utf-8")

        do_HEAD = do_GET

        def do_POST(self):
            self.do_GET()

        def log_message(self, fmt, *args):
            return None

    httpd = None
    for offset in range(0, 20):
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", port + offset), _Handler)
            port = port + offset
            break
        except OSError:
            continue
    if httpd is None:
        raise RuntimeError("端口被占用，无法启动")
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.3},
                     daemon=True).start()
    return {"httpd": httpd, "port": port, "urls": lan_urls(port),
            "pin": "-", "local_token": "-", "recv_dir": "-", "degraded": True}


def start_server(port: int = 8848, pin: str = "", recv: str = "",
                 background: bool = True):
    """启动服务。background=True 时在工作线程里跑，立刻返回运行信息。"""
    if pin:
        lo, hi = (8, 12) if config.is_secure() else (4, 8)
        if not (pin.isdigit() and lo <= len(pin) <= hi):
            raise RuntimeError("配对码必须是 %d–%d 位数字" % (lo, hi))
        STATE.pin = pin
    if recv:
        target = os.path.abspath(recv)
        if config.is_secure_edition():
            # 安全版：用户显式给了 --recv，就必须「要么用它，要么拒绝启动」。
            # 先判 UNC/网络路径（否则 makedirs 只会抛 WinError 5，看不出原因），
            # 建不出来也直接拒绝，绝不回落到上一次的目录（P0-1 fail-open）。
            from dlcore.sec import pathguard
            try:
                pathguard.check_recv_dir(target)
            except ValueError as exc:
                raise RuntimeError("安全版拒绝启动：%s" % exc)
            try:
                os.makedirs(target, exist_ok=True)
            except OSError as exc:
                raise RuntimeError("安全版拒绝启动：接收目录不可用（%s）" % exc)
        else:
            try:
                os.makedirs(target, exist_ok=True)
            except OSError as exc:
                print(f"接收目录不可用：{exc}")
                target = ""
        if target:
            STATE.recv_dir = target
            STATE.config["recv_dir"] = target
            STATE.save()

    # 启动时清理推送暂存，避免历史副本长期占磁盘
    try:
        n, freed = purge_send_cache()
        if n:
            log(f"[清理] 推送暂存移除 {n} 个文件，释放 {freed / 1048576:.1f} MB")
    except Exception:
        pass

    # 清掉上次中断留下的临时文件（超过 24 小时的 .dl_*.part），之后每小时扫一次（S-11）
    def _sweep_parts():
        try:
            n, freed = sweep_parts(STATE.recv_dir, 86400)
            if n:
                print("[清理] 过期 .part 移除 %d 个，释放 %.1f MB" % (n, freed / 1048576))
        except Exception:
            pass

    threading.Thread(target=_sweep_parts, daemon=True).start()

    def _parts_janitor():
        while True:
            time.sleep(3600)
            _sweep_parts()

    threading.Thread(target=_parts_janitor, daemon=True).start()

    # 安全版：先把证书备好再监听；拿不到证书就拒绝启动（不允许悄悄退回明文）。
    tls_ctx = None
    cert_info = None
    if config.is_secure_edition():
        try:
            from dlcore.sec import pathguard
            # 接收目录本身是 UNC / 网络路径时直接拒绝（S-06，不等上传才炸）
            pathguard.check_recv_dir(STATE.recv_dir)
        except ValueError as exc:
            raise RuntimeError("安全版拒绝启动：%s" % exc)
        try:
            from dlcore.sec import certgen, tls
            cert_info = certgen.ensure_cert()          # 首次会打印「正在生成证书…」
            tls_ctx = tls.make_context(cert_info["cert_path"], cert_info["key_path"])
            # Host 与证书 SAN 不一致时只记日志、不拒绝（S-13）
            STATE.cert_sans = {str(s).lower() for s in (cert_info.get("sans") or [])}
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("安全版 TLS 初始化失败，已停止启动：%s" % exc)

    scheme = "https" if tls_ctx is not None else "http"
    httpd = None
    for offset in range(0, 20):
        try:
            httpd = Server(("0.0.0.0", port + offset), Handler,
                           context=tls_ctx, scheme=scheme)
            port = port + offset
            break
        except OSError:
            continue
    if httpd is None:
        raise RuntimeError("端口被占用，无法启动")

    httpd.daemon_threads = True
    urls = lan_urls(port, scheme)
    info = {
        "httpd": httpd,
        "port": port,
        "urls": urls,
        "pin": STATE.pin,
        "local_token": LOCAL_TOKEN,
        "recv_dir": STATE.recv_dir,
        "scheme": scheme,
        "local_url": "%s://127.0.0.1:%d" % (scheme, port),
        "cert": cert_info,
    }
    # 定期清理过期会话/失败记录，避免长期运行内存增长（DL-09）
    def _janitor():
        while True:
            time.sleep(300)
            try:
                STATE.cleanup()
            except Exception:
                pass
    threading.Thread(target=_janitor, daemon=True).start()
    if background:
        threading.Thread(target=httpd.serve_forever,
                         kwargs={"poll_interval": 0.3}, daemon=True).start()
    return info


def main():
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--port", type=int, default=8848, help="监听端口，默认 8848")
    ap.add_argument("--pin", default="", help="固定配对码（默认每次随机）")
    ap.add_argument("--recv", default="", help="指定接收目录")
    ap.add_argument("--no-browser", action="store_true", help="启动时不自动打开浏览器")
    ap.add_argument("--version", "-V", action="version",
                    version="%s %s · HU4222" % (APP_NAME, VERSION),
                    help="输出版本号与作者后退出")
    ap.add_argument("--secure", action="store_true",
                    help="启用安全加固（Host/Origin 校验、本机令牌、敏感目录限制、8 位配对码）")
    ap.add_argument("--selfcheck", action="store_true",
                    help="只检查 dlcore 模块完整性并退出（0=全部正常，1=有降级）")
    args = ap.parse_args()
    if args.secure:
        config.set_secure(True)

    if args.selfcheck:
        # 自检也报版本身份：同一份代码两个产物，排障时先确认「这是哪一版」
        print("版本身份：edition=%s（%s）  v%s" % (
            config.EDITION,
            "安全版" if config.is_secure_edition() else "个人版",
            VERSION))
        print(health_line())
        for item in describe():
            mark = "OK  " if item["state"] == "ok" else "降级"
            print("  %-2s dlcore/%-9s %-8s %s" % (
                mark, item["name"] + ".py", item["state"], item["error"] or "-"))
        return 0 if not degraded_names() else 1

    # 安全版：模块不完整就直接拒绝启动，不要先打印「降级运行…其余功能照常」
    # 那句会让人以为还在跑（P3-2）。
    if not module_ok("httpd") and config.is_secure_edition():
        print_refuse_degraded()
        return 2

    print_degraded_report()

    # httpd 起不来时退回内置最小服务：至少能打开诊断页，而不是端口全无声。
    if not module_ok("httpd"):
        try:
            info = start_degraded(args.port)
        except RuntimeError as exc:
            print(exc)
            return 1
        print()
        print("  %s v%s  ·  降级模式（HTTP 模块不可用）" % (APP_NAME, VERSION))
        print("  接收端本机：  http://127.0.0.1:%d" % info["port"])
        print("  此页面只显示模块状态，不能传文件。")
        print()
        if not args.no_browser:
            webbrowser.open("http://127.0.0.1:%d" % info["port"])
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n已停止。")
        finally:
            info["httpd"].shutdown()
            info["httpd"].server_close()
        return 0

    try:
        info = start_server(args.port, args.pin, args.recv, background=True)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)   # 启动失败写 stderr（脚本/日志都能抓到）
        return 1

    port, urls = info["port"], info["urls"]
    print_banner(port, urls, info.get("cert"))

    if not args.no_browser:
        def _open():
            time.sleep(0.8)
            open_app_window(f"{info['local_url']}/?k={LOCAL_TOKEN}")
        threading.Thread(target=_open, daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        info["httpd"].shutdown()
        info["httpd"].server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
