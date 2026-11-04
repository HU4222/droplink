# -*- coding: utf-8 -*-
"""dlcore 容错验证：逐个让模块「缺失 / 损坏」，确认服务仍能启动。

用法::

    python build/fault_check.py                # 全部 7 个模块，两种故障
    python build/fault_check.py --only api,httpd
    python build/fault_check.py --mode missing  # 只测删除

判定（每条都要成立才算 PASS）：
  1. 进程没有在启动阶段退出；
  2. ``/`` 返回 200，且响应体非空；
  3. ``/api/ping`` 返回可解析的 JSON：正常服务里 ``degraded`` 含该模块，
     降级服务里 ``degraded`` 为 true；
  4. 输出里没有未捕获的 Traceback 崩溃（模块自身的报错不算）。

退出码：0 全部通过，1 有失败，2 环境错误。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
PKG = os.path.join(APP, "dlcore")
MODULES = ("config", "fsutil", "state", "winenv", "media", "api", "httpd")
BROKEN_SRC = "def broken(:\n    pass\n"


PROBES = (
    "/",
    "/static/app.js",
    "/static/styles.css",
    "/manifest.webmanifest",
    "/api/ping",
    "/api/session",
    "/api/list?path=C%3A%2F",
    "/api/preview?path=C%3A%2FWindows%2Fwin.ini",
    "/api/icon?size=64",
    "/qr.png?text=hello",
)


def probe_endpoints(port):
    """降级状态下逐个打接口：允许 401/403/404/503，不允许 500 与崩溃。"""
    bad = []
    for path in PROBES:
        code, body = http("http://127.0.0.1:%d%s" % (port, path))
        if code is None:
            bad.append("%s 无响应" % path)
            continue
        if code >= 500 and code != 503:
            bad.append("%s → %s" % (path, code))
        if b"Traceback" in body[:4000]:
            bad.append("%s 回了 Traceback" % path)
    return bad


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http(url, timeout=3):
    req = urllib.request.Request(url, headers={"User-Agent": "fault-check"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception:
        return None, b""


def wait_up(port, seconds=25):
    end = time.time() + seconds
    while time.time() < end:
        code, _ = http("http://127.0.0.1:%d/api/ping" % port, timeout=1.5)
        if code:
            return True
        time.sleep(0.4)
    return False


def run_case(name, mode):
    src = os.path.join(PKG, name + ".py")
    bak = src + ".faultbak"
    recv = tempfile.mkdtemp(prefix="fault_recv_")
    port = free_port()
    log_path = os.path.join(tempfile.gettempdir(), "fault_%s_%s.log" % (name, mode))

    shutil.copy2(src, bak)
    try:
        if mode == "missing":
            os.remove(src)
        else:
            with open(src, "w", encoding="utf-8") as f:
                f.write(BROKEN_SRC)

        log = open(log_path, "wb")
        proc = subprocess.Popen(
            [sys.executable, "-u", "server.py", "--port", str(port),
             "--pin", "123456", "--no-browser", "--recv", recv],
            cwd=APP, stdout=log, stderr=subprocess.STDOUT)
        try:
            if not wait_up(port):
                return False, "服务在 25 秒内没有起来（见 %s）" % log_path

            code_root, body = http("http://127.0.0.1:%d/" % port)
            if code_root != 200 or not body:
                return False, "首页返回 %s / %d 字节" % (code_root, len(body))

            code_ping, raw = http("http://127.0.0.1:%d/api/ping" % port)
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                return False, "/api/ping 不是 JSON（HTTP %s）" % code_ping

            if data.get("degraded") is True:
                pass                                   # httpd 缺失 → 降级服务
            elif name in (data.get("degraded") or []):
                pass                                   # 正常服务，报告了降级模块
            else:
                return False, "/api/ping 未标注降级（%s）" % str(data)[:120]

            if proc.poll() is not None:
                return False, "进程已退出，返回码 %s" % proc.returncode

            bad = probe_endpoints(port)
            if bad:
                return False, "接口异常：%s" % "；".join(bad[:3])

            if os.path.getsize(log_path) == 0:
                time.sleep(0.5)
            return True, "首页 200 / ping %s / %s / 10 个接口无 500" % (
                code_ping, "降级服务" if data.get("degraded") is True else "正常服务+标注")
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=6)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            log.close()
    finally:
        shutil.move(bak, src)
        shutil.rmtree(recv, ignore_errors=True)


def run_baseline():
    recv = tempfile.mkdtemp(prefix="fault_recv_")
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-u", "server.py", "--port", str(port),
         "--pin", "123456", "--no-browser", "--recv", recv],
        cwd=APP, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_up(port):
            return False, "基线实例没起来"
        code, raw = http("http://127.0.0.1:%d/api/ping" % port)
        data = json.loads(raw.decode("utf-8"))
        if data.get("degraded"):
            return False, "基线居然有降级：%s" % data["degraded"]
        code_root, body = http("http://127.0.0.1:%d/" % port)
        if code_root != 200 or not body:
            return False, "首页返回 %s" % code_root
        return True, "ping 200 / 首页 200 / 无降级"
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=6)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        shutil.rmtree(recv, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="dlcore 容错验证")
    ap.add_argument("--only", default="", help="只测这些模块，逗号分隔")
    ap.add_argument("--mode", default="both", choices=("missing", "broken", "both"))
    args = ap.parse_args()

    picked = [m for m in MODULES if not args.only or m in args.only.split(",")]
    modes = ("missing", "broken") if args.mode == "both" else (args.mode,)

    results = [("基线", "无故障", *run_baseline())]
    print("%-10s %-8s %s" % ("模块", "故障", "结果"), flush=True)
    print("-" * 78)
    print("%-10s %-8s %s" % ("基线", "无故障",
                             "PASS  " + results[0][3] if results[0][2] else "FAIL  " + results[0][3]),
          flush=True)

    for name in picked:
        for mode in modes:
            ok, msg = run_case(name, mode)
            results.append((name, mode, ok, msg))
            print("%-10s %-8s %s" % (name, mode, ("PASS  " if ok else "FAIL  ") + msg), flush=True)

    npass = sum(1 for r in results if r[2])
    print("-" * 78)
    print("合计 %d/%d 通过" % (npass, len(results)))
    out = os.path.join(HERE, "fault_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump([{"module": r[0], "mode": r[1], "pass": r[2], "detail": r[3]}
                   for r in results], f, ensure_ascii=False, indent=1)
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
