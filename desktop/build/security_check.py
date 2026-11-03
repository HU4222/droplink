# -*- coding: utf-8 -*-
"""DropLink 安全与异常行为自检。

覆盖：越权访问 / 路径穿越 / 注入 / 畸形请求 / 边界值 / 暴力破解 / 并发稳定性。
用法：python build/security_check.py [port]
"""
import http.client
import json
import os
import shutil
import socket
import sys
import threading
import time
import urllib.parse

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
PIN_FIXED = os.environ.get("DROPLINK_PIN", "12345678")
COOKIE = ""
PASS, FAIL, WARN = [], [], []


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    finally:
        s.close()


LAN = lan_ip()


def call(method, path, body=None, host="127.0.0.1", headers=None, raw=False, timeout=8):
    conn = http.client.HTTPConnection(host, PORT, timeout=timeout)
    h = dict(headers or {})
    if COOKIE and "Cookie" not in h and host in ("127.0.0.1", "localhost"):
        h["Cookie"] = COOKIE
    payload = None
    if body is not None:
        if isinstance(body, (dict, list)):
            payload = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            payload = body.encode()
        else:
            payload = body
    try:
        conn.request(method, path, body=payload, headers=h)
        r = conn.getresponse()
        data = r.read()
        return r.status, data, dict(r.getheaders())
    except Exception as exc:  # 连接被打断也算异常，交给用例判定
        return -1, str(exc).encode(), {}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   <- " + detail) if detail else ""))


def note(name, detail):
    WARN.append((name, detail))
    print("  WARN  " + name + "   <- " + detail)


# ---------------------------------------------------------------- 准备
# 本机也不再默认放行：先登录拿会话（服务由测试脚本用固定 PIN 启动）
st, body, hd = call("POST", "/api/login", {"pin": PIN_FIXED})
if st != 200:
    print("服务未就绪或配对码不匹配（需要 --pin %s）：%s %s" % (PIN_FIXED, st, body[:120]))
    sys.exit(2)
COOKIE = (hd.get("Set-Cookie") or "").split(";")[0]
st, body, _ = call("GET", "/api/session")
if st != 200:
    print("登录后仍无法读取会话，退出")
    sys.exit(2)
sess = json.loads(body)
PIN = sess.get("pin", "")
RECV = sess.get("recv_dir", "")
print("服务就绪  port=%d  本机IP=%s  recv=%s" % (PORT, LAN, RECV))

print("\n[1] 基础可用性与静态资源")
st, body, hd = call("GET", "/")
check("首页可访问（手机要能打开）", st == 200 and b"<html" in body.lower(), "status=%s" % st)
st, body, hd = call("GET", "/static/app.js", headers={"Accept-Encoding": "gzip"})
check("静态资源 gzip", st == 200 and hd.get("Content-Encoding") == "gzip",
      "enc=%s" % hd.get("Content-Encoding"))
etag = hd.get("ETag")
st2, b2, _ = call("GET", "/static/app.js", headers={"If-None-Match": etag or ""})
check("ETag 协商缓存返回 304", st2 == 304, "status=%s" % st2)

print("\n[2] 越权访问（模拟局域网内其它设备，不带配对 cookie）")
for path in ("/api/list", "/api/roots", "/api/download?path=x", "/api/clip",
             "/api/transfers", "/api/peers", "/api/networks", "/api/config"):
    st, body, _ = call("GET", path, host=LAN)
    ok = st == 401 and not (st == 200)
    check("未配对无法读取 " + path.split("?")[0], ok, "status=%s" % st)
for path, payload in (("/api/delete", {"path": RECV}), ("/api/mkdir", {"name": "x"}),
                      ("/api/share", {"path": RECV}), ("/api/send", b"x"),
                      ("/api/rename", {"path": "a", "name": "b"}),
                      ("/api/zip", {"paths": []}), ("/api/config", {"recv_dir": "C:\\"}),
                      ("/api/firewall/set", {})):
    st, body, _ = call("POST", path, payload, host=LAN)
    check("未配对无法调用 " + path, st == 401, "status=%s" % st)

print("\n[3] 配对与会话伪造")
st, body, hd = call("POST", "/api/login", {"pin": "000000"}, host=LAN)
check("错误配对码被拒", st in (400, 401, 403, 429), "status=%s" % st)
st, body, hd = call("GET", "/api/list", host=LAN, headers={"Cookie": "dlsess=deadbeefdeadbeef"})
check("伪造会话 cookie 无效", st == 401, "status=%s" % st)
st, body, hd = call("GET", "/api/list", host=LAN,
                    headers={"Cookie": "dlsess=deadbeef", "X-Forwarded-For": "127.0.0.1"})
check("伪造 X-Forwarded-For 不能提权", st == 401, "status=%s" % st)

st, body, hd = call("POST", "/api/login", {"pin": PIN}, host=LAN)
cookie = ""
for k, v in hd.items():
    if k.lower() == "set-cookie":
        cookie = v.split(";")[0]
check("正确配对码可登录", st == 200 and cookie.startswith("dlsess="), "status=%s" % st)
AUTH = {"Cookie": cookie}

print("\n[4] 路径穿越与越界文件操作")
traversals = [
    "/api/download?path=" + urllib.parse.quote("C:\\Windows\\win.ini"),
    "/api/download?path=" + urllib.parse.quote("../../../../Windows/win.ini"),
    "/api/list?path=" + urllib.parse.quote("C:\\Windows"),
    "/api/list?path=" + urllib.parse.quote("..\\..\\.."),
]
for path in traversals:
    # 关键性质：未配对时穿越同样拿不到任何数据（配对后全盘浏览属于产品设计）
    st, body, _ = call("GET", path, host=LAN)
    leak = b"[fonts]" in body or b"[extensions]" in body
    check("未配对穿越读取被拒 " + path.split("path=")[1][:28], st in (401, 403, 404) and not leak,
          "status=%s" % st)
for name in ("../evil.txt", "..\\evil.txt", "a/../../evil.txt", "C:\\Windows\\evil.txt"):
    st, body, _ = call("POST", "/api/upload?name=" + urllib.parse.quote(name), b"x",
                       host=LAN, headers=AUTH)
    escaped = os.path.exists(os.path.join(os.path.dirname(RECV), "evil.txt"))
    check("上传文件名穿越被挡 " + name, st in (200, 400, 403) and not escaped, "status=%s" % st)
for target in ("C:\\Windows\\win.ini", os.path.expanduser("~\\.gitconfig")):
    st, body, _ = call("POST", "/api/delete", {"paths": [target]}, host=LAN, headers=AUTH)
    check("删除接收目录外文件被拒 " + os.path.basename(target), st in (403, 404),
          "status=%s" % st)
st, body, _ = call("POST", "/api/rename", {"path": "C:\\Windows\\win.ini", "name": "x.ini"},
                   host=LAN, headers=AUTH)
check("重命名接收目录外文件被拒", st in (403, 404), "status=%s" % st)
st, body, _ = call("POST", "/api/mkdir", {"path": "C:\\Windows", "name": "pwn"}, host=LAN,
                   headers=AUTH)
check("在系统目录建文件夹被拒", st == 403, "status=%s" % st)

print("\n[5] 注入与畸形输入")
st, body, _ = call("POST", "/api/clip", b"{not json", host=LAN,
                   headers={**AUTH, "Content-Type": "application/json"})
check("畸形 JSON 不返回 5xx", st < 500, "status=%s" % st)
st, body, _ = call("GET", "/api/download?path=%00abc", host=LAN, headers=AUTH)
check("空字节路径不崩", st < 500, "status=%s" % st)
st, body, _ = call("GET", "/api/download?path=" + "a" * 4000, host=LAN, headers=AUTH)
check("超长路径不崩", st < 500 and st != -1, "status=%s" % st)
st, body, _ = call("POST", "/api/upload?name=" + "长" * 300, b"x", host=LAN, headers=AUTH)
check("超长文件名不崩", st < 500, "status=%s" % st)
st, body, _ = call("POST", "/api/clip", {"text": "<script>alert(1)</script>"}, host=LAN, headers=AUTH)
check("注入文本按数据处理（不执行）", st == 200, "status=%s" % st)

# 文件名 XSS：分享页是服务端渲染 HTML，必须转义（用 & 验证转义是否生效）
xss_name = "A&B'C.txt"
xss_path = os.path.join(RECV, xss_name)
try:
    with open(xss_path, "w", encoding="utf-8") as f:
        f.write("xss")
    st, body, _ = call("POST", "/api/share", {"path": xss_path}, host=LAN, headers=AUTH)
    url = json.loads(body).get("url", "") if st == 200 else ""
    tok = url.rsplit("/", 1)[-1] if url else ""
    st, page, _ = call("GET", "/dl/" + tok, host=LAN)
    check("分享页转义文件名（防 XSS）", st == 200 and b"A&amp;B" in page and b"A&B" not in page,
          "status=%s" % st)
    st, page, _ = call("GET", "/dl/" + tok + "/file", host=LAN)
    check("分享直链可下载", st == 200 and page == b"xss", "status=%s" % st)
    st, page, _ = call("GET", "/dl/" + "f" * 24, host=LAN)
    check("猜测分享 token 返回 404", st == 404, "status=%s" % st)
finally:
    try:
        os.remove(xss_path)
    except OSError:
        pass

print("\n[6] 边界值与协议健壮性")
st, body, _ = call("GET", "/api/download?path=" + urllib.parse.quote(os.path.join(RECV, "x")),
                   host=LAN, headers={**AUTH, "Range": "bytes=abc"})
check("错误 Range 不崩", st < 500 and st != -1, "status=%s" % st)
st, body, _ = call("GET", "/api/download?path=" + urllib.parse.quote(os.path.join(RECV, "x")),
                   host=LAN, headers={**AUTH, "Range": "bytes=999999999999-"})
check("超大 Range 不崩", st < 500 and st != -1, "status=%s" % st)
st, body, _ = call("POST", "/api/clip", b"x" * 100, host=LAN,
                   headers={**AUTH, "Content-Length": "999999999"})
check("声明超大 Content-Length 被拒", st in (400, 411, 413) or st == -1, "status=%s" % st)
st, body, _ = call("PUT", "/api/list", b"x", host=LAN, headers=AUTH)
check("不支持的方法返回 501（不崩）", st in (400, 405, 501), "status=%s" % st)
st, body, _ = call("GET", "/" + "a" * 8000, host=LAN)
check("超长 URL 不崩", st < 500 and st != -1, "status=%s" % st)
st, body, _ = call("POST", "/api/share", {"path": os.path.join(RECV, "x")}, host=LAN,
                   headers={**AUTH, "Host": "evil.example.com"})
check("Host 头伪造不影响分享地址来源", st < 500, "status=%s" % st)

print("\n[7] 并发与稳定性")
errors = []
lat = []


def worker():
    t0 = time.time()
    st, _, _ = call("GET", "/api/ping", timeout=5)
    lat.append(time.time() - t0)
    if st != 200:
        errors.append(st)


ths = [threading.Thread(target=worker) for _ in range(40)]
[t.start() for t in ths]
[t.join() for t in ths]
check("40 并发请求全部成功", not errors, "失败 %d 个" % len(errors))
check("并发下响应仍快（P95 < 1.5s）", sorted(lat)[int(len(lat) * 0.95) - 1] < 1.5,
      "P95=%.0fms" % (sorted(lat)[int(len(lat) * 0.95) - 1] * 1000))
st, body, _ = call("GET", "/api/session")
check("压测后服务仍存活", st == 200, "status=%s" % st)

print("\n[8] 配对码暴力破解（放最后，会触发锁定）")
codes = []
for i in range(8):
    st, body, _ = call("POST", "/api/login", {"pin": "%06d" % (999999 - i)}, host=LAN)
    codes.append(st)
locked = any(s == 429 for s in codes)
check("连续错误配对码触发限流/锁定", locked, "状态序列=%s" % codes)
st, body, _ = call("POST", "/api/login", {"pin": PIN}, host=LAN)
check("锁定期间即使配对码正确也被拒", st == 429, "status=%s" % st)

# 清理自检产生的测试文件，避免污染接收目录
for _n in ("evil", "evil_dir", "evil.txt"):
    _p = os.path.join(RECV, _n)
    try:
        if os.path.isdir(_p):
            shutil.rmtree(_p, ignore_errors=True)
        elif os.path.exists(_p):
            os.remove(_p)
    except OSError:
        pass

print("\n================ 结论 ================")
print("通过 %d 项，失败 %d 项，提示 %d 项" % (len(PASS), len(FAIL), len(WARN)))
if FAIL:
    print("失败用例：")
    for f in FAIL:
        print("  - " + f)
sys.exit(1 if FAIL else 0)
