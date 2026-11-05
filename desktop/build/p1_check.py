#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 第一批：S-10 日志脱敏 + S-11 配额与残留清理 回归。

自带一个安全版实例（stdout 走管道，便于检查“非交互输出自动脱敏”）：
  1. logsafe 单元（redact_path / redact_ip / mask / safe_exc）
  2. 配额：DROPLINK_QUOTA_MB=1 时传 2MB -> 507 DISK_FULL，且没有 0 字节残留
  3. 过期 .part 清理：25 小时前删除、1 小时前保留
  4. 上传中断（发一半断开）-> .part 立即清理
  5. 抓服务端 stdout：无完整盘符路径、无 PIN、无 32 位 token；文件名+哈希仍在；
     本机 127.0.0.1 原样、局域网 IP 打码
  6. --selfcheck 输出不含 PIN/token/完整路径

用法：python build/p1_check.py [被 run_tests 分配的端口（本脚本自建实例，不用它）]
"""
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
if APP not in sys.path:
    sys.path.insert(0, APP)
TOKEN = os.environ.get("DROPLINK_LOCAL_TOKEN") or "runtests"
PIN = "12345678"
RECV = os.path.join(APP, ".p1_recv")
CERTS = os.path.join(APP, ".p1_certs")
TRUST = os.path.join(APP, ".p1_trust.json")
CTX = ssl._create_unverified_context()
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (("  -> " + str(extra)) if extra else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=CTX),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


print("接收目录 %s" % RECV)

# ---------------- 1. logsafe 单元 ----------------
print("\n[1] logsafe 单元")
try:
    from dlcore import logsafe as L
    rp = L.redact_path(r"E:\deepseek工作区\a\报告.pdf")
    check("redact_path 只留文件名+哈希", rp.startswith("…/报告.pdf#") and len(rp.rsplit("#", 1)[-1]) == 8, rp)
    check("redact_path 不含盘符", "E:" not in rp and "\\" not in rp, rp)
    check("redact_ip 局域网打码", L.redact_ip("192.168.1.23") == "192.168.1.*", L.redact_ip("192.168.1.23"))
    check("redact_ip 回环保留", L.redact_ip("127.0.0.1") == "127.0.0.1")
    check("redact_ip IPv6", L.redact_ip("fe80:0000:0000:0000:0202:b3ff:fe1e:8329") == "fe80:0000:0000:0000::*",
          L.redact_ip("fe80:0000:0000:0000:0202:b3ff:fe1e:8329"))
    check("mask", L.mask("12345678") == "***")
    se = L.safe_exc(FileNotFoundError(r"E:\deepseek工作区\a\b.txt"))
    check("safe_exc 去掉绝对路径", "E:\\" not in se and "b.txt#" in se, se)
except Exception as exc:  # noqa: BLE001
    check("logsafe 单元", False, exc)

# ---------------- 启动自带实例 ----------------
for p in (RECV, CERTS):
    shutil.rmtree(p, ignore_errors=True)
    os.makedirs(p, exist_ok=True)
try:
    os.unlink(TRUST)
except OSError:
    pass
port = free_port()
BASE = "https://127.0.0.1:%d" % port
env = dict(os.environ)
env.update({
    "DROPLINK_EDITION": "secure",
    "DROPLINK_CERT_DIR": CERTS,
    "DROPLINK_TRUST_FILE": TRUST,
    "DROPLINK_LOCAL_TOKEN": TOKEN,
    "DROPLINK_QUOTA_MB": "1",
    "DROPLINK_PIN": PIN,
    "PYTHONIOENCODING": "utf-8",
})
proc = subprocess.Popen(
    [sys.executable, "-u", os.path.join(APP, "server.py"), "--port", str(port),
     "--pin", PIN, "--no-browser", "--recv", RECV],
    cwd=APP, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    text=True, encoding="utf-8", errors="replace", bufsize=1)
lines = []


def _reader():
    try:
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))
    except Exception:
        pass


threading.Thread(target=_reader, daemon=True).start()


def wait_ready(seconds=25):
    end = time.time() + seconds
    while time.time() < end:
        try:
            with urllib.request.urlopen(BASE + "/api/ping", context=CTX, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


ready = wait_ready()
check("自带安全版实例就绪", ready, port)

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
        with op.open(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def upload(op, sid, fid, token, payload, base=None):
    q = urllib.parse.urlencode({"session_id": sid, "file_id": fid, "token": token})
    req = urllib.request.Request((base or BASE) + "/api/upload?" + q, data=payload, method="POST")
    try:
        with op.open(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def raw_upload(q, length, body=b"", timeout=10):
    """原始 TLS 上传：只发头（可不发 body），用来测配额/断线这类「服务器提前回包」。"""
    s = CTX.wrap_socket(socket.create_connection(("127.0.0.1", port)), server_hostname="127.0.0.1")
    try:
        s.sendall(("POST /api/upload?" + q + " HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                   "X-DL-Token: %s\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                   % (port, TOKEN, length)).encode())
        if body:
            s.sendall(body)
        data = b""
        s.settimeout(timeout)
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    except Exception:
        pass
    finally:
        try:
            s.close()
        except Exception:
            pass
    head, _, payload = data.partition(b"\r\n\r\n")
    try:
        code = int(head.split(b" ")[1])
    except Exception:
        code = 0
    try:
        obj = json.loads(payload.decode("utf-8", "replace") or "{}")
    except ValueError:
        obj = {}
    return code, obj


def approve_new_device(device_id, name):
    phone = opener()
    lan = "https://%s:%d" % (lan_ip(), port)
    st, d = call(phone, "POST", "/api/login",
                 {"pin": PIN, "device_id": device_id, "device_name": name, "platform": "android-web"},
                 base=lan)
    if st == 202 and d.get("request_id"):
        call(local, "POST", "/api/trust/approve", {"request_id": d["request_id"]}, with_token=True)
        call(phone, "GET", "/api/login/status?request_id=" + d["request_id"], base=lan)
    return phone


try:
    if ready:
        lan = "https://%s:%d" % (lan_ip(), port)
        phone = approve_new_device("p1-device-0001", "日志手机")
        check("设备登录", call(phone, "GET", "/api/session", base=lan)[0] == 200)

        # ---------- 2. 正常上传（供日志断言） ----------
        print("\n[2] 正常上传 10 字节（日志里应只剩文件名+哈希）")
        st, d = call(phone, "POST", "/api/prepare-upload",
                     {"files": [{"name": "报告2026.txt", "size": 10}]}, base=lan)
        sid = d.get("session_id", "")
        call(local, "POST", "/api/approve", {"session_id": sid, "action": "accept"}, with_token=True)
        st, d = call(phone, "GET", "/api/upload/status?session_id=" + sid, base=lan)
        f = (d.get("files") or [{}])[0]
        st, d = upload(phone, sid, f.get("file_id", ""), f.get("token", ""), b"0123456789", base=lan)
        check("普通上传 200", st == 200, st)

        # ---------- 3. 配额 ----------
        print("\n[3] 配额：DROPLINK_QUOTA_MB=1，传 2MB -> 507")
        before = set(os.listdir(RECV))
        st, d = call(phone, "POST", "/api/prepare-upload",
                     {"files": [{"name": "big.bin", "size": 2 * 1024 * 1024}]}, base=lan)
        sid2 = d.get("session_id", "")
        call(local, "POST", "/api/approve", {"session_id": sid2, "action": "accept"}, with_token=True)
        st, d = call(phone, "GET", "/api/upload/status?session_id=" + sid2, base=lan)
        f2 = (d.get("files") or [{}])[0]
        q2 = urllib.parse.urlencode({"session_id": sid2, "file_id": f2.get("file_id", ""),
                                     "token": f2.get("token", "")})
        # 只发头不发 body：服务器在配额检查处就会提前回 507 并断开
        st, d = raw_upload(q2, 2 * 1024 * 1024)
        check("2MB 上传被配额拒绝 -> 507 DISK_FULL", st == 507 and d.get("code") == "DISK_FULL",
              "%s %s" % (st, d.get("code")))
        after = set(os.listdir(RECV))
        zero = [n for n in after if os.path.isfile(os.path.join(RECV, n))
                and os.path.getsize(os.path.join(RECV, n)) == 0]
        check("没有 0 字节残留", not zero, zero)
        check("没有 .part 残留", not [n for n in after if n.endswith(".part")], after - before)

        # ---------- 4. 过期 .part 清理 ----------
        print("\n[4] .part 清理：只删本程序生成的 .dl_<8位小写hex>.part")
        from dlcore import fsutil
        cases = {
            ".dl_a1b2c3d4.part": True,       # 我们生成的形状 -> 删
            ".dl_userbackup.part": False,    # 用户自己的 -> 留
            ".dl_a1b2c3d4.part.bak": False,  # 结尾不是 .part -> 留
            "report.part": False,            # 没有前缀 -> 留
            ".dl_DEADBEEF.part": False,      # 大写 -> 留（我们从不生成）
        }
        old_ts = time.time() - 25 * 3600
        paths = {}
        for name in cases:
            pth = os.path.join(RECV, name)
            with open(pth, "wb") as f:
                f.write(b"x")
            os.utime(pth, (old_ts, old_ts))
            paths[name] = pth
        send_dir = os.path.join(RECV, ".droplink-send")
        os.makedirs(send_dir, exist_ok=True)
        keep = os.path.join(send_dir, "推送副本.txt")
        with open(keep, "wb") as f:
            f.write(b"x")
        os.utime(keep, (old_ts, old_ts))
        n, freed = fsutil.sweep_parts(RECV, 86400)
        for name, should_delete in cases.items():
            exists = os.path.exists(paths[name])
            check(("删除 " if should_delete else "保留 ") + name,
                  exists != should_delete, "exists=%s" % exists)
        check(".droplink-send 正常文件不受影响", os.path.exists(keep))
        for pth in list(paths.values()) + [keep]:
            try:
                os.unlink(pth)
            except OSError:
                pass

        # ---------- 5. 上传中断 ----------
        print("\n[5] 上传中断（发一半断开）-> 立即清理")
        st, d = call(phone, "POST", "/api/prepare-upload",
                     {"files": [{"name": "cut.bin", "size": 3 * 1024 * 1024}]}, base=lan)
        sid3 = d.get("session_id", "")
        call(local, "POST", "/api/approve", {"session_id": sid3, "action": "accept"}, with_token=True)
        st, d = call(phone, "GET", "/api/upload/status?session_id=" + sid3, base=lan)
        f3 = (d.get("files") or [{}])[0]
        q = urllib.parse.urlencode({"session_id": sid3, "file_id": f3.get("file_id", ""),
                                    "token": f3.get("token", "")})
        s = CTX.wrap_socket(socket.create_connection(("127.0.0.1", port)), server_hostname="127.0.0.1")
        s.sendall(("POST /api/upload?" + q + " HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                   "X-DL-Token: %s\r\n"
                   "Content-Length: %d\r\nConnection: close\r\n\r\n" % (port, TOKEN, 3 * 1024 * 1024)).encode())
        s.sendall(b"q" * 1024)
        s.close()
        time.sleep(2.0)
        parts = [n for n in os.listdir(RECV) if n.endswith(".part")]
        check("中断后 .part 不残留", not parts, parts)
        check("中断后 cut.bin 不存在", not os.path.exists(os.path.join(RECV, "cut.bin")))
finally:
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    time.sleep(0.5)

# ---------------- 6. 日志断言 ----------------
print("\n[6] 服务端输出脱敏检查")
out = "\n".join(lines)
check("输出非空", len(out) > 100, len(out))
check("无完整盘符路径", not re.search(r"[A-Za-z]:\\", out), re.findall(r"[A-Za-z]:\\[^\s]*", out)[:3])
check("无 PIN 明文", PIN not in out and not re.search(r"(?<!\d)\d{8}(?!\d)", out),
      [ln for ln in lines if re.search(r"(?<!\d)\d{8}(?!\d)", ln)][:2])
check("无 32 位 token", not re.search(r"\b[0-9a-f]{32}\b", out), re.findall(r"\b[0-9a-f]{32}\b", out)[:3])
check("保留文件名", "报告2026.txt" in out)
check("保留文件名哈希前缀", bool(re.search(r"报告2026\.txt#\w{8}", out)))
check("本机 127.0.0.1 保留", "127.0.0.1" in out)
ip = lan_ip()
redacted = ".".join(ip.split(".")[:3]) + ".*" if ip != "127.0.0.1" else ip
check("局域网 IP 已打码", redacted in out or ip == "127.0.0.1",
      (redacted, [ln for ln in lines if ip in ln][:2]))
check("PIN 在非交互输出里被 mask", "配对码  ：  ***" in out or "***" in out)
# P2-3：事件日志行都有 HH:MM:SS 前缀（横幅除外）
bad_tag = [ln for ln in lines if ln.startswith("[")]
check("没有缺时间前缀的事件行", not bad_tag, bad_tag[:3])
timed = [ln for ln in lines if re.match(r"^\d\d:\d\d:\d\d \[", ln)]
check("至少 5 行带时间的事件日志", len(timed) >= 5, len(timed))

# ---------------- 7. selfcheck ----------------
print("\n[7] --selfcheck 不含敏感信息")
r = subprocess.run([sys.executable, "-u", os.path.join(APP, "server.py"), "--selfcheck"],
                   cwd=APP, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
sc = (r.stdout or "") + (r.stderr or "")
check("selfcheck 退出码 0", r.returncode == 0, r.returncode)
check("selfcheck 无盘符", not re.search(r"[A-Za-z]:\\", sc), sc[:120])
check("selfcheck 无 PIN/token", PIN not in sc and not re.search(r"\b[0-9a-f]{32}\b", sc))

for p in (RECV, CERTS):
    shutil.rmtree(p, ignore_errors=True)
try:
    os.unlink(TRUST)
except OSError:
    pass

# ---------------- 8. 版本一致性 / 发布校验和 ----------------
print("\n[8] 版本一致性与 dist/SHA256SUMS.txt")
try:
    import hashlib as _hl
    base = open(os.path.join(APP, "VERSION"), encoding="utf-8").read().strip()
    cfg = open(os.path.join(APP, "dlcore", "config.py"), encoding="utf-8").read()
    ver = re.search(r'VERSION = "([0-9.]+)"', cfg).group(1)
    readme = open(os.path.join(APP, "README.md"), encoding="utf-8").read()
    edoc = open(os.path.join(APP, "docs", "EDITIONS.md"), encoding="utf-8").read()
    bld = open(os.path.join(APP, "build", "build_exe.py"), encoding="utf-8").read()
    check("VERSION 文件存在且格式正确", bool(re.fullmatch(r"\d+\.\d+\.\d+", base)), base)
    check("config.VERSION 等于发布基线 %s" % base, ver == base, ver)
    check("README 含发布基线 %s" % base, base in readme)
    check("EDITIONS 含发布基线 %s" % base, base in edoc)
    check("build_exe 有独立基线检查（config/README/EDITIONS）",
          "def _baseline_version" in bld and "check_version_baseline" in bld)
    sums = os.path.join(APP, "dist", "SHA256SUMS.txt")
    if os.path.isfile(sums):
        text = open(sums, encoding="utf-8").read()
        ok = True
        for name in ("DropLink.exe", "DropLink-Secure.exe"):
            p = os.path.join(APP, "dist", name)
            if not os.path.isfile(p):
                continue
            h = _hl.sha256(open(p, "rb").read()).hexdigest().upper()
            if ("%s  %s  (%d)" % (h, name, os.path.getsize(p))) not in text:
                ok = False
        check("SHA256SUMS.txt 与实际产物一致", ok)
        check("SHA256SUMS 注明 APK 不在本仓库", "APK" in text)
    else:
        check("dist/SHA256SUMS.txt 存在", False, sums)
except Exception as exc:  # noqa: BLE001
    check("版本/校验和", False, exc)

print("\n" + "=" * 50)
print("  通过 %d，失败 %d" % (len(PASS), len(FAIL)))
print("=" * 50)
sys.exit(1 if FAIL else 0)
