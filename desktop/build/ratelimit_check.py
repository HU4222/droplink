#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S-07 分接口限流 回归（安全版，TLS，**默认参数**）。

覆盖（不注入 DROPLINK_RL_LIST，按默认 50/50 跑）：
  1. 令牌桶单元：默认值、环境变量覆盖、突发、表上限 4096
  2. 局域网 IP 同时打 60 次 /api/list -> 至少一次 429 + Retry-After + RATE_LIMITED
  3. 持续 3 秒 300 次 -> 大量 429；同时本机 100 次 -> 全部 200（豁免）
  4. 普通节奏 20 次（间隔 0.1s）-> 全部 200，不误伤
  5. 同一 session 并发 3 个上传 -> 第三个 429

用法：python build/ratelimit_check.py <port>
"""
import json
import os
import socket
import ssl
import sys
import threading
import time
import http.client
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
RECV = os.environ.get("DROPLINK_RECV") or os.path.join(APP, ".rl_recv")
CTX = ssl._create_unverified_context()
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (("  -> " + str(extra)) if extra else ""))


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


def opener():
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=CTX),
        urllib.request.HTTPCookieProcessor(jar))
    op.jar = jar
    return op


def call(op, method, path, body=None, with_token=False, base=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    h = dict(headers or {})
    if data:
        h["Content-Type"] = "application/json"
    if with_token:
        h["X-DL-Token"] = TOKEN
    req = urllib.request.Request((base or BASE) + path, data=data, method=method, headers=h)
    try:
        with op.open(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode() or "{}"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}"), dict(e.headers or {})


print("目标 %s   接收目录 %s" % (BASE, RECV))

# ---------------- 1. 令牌桶单元 ----------------
print("\n[1] 令牌桶单元（默认 burst == rate）")
try:
    from dlcore.sec import ratelimit as rl
    check("默认 list 50/50", rl.limits("list") == (50.0, 50), rl.limits("list"))
    check("默认 preview 30/30", rl.limits("preview") == (30.0, 30), rl.limits("preview"))
    check("默认 upload 20/20", rl.limits("upload") == (20.0, 20), rl.limits("upload"))
    os.environ["DROPLINK_RL_LIST"] = "50,100"
    check("环境变量可覆盖 burst", rl.limits("list") == (50.0, 100), rl.limits("list"))
    del os.environ["DROPLINK_RL_LIST"]
    rl.reset()
    oks = [rl.allow("unit", "k1", rate=0.001, burst=3)[0] for _ in range(5)]
    check("burst=3 只放行 3 次", oks == [True, True, True, False, False], oks)
    ok, wait = rl.allow("unit", "k1", rate=0.001, burst=3)
    check("超限返回等待秒数", not ok and wait > 0, wait)
    rl.reset()
    for i in range(5000):
        rl.allow("cap", "key%d" % i, rate=1, burst=1)
    check("限流表不超过 4096", rl.table_size() <= 4096, rl.table_size())
except Exception as exc:  # noqa: BLE001
    check("令牌桶单元", False, exc)
finally:
    try:
        rl.reset()
    except Exception:
        pass

# ---------------- 登录一台局域网设备 ----------------
lan = lan_base()
phone = opener()
st, d, _ = call(phone, "POST", "/api/login",
                {"pin": PIN, "device_id": "rl-device-0001", "device_name": "限流设备",
                 "platform": "android-web"}, base=lan)
if st == 202 and d.get("request_id"):
    call(opener(), "POST", "/api/trust/approve", {"request_id": d["request_id"]},
         with_token=True)
    call(phone, "GET", "/api/login/status?request_id=" + d["request_id"], base=lan)
check("局域网设备已登录", call(phone, "GET", "/api/session", base=lan)[0] == 200)
sess = ""
for c in phone.jar:
    if c.name == "dlsess":
        sess = c.value
COOKIE = {"Cookie": "dlsess=" + sess} if sess else {}


def burst(n, path, conns=6):
    """n 个请求、conns 条 keep-alive 连接同时起跑，尽量压进极短时间窗口。"""
    per = max(1, n // conns)
    out = []
    lock = threading.Lock()
    barrier = threading.Barrier(conns)
    host = lan.split("//", 1)[1].rsplit(":", 1)[0]

    def one():
        c = http.client.HTTPSConnection(host, PORT, timeout=10, context=CTX)
        barrier.wait()
        for _ in range(per):
            try:
                c.request("GET", path)
                r = c.getresponse()
                st, b, h = r.status, r.read(), dict(r.getheaders())
            except Exception:  # noqa: BLE001
                st, b, h = 0, b"", {}
            with lock:
                out.append((st, b, h))
        try:
            c.close()
        except Exception:
            pass

    threads = [threading.Thread(target=one) for _ in range(conns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


# ---------------- 2. 局域网 60 次并发 ----------------
print("\n[2] 局域网 IP 同时打 60 次 /api/list（默认 50/50）")
# 不带 Cookie：限流在鉴权之前生效，这样每个请求最轻，突发最集中
res = burst(60, "/api/list?path=")
codes = [r[0] for r in res]
bad = next((r for r in res if r[0] == 429), None)
hdr = bad[2] if bad else {}
try:
    bad_code = json.loads((bad[1] or b"{}").decode("utf-8", "replace")).get("code") if bad else None
except ValueError:
    bad_code = None
check("至少一次 429", 429 in codes,
      "429x%d / 401x%d / %s" % (codes.count(429), codes.count(401), sorted(set(codes))))
check("429 带 Retry-After", bool(hdr.get("Retry-After")), hdr.get("Retry-After"))
check("限流响应带 RATE_LIMITED", bad_code == "RATE_LIMITED", bad_code)

# ---------------- 3. 持续 3 秒 300 次 + 本机 100 次 ----------------
print("\n[3] 持续 3 秒 300 次（局域网） + 同时本机 100 次")
stop = time.time() + 3.0
lan_codes = []
local_codes = []
sent = {"n": 0}
lock = threading.Lock()


def lan_worker():
    # 复用一条 keep-alive 连接：每秒能打几百次，才测得出「持续 300 次」
    host = lan.split("//", 1)[1].rsplit(":", 1)[0]
    conn = http.client.HTTPSConnection(host, PORT, timeout=10, context=CTX)
    while time.time() < stop:
        with lock:
            if sent["n"] >= 300:
                break
            sent["n"] += 1
        try:
            conn.request("GET", "/api/list?path=", headers=COOKIE)
            r = conn.getresponse()
            st = r.status
            r.read()
        except Exception:
            st = 0
            try:
                conn.close()
            except Exception:
                pass
            conn = http.client.HTTPSConnection(host, PORT, timeout=10, context=CTX)
        with lock:
            lan_codes.append(st)
    try:
        conn.close()
    except Exception:
        pass


def local_worker():
    conn = http.client.HTTPSConnection("127.0.0.1", PORT, timeout=10, context=CTX)
    for _ in range(10):
        try:
            conn.request("GET", "/api/list?path=", headers={"X-DL-Token": TOKEN})
            r = conn.getresponse()
            st = r.status
            r.read()
        except Exception:
            st = 0
            try:
                conn.close()
            except Exception:
                pass
            conn = http.client.HTTPSConnection("127.0.0.1", PORT, timeout=10, context=CTX)
        with lock:
            local_codes.append(st)
    try:
        conn.close()
    except Exception:
        pass


time.sleep(1.2)          # 让 [2] 消耗的桶先回一点
ths = [threading.Thread(target=lan_worker) for _ in range(30)]
ths += [threading.Thread(target=local_worker) for _ in range(10)]
for t in ths:
    t.start()
for t in ths:
    t.join()
check("300 次里大量 429", lan_codes.count(429) >= 20,
      "总 %d：429x%d / 200x%d" % (len(lan_codes), lan_codes.count(429), lan_codes.count(200)))
check("本机 100 次全部 200", len(local_codes) == 100 and all(c == 200 for c in local_codes),
      set(local_codes))

# ---------------- 4. 普通节奏 20 次 ----------------
print("\n[4] 普通节奏 20 次（间隔 0.1s，局域网）")
time.sleep(1.2)
op = opener()
codes20 = []
for _ in range(20):
    st, _d, _h = call(op, "GET", "/api/list?path=", base=lan, headers=COOKIE)
    codes20.append(st)
    time.sleep(0.1)
check("20 次全部 200", all(c == 200 for c in codes20), set(codes20))

# ---------------- 5. 同会话并发 3 个上传 ----------------
print("\n[5] 同一 session 并发 3 个上传")
phone2 = opener()
st, d, _ = call(phone2, "POST", "/api/login",
                {"pin": PIN, "device_id": "rl-device-0002", "device_name": "限流设备2",
                 "platform": "android-web"}, base=lan)
if st == 202 and d.get("request_id"):
    call(opener(), "POST", "/api/trust/approve", {"request_id": d["request_id"]},
         with_token=True)
    call(phone2, "GET", "/api/login/status?request_id=" + d["request_id"], base=lan)
st, d, _ = call(phone2, "POST", "/api/prepare-upload", {"files": [
    {"name": "rl-1.bin", "size": 100000},
    {"name": "rl-2.bin", "size": 100000},
    {"name": "rl-3.bin", "size": 100000},
]}, base=lan)
sid = d.get("session_id", "")
call(opener(), "POST", "/api/approve", {"session_id": sid, "action": "accept"}, with_token=True)
st, d, _ = call(phone2, "GET", "/api/upload/status?session_id=" + sid, base=lan)
files = d.get("files") or []
check("三个文件都拿到 token", len(files) == 3 and all(f.get("token") for f in files), len(files))

socks = []
try:
    for f in files[:2]:
        q = urllib.parse.urlencode({"session_id": sid, "file_id": f["file_id"], "token": f["token"]})
        s = CTX.wrap_socket(socket.create_connection(("127.0.0.1", PORT)), server_hostname="127.0.0.1")
        s.sendall(("POST /api/upload?" + q + " HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                   "X-DL-Token: %s\r\n"
                   "Content-Length: 100000\r\nConnection: close\r\n\r\n" % (PORT, TOKEN)).encode())
        s.sendall(b"x" * 1024)     # 慢慢写，占住 uploading 槽位
        socks.append(s)
    time.sleep(0.8)
    f3 = files[2]
    q = urllib.parse.urlencode({"session_id": sid, "file_id": f3["file_id"], "token": f3["token"]})
    req = urllib.request.Request(BASE + "/api/upload?" + q, data=b"y" * 16, method="POST",
                                 headers={"X-DL-Token": TOKEN})
    try:
        with opener().open(req, timeout=8) as r:
            st3, body = r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        st3, body = e.code, json.loads(e.read().decode() or "{}")
    check("第三个并发上传 -> 429 RATE_LIMITED",
          st3 == 429 and body.get("code") == "RATE_LIMITED", "%s %s" % (st3, body.get("code")))
finally:
    for s in socks:
        try:
            s.close()
        except Exception:
            pass
time.sleep(1.5)
parts = [n for _r, _d, fs in os.walk(RECV) for n in fs if n.endswith(".part")]
check("断开后没有 .part 残留", not parts, parts)

print("\n" + "=" * 50)
print("  通过 %d，失败 %d" % (len(PASS), len(FAIL)))
print("=" * 50)
sys.exit(1 if FAIL else 0)
