# -*- coding: utf-8 -*-
"""针对 EXE 启动的服务做回归（不含启动/停止，服务由外部拉起）。"""
import hashlib
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8848
BASE = f"http://127.0.0.1:{PORT}"
RECV = os.environ.get("DROPLINK_RECV") or os.path.join(os.path.expanduser("~"), "Desktop", "DropLink 接收")
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (("  -> " + str(extra)) if extra else ""))


opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),  # 不走系统代理，否则 127.0.0.1 会被代理拦下
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def req(path, data=None, headers=None):
    body, h = data, {}
    if isinstance(data, dict):
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    r = urllib.request.Request(BASE + path, data=body, headers=h)
    try:
        with opener.open(r, timeout=30) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


# 模拟手机：局域网地址 + 独立 cookie
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.connect(("10.255.255.255", 1))
LAN = "http://" + s.getsockname()[0] + f":{PORT}"
s.close()
phone = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def preq(path, data=None):
    body, h = data, {}
    if isinstance(data, dict):
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(LAN + path, data=body, headers=h)
    try:
        with phone.open(r, timeout=30) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


print(f"\n目标 {BASE}   手机模拟 {LAN}")

# 本机界面同样需要认证（回环不再等于已认证），用固定配对码登录
PIN = os.environ.get("DROPLINK_PIN", "12345678")
st, body, _ = req("/api/login", {"pin": PIN})
if st != 200:
    print("本机登录失败（测试实例需以 --pin %s 启动）：%s %s" % (PIN, st, body[:120]))
    sys.exit(2)

print("\n[1] 页面与静态资源")
for p, ct in [("/", "text/html"), ("/static/app.js", "javascript"),
              ("/static/styles.css", "text/css"), ("/manifest.webmanifest", "json")]:
    st, _, hd = req(p)
    check(f"GET {p}", st == 200 and ct in (hd.get("Content-Type") or ""), hd.get("Content-Type"))

print("\n[2] 二维码 / 图标（零依赖内置）")
st, body, _ = req("/qr.png?" + urllib.parse.urlencode({"text": "http://192.168.1.7:8848"}))
check("二维码 PNG", st == 200 and body[:8] == b"\x89PNG\r\n\x1a\n" and len(body) > 300, f"{len(body)}B")
st, body, _ = req("/api/icon?size=192")
check("图标 PNG", st == 200 and body[:8] == b"\x89PNG\r\n\x1a\n", f"{len(body)}B")

print("\n[3] 配对鉴权")
st, body, _ = preq("/api/session")
check("手机未配对拿不到会话", st == 401, st)
st, _, _ = preq("/api/list")
check("未配对不能列目录", st == 401, st)
st, _, _ = preq("/api/login", {"pin": "000000"})
check("错配对码被拒", st in (400, 403, 429), st)
st, body, _ = preq("/api/login", {"pin": PIN})
check("正确配对码可登录", st == 200, st)
st, body, _ = preq("/api/session")
check("配对后 authed=true", st == 200 and json.loads(body).get("authed") is True, st)
# 手机登录后紧接着做后续用例（复用这台“手机”的 cookie）
phone_cookie = None

print("\n[4] 上传 / 下载 / 断点 / 打包 / 直链")
data = os.urandom(240000)
st, body, _ = req("/api/upload?" + urllib.parse.urlencode({"name": "EXE 测试.bin"}),
                  data, headers={"Content-Type": "application/octet-stream"})
up = json.loads(body)
check("上传", up.get("ok") is True, up.get("name"))
fp = os.path.join(RECV, up.get("name") or "EXE 测试.bin")
check("落盘一致", os.path.isfile(fp) and open(fp, "rb").read() == data)
q = urllib.parse.urlencode({"path": fp, "log": "1"})
st, body, _ = req("/api/download?" + q)
check("下载一致", body == data)
st, body, hd = req("/api/download?" + q, headers={"Range": "bytes=500-999"})
check("Range 断点", st == 206 and body == data[500:1000], hd.get("Content-Range"))
st, body, _ = req("/api/zip", {"paths": [fp]})
check("打包 zip", st == 200 and body[:2] == b"PK")
st, body, _ = req("/api/share", {"path": fp})
tok = json.loads(body)["url"].rsplit("/", 1)[-1]
st, body, _ = req("/dl/" + tok)
check("扫码下载页(HTML)", st == 200 and b"<html" in body.lower() and "下载" in body.decode("utf-8", "replace"))
st, body, _ = req("/dl/" + tok + "/file")
check("扫码直链下载", st == 200 and body == data)

print("\n[5] 预览 / 搜索 / 剪贴板 / 删除")
txt = os.path.join(RECV, "EXE 备忘.txt")
open(txt, "w", encoding="utf-8").write("来自 EXE 的测试\n")
st, body, _ = req("/api/preview?" + urllib.parse.urlencode({"path": txt}))
check("文本预览", "来自 EXE 的测试" in json.loads(body).get("text", ""))
st, body, _ = req("/api/search?" + urllib.parse.urlencode({"q": "EXE 备忘", "path": RECV}))
check("搜索命中", len(json.loads(body)["entries"]) >= 1)
st, body, _ = req("/api/clip", {"text": "EXE 剪贴板", "from": "手机"})
st, body, _ = req("/api/clip")
check("剪贴板", any("EXE 剪贴板" in i.get("text", "") for i in json.loads(body)["items"]))
st, body, _ = req("/api/delete", {"paths": [txt, fp]})
check("删除进回收站", json.loads(body).get("ok") is True and not os.path.exists(txt))

print("\n" + "=" * 50)
print(f"  通过 {len(PASS)}，失败 {len(FAIL)}")
if FAIL:
    print("  失败：" + "、".join(FAIL))
print("=" * 50)
sys.exit(1 if FAIL else 0)
