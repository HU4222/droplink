# -*- coding: utf-8 -*-
"""安全评估报告（DL-01~DL-15 / X-01~X-30）修复验证。

用法：
    set DROPLINK_PIN=12345678
    set DROPLINK_LOCAL_TOKEN=testtoken123
    python build/hardening_check.py [port] [--slow]

服务需以 `--pin 12345678` 启动，且进程环境带 DROPLINK_LOCAL_TOKEN。
"""
import http.client
import json
import os
import socket
import sys
import threading
import time

PORT = 8848
SLOW = "--slow" in sys.argv
for a in sys.argv[1:]:
    if a.isdigit():
        PORT = int(a)

PIN = os.environ.get("DROPLINK_PIN", "12345678")
TOKEN = os.environ.get("DROPLINK_LOCAL_TOKEN", "testtoken123")
RECV = os.environ.get("DROPLINK_RECV") or os.path.join(os.path.expanduser("~"), "Desktop", "DropLink 接收")
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   <- " + str(detail)) if detail else ""))


def call(method, path, body=None, headers=None, host="127.0.0.1", timeout=10):
    conn = http.client.HTTPConnection(host, PORT, timeout=timeout)
    h = dict(headers or {})
    payload = body
    if isinstance(body, (dict, list)):
        payload = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    elif isinstance(body, str):
        payload = body.encode()
    try:
        conn.request(method, path, body=payload, headers=h)
        r = conn.getresponse()
        return r.status, r.read(), dict(r.getheaders())
    finally:
        conn.close()


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    finally:
        s.close()


LAN = lan_ip()

print("目标 127.0.0.1:%d   本机IP=%s" % (PORT, LAN))

# ---------------------------------------------------------------- P0：认证边界
print("\n[1] DL-01 / X-11 / X-12 / X-23：Host 与来源校验")
st, body, _ = call("GET", "/api/session", headers={"Host": "evil.example.com", "X-DL-Token": TOKEN},
                   host="127.0.0.1")
check("伪造 Host 被 421 拒绝", st == 421, st)
st, body, _ = call("GET", "/api/session", headers={"X-DL-Token": TOKEN})
check("合法 Host + 令牌可读会话", st == 200 and b"pin" in body, st)

st, body, _ = call("POST", "/api/config", {"recv_dir": RECV},
                   headers={"X-DL-Token": TOKEN, "Origin": "https://evil.example"})
check("跨站 Origin 被 403 拒绝", st == 403, st)
st, body, _ = call("POST", "/api/config", {"recv_dir": RECV},
                   headers={"X-DL-Token": TOKEN, "Origin": "null"})
check("Origin: null 被拒绝", st == 403, st)
st, body, _ = call("POST", "/api/config", {"recv_dir": RECV},
                   headers={"X-DL-Token": TOKEN, "Sec-Fetch-Site": "cross-site"})
check("Sec-Fetch-Site: cross-site 被拒绝", st == 403, st)

st, body, _ = call("GET", "/api/session")
check("无凭证读取会话返回 401", st == 401, st)
check("401 响应不含配对码/主机名", b'"pin"' not in body and b"device" not in body, body[:80])
st, body, _ = call("GET", "/api/list", host=LAN)
check("局域网未配对不能列目录", st == 401, st)

# ---------------------------------------------------------------- DL-03：会话生命周期
print("\n[2] DL-03 / X-16：会话过期与注销")
st, body, hd = call("POST", "/api/login", {"pin": PIN})
cookie = ""
for k, v in hd.items():
    if k.lower() == "set-cookie":
        cookie = v.split(";")[0]
check("正确配对码登录成功", st == 200 and cookie.startswith("dlsess="), st)
st, body, _ = call("GET", "/api/list", headers={"Cookie": cookie})
check("登录后可列目录", st == 200, st)
st, body, _ = call("POST", "/api/logout", {}, headers={"Cookie": cookie})
check("注销接口可用", st == 200, st)
st, body, _ = call("GET", "/api/list", headers={"Cookie": cookie})
check("注销后旧 cookie 失效", st == 401, st)
st, body, _ = call("POST", "/api/login", '{"pin":"bad","pin":"%s"}' % PIN,
                   headers={"Content-Type": "application/json"})
check("重复 JSON 键被拒绝", st == 400, st)
st, body, _ = call("POST", "/api/login", {"pin": 12345678})
check("数字类型配对码被拒绝", st == 400, st)

# ---------------------------------------------------------------- X-01 / X-05：路径
print("\n[3] X-01 / X-05 / DL-04：路径与敏感目录")
H = {"X-DL-Token": TOKEN}
unc = [
    "\\\\127.0.0.1\\C$\\Windows\\win.ini",
    "\\\\?\\C:\\Windows\\win.ini",
    "\\\\.\\PhysicalDrive0",
]
for p in unc:
    import urllib.parse
    st, body, _ = call("GET", "/api/download?path=" + urllib.parse.quote(p), headers=H)
    check("UNC/设备路径被拒 " + p[:24], st == 400, st)

import urllib.parse
probe = os.path.join(RECV, "_ads_probe.txt")
open(probe, "wb").write(b"normal")
try:
    open(probe + ":hidden", "w").write("SECRET-STREAM")
    st, body, _ = call("GET", "/api/download?path=" + urllib.parse.quote(probe + ":hidden"), headers=H)
    check("NTFS ADS 被拒", st == 400, st)
except OSError:
    check("NTFS ADS 被拒", True, "本机不支持 ADS，跳过")
for p in (r"C:\Windows", os.path.expanduser(r"~\AppData"), os.environ.get("LOCALAPPDATA", "")):
    if not p:
        continue
    st, body, _ = call("GET", "/api/list?path=" + urllib.parse.quote(p), headers=H)
    check("敏感目录被拒 " + p, st == 403, st)

# ---------------------------------------------------------------- X-02 / X-28：删除
print("\n[4] X-02 / X-28：删除边界")
st, body, _ = call("POST", "/api/delete", {"paths": [RECV]}, headers=H)
check("不能删除接收目录本身", st == 403, st)
st, body, _ = call("POST", "/api/delete", {"paths": ["C:\\"]}, headers=H)
check("不能删除盘根", st == 403, st)
d = os.path.join(RECV, "_hard_del")
os.makedirs(d, exist_ok=True)
f = os.path.join(d, "a.txt")
open(f, "w").write("x")
st, body, _ = call("POST", "/api/delete", {"paths": [d, f]}, headers=H)
ok = st == 200 and json.loads(body).get("count") == 1
check("父+子同时提交只算一项", ok, body[:80])

# ---------------------------------------------------------------- X-03：junction
print("\n[5] X-03：junction 不能把写入引到目录外")
jl = os.path.join(RECV, "_hard_jlink")
target = os.path.join(os.path.dirname(RECV), "_hard_jtarget")
import subprocess
if os.path.isdir(target):
    import shutil
    shutil.rmtree(target, ignore_errors=True)
os.makedirs(target, exist_ok=True)
if os.path.exists(jl):
    try:
        os.rmdir(jl)
    except OSError:
        pass
made = False
try:
    subprocess.run(["cmd", "/c", "mklink", "/J", jl, target],
                   capture_output=True, timeout=10)
    made = os.path.isdir(jl)
except Exception:
    made = False
if made:
    st, body, _ = call("POST", "/api/upload?rel=_hard_jlink&name=escape.txt", b"ESCAPED", headers=H)
    leaked = os.path.exists(os.path.join(target, "escape.txt"))
    check("junction 上传被拒且未写出目录", st == 403 and not leaked, st)
    try:
        os.rmdir(jl)
    except OSError:
        pass
else:
    check("junction 上传被拒且未写出目录", True, "当前环境无法创建 junction，跳过")

# ---------------------------------------------------------------- X-04：竞态
print("\n[6] X-04：并发同名上传不再相互覆盖")
import urllib.request
name = "_hard_race.txt"
for i in range(0, 20):
    p = os.path.join(RECV, name if i == 0 else name.replace(".txt", " (%d).txt" % i))
    try:
        os.remove(p)
    except OSError:
        pass
res = []


def up(i):
    req = urllib.request.Request(
        "http://127.0.0.1:%d/api/upload?%s" % (PORT, urllib.parse.urlencode({"name": name})),
        data=("race-%02d" % i).encode(), method="POST")
    req.add_header("X-DL-Token", TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            res.append(r.status)
    except Exception as e:
        res.append(str(e))


ths = [threading.Thread(target=up, args=(i,)) for i in range(12)]
[t.start() for t in ths]
[t.join() for t in ths]
files = [x for x in os.listdir(RECV) if x.startswith("_hard_race")]
contents = {open(os.path.join(RECV, x), "rb").read() for x in files}
check("12 并发同名上传全部成功", res.count(200) == 12, res)
check("落盘 12 个互不覆盖的文件", len(files) == 12 and len(contents) == 12, "%d 文件 / %d 内容" % (len(files), len(contents)))

# ---------------------------------------------------------------- X-06 / X-19：文件名
print("\n[7] X-06 / X-19：文件名伪装与保留名")
for nm in ("invoice\u202efdp.exe", "photo\u200b.jpg", "CON", "com1.txt"):
    st, body, _ = call("POST", "/api/upload?name=" + urllib.parse.quote(nm), b"x", headers=H)
    check("危险文件名被拒 " + nm.encode("unicode_escape").decode(), st == 400, st)

# ---------------------------------------------------------------- X-08 / X-10：协议
print("\n[8] X-08 / X-10：参数与 Range")
st, body, _ = call("GET", "/api/icon?size=abc", headers=H)
check("非法参数返回 400 且不回显异常", st == 400 and b"invalid literal" not in body, body[:80])
st, body, _ = call("GET", "/api/thumb?w=abc&path=" + urllib.parse.quote(probe), headers=H)
check("缩略图非法宽度返回 400", st == 400, st)
st, body, _ = call("GET", "/api/download?path=" + urllib.parse.quote(probe),
                   headers={**H, "Range": "bytes=5-1"})
check("倒序 Range 返回 416", st == 416, st)
st, body, _ = call("GET", "/api/download?path=" + urllib.parse.quote(probe),
                   headers={**H, "Range": "bytes=0-3"})
check("正常 Range 返回 206/4B", st == 206 and len(body) == 4, (st, len(body)))

# ---------------------------------------------------------------- DL-08：响应头
print("\n[9] DL-08：安全响应头")
st, body, hd = call("GET", "/")
check("X-Frame-Options: DENY", hd.get("X-Frame-Options") == "DENY", hd.get("X-Frame-Options"))
csp = hd.get("Content-Security-Policy") or ""
check("CSP 含 frame-ancestors 'none'", "frame-ancestors 'none'" in csp and "script-src 'self'" in csp, csp[:60])
check("Permissions-Policy 存在", bool(hd.get("Permissions-Policy")))

# ---------------------------------------------------------------- DL-07 / X-07：慢连接
if SLOW:
    print("\n[10] X-07：半开连接 30 秒被断开")
    socks = []
    for _ in range(5):
        s = socket.create_connection(("127.0.0.1", PORT), timeout=3)
        s.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n" % PORT)
        socks.append(s)
    time.sleep(32)
    closed = 0
    for s in socks:
        try:
            s.settimeout(2)
            if s.recv(1024) == b"":
                closed += 1
        except Exception:
            closed += 1
        try:
            s.close()
        except Exception:
            pass
    check("半开连接全部超时关闭", closed == 5, "%d/5" % closed)

# ---------------------------------------------------------------- 清理
for x in os.listdir(RECV):
    if x.startswith("_hard_") or x.startswith("_ads_probe"):
        p = os.path.join(RECV, x)
        try:
            if os.path.isdir(p):
                import shutil
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.remove(p)
        except OSError:
            pass

print("\n================ 结论 ================")
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败用例：")
    for f in FAIL:
        print("  - " + f)
sys.exit(1 if FAIL else 0)
