#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S-12 / S-13 / S-14 回归（Web 面加固）。

自带两个实例（本脚本自己起，安全版 + 个人版），覆盖：
  S-12 目录深度 40/33/32、个人版 40 段放行、html/svg 强制下载、MIME、预览 JSON
  S-13 Cookie Secure、Origin 校验（安全版全 POST / 个人版改文件 POST）、Host 421、响应头
  S-14 安全版首次提示条（静态 + 版本化 localStorage 键）、个人版不出现
       补充：错误回包排空有上限（401 + 4MB 慢速 -> ≤1.5s 断开）、10MB 上传正常

用法：python build/websec_check.py [run_tests 分配的端口（本脚本不用）]
"""
import http.client
import json
import os
import random
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
if APP not in sys.path:
    sys.path.insert(0, APP)
TOKEN = os.environ.get("DROPLINK_LOCAL_TOKEN") or "runtests"
PIN = "12345678"
TAG = "%06x" % random.randrange(16 ** 6)
RECV_S = os.path.join(APP, ".websec_recv_secure")
RECV_P = os.path.join(APP, ".websec_recv_personal")
CERT = os.path.join(APP, ".websec_certs")
TRUST = os.path.join(APP, ".websec_trust.json")
CTX = ssl._create_unverified_context()
PASS, FAIL = [], []
SEC = {}
PERS = {}


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (("  -> " + str(extra)) if extra else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_ready(host, port, tls, seconds=25):
    end = time.time() + seconds
    while time.time() < end:
        try:
            c = (http.client.HTTPSConnection(host, port, timeout=1, context=CTX)
                 if tls else http.client.HTTPConnection(host, port, timeout=1))
            c.request("GET", "/api/ping")
            r = c.getresponse()
            r.read()
            c.close()
            if r.status in (200, 401):
                return True
        except Exception:
            time.sleep(0.3)
    return False


def spawn(edition, port, recv, log_path, extra_env=None):
    env = dict(os.environ)
    env.pop("DROPLINK_EDITION", None)
    env.update({
        "DROPLINK_LOCAL_TOKEN": TOKEN,
        "DROPLINK_PIN": PIN,
        "DROPLINK_CERT_DIR": CERT,
        "DROPLINK_TRUST_FILE": TRUST,
        "PYTHONIOENCODING": "utf-8",
        "DROPLINK_RECV": recv,
    })
    if edition == "secure":
        env["DROPLINK_EDITION"] = "secure"
    env.update(extra_env or {})
    log = open(log_path, "wb")
    p = subprocess.Popen(
        [sys.executable, "-u", os.path.join(APP, "server.py"), "--port", str(port),
         "--pin", PIN, "--no-browser", "--recv", recv],
        cwd=APP, env=env, stdout=log, stderr=subprocess.STDOUT)
    return p, log


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def req(port, method, path, body=None, headers=None, tls=True, host="127.0.0.1", timeout=15):
    c = (http.client.HTTPSConnection(host, port, timeout=timeout, context=CTX)
         if tls else http.client.HTTPConnection(host, port, timeout=timeout))
    h = dict(headers or {})
    try:
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, data
    finally:
        try:
            c.close()
        except Exception:
            pass


def cookie_of(headers):
    sc = headers.get("set-cookie") or ""
    m = re.search(r"dlsess=([^;]+)", sc)
    return m.group(1) if m else ""


def secure_login(port):
    """登录 + 本机批准 + 拿 Cookie；返回 (cookie, set_cookie_raw)。"""
    st, h, b = req(port, "POST", "/api/login",
                   json.dumps({"pin": PIN, "device_id": "websec-dev-1",
                               "device_name": "WebSec", "platform": "android-web"}).encode(),
                   {"Content-Type": "application/json"})
    rid = (json.loads(b or b"{}").get("request_id") if st == 202 else "")
    if rid:
        req(port, "POST", "/api/trust/approve", json.dumps({"request_id": rid}).encode(),
            {"Content-Type": "application/json", "X-DL-Token": TOKEN})
        st, h, b = req(port, "GET", "/api/login/status?request_id=" + rid)
        return cookie_of(h), h.get("set-cookie") or "", st
    return "", "", st


def secure_upload(port, cookie, name, rel, data):
    st, h, b = req(port, "POST", "/api/prepare-upload",
                   json.dumps({"files": [{"name": name, "size": len(data), "rel": rel}]}).encode(),
                   {"Content-Type": "application/json", "Cookie": "dlsess=" + cookie})
    if st != 200:
        return st, json.loads(b or b"{}"), ""
    sid = json.loads(b)["session_id"]
    req(port, "POST", "/api/approve", json.dumps({"session_id": sid, "action": "accept"}).encode(),
        {"Content-Type": "application/json", "X-DL-Token": TOKEN})
    st, h, b = req(port, "GET", "/api/upload/status?session_id=" + sid,
                   headers={"Cookie": "dlsess=" + cookie})
    f = (json.loads(b or b"{}").get("files") or [{}])[0]
    q = urllib.parse.urlencode({"session_id": sid, "file_id": f.get("file_id", ""),
                                "token": f.get("token", "")})
    st, h, b = req(port, "POST", "/api/upload?" + q, data,
                   {"Cookie": "dlsess=" + cookie, "Content-Type": "application/octet-stream"})
    saved = json.loads(b or b"{}").get("saved", "") if b else ""
    return st, json.loads(b or b"{}"), saved


def personal_upload(port, name, rel, data, headers=None):
    q = urllib.parse.urlencode({"name": name, "rel": rel})
    return req(port, "POST", "/api/upload?" + q, data, headers, tls=False)


def count_tree(root):
    n = 0
    for _r, _d, fs in os.walk(root):
        n += len(fs)
    return n


for p in (RECV_S, RECV_P, CERT):
    shutil.rmtree(p, ignore_errors=True)
    os.makedirs(p, exist_ok=True)
try:
    os.unlink(TRUST)
except OSError:
    pass

sec_port = free_port()
pers_port = free_port()
LOG_S = os.path.join(tempfile.gettempdir(), "dl_websec_secure.log")
LOG_P = os.path.join(tempfile.gettempdir(), "dl_websec_personal.log")
proc_s, log_s = spawn("secure", sec_port, RECV_S, LOG_S)
proc_p, log_p = spawn("personal", pers_port, RECV_P, LOG_P)
try:
    check("安全版实例就绪", wait_ready("127.0.0.1", sec_port, True), sec_port)
    check("个人版实例就绪", wait_ready("127.0.0.1", pers_port, False), pers_port)

    # ================= S-12 =================
    print("\n[S-12.1] 目录深度（安全版 32 / 个人版不限）")
    cookie, set_cookie, _st = secure_login(sec_port)
    check("安全版已登录", bool(cookie))

    def rel_of(n):
        return "/".join("d%02d" % i for i in range(n))

    before = count_tree(RECV_S)
    st, d, _ = secure_upload(sec_port, cookie, "x.txt", rel_of(40), b"x")
    check("40 段 -> 422 INVALID_NAME", st == 422 and d.get("code") == "INVALID_NAME",
          "%s %s" % (st, d.get("code")))
    check("40 段文案带层级", "层级过深" in (d.get("error") or ""), d.get("error"))
    st, d, _ = secure_upload(sec_port, cookie, "x.txt", rel_of(33), b"x")
    check("33 段 -> 422 INVALID_NAME", st == 422 and d.get("code") == "INVALID_NAME", st)
    st, d, saved = secure_upload(sec_port, cookie, "ok32.txt", rel_of(32), b"ok")
    check("32 段 -> 200 落盘", st == 200 and os.path.isfile(saved), "%s %s" % (st, saved))
    check("32 段只多 1 个文件", count_tree(RECV_S) == before + 1,
          "%d -> %d" % (before, count_tree(RECV_S)))
    st, h, b = personal_upload(pers_port, "p40.txt", rel_of(40), b"personal")
    check("个人版 40 段放行 -> 200", st == 200, st)

    # ================= S-12.2 / 12.3 =================
    print("\n[S-12.2] 可执行内容强制下载")
    st, d, saved_html = secure_upload(sec_port, cookie, "evil.html", "", b"<script>alert(1)</script>")
    check("安全版 evil.html 上传 200", st == 200 and bool(saved_html), st)
    pth = urllib.parse.quote(saved_html)
    st, h, b = req(sec_port, "GET", "/api/download?path=" + pth, headers={"Cookie": "dlsess=" + cookie})
    check("安全版 evil.html -> attachment", st == 200 and "attachment" in (h.get("content-disposition") or ""),
          h.get("content-disposition"))
    check("安全版 evil.html -> octet-stream", (h.get("content-type") or "").startswith("application/octet-stream"),
          h.get("content-type"))
    st, d, saved_svg = secure_upload(sec_port, cookie, "evil.svg", "", b"<svg onload=alert(1)>")
    st, h, b = req(sec_port, "GET", "/api/download?path=" + urllib.parse.quote(saved_svg),
                   headers={"Cookie": "dlsess=" + cookie})
    check("安全版 evil.svg -> attachment + octet-stream",
          "attachment" in (h.get("content-disposition") or "")
          and (h.get("content-type") or "").startswith("application/octet-stream"), h.get("content-type"))

    print("\n[S-12.3] MIME 一致性与预览 JSON")
    st, h, b = req(sec_port, "GET", "/api/download?path=" + urllib.parse.quote(saved_html),
                   headers={"Cookie": "dlsess=" + cookie})
    check("下载带 nosniff", (h.get("x-content-type-options") or "").lower() == "nosniff")
    st, d, saved_txt = secure_upload(sec_port, cookie, "note.txt", "", b"hello text")
    st, h, b = req(sec_port, "GET", "/api/download?path=" + urllib.parse.quote(saved_txt),
                   headers={"Cookie": "dlsess=" + cookie})
    check(".txt 下载仍是 text/plain", (h.get("content-type") or "").startswith("text/plain"),
          h.get("content-type"))
    st, h, b = req(sec_port, "GET", "/api/preview?path=" + urllib.parse.quote(saved_html),
                   headers={"Cookie": "dlsess=" + cookie})
    try:
        pv = json.loads(b.decode("utf-8"))
    except ValueError:
        pv = {}
    check("预览返回 JSON（不回 HTML）",
          (h.get("content-type") or "").startswith("application/json") and "text" in pv,
          h.get("content-type"))

    # 个人版保持原行为（html 仍是 text/html，只是附件下载）
    st, h, b = personal_upload(pers_port, "evil.html", "", b"<script>x</script>")
    pe = json.loads(b or b"{}")
    ppath = pe.get("saved", "")
    st, h, b = req(pers_port, "GET", "/api/download?path=" + urllib.parse.quote(ppath), tls=False)
    check("个人版 evil.html 保持 text/html", (h.get("content-type") or "").startswith("text/html"),
          h.get("content-type"))
    check("个人版 evil.html 不被强制下载", "attachment" not in (h.get("content-disposition") or ""),
          h.get("content-disposition"))
    st, h, b = req(pers_port, "GET", "/api/preview?path=" + urllib.parse.quote(ppath), tls=False)
    check("个人版预览也是 JSON", (h.get("content-type") or "").startswith("application/json"))

    # ================= S-13 =================
    print("\n[S-13.1] Cookie Secure")
    check("安全版 Set-Cookie 含 Secure", "Secure" in set_cookie, set_cookie[:80])
    # P1-1：api_login（已信任设备直接登录）这条路也必须带 Secure
    st, h, b = req(sec_port, "POST", "/api/login",
                   json.dumps({"pin": PIN, "device_id": "websec-dev-1",
                               "device_name": "WebSec", "platform": "android-web"}).encode(),
                   {"Content-Type": "application/json"})
    lc = h.get("set-cookie") or ""
    check("安全版 api_login Set-Cookie 含 Secure", st == 200 and "Secure" in lc,
          "%s %s" % (st, lc[:70]))
    st, h, b = req(pers_port, "POST", "/api/login",
                   json.dumps({"pin": PIN}).encode(), {"Content-Type": "application/json"}, tls=False)
    pcookie = h.get("set-cookie") or ""
    check("个人版 Set-Cookie 不含 Secure", "Secure" not in pcookie, pcookie[:80])

    print("\n[S-13.2] Origin 校验")
    # 直接构造一次带 Origin 的 POST（不带会话也要先被 Origin 拦）
    st, h, b = req(sec_port, "POST", "/api/upload?name=evil.txt", b"x",
                   {"Origin": "http://evil.com", "Content-Type": "application/octet-stream"})
    check("安全版 evil Origin 上传 -> 403", st == 403 and json.loads(b or b"{}").get("code") == "BAD_ORIGIN",
          "%s %s" % (st, b[:80]))
    st, h, b = req(sec_port, "POST", "/api/upload?name=no-origin.txt", b"x",
                   {"Content-Type": "application/octet-stream"})
    check("安全版缺 Origin/Sec-Fetch 不被 Origin 拦",
          json.loads(b or b"{}").get("code") != "BAD_ORIGIN", "%s %s" % (st, b[:80]))
    st, h, b = personal_upload(pers_port, "evil-origin.txt", "", b"x", {"Origin": "http://evil.com"})
    check("个人版 evil Origin 上传 -> 403", st == 403, st)
    st, h, b = personal_upload(pers_port, "android.txt", "", b"x",
                               {"Origin": "http://%s:%d" % (lan_ip(), pers_port)})
    check("个人版 Android 正常 Origin -> 200", st == 200, st)
    st, h, b = personal_upload(pers_port, "android2.txt", "", b"x",
                               {"Origin": "http://127.0.0.1:%d" % pers_port})
    check("个人版 本机 Origin -> 200", st == 200, st)
    # 个人版 GET 保持宽松（带 evil Origin 也不拦）
    st, h, b = req(pers_port, "GET", "/api/session", tls=False, headers={"Origin": "http://evil.com"})
    check("个人版 GET 不被 Origin 拦", st == 200, st)

    print("\n[S-13.3] Host 头")
    st, h, b = req(sec_port, "GET", "/api/ping", headers={"Host": "evil.com"})
    check("安全版 Host: evil.com -> 421", st == 421, st)
    st, h, b = req(pers_port, "GET", "/api/ping", tls=False, headers={"Host": "evil.com"})
    check("个人版 Host: evil.com -> 421", st == 421, st)

    print("\n[S-13.4] 响应头")
    st, h, b = req(sec_port, "GET", "/")
    for k in ("x-frame-options", "referrer-policy", "x-content-type-options",
              "permissions-policy", "content-security-policy"):
        check("安全版头 %s" % k, bool(h.get(k)), h.get(k))
    st, h2, b = req(pers_port, "GET", "/", tls=False)
    for k in ("x-frame-options", "referrer-policy", "x-content-type-options"):
        check("个人版头 %s" % k, bool(h2.get(k)), h2.get(k))
    check("个人版不含 CSP", not h2.get("content-security-policy"), h2.get("content-security-policy"))

    # ================= S-14 =================
    print("\n[S-14] 安全默认值：首次提示条")
    st, h, page = req(sec_port, "GET", "/")
    body = page.decode("utf-8", "replace")
    check("首页含 secureNotice 元素", 'id="secureNotice"' in body)
    check("提示文案完整", "TLS 加密" in body and "传输前确认" in body and "设备审批" in body)
    st, h, js = req(sec_port, "GET", "/static/app.js?v=17")
    js = js.decode("utf-8", "replace")
    check("提示键带版本号", "dl_secure_notice_" in js)
    check("未登录首屏也显示提示", "showSecureNoticeIfNeeded" in js and "edition === 'secure'" in js)
    check("登录后仍只在 secure 时显示", "if (s.secure)" in js)
    st, h, b = req(sec_port, "GET", "/api/ping")
    ping = json.loads(b or b"{}")
    check("ping 公开 edition/version", ping.get("edition") == "secure" and bool(ping.get("version")),
          ping)
    st, h, b = req(pers_port, "GET", "/api/ping", tls=False)
    check("个人版 ping edition=personal", json.loads(b or b"{}").get("edition") == "personal")
    st, h, b = req(sec_port, "GET", "/api/session", headers={"Cookie": "dlsess=" + cookie})
    check("安全版 session.secure = true", json.loads(b or b"{}").get("secure") is True)
    st, h, b = req(pers_port, "GET", "/api/session", tls=False)
    check("个人版 session.secure = false", json.loads(b or b"{}").get("secure") is False)

    # ================= 补充：错误回包排空上限 =================
    print("\n[补充] 错误回包排空有上限")
    # 1) 401 + 1MB body 后正常断开：客户端要能读完 401 响应
    c = http.client.HTTPSConnection("127.0.0.1", sec_port, timeout=10, context=CTX)
    c.request("POST", "/api/upload?name=x", body=b"a" * (1024 * 1024),
              headers={"Content-Type": "application/octet-stream"})
    r = c.getresponse()
    st1 = r.status
    r.read()
    c.close()
    check("401 + 1MB body 客户端不中断", st1 == 401, st1)
    # 2) 慢速 4MB：服务器应在 ≤1.5s 内关闭连接
    s = CTX.wrap_socket(socket.create_connection(("127.0.0.1", sec_port)), server_hostname="127.0.0.1")
    s.sendall(("POST /api/upload?name=slow HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
               "Content-Length: %d\r\nConnection: close\r\n\r\n" % (sec_port, 4 * 1024 * 1024)).encode())
    t0 = time.time()
    closed_at = None
    try:
        while time.time() - t0 < 5:
            s.sendall(b"z" * 8192)
            time.sleep(0.1)
    except OSError:
        closed_at = time.time() - t0
    finally:
        try:
            s.close()
        except Exception:
            pass
    check("慢速大包 <=1.5s 被断开", closed_at is not None and closed_at <= 1.5, closed_at)
    # 3) 10MB 正常上传不受影响
    big = b"b" * (10 * 1024 * 1024)
    st, d, saved_big = secure_upload(sec_port, cookie, "big10.bin", "", big)
    check("10MB 正常上传 200", st == 200 and os.path.getsize(saved_big) == len(big), st)
finally:
    for p, lf in ((proc_s, log_s), (proc_p, log_p)):
        try:
            p.terminate()
            p.wait(timeout=8)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        try:
            lf.close()
        except Exception:
            pass
    for p in (RECV_S, RECV_P, CERT):
        shutil.rmtree(p, ignore_errors=True)
    try:
        os.unlink(TRUST)
    except OSError:
        pass
    for lp in (LOG_S, LOG_P):
        try:
            os.unlink(lp)
        except OSError:
            pass

print("\n" + "=" * 50)
print("  通过 %d，失败 %d" % (len(PASS), len(FAIL)))
print("=" * 50)
sys.exit(1 if FAIL else 0)
