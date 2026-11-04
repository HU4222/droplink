#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DropLink 设备端验收检测（G-05 / 复测取证）。

把「装完包之后要手工点一遍」的检查收成一条命令：设备、版本、进程、服务、
列目录、预览、图标、下载、崩溃、界面，逐项给 PASS / FAIL / SKIP，并落一份
JSON 结果，方便贴进复测报告。

只读为主：唯一会写设备的地方是「建一个临时样例文件用于预览/下载校验」，
运行结束会删掉（--keep 可保留）。

用法:
    python build/device_check.py
    python build/device_check.py --serial <设备序列号> --expect-version 1.4.7
    python build/device_check.py --no-ui --outdir probe_runs

退出码: 0 全通过（允许 SKIP）；1 有 FAIL；2 环境错误（找不到 adb / 无设备）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PKG = "com.droplink.app"
PORT = 8848
SAMPLE_NAME = "验收样例.txt"
SAMPLE_BODY = "第一行 中文内容\nsecond line\n"
SAMPLE_DIR = "/sdcard/Documents"

results = []  # [(id, 名称, 状态, 说明)]


def record(cid, name, status, detail=""):
    results.append((cid, name, status, detail))
    tag = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP"}[status]
    print("[%s] %-4s %s%s" % (tag, cid, name, ("  — " + detail) if detail else ""), flush=True)


def find_adb() -> str:
    cands = [os.environ.get("ADB", "")]
    home = os.path.expanduser("~")
    cands += [
        os.path.join(home, "Desktop", "手机", "platform-tools", "adb.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk", "platform-tools", "adb.exe"),
        r"C:\Android\adb.exe",
        shutil.which("adb") or "",
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    raise SystemExit("找不到 adb（可用环境变量 ADB 指定）")


def run(args, timeout=60, binary=False):
    p = subprocess.run(args, capture_output=True, timeout=timeout)
    if binary:
        return p.returncode, p.stdout
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    return p.returncode, (out + err)


def adb_shell(adb, serial, cmd, timeout=60):
    return run([adb, "-s", serial, "shell", cmd], timeout=timeout)[1]


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http_get(port, path, timeout=15, want_bytes=False):
    url = "http://127.0.0.1:%d%s" % (port, path)
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return r.status, dict(r.headers), (body if want_bytes else body.decode("utf-8", "replace"))


def crash_blocks(adb, serial):
    """返回 crash buffer 中属于本应用的块数（用于「运行前后增量」判断）。"""
    txt = adb_shell(adb, serial, "logcat -d -b crash -v brief", timeout=60)
    blocks = re.split(r"^-{5,}", txt, flags=re.M)
    return sum(1 for b in blocks if PKG in b)


def main():
    ap = argparse.ArgumentParser(description="DropLink 设备端验收检测")
    ap.add_argument("--serial", default="", help="设备序列号，默认取第一台")
    ap.add_argument("--expect-version", default="", help="期望的 versionName，不一致判 FAIL")
    ap.add_argument("--outdir", default="probe_runs", help="结果输出目录")
    ap.add_argument("--no-ui", action="store_true", help="跳过界面抽检")
    ap.add_argument("--keep", action="store_true", help="保留样例文件")
    args = ap.parse_args()

    outdir = os.path.abspath(args.outdir)
    os.makedirs(outdir, exist_ok=True)
    started = time.time()

    # ---------- C1 设备 ----------
    adb = find_adb()
    code, out = run([adb, "devices"])
    serials = [ln.split("\t")[0] for ln in out.splitlines()[1:] if "\tdevice" in ln]
    if args.serial:
        if args.serial not in serials:
            print("设备 %s 不在线（在线：%s）" % (args.serial, serials or "无"))
            return 2
        serial = args.serial
    elif len(serials) == 1:
        serial = serials[0]
    elif not serials:
        print("没有处于 device 状态的设备")
        return 2
    else:
        print("检测到多台设备，请用 --serial 指定：%s" % serials)
        return 2
    model = adb_shell(adb, serial, "getprop ro.product.model").strip()
    sdk = adb_shell(adb, serial, "getprop ro.build.version.sdk").strip()
    record("C1", "设备在线", "PASS", "%s / API %s / %s" % (serial, sdk, model))

    # ---------- C2 版本 ----------
    dump = adb_shell(adb, serial, "dumpsys package %s | grep -E 'versionName|versionCode'" % PKG)
    m = re.search(r"versionName=(\S+)", dump)
    code_m = re.search(r"versionCode=(\d+)", dump)
    tgt = re.search(r"targetSdk=(\d+)", adb_shell(adb, serial, "dumpsys package %s | grep targetSdk" % PKG))
    if not m:
        record("C2", "版本与 targetSdk", "FAIL", "未读到 versionName（应用未安装？）")
        ver = ""
    else:
        ver = m.group(1)
        desc = "versionName=%s versionCode=%s targetSdk=%s" % (
            ver, code_m.group(1) if code_m else "?", tgt.group(1) if tgt else "?")
        if args.expect_version and ver != args.expect_version:
            record("C2", "版本与 targetSdk", "FAIL", "%s（期望 %s）" % (desc, args.expect_version))
        else:
            record("C2", "版本与 targetSdk", "PASS", desc)

    # ---------- C3 进程 ----------
    pid = adb_shell(adb, serial, "pidof %s" % PKG).strip()
    if pid:
        record("C3", "应用进程存活", "PASS", "pid=%s" % pid)
    else:
        record("C3", "应用进程存活", "FAIL", "进程不存在，先启动 App 再测")
        return finish(outdir, started, serial, ver, args)

    crash_before = crash_blocks(adb, serial)

    # 端口转发：让本机直接访问手机内置服务，不依赖局域网 IP
    lport = free_port()
    run([adb, "-s", serial, "forward", "tcp:%d" % lport, "tcp:%d" % PORT])
    time.sleep(0.5)

    try:
        # ---------- C4 服务 ----------
        try:
            st, _, body = http_get(lport, "/api/ping", timeout=8)
            ok = st == 200 and '"ok"' in body
            record("C4", "内置服务可达", "PASS" if ok else "FAIL", "HTTP %s %s" % (st, body[:60]))
        except Exception as exc:
            record("C4", "内置服务可达", "FAIL", "连接失败：%s" % exc)
            return finish(outdir, started, serial, ver, args)

        # ---------- C5 会话 ----------
        try:
            st, _, body = http_get(lport, "/api/session")
            sess = json.loads(body)
            rv = sess.get("version", "")
            recv = sess.get("recv_dir", "")
            ok = st == 200 and rv == ver and bool(recv)
            record("C5", "会话信息一致", "PASS" if ok else "FAIL",
                   "version=%s recv_dir=%s" % (rv, recv))
        except Exception as exc:
            record("C5", "会话信息一致", "FAIL", "解析失败：%s" % exc)

        # ---------- C6 列目录 ----------
        try:
            st, _, body = http_get(lport, "/api/list?path=/storage/emulated/0")
            d = json.loads(body)
            n = len(d.get("entries") or [])
            ok = st == 200 and d.get("ok") and n > 0
            record("C6", "根目录可列", "PASS" if ok else "FAIL", "%d 项（需全盘访问权限）" % n)
        except Exception as exc:
            record("C6", "根目录可列", "FAIL", "请求失败：%s" % exc)

        # ---------- 准备样例文件 ----------
        sample = "%s/%s" % (SAMPLE_DIR, SAMPLE_NAME)
        adb_shell(adb, serial, "mkdir -p %s" % SAMPLE_DIR)
        adb_shell(adb, serial, "printf '%s' > '%s'" % (SAMPLE_BODY.replace("\n", "\\n"), sample))
        quoted = urllib.parse.quote(sample)
        size_expected = len(SAMPLE_BODY.encode("utf-8"))

        # ---------- C7 预览 ----------
        try:
            st, _, body = http_get(lport, "/api/preview?path=" + quoted)
            d = json.loads(body)
            ok = st == 200 and d.get("ok") and d.get("text") == SAMPLE_BODY
            detail = "text=%r" % (d.get("text", "")[:20] if isinstance(d.get("text"), str) else d.get("text"))
            if d.get("ok") and d.get("text") != SAMPLE_BODY and d.get("text", "").strip() == SAMPLE_BODY.strip():
                ok = True
                detail += "（仅行尾空白差异）"
            record("C7", "文本预览返回正文", "PASS" if ok else "FAIL", detail)
        except Exception as exc:
            record("C7", "文本预览返回正文", "FAIL", "解析失败（可能仍在返回裸字节）：%s" % exc)

        # ---------- C8 错误路径 ----------
        try:
            st, _, body = http_get(lport, "/api/preview?path=" + urllib.parse.quote("/sdcard/不存在的文件_9f8e.txt"))
            d = json.loads(body)
            ok = d.get("ok") is False and bool(d.get("error"))
            record("C8", "不存在的文件报错", "PASS" if ok else "FAIL", str(d.get("error"))[:50])
        except urllib.error.HTTPError as exc:
            record("C8", "不存在的文件报错", "PASS", "HTTP %s" % exc.code)
        except Exception as exc:
            record("C8", "不存在的文件报错", "FAIL", "异常：%s" % exc)

        # ---------- C9 图标 ----------
        try:
            st, hdr, raw = http_get(lport, "/api/icon?size=64", want_bytes=True)
            ctype = hdr.get("Content-Type", "")
            ok = st == 200 and raw[:8] == b"\x89PNG\r\n\x1a\n" and "image/png" in ctype and len(raw) > 100
            record("C9", "图标为真实 PNG", "PASS" if ok else "FAIL",
                   "%s / %d 字节 / %s" % (st, len(raw), ctype))
        except Exception as exc:
            record("C9", "图标为真实 PNG", "FAIL", "请求失败：%s" % exc)

        # ---------- C10 下载 ----------
        try:
            st, hdr, raw = http_get(lport, "/api/download?path=" + quoted, want_bytes=True)
            clen = hdr.get("Content-Length", "")
            disp = hdr.get("Content-Disposition", "")
            ok = (st == 200 and len(raw) == size_expected
                  and clen == str(size_expected) and "filename*=UTF-8''" in disp)
            record("C10", "下载响应与中文名", "PASS" if ok else "FAIL",
                   "HTTP %s %d/%s 字节 filename*=%s" % (st, len(raw), clen or "?", "有" if "filename*" in disp else "无"))
        except Exception as exc:
            record("C10", "下载响应与中文名", "FAIL", "请求失败：%s" % exc)

        # ---------- C11 界面抽检 ----------
        if args.no_ui:
            record("C11", "界面底部导航", "SKIP", "--no-ui")
        else:
            adb_shell(adb, serial, "uiautomator dump /sdcard/_dc_ui.xml", timeout=90)
            xml = adb_shell(adb, serial, "cat /sdcard/_dc_ui.xml", timeout=60)
            if "<?xml" not in xml:
                record("C11", "界面底部导航", "SKIP",
                       "uiautomator 拿不到 idle（WebView 有常驻动画），改用截图人工确认")
            else:
                tabs = [t for t in ("浏览", "传文件", "剪贴板", "我的") if t in xml]
                ok = len(tabs) >= 3
                record("C11", "界面底部导航", "PASS" if ok else "FAIL", "命中 " + "/".join(tabs))
            adb_shell(adb, serial, "rm -f /sdcard/_dc_ui.xml")

            shot = os.path.join(outdir, "device_check.png")
            code, png = run([adb, "-s", serial, "exec-out", "screencap", "-p"], timeout=90, binary=True)
            if code == 0 and png[:8] == b"\x89PNG\r\n\x1a\n":
                open(shot, "wb").write(png)
                record("C12", "截图取证", "PASS", shot)
            else:
                record("C12", "截图取证", "FAIL", "screencap 失败")

        # ---------- C13 崩溃增量 ----------
        crash_after = crash_blocks(adb, serial)
        if crash_after == crash_before:
            record("C13", "运行期无新崩溃", "PASS", "crash buffer 块数 %d → %d" % (crash_before, crash_after))
        else:
            record("C13", "运行期无新崩溃", "FAIL", "crash buffer 块数 %d → %d，请查 logcat -b crash" % (crash_before, crash_after))

    finally:
        if not args.keep:
            adb_shell(adb, serial, "rm -f '%s'" % sample)
        run([adb, "-s", serial, "forward", "--remove", "tcp:%d" % lport])

    return finish(outdir, started, serial, ver, args)


def finish(outdir, started, serial, ver, args):
    n_pass = sum(1 for r in results if r[2] == "PASS")
    n_fail = sum(1 for r in results if r[2] == "FAIL")
    n_skip = sum(1 for r in results if r[2] == "SKIP")
    verdict = "不通过" if n_fail else ("通过（含跳过项）" if n_skip else "通过")
    print()
    print("=" * 62)
    print("结果：%d 通过 / %d 失败 / %d 跳过 → %s" % (n_pass, n_fail, n_skip, verdict))
    print("=" * 62)

    payload = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
        "duration": round(time.time() - started, 1),
        "serial": serial,
        "version": ver,
        "pass": n_pass, "fail": n_fail, "skip": n_skip,
        "verdict": verdict,
        "checks": [{"id": c, "name": n, "status": s, "detail": d} for c, n, s, d in results],
    }
    path = os.path.join(outdir, "device_check.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("结果文件：%s" % path)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
