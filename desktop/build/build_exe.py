# -*- coding: utf-8 -*-
"""
把 DropLink 打成单个独立 EXE。

原理：把「精简的 Python 运行时 + 应用代码」作为 payload 附在启动器 EXE 尾部，
运行时释放到 %LOCALAPPDATA%\\DropLink\\runtime 再拉起 pythonw.exe。
不依赖 PyInstaller（本机装不了），也不需要目标机器装 Python。

用法：  python build/build_exe.py [-o 输出路径]
"""

from __future__ import annotations

import compileall
import glob
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)

MAGIC = b"DLPAY001"

# 需要一起打包的扩展模块（已确认不依赖体积大的第三方 DLL）
PYDS = ["_socket.pyd", "_ctypes.pyd", "select.pyd", "unicodedata.pyd"]
EXTRA_DLLS = ["libffi-8.dll"]

# stdlib 里这些目录用不上，剔除后可以小一大截
LIB_EXCLUDE_DIRS = {
    "site-packages", "test", "tests", "idlelib", "tkinter", "turtle",
    "lib2to3", "ensurepip", "venv", "distutils", "unittest", "doctest",
    "pydoc_data", "wsgiref", "dbm", "sqlite3", "curses", "asyncio",
    "multiprocessing", "concurrent", "tcl", "tk", "Tools", "scripts",
}
LIB_EXCLUDE_FILES = {
    "pydoc.py", "turtle.py", "antigravity.py", "this.py",
}
LIB_EXCLUDE_SUFFIX = (".pyc", ".pyo", ".txt", ".rst", ".exe", ".dll")


def find_python() -> str:
    for c in [os.environ.get("DL_PYTHON_HOME"), sys.prefix,
              r"C:\Python313", r"C:\Python312"]:
        if c and os.path.isfile(os.path.join(c, "pythonw.exe")):
            return c
    raise SystemExit("找不到 Python 安装目录，可用 DL_PYTHON_HOME 指定")


def find_gxx() -> str:
    for c in ["g++", r"E:\DevTools\msys64\mingw64\bin\g++.exe"]:
        try:
            out = subprocess.run([c, "--version"], capture_output=True, timeout=30)
            if out.returncode == 0:
                # 必须返回绝对路径：find_windres 会在同目录找 windres.exe，
                # 之前返回裸 "g++" 导致 dirname 为空、windres 永远找不到，
                # 结果 EXE 里没有图标资源（资源管理器显示成系统默认空白图标）。
                return shutil.which(c) or c
        except Exception:
            continue
    raise SystemExit("找不到 g++")


def find_windres(gxx: str):
    cand = os.path.join(os.path.dirname(gxx), "windres.exe")
    if os.path.isfile(cand):
        return cand
    # 兜底：直接从 PATH 找
    return shutil.which("windres")


def log(msg):
    print("[build] " + msg, flush=True)


# ---------------------------------------------------------------- stdlib

def build_stdlib_zip(pyhome: str, stage: str) -> str:
    src = os.path.join(stage, "libsrc")
    if os.path.isdir(src):
        shutil.rmtree(src, ignore_errors=True)
    os.makedirs(src)

    kept = 0
    for root, dirs, files in os.walk(os.path.join(pyhome, "Lib")):
        rel = os.path.relpath(root, os.path.join(pyhome, "Lib"))
        parts = [] if rel == "." else rel.split(os.sep)
        if any(p in LIB_EXCLUDE_DIRS or p == "__pycache__" for p in parts):
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs
                   if d not in LIB_EXCLUDE_DIRS and d != "__pycache__"]
        for f in files:
            if f in LIB_EXCLUDE_FILES or f.endswith(LIB_EXCLUDE_SUFFIX):
                continue
            s = os.path.join(root, f)
            d = os.path.join(src, rel if rel != "." else "", f)
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(s, d)
            kept += 1
    log(f"stdlib 保留 {kept} 个文件，开始预编译…")

    compileall.compile_dir(src, quiet=2, workers=8, force=True, legacy=False)
    npyc = len(glob.glob(os.path.join(src, "**", "*.pyc"), recursive=True))
    log(f"预编译完成，{npyc} 个 .pyc")

    zpath = os.path.join(stage, "python313.zip")
    if os.path.exists(zpath):
        os.remove(zpath)
    n = 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for root, dirs, files in os.walk(src):
            for f in files:
                if f.endswith(".pyc") or f.endswith(".py"):
                    full = os.path.join(root, f)
                    arc = os.path.relpath(full, src).replace(os.sep, "/")
                    z.write(full, arc)
                    n += 1
    log(f"stdlib.zip -> {n} 项，{os.path.getsize(zpath)/1e6:.1f} MB")
    return zpath


# ---------------------------------------------------------------- payload

def collect_files(pyhome: str, stage: str):
    """返回 [(相对路径, 本地绝对路径)]"""
    out = []
    runtime = {
        "pythonw.exe": os.path.join(pyhome, "pythonw.exe"),
        "python313.dll": os.path.join(pyhome, "python313.dll"),
        "vcruntime140.dll": os.path.join(pyhome, "vcruntime140.dll"),
        "python313.zip": os.path.join(stage, "python313.zip"),
    }
    if os.path.isfile(os.path.join(pyhome, "python3.dll")):
        runtime["python3.dll"] = os.path.join(pyhome, "python3.dll")
    if os.path.isfile(os.path.join(pyhome, "vcruntime140_1.dll")):
        runtime["vcruntime140_1.dll"] = os.path.join(pyhome, "vcruntime140_1.dll")

    for k, v in runtime.items():
        if not os.path.isfile(v):
            raise SystemExit("缺少运行时文件：" + v)
        out.append((k, v))

    dlls = os.path.join(pyhome, "DLLs")
    for name in PYDS + EXTRA_DLLS:
        p = os.path.join(dlls, name)
        if not os.path.isfile(p):
            raise SystemExit("缺少扩展模块：" + name)
        out.append((name, p))

    # 应用代码
    for name in ["app_main.py", "server.py", "qrmini.py", "gui.py", "README.md"]:
        p = os.path.join(APP, name)
        if os.path.isfile(p):
            out.append(("app/" + name, p))
    web = os.path.join(APP, "web")
    for root, dirs, files in os.walk(web):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            full = os.path.join(root, f)
            arc = "app/web/" + os.path.relpath(full, web).replace(os.sep, "/")
            out.append((arc, full))
    return out


def write_payload(files, build_id: str, path: str):
    total = 0
    with open(path, "wb") as out:
        def put(rel: str, data: bytes):
            nonlocal total
            nb = rel.encode("utf-8")
            out.write(struct.pack("<I", len(nb)))
            out.write(nb)
            out.write(struct.pack("<Q", len(data)))
            out.write(data)
            total += 4 + len(nb) + 8 + len(data)

        put(".buildid", build_id.encode("utf-8"))
        for rel, src in files:
            with open(src, "rb") as f:
                put(rel, f.read())
        out.write(MAGIC)
        out.write(struct.pack("<Q", total))
    log(f"payload {total/1e6:.1f} MB")
    return total


# ---------------------------------------------------------------- 图标

def make_icon(path: str):
    """生成多尺寸 ICO（16~256px）。

    同一张图同时用于：EXE 文件图标（资源管理器/任务栏） + 运行时窗口图标 + 托盘图标，
    保证桌面端各处与手机 App 的图标完全一致。
    """
    try:
        import qrmini
    except Exception as e:
        log("图标生成失败：%r" % (e,))
        return None

    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = []
    for s in sizes:
        try:
            images.append((s, qrmini.icon_png_bytes(s)))
        except Exception as e:
            log(f"  {s}px 图标生成失败：{e!r}")
    if not images:
        return None

    # ICO 容器内直接放 PNG（Vista+ 原生支持；多尺寸可避免小图标被缩放糊掉）
    n = len(images)
    hdr = struct.pack("<HHH", 0, 1, n)
    offset = 6 + 16 * n
    entries = b""
    blobs = b""
    for s, png in images:
        dim = 0 if s >= 256 else s          # 0 表示 256
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
        blobs += png
    with open(path, "wb") as f:
        f.write(hdr + entries + blobs)
    log(f"图标 {n} 个尺寸（{images[0][0]}~{images[-1][0]}px）共 {os.path.getsize(path)} B")
    return path


def pe_icon_count(path: str) -> int:
    """统计 PE 资源表里的 RT_ICON(3) 条目数，用来校验图标是否真的嵌进去了。"""
    try:
        d = open(path, "rb").read()
        pe = struct.unpack_from("<I", d, 0x3C)[0]
        nsec = struct.unpack_from("<H", d, pe + 6)[0]
        opt_size = struct.unpack_from("<H", d, pe + 20)[0]
        opt = pe + 24
        is64 = struct.unpack_from("<H", d, opt)[0] == 0x20B
        dd = opt + (112 if is64 else 96)
        res_rva, _ = struct.unpack_from("<II", d, dd + 16)
        if not res_rva:
            return 0
        secs = []
        so = opt + opt_size
        for i in range(nsec):
            s = so + i * 40
            vsize, vaddr, rawsize, rawaddr = struct.unpack_from("<IIII", d, s + 8)
            secs.append((vaddr, max(vsize, rawsize), rawaddr))

        def r2o(rva):
            for vaddr, size, raw in secs:
                if vaddr <= rva < vaddr + size:
                    return raw + (rva - vaddr)
            return None

        base = r2o(res_rva)
        cnt = 0

        def walk(off, level, type_id):
            nonlocal cnt
            nn, ni = struct.unpack_from("<HH", d, off + 12)
            for i in range(nn + ni):
                name_id, doff = struct.unpack_from("<II", d, off + 16 + i * 8)
                if level == 0:
                    walk(base + (doff & 0x7FFFFFFF), 1, name_id & 0xFFFF)
                elif level == 1:
                    walk(base + (doff & 0x7FFFFFFF), 2, type_id)
                elif type_id == 3:
                    cnt += 1

        walk(base, 0, 0)
        return cnt
    except Exception:
        return 0


# ---------------------------------------------------------------- 主流程

def main():
    out_path = None
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a in ("-o", "--out") and i + 1 < len(args):
            out_path = args[i + 1]
    if not out_path:
        out_path = os.path.join(os.path.dirname(APP), "DropLink.exe")
    out_path = os.path.abspath(out_path)

    pyhome = find_python()
    gxx = find_gxx()
    windres = find_windres(gxx)
    log(f"Python: {pyhome}")
    log(f"编译器: {gxx}")

    stage = os.path.join(HERE, "_stage")
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage, exist_ok=True)

    build_stdlib_zip(pyhome, stage)
    files = collect_files(pyhome, stage)

    # 图标：生成一次，既用于 EXE 资源，也放进运行时 app/ 供托盘/窗口加载（保持图标风格统一）
    ico = make_icon(os.path.join(stage, "icon.ico"))
    if ico:
        files.append(("app/icon.ico", ico))

    digest = hashlib.sha256()
    for rel, src in files:
        digest.update(rel.encode("utf-8"))
        digest.update(str(os.path.getsize(src)).encode())
    build_id = time.strftime("%Y%m%d-%H%M%S") + "-" + digest.hexdigest()[:12]

    payload = os.path.join(stage, "payload.bin")
    write_payload(files, build_id, payload)

    # 图标 + 资源（icon.ico 已在收集文件时生成，这里直接复用）
    obj = None
    if ico and windres:
        rc = os.path.join(stage, "icon.rc")
        ico_abs = os.path.abspath(ico).replace("\\", "/")
        with open(rc, "w", encoding="utf-8") as f:
            f.write('1 ICON "%s"\n' % ico_abs)
        obj = os.path.join(stage, "icon_res.o")
        # 在 stage 目录里跑，rc 用绝对路径，双重保证能找到图标文件
        r = subprocess.run([windres, "icon.rc", "-O", "coff", "-o", "icon_res.o"],
                           cwd=stage, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.isfile(obj):
            log("windres 失败：" + ((r.stderr or r.stdout or "")[:300]))
            obj = None
        else:
            log(f"图标资源编译完成 {os.path.getsize(obj)} B")
    elif ico:
        log("⚠️ 未找到 windres，EXE 将没有图标（检查 MinGW 安装）")
    else:
        log("⚠️ 图标生成失败，EXE 将没有图标")

    exe_tmp = os.path.join(stage, "launcher.exe")
    cmd = [gxx, "-O2", "-mwindows", "-municode", "-static",
           "-static-libgcc", "-static-libstdc++", "-s",
           os.path.join(HERE, "launcher.cpp")]
    if obj:
        cmd.append(obj)
    cmd += ["-lshlwapi", "-lshell32", "-lole32", "-lcomctl32", "-lgdi32",
            "-o", exe_tmp]
    log("编译启动器…")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-3000:])
        print(r.stderr[-3000:])
        raise SystemExit("启动器编译失败")
    log(f"启动器 {os.path.getsize(exe_tmp)/1024:.0f} KB")
    icons = pe_icon_count(exe_tmp)
    if icons:
        log(f"EXE 图标资源：{icons} 张（16~256px）")
    else:
        log("⚠️ EXE 未嵌入图标资源，资源管理器会显示系统默认图标")

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as out, open(exe_tmp, "rb") as a, open(payload, "rb") as b:
        shutil.copyfileobj(a, out)
        shutil.copyfileobj(b, out)
    log(f"完成：{out_path}  ({os.path.getsize(out_path)/1e6:.1f} MB)")
    print("BUILD_ID=" + build_id)


if __name__ == "__main__":
    main()
