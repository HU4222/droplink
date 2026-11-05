#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S-06 写入侧路径加强 回归（安全版）。

分两部分：
  单元：直接 import dlcore.sec.pathguard，覆盖 35 条非法名 + 12 条合法名 +
        resolve_write_target / check_upload_path / check_recv_dir 的边界。
  真跑：安全版服务（TLS）上 prepare-upload 塞脏名字 -> 422 INVALID_NAME 且接收目录零新增；
        接收目录里放一个指向 %TEMP% 的目录联接，往它里面传 -> 422 且 %TEMP% 无文件。

用法：python build/pathguard_check.py <port>
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
RECV = os.environ.get("DROPLINK_RECV") or os.path.join(APP, ".pg_recv")
CTX = ssl._create_unverified_context()
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (("  -> " + str(extra)) if extra else ""))


BAD_NAMES = [
    "", ".", "..", "a\\b", "a/b", "a.txt:evil", "a.txt::$DATA",
    "<x", "x>", 'a"b', "a|b", "a?b", "a*b", "a\x01b", "a\x7fb",
    "CON", "con.txt", "PRN", "AUX", "NUL", "COM1", "COM9.txt", "LPT1", "LPT9.txt",
    "CONIN$", "CONIN$.txt", "CONOUT$", "a.", "a ", "a. . ",
    "x\u200by", "x\ufeffy", "a\u202eb", "a\u2066b", "a" * 256,
]
GOOD_NAMES = [
    "CONSOLE.log", "com10.txt", "lpt0.txt", "a.b.c.zip",
    "期末 报告.docx", "报告 2026（终版）.txt", "normal.txt", "a b.txt",
    "_hidden.txt", "..hidden.txt", "文件-01.tar.gz", "UPPER.TXT",
]

print("目标 %s   接收目录 %s" % (BASE, RECV))

# ---------------- 单元 ----------------
print("\n[1] 单元：非法名 %d 条全部拒绝 / 合法名 %d 条不被误拒" % (len(BAD_NAMES), len(GOOD_NAMES)))
try:
    from dlcore.sec import pathguard as pg
    missed = [n for n in BAD_NAMES if not pg.audit(n)]
    check("非法名全部 audit 非空", not missed, missed)
    wrong = [n for n in GOOD_NAMES if pg.audit(n)]
    check("合法名全部 audit 为空", not wrong, [(n, pg.audit(n)) for n in wrong])

    tmp = os.path.join(APP, ".pg_unit_recv")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    got = pg.resolve_write_target(tmp, "ok.txt")
    check("resolve_write_target 正常名字返回 base 内路径",
          os.path.normcase(os.path.dirname(got)) == os.path.normcase(os.path.realpath(tmp)), got)
    for bad in ("../x.txt", "a\\b.txt", "CON", "a.txt:ads"):
        try:
            pg.resolve_write_target(tmp, bad)
            check("resolve_write_target 拒绝 %r" % bad, False, "没抛异常")
        except ValueError:
            check("resolve_write_target 拒绝 %r" % bad, True)
    try:
        pg.check_upload_path(tmp, os.path.join(tmp, "..", "evil.txt"))
        check("check_upload_path 拒绝越界 ..", False, "没抛异常")
    except ValueError:
        check("check_upload_path 拒绝越界 ..", True)
    if os.path.isdir("D:\\"):
        try:
            pg.check_upload_path(tmp, "D:\\evil.txt")
            check("check_upload_path 拒绝跨盘符", False, "没抛异常")
        except ValueError:
            check("check_upload_path 拒绝跨盘符", True)
    try:
        pg.check_recv_dir("\\\\server\\share")
        check("check_recv_dir 拒绝 UNC", False, "没抛异常")
    except ValueError:
        check("check_recv_dir 拒绝 UNC", True)
except Exception as exc:  # noqa: BLE001
    check("pathguard 单元", False, exc)
finally:
    shutil.rmtree(os.path.join(APP, ".pg_unit_recv"), ignore_errors=True)

# ---------------- 真跑 ----------------
def opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=CTX),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


phone, local = opener(), opener()


def call(op, method, path, body=None, with_token=False):
    data = json.dumps(body).encode() if body is not None else None
    headers = {}
    if data:
        headers["Content-Type"] = "application/json"
    if with_token:
        headers["X-DL-Token"] = TOKEN
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with op.open(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def upload(op, sid, fid, token, payload):
    q = urllib.parse.urlencode({"session_id": sid, "file_id": fid, "token": token})
    req = urllib.request.Request(BASE + "/api/upload?" + q, data=payload, method="POST")
    try:
        with op.open(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def recv_files():
    out = []
    for root, dirs, files in os.walk(RECV):
        dirs[:] = [d for d in dirs if d not in (".droplink-send", "jlink")]
        for f in files:
            out.append(os.path.join(root, f))
    return out


DEV = "pg-device-0001"
st, d = call(phone, "POST", "/api/login",
             {"pin": PIN, "device_id": DEV, "device_name": "路径测试", "platform": "ios"})
if st == 202:
    call(local, "POST", "/api/trust/approve", {"request_id": d["request_id"]}, with_token=True)
    call(phone, "GET", "/api/login/status?request_id=" + d["request_id"])
check("设备已登录", call(phone, "GET", "/api/session")[0] == 200)

print("\n[2] prepare 脏名字 -> 422 INVALID_NAME，接收目录零新增")
before = recv_files()
cases = [
    {"name": "../evil.txt", "size": 1},
    {"name": "C:\\Windows\\x.txt", "size": 1},
    {"name": "a.txt:ads", "size": 1},
    {"name": "CON", "size": 1},
    {"name": "a\u202Eb.txt", "size": 1},
    {"name": "ok.txt", "rel": "CON", "size": 1},
    {"name": "ok.txt", "rel": "a.txt:ads", "size": 1},
]
for f in cases:
    st, d = call(phone, "POST", "/api/prepare-upload", {"files": [f]})
    check("坏名字 %r -> 422 INVALID_NAME" % (f.get("rel") or f["name"]),
          st == 422 and d.get("code") == "INVALID_NAME", "%s %s" % (st, d.get("code")))
after = recv_files()
check("接收目录零新增", after == before, (before, after))

print("\n[3] 目录联接（junction）-> 拒绝且目标目录无文件")
target = os.path.join(tempfile.gettempdir(), "dl_pg_jtarget")
shutil.rmtree(target, ignore_errors=True)
os.makedirs(target, exist_ok=True)
jlink = os.path.join(RECV, "jlink")
made = False
try:
    if os.path.isdir(jlink):
        os.rmdir(jlink)
    r = subprocess.run(["cmd", "/c", "mklink", "/J", jlink, target],
                       capture_output=True, text=True)
    made = (r.returncode == 0 and os.path.isdir(jlink))
except Exception:
    made = False
if not made:
    check("创建目录联接", False, "mklink 失败（环境不支持）")
else:
    st, d = call(phone, "POST", "/api/prepare-upload", {"files": [{"name": "x.txt", "rel": "jlink", "size": 2}]})
    sid = d.get("session_id", "")
    check("经 junction 的申请先挂起", st == 200 and d.get("state") == "pending", st)
    call(local, "POST", "/api/approve", {"session_id": sid, "action": "accept"}, with_token=True)
    st, d2 = call(phone, "GET", "/api/upload/status?session_id=" + sid)
    f = (d2.get("files") or [{}])[0]
    st, d3 = upload(phone, sid, f.get("file_id", ""), f.get("token", ""), b"hi")
    check("经 junction 上传 -> 422 INVALID_NAME",
          st == 422 and d3.get("code") == "INVALID_NAME", "%s %s" % (st, d3.get("code")))
    check("%TEMP% 目标目录无文件", os.listdir(target) == [], os.listdir(target))
    shutil.rmtree(target, ignore_errors=True)

print("\n" + "=" * 50)
print("  通过 %d，失败 %d" % (len(PASS), len(FAIL)))
print("=" * 50)
sys.exit(1 if FAIL else 0)
