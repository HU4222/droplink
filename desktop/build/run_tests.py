#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一测试入口（S3）：一次跑完整套回归，出汇总与退出码。

原先 8 个脚本散在 build/ 下，靠人记着跑；漏跑一套就等于没测。现在：
  起一个干净的源码实例（独立端口 + 临时接收目录）→ 依次跑选中的套件 → 汇总 → 关服务。

用法：
    python build/run_tests.py                 # 默认跑 5 套（功能/文件夹/大文件/安全/加固）
    python build/run_tests.py --only e2e,folder
    python build/run_tests.py --skip bulk     # 磁盘不够时跳过 10 GB 用例
    python build/run_tests.py --port 8899 --pin 123456 --keep

说明：
  - 安全/加固两套必须跑在 --secure 实例上，脚本会自动起第二个实例；
  - 大文件套件需要 ≥12 GB 可用空间，不足时自动跳过并标记 SKIP（不伪造通过）；
  - 每套的输出原样落盘到 runs/<时间戳>/<套件>.log，失败时贴最后 20 行。

退出码：0 全通过（允许 SKIP）；1 有失败；2 环境错误。
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

SUITES = {
    "e2e":        ("e2e_exe.py",        False),
    "folder":     ("folder_check.py",   False),
    "bulk":       ("bulk_check.py",     False),
    "security":   ("security_check.py", True),
    "hardening":  ("hardening_check.py", True),
}


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_ping(port, seconds=25) -> bool:
    """探活：能连通即算起来了。

    --secure 实例的 /api/ping 也要鉴权，返回 401 说明服务已经在跑——
    把 401/403 当失败会让加固套件永远「服务未就绪」。
    """
    end = time.time() + seconds
    while time.time() < end:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % port, timeout=2) as r:
                if r.status in (200, 401, 403):
                    return True
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def start_server(port, pin, recv, secure, log_path):
    cmd = [sys.executable, "-u", os.path.join(APP, "server.py"),
           "--port", str(port), "--pin", pin, "--no-browser", "--recv", recv]
    if secure:
        cmd.append("--secure")
    log = open(log_path, "wb")
    env = dict(os.environ, DROPLINK_LOCAL_TOKEN="runtests")
    p = subprocess.Popen(cmd, cwd=APP, stdout=log, stderr=subprocess.STDOUT, env=env)
    return p, log


def disk_free_gb(path) -> float:
    try:
        return shutil.disk_usage(path).free / (1024 ** 3)
    except Exception:
        return 0.0


def main():
    ap = argparse.ArgumentParser(description="DropLink 统一回归入口")
    ap.add_argument("--only", default="", help="只跑这些套件，逗号分隔：%s" % ",".join(SUITES))
    ap.add_argument("--skip", default="", help="跳过这些套件，逗号分隔")
    ap.add_argument("--port", type=int, default=0, help="服务端口，默认自动分配")
    ap.add_argument("--pin", default="123456")
    ap.add_argument("--recv", default="", help="接收目录，默认用临时目录")
    ap.add_argument("--keep", action="store_true", help="保留临时接收目录与日志")
    ap.add_argument("--min-free-gb", type=float, default=12.0, help="大文件套件所需可用空间")
    args = ap.parse_args()

    picked = list(SUITES)
    if args.only:
        picked = [x.strip() for x in args.only.split(",") if x.strip()]
        bad = [x for x in picked if x not in SUITES]
        if bad:
            print("未知套件：%s（可选：%s）" % (",".join(bad), ",".join(SUITES)))
            return 2
    if args.skip:
        for x in [s.strip() for s in args.skip.split(",") if s.strip()]:
            if x in picked:
                picked.remove(x)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    outdir = os.path.join(HERE, "runs", stamp)
    os.makedirs(outdir, exist_ok=True)

    # 默认用与其它套件一致的接收目录：这些脚本的用例是基于该目录写的，
    # 换成临时目录会让「分享直链 / Range」之类的用例找不到文件而假失败。
    recv = args.recv or os.path.join(os.path.expanduser("~"), "Desktop", "DropLink 接收")
    os.makedirs(recv, exist_ok=True)

    results = []

    for name in picked:
        script, need_secure = SUITES[name]
        path = os.path.join(HERE, script)
        if not os.path.isfile(path):
            results.append((name, "SKIP", "脚本不存在"))
            continue
        if name == "bulk" and disk_free_gb(recv) < args.min_free_gb:
            results.append((name, "SKIP", "可用空间 %.1f GB < %.0f GB" % (
                disk_free_gb(recv), args.min_free_gb)))
            continue

        # 每套单独起一个实例：套件之间会互相污染状态（上传的探针文件被前一套删掉，
        # 后面的 Range 用例就会假失败）。隔离后每个结果都能独立复现。
        port = args.port or free_port()      # 每套独立实例，直接取空闲端口，不做 +1（+1 可能被占用）
        pin = (args.pin + "78")[:8] if need_secure and len(args.pin) < 8 else args.pin
        log_path = os.path.join(outdir, "server_%s_%s.log" % (name, "secure" if need_secure else "plain"))
        proc, logf = start_server(port, pin, recv, need_secure, log_path)
        try:
            if not wait_ping(port):
                results.append((name, "FAIL", "服务未就绪（见 %s）" % os.path.basename(log_path)))
                continue
            # 令牌必须与服务端一致：套件里默认是 testtoken123，不一致会让部分用例 401，
            # 进而触发错误计数与锁定，后面的用例就连锁失败。
            env = dict(os.environ, DROPLINK_PIN=pin, DROPLINK_RECV=recv,
                       DROPLINK_LOCAL_TOKEN="runtests")
            print("→ %-10s 跑 %s（端口 %d%s）..." % (
                name, script, port, "，--secure" if need_secure else ""), flush=True)
            t0 = time.time()
            p = subprocess.run([sys.executable, "-u", path, str(port)], cwd=APP,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", env=env)
            cost = time.time() - t0
            out = (p.stdout or "") + (p.stderr or "")
            with open(os.path.join(outdir, name + ".log"), "w", encoding="utf-8") as f:
                f.write(out)
            tail = [ln for ln in out.strip().splitlines() if ln.strip()][-1:] or [""]
            status = "PASS" if p.returncode == 0 else "FAIL"
            results.append((name, status, "%s（%.1f s）" % (tail[-1][:60], cost)))
            print("   %s  %s" % (status, results[-1][2]), flush=True)
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                logf.close()
            except Exception:
                pass

    n_pass = sum(1 for r in results if r[1] == "PASS")
    n_fail = sum(1 for r in results if r[1] == "FAIL")
    n_skip = sum(1 for r in results if r[1] == "SKIP")

    print()
    print("=" * 58)
    for name, st, msg in results:
        print("  %-4s %-10s %s" % (st, name, msg))
    print("-" * 58)
    print("  %d 通过 / %d 失败 / %d 跳过" % (n_pass, n_fail, n_skip))
    print("  日志：%s" % outdir)
    print("=" * 58)

    summary = {"time": stamp, "results": [{"suite": n, "status": s, "detail": m} for n, s, m in results],
               "pass": n_pass, "fail": n_fail, "skip": n_skip}
    with open(os.path.join(outdir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    if not args.keep:
        try:
            shutil.rmtree(recv, ignore_errors=True)
        except Exception:
            pass
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
