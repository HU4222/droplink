#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S-05 授权语义 + P3-3 token 归还 回归（安全版，TLS）。

覆盖：
  1. 未登录调受保护接口 -> 401 AUTH_REQUIRED
  2. 错 PIN -> 401 AUTH_BAD_PIN
  3. 非本机 IP 调审批 -> 403 LOCAL_ONLY
  4. prepare 过期 -> 410 GONE（需要服务端 DROPLINK_TRANSFER_TTL<=10，由 run_tests 注入）
  5. 已接受的会话再 approve -> 409 STATE_CONFLICT
  6. 伪造 token -> 403 TOKEN_INVALID
  7. 尺寸不符 -> 422 SIZE_MISMATCH + 新 token 可用、旧 token 作废（P3-3）
  8. 个人版同路径 -> 仍 401/404/400，且没有 code 字段
  9. P3-1 单测：uploading 中的会话不参与 prune

用法：python build/status_check.py <port>
"""
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
if APP not in sys.path:
    sys.path.insert(0, APP)
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8848
BASE = "https://127.0.0.1:%d" % PORT
TOKEN = os.environ.get("DROPLINK_LOCAL_TOKEN") or "runtests"
PIN = os.environ.get("DROPLINK_PIN") or "12345678"
RECV = os.environ.get("DROPLINK_RECV") or os.path.join(APP, ".status_recv")
TTL = float(os.environ.get("DROPLINK_TRANSFER_TTL") or 0)
CTX = ssl._create_unverified_context()
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (("  -> " + str(extra)) if extra else ""))


def opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=CTX),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


phone = opener()
local = opener()


def call(op, method, path, body=None, with_token=False, base=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {}
    if data:
        headers["Content-Type"] = "application/json"
    if with_token:
        headers["X-DL-Token"] = TOKEN
    req = urllib.request.Request((base or BASE) + path, data=data, method=method, headers=headers)
    try:
        with op.open(req, timeout=10) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw), dict(r.headers)
            except ValueError:
                return r.status, {"raw": raw}, dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw), dict(e.headers or {})
        except ValueError:
            return e.code, {"raw": raw}, dict(e.headers or {})


def raw_upload(op, sid, fid, token, payload):
    q = urllib.parse.urlencode({"session_id": sid, "file_id": fid, "token": token})
    req = urllib.request.Request(BASE + "/api/upload?" + q, data=payload, method="POST",
                                 headers={"Content-Type": "application/octet-stream"})
    try:
        with op.open(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def lan_base():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return "https://%s:%d" % (ip, PORT)


def login_device(device_id, name="状态机手机"):
    st, d, _ = call(phone, "POST", "/api/login",
                    {"pin": PIN, "device_id": device_id, "device_name": name, "platform": "ios"})
    if st == 202 and d.get("request_id"):
        call(local, "POST", "/api/trust/approve", {"request_id": d["request_id"]}, with_token=True)
        call(phone, "GET", "/api/login/status?request_id=" + d["request_id"])
    return st, d


def prepare(files):
    return call(phone, "POST", "/api/prepare-upload", {"files": files})


print("目标 %s   接收目录 %s" % (BASE, RECV))

# ---------------- P3-1 单测 ----------------
print("\n[0] P3-1 单元：uploading 中的会话不参与 prune")
try:
    from dlcore.sec import approval as _ap
    now = time.time()
    rec = {"expires_at": now - 1000, "files": [{"status": "uploading"}]}
    check("uploading 会话不可 prune", _ap._prunable(rec, now) is False)
    rec["files"][0]["status"] = "done"
    check("完成后 + 墓碑过期可 prune", _ap._prunable(rec, now) is True)
    rec2 = {"expires_at": now + 100, "files": [{"status": "done"}]}
    check("未过墓碑不 prune", _ap._prunable(rec2, now) is False)
except Exception as exc:  # noqa: BLE001
    check("P3-1 单元", False, exc)

# ---------------- 429 的 Retry-After 头 ----------------
print("\n[0b] 429 的 Retry-After 头（_fail 单元）")
try:
    from dlcore.httpd import Handler

    class _Fake:
        close_connection = False

        def __init__(self):
            self.last = None

        def _json(self, obj, code=200, extra=None):
            self.last = (code, obj, dict(extra or {}))

    fake = _Fake()
    Handler._fail(fake, 429, "请求过于频繁", why="RATE_LIMITED", retry_after=30)
    code, obj, extra = fake.last
    check("_fail(429, retry_after=30) -> Retry-After: 30",
          code == 429 and extra.get("Retry-After") == "30", (code, extra))
except Exception as exc:  # noqa: BLE001
    check("429 Retry-After 单元", False, exc)

# ---------------- 1. 未登录 ----------------
print("\n[1] 未登录调受保护接口")
for path in ("/api/session", "/api/list?path=", "/api/roots", "/api/transfers", "/api/clip"):
    st, d, _ = call(phone, "GET", path)
    check("GET %s -> 401 AUTH_REQUIRED" % path,
          st == 401 and d.get("code") == "AUTH_REQUIRED", "%s %s" % (st, d.get("code")))

# ---------------- 2. 错 PIN ----------------
print("\n[2] 配对码错误")
st, d, _ = call(phone, "POST", "/api/login",
                {"pin": "00000000", "device_id": "status-badpin-01", "device_name": "错码"})
check("错 PIN -> 401 AUTH_BAD_PIN", st == 401 and d.get("code") == "AUTH_BAD_PIN",
      "%s %s" % (st, d.get("code")))

# ---------------- 3. 非本机审批 ----------------
print("\n[3] 非本机 IP 调审批")
st, d, _ = call(phone, "POST", "/api/approve", {"session_id": "x", "action": "accept"},
                base=lan_base())
check("LAN approve -> 403 LOCAL_ONLY", st == 403 and d.get("code") == "LOCAL_ONLY",
      "%s %s" % (st, d.get("code")))

# ---------------- 登录一台设备 ----------------
print("\n[4] 登录并批准一台测试设备")
st, d = login_device("status-device-0001")
check("设备登录并批准", call(phone, "GET", "/api/session")[0] == 200)

# ---------------- 5. 过期 -> 410 ----------------
print("\n[5] prepare 过期 -> 410 GONE")
if TTL and TTL <= 10:
    st, d, _ = prepare([{"name": "expire.txt", "size": 3}])
    sid = d.get("session_id", "")
    time.sleep(TTL + 0.8)
    st, d, _ = call(phone, "GET", "/api/upload/status?session_id=" + sid)
    check("过期 status -> 410 GONE", st == 410 and d.get("code") == "GONE",
          "%s %s" % (st, d.get("code")))
    st, d, _ = call(local, "POST", "/api/approve", {"session_id": sid, "action": "accept"},
                    with_token=True)
    check("过期后 approve -> 410 GONE", st == 410 and d.get("code") == "GONE",
          "%s %s" % (st, d.get("code")))
else:
    print("  skip  过期用例（服务端 DROPLINK_TRANSFER_TTL=%r，需 <=10）" % TTL)

# ---------------- 6. 状态冲突 409 ----------------
print("\n[6] 再 approve 已接受的会话 -> 409 STATE_CONFLICT")
st, d, _ = prepare([{"name": "conflict.txt", "size": 4}])
sid = d.get("session_id", "")
st, d, _ = call(local, "POST", "/api/approve", {"session_id": sid, "action": "accept"},
                with_token=True)
check("首次 accept 200", st == 200, st)
st, d, _ = call(local, "POST", "/api/approve", {"session_id": sid, "action": "accept"},
                with_token=True)
check("再次 accept -> 409 STATE_CONFLICT", st == 409 and d.get("code") == "STATE_CONFLICT",
      "%s %s" % (st, d.get("code")))

# ---------------- 7. 伪造 token ----------------
print("\n[7] 伪造 token 上传 -> 403 TOKEN_INVALID")
st, d, _ = call(phone, "GET", "/api/upload/status?session_id=" + sid)
f = (d.get("files") or [{}])[0]
st, d = raw_upload(phone, sid, f.get("file_id", ""), "0" * 32, b"abcd")
check("伪造 token -> 403 TOKEN_INVALID", st == 403 and d.get("code") == "TOKEN_INVALID",
      "%s %s" % (st, d.get("code")))

# ---------------- 8. 尺寸不符 + 换发 token（P3-3） ----------------
print("\n[8] 尺寸不符 -> 422 + 换发新 token，旧 token 作废")
st, d, _ = prepare([{"name": "size.txt", "size": 10}])
sid = d.get("session_id", "")
call(local, "POST", "/api/approve", {"session_id": sid, "action": "accept"}, with_token=True)
st, d, _ = call(phone, "GET", "/api/upload/status?session_id=" + sid)
f = (d.get("files") or [{}])[0]
tok1 = f.get("token", "")
st, d = raw_upload(phone, sid, f.get("file_id", ""), tok1, b"12345")   # CL=5 != 10
tok2 = d.get("token", "")
check("错误长度 -> 422 SIZE_MISMATCH", st == 422 and d.get("code") == "SIZE_MISMATCH",
      "%s %s" % (st, d.get("code")))
check("422 带回新 token 且不等于旧 token", bool(tok2) and tok2 != tok1)
parts = [n for _r, _d, fs in os.walk(RECV) for n in fs if n.endswith(".part")]
check("没有 .part 残留", not parts, parts)
st, d = raw_upload(phone, sid, f.get("file_id", ""), tok1, b"0123456789")
check("旧 token -> 403 TOKEN_INVALID", st == 403 and d.get("code") == "TOKEN_INVALID",
      "%s %s" % (st, d.get("code")))
st, d = raw_upload(phone, sid, f.get("file_id", ""), tok2, b"0123456789")
check("新 token 上传 -> 200", st == 200, st)
st, d = raw_upload(phone, sid, f.get("file_id", ""), tok2, b"0123456789")
check("新 token 二次使用 -> 403 TOKEN_INVALID", st == 403 and d.get("code") == "TOKEN_INVALID",
      "%s %s" % (st, d.get("code")))

# ---------------- 9. 个人版：状态码与错误体不变 ----------------
print("\n[9] 个人版同路径：语义不变、没有 code 字段")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_http(port, seconds=20):
    end = time.time() + seconds
    while time.time() < end:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % port, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


pport = free_port()
# 注意：不用 tempfile.mkdtemp —— 本环境下它建出来的目录会带异常 ACL，
# 子进程写不进去；用 os.makedirs 建在工程目录下，跑完删掉。
precv = os.path.join(APP, ".status_personal_recv")
shutil.rmtree(precv, ignore_errors=True)
os.makedirs(precv, exist_ok=True)
env = dict(os.environ)
env.pop("DROPLINK_EDITION", None)
env["DROPLINK_LOCAL_TOKEN"] = TOKEN
proc = subprocess.Popen(
    [sys.executable, "-u", os.path.join(APP, "server.py"), "--port", str(pport),
     "--pin", "12345678", "--no-browser", "--recv", precv],
    cwd=APP, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    ready = wait_http(pport)
    check("个人版实例就绪", ready, pport)
    if ready:
        pop = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                          urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        pbase = "http://127.0.0.1:%d" % pport

        def pcall(method, path, body=None, raw=None):
            data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
            headers = {}
            if raw is None and data is not None:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(pbase + path, data=data, method=method, headers=headers)
            try:
                with pop.open(req, timeout=8) as r:
                    return r.status, json.loads(r.read().decode() or "{}")
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read().decode() or "{}")

        st, d = pcall("POST", "/api/login", {"pin": "00000000"})
        check("个人版错 PIN -> 403 且无 code", st == 403 and "code" not in d, "%s %s" % (st, d))
        st, d = pcall("GET", "/api/prepare-upload")
        check("个人版 /api/prepare-upload -> 404 且无 code", st == 404 and "code" not in d,
              "%s %s" % (st, d))
        st, d = pcall("POST", "/api/upload?name=plain.txt", raw=b"personal-body")
        check("个人版 upload 无 token -> 200", st == 200, "%s %s" % (st, d))
finally:
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    shutil.rmtree(precv, ignore_errors=True)

print("\n" + "=" * 50)
print("  通过 %d，失败 %d" % (len(PASS), len(FAIL)))
print("=" * 50)
sys.exit(1 if FAIL else 0)
