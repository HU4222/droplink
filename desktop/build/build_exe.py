# -*- coding: utf-8 -*-
"""
把 DropLink 打成单个独立 EXE。

原理：把「精简的 Python 运行时 + 应用代码」作为 payload 附在启动器 EXE 尾部，
运行时释放到 %LOCALAPPDATA%\\DropLink\\runtime 再拉起 pythonw.exe。
不依赖 PyInstaller（本机装不了），也不需要目标机器装 Python。

用法：
    python build/build_exe.py [--edition personal|secure] [-o 输出路径]

分版（1.5.0）：
    --edition personal（默认）  个人版，不收集 dlcore/sec/，产物默认 DropLink.exe
    --edition secure            安全版，收集 dlcore/sec/，产物默认 DropLink-Secure.exe
两种产物共用同一份代码与版本号，区别只在：打包的安全模块 + 构建期烤入的
EDITION 常量（dlcore/_edition_baked.py）+ VERSIONINFO 的 ProductName/FileDescription。
"""

from __future__ import annotations

import compileall
import glob
import hashlib
import os
import re
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

# 版本身份 -> 产物信息（名字与 VERSIONINFO 两处必须一起改）
EDITIONS = {
    "personal": {
        "product": "DropLink Personal",
        "exe": "DropLink.exe",
        "description": "DropLink Personal - LAN file transfer",
    },
    "secure": {
        "product": "DropLink Secure",
        "exe": "DropLink-Secure.exe",
        "description": "DropLink Secure - LAN file transfer",
    },
}

# 需要一起打包的扩展模块（已确认不依赖体积大的第三方 DLL）
PYDS = ["_socket.pyd", "_ctypes.pyd", "select.pyd", "unicodedata.pyd"]
EXTRA_DLLS = ["libffi-8.dll"]

# 安全版额外需要的 TLS 运行时：Python 的 ssl 模块依赖 _ssl.pyd + OpenSSL。
# 只有安全版打包（个人版一行 ssl 都不 import），否则产物会白胖 ~10 MB。
SECURE_PYDS = ["_ssl.pyd"]
SECURE_DLL_GLOBS = ["libssl-3*.dll", "libcrypto-3*.dll"]

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


def python_tag(pyhome: str) -> str:
    """返回运行时 ABI 标签（"312" / "313"），用于 python3XX.dll 与 python3XX.zip。

    原先写死 313；换成其它 Python（如内置的 3.12）构建时会找不到 DLL、
    且 stdlib 压缩包名字对不上，导致产物启动即崩。这里按实际文件推断。
    """
    try:
        names = os.listdir(pyhome)
    except OSError:
        names = []
    for name in names:
        m = re.fullmatch(r"python(3\d+)\.dll", name, re.I)
        if m:
            return m.group(1)
    raise SystemExit("在 %s 里找不到 python3XX.dll，无法确定运行时版本" % pyhome)


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
    """构建日志。控制台可能是 GBK（中文 Windows），emoji/生僻字会抛
    UnicodeEncodeError 把「干净报错」变成 traceback，所以这里退化成可替换编码。"""
    try:
        print("[build] " + msg, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.write(("[build] " + msg).encode(enc, "replace").decode(enc, "replace") + "\n")
        sys.stdout.flush()


# ---------------------------------------------------------------- stdlib

def build_stdlib_zip(pyhome: str, stage: str, tag: str) -> str:
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

    try:
        compileall.compile_dir(src, quiet=2, workers=8, force=True, legacy=False)
    except (PermissionError, OSError) as exc:
        # 受限环境（沙箱/拒绝创建命名管道）里进程池起不来；退回单进程编译，
        # 只是慢几秒，结果一致。workers=1 才是 compileall 的串行分支。
        log("进程池不可用（%s），改为单进程预编译…" % type(exc).__name__)
        compileall.compile_dir(src, quiet=2, workers=1, force=True, legacy=False)
    npyc = len(glob.glob(os.path.join(src, "**", "*.pyc"), recursive=True))
    log(f"预编译完成，{npyc} 个 .pyc")

    zpath = os.path.join(stage, "python%s.zip" % tag)
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

def collect_files(pyhome: str, stage: str, edition: str):
    """返回 [(相对路径, 本地绝对路径)]"""
    tag = python_tag(pyhome)
    out = []
    runtime = {
        "pythonw.exe": os.path.join(pyhome, "pythonw.exe"),
        "python%s.dll" % tag: os.path.join(pyhome, "python%s.dll" % tag),
        "vcruntime140.dll": os.path.join(pyhome, "vcruntime140.dll"),
        "python%s.zip" % tag: os.path.join(stage, "python%s.zip" % tag),
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

    # 安全版：TLS 运行时（ssl 模块的 C 扩展 + OpenSSL）
    if edition == "secure":
        for name in SECURE_PYDS:
            p = os.path.join(dlls, name)
            if not os.path.isfile(p):
                raise SystemExit("安全版缺少 TLS 扩展模块：" + name)
            out.append((name, p))
        for pattern in SECURE_DLL_GLOBS:
            found = sorted(glob.glob(os.path.join(dlls, pattern)))
            if not found:
                raise SystemExit("安全版缺少 OpenSSL 运行库：" + pattern)
            for p in found:
                out.append((os.path.basename(p), p))

    # 应用代码
    for name in ["app_main.py", "server.py", "qrmini.py", "gui.py", "README.md"]:
        p = os.path.join(APP, name)
        if os.path.isfile(p):
            out.append(("app/" + name, p))

    # 服务端模块包（1.4.1 起 server.py 拆成 dlcore/*.py）
    pkg = os.path.join(APP, "dlcore")
    if os.path.isdir(pkg):
        for f in sorted(os.listdir(pkg)):
            if not f.endswith(".py"):
                continue
            out.append(("app/dlcore/" + f, os.path.join(pkg, f)))
    # 安全版专用模块：只有 secure 构建才收集。
    # 个人版包里不允许出现 dlcore/sec/ —— 这是分版的核心隔离点，别改成「总是收集」。
    if edition == "secure":
        sec = os.path.join(pkg, "sec")
        if not os.path.isdir(sec):
            raise SystemExit("安全版构建缺少 dlcore/sec/ 目录")
        for f in sorted(os.listdir(sec)):
            if f.endswith(".py"):
                out.append(("app/dlcore/sec/" + f, os.path.join(sec, f)))
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


# ---------------------------------------------------------------- 发布自检 / 校验和

# 真实私钥长这样：BEGIN 行之后紧跟至少一行 64+ 字符的 base64。
# OpenSSL DLL 里的 "<INVALID PRIVATE KEY>" 之类没有 base64 行，不会误报（S-15）。
_PRIVATE_KEY_RE = re.compile(
    rb"-----BEGIN [A-Z0-9 ]{0,32}PRIVATE KEY-----\r?\n(?:[A-Za-z0-9+/=]{64,}\r?\n)+")

SUMS_NAME = "SHA256SUMS.txt"


def read_payload_entries(path: str):
    """读回 payload.bin，返回 [(arcname, bytes), ...]（构建后自检用）。"""
    with open(path, "rb") as f:
        data = f.read()
    out, pos = [], 0
    while pos + 8 <= len(data):
        if data[pos:pos + 8] == MAGIC:
            break
        n = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        name = data[pos:pos + n].decode("utf-8")
        pos += n
        size = struct.unpack_from("<Q", data, pos)[0]
        pos += 8
        out.append((name, data[pos:pos + size]))
        pos += size
    return out


def _source_version() -> str:
    cfg = os.path.join(APP, "dlcore", "config.py")
    with open(cfg, "r", encoding="utf-8") as f:
        m = re.search(r'VERSION = "([0-9.]+)"', f.read())
    if not m:
        raise SystemExit("无法从 dlcore/config.py 读取 VERSION")
    return m.group(1)


def _baseline_version() -> str:
    """发布基线：仓库根目录 VERSION 文件（独立于 config.py）。"""
    path = os.path.join(APP, "VERSION")
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            text = f.read().strip()
    except OSError:
        raise SystemExit("缺少发布基线文件 VERSION（仓库根目录，一行版本号）")
    if not re.fullmatch(r"\d+\.\d+\.\d+", text):
        raise SystemExit("VERSION 文件格式不对：%r" % text)
    return text


def check_version_baseline(version: str, baseline: str) -> None:
    """版本漂移检查：config / README / EDITIONS 都必须等于 VERSION 文件（S-15）。"""
    errors = []
    if version != baseline:
        errors.append("源码版本 %s 与发布基线 %s 不一致" % (version, baseline))
    for name in ("README.md", os.path.join("docs", "EDITIONS.md")):
        path = os.path.join(APP, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            errors.append("%s 不存在" % name)
            continue
        if baseline not in text:
            errors.append("%s 未同步发布基线版本 %s" % (name, baseline))
    if errors:
        log("❌ 版本基线检查失败：")
        for e in errors:
            log("   - " + e)
        raise SystemExit(1)
    log("版本基线检查通过：%s（VERSION / config / README / EDITIONS）" % baseline)


def selfcheck_payload(exe_path: str, payload_path: str, edition: str, version: str) -> None:
    """发布前自检：任一条不满足直接让构建失败（S-15）。"""
    entries = read_payload_entries(payload_path)
    names = [n for n, _ in entries]
    errors = []

    # 1) 不能把运行期状态/证书/日志打进包
    forbidden = []
    for n in names:
        low = n.lower()
        if low.endswith(".log") or "trusted_devices.json" in low:
            forbidden.append(n)
        elif n.startswith("app/config/") or n.startswith("app/.certs/") or "/.certs/" in low:
            forbidden.append(n)
    if forbidden:
        errors.append("payload 含运行期文件：%s" % forbidden[:5])

    # 只允许 .py、web/、白名单资源，以及启动器运行时（pythonw/dll/zip/pyd）
    whitelist = {"app/icon.ico", "app/README.md", ".buildid"}
    runtime_ok = re.compile(
        r"^(?:pythonw?\.exe|python\d*\.(?:dll|zip)|vcruntime[\w.]*\.dll|"
        r"lib[\w.-]+\.dll|[\w-]+\.pyd)$", re.I)
    odd = [n for n in names
           if not (n in whitelist or n.endswith(".py") or n.startswith("app/web/")
                   or (not n.startswith("app/") and runtime_ok.match(n)))]
    if odd:
        errors.append("payload 含白名单外文件：%s" % odd[:5])

    # 2) 个人版不能带 sec/ 模块
    if edition == "personal":
        sec = [n for n in names if n.startswith("app/dlcore/sec/")]
        if sec:
            errors.append("个人版 payload 含 sec/ 模块：%s" % sec[:5])

    # 3) 产物里不能有真实私钥
    with open(exe_path, "rb") as f:
        blob = f.read()
    if _PRIVATE_KEY_RE.search(blob):
        errors.append("产物里发现 PEM 私钥")

    # 4) 版本一致性：源码 / payload 内 config.py / EXE VERSIONINFO
    baked = dict(entries).get("app/dlcore/config.py", b"").decode("utf-8", "replace")
    m = re.search(r'VERSION = "([0-9.]+)"', baked)
    if not m:
        errors.append("payload 里缺 dlcore/config.py 或 VERSION")
    else:
        if m.group(1) != version:
            errors.append("payload VERSION=%s 与源码 %s 不一致" % (m.group(1), version))
        if version.encode("utf-16-le") not in blob:
            errors.append("EXE VERSIONINFO 里找不到 %s（版本漂移）" % version)

    # 5) 版本身份文件要对
    baked_ed = dict(entries).get("app/dlcore/_edition_baked.py", b"").decode("utf-8", "replace")
    if ('EDITION = "%s"' % edition) not in baked_ed:
        errors.append("_edition_baked.py 不是 %r：%r" % (edition, baked_ed[:80]))

    if errors:
        log("❌ 发布自检失败：")
        for e in errors:
            log("   - " + e)
        raise SystemExit(1)
    log("发布自检通过：%d 个文件，无运行期文件/私钥，版本与版本身份一致" % len(names))


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def write_sums(dist_dir: str) -> str:
    """生成/更新 dist/SHA256SUMS.txt：`HASH  文件名  (字节数)`。"""
    names = ["DropLink.exe", "DropLink-Secure.exe"]
    lines = []
    for name in names:
        p = os.path.join(dist_dir, name)
        if os.path.isfile(p):
            lines.append("%s  %s  (%d)" % (_sha256(p), name, os.path.getsize(p)))
    lines.append("# APK 由安卓工程产出，不在本仓库构建")
    text = "\n".join(lines) + "\n"
    path = os.path.join(dist_dir, SUMS_NAME)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)

    # 写完后立刻自校验：防止格式/哈希写错
    for name in names:
        p = os.path.join(dist_dir, name)
        if not os.path.isfile(p):
            continue
        if ("%s  %s  (%d)" % (_sha256(p), name, os.path.getsize(p))) not in text:
            raise SystemExit("SHA256SUMS.txt 与实际文件不一致：%s" % name)
    log("校验和已写入：%s（%d 个产物）" % (path, len(lines) - 1))
    return path


# ---------------------------------------------------------------- 主流程

def main():
    out_path = None
    edition = "personal"
    args = sys.argv[1:]
    if "--sums" in args:
        # 只重算 dist/SHA256SUMS.txt，不构建（发布前核对用）
        path = write_sums(os.path.join(APP, "dist"))
        print("SUMS=" + path)
        return
    for i, a in enumerate(args):
        if a in ("-o", "--out") and i + 1 < len(args):
            out_path = args[i + 1]
        elif a in ("--edition",) and i + 1 < len(args):
            edition = args[i + 1].strip().lower()
    if edition not in EDITIONS:
        raise SystemExit("--edition 只能是 personal 或 secure（收到：%s）" % edition)
    meta = EDITIONS[edition]
    if not out_path:
        out_path = os.path.join(os.path.dirname(APP), meta["exe"])
    out_path = os.path.abspath(out_path)
    version = _source_version()
    baseline = _baseline_version()
    check_version_baseline(version, baseline)
    version = baseline   # 后续 VERSIONINFO / 自检统一用发布基线

    pyhome = find_python()
    gxx = find_gxx()
    windres = find_windres(gxx)
    log(f"版本身份: {edition}（{meta['product']}）")
    log(f"Python: {pyhome}（ABI {python_tag(pyhome)}）")
    log(f"编译器: {gxx}")

    stage = os.path.join(HERE, "_stage")
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage, exist_ok=True)

    build_stdlib_zip(pyhome, stage, python_tag(pyhome))
    files = collect_files(pyhome, stage, edition)

    # 构建期烤入版本身份：运行时 config.py 优先读这个文件（见 dlcore/config.py）。
    # 只写进 payload，不落回源码目录，避免源码运行被上一次构建「污染」。
    baked = os.path.join(stage, "_edition_baked.py")
    with open(baked, "w", encoding="utf-8", newline="\n") as f:
        f.write("# -*- coding: utf-8 -*-\n"
                '"""构建期生成，勿手改：本产物的版本身份。"""\n\n'
                'EDITION = "%s"\n' % edition)
    files.append(("app/dlcore/_edition_baked.py", baked))

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
        # 图标必须用**相对路径**引用：msys2 的 windres 按 ANSI 代码页读 .rc，
        # 工程路径里的中文会被读成乱码，导致 "can't open icon file"。
        # icon.ico 就在 stage 里、windres 的 cwd 也是 stage，写 "icon.ico" 即可。
        # 版本资源：CompanyName 是 G-16 要求的署名为之一（资源管理器属性里可见）
        # 1.4.1 起 VERSION 定义在 dlcore/config.py，这里直接用 main() 读到的同一个值
        _ver = version
        _parts = (_ver.split(".") + ["0", "0", "0", "0"])[:4]
        _v4 = ",".join(str(int(x)) for x in _parts)
        with open(rc, "w", encoding="utf-8") as f:
            f.write('1 ICON "icon.ico"\n')
            f.write(
                '1 VERSIONINFO\n'
                'FILEVERSION %s\n'
                'PRODUCTVERSION %s\n'
                'FILEOS 0x4L\n'
                'FILETYPE 0x1L\n'
                'BEGIN\n'
                '  BLOCK "StringFileInfo"\n'
                '  BEGIN\n'
                '    BLOCK "040904B0"\n'
                '    BEGIN\n'
                '      VALUE "CompanyName", "HU4222"\n'
                '      VALUE "FileDescription", "%s"\n'
                '      VALUE "FileVersion", "%s"\n'
                '      VALUE "InternalName", "DropLink"\n'
                '      VALUE "LegalCopyright", "(c) 2026 HU4222"\n'
                '      VALUE "OriginalFilename", "%s"\n'
                '      VALUE "ProductName", "%s"\n'
                '      VALUE "ProductVersion", "%s"\n'
                '    END\n'
                '  END\n'
                '  BLOCK "VarFileInfo"\n'
                '  BEGIN\n'
                '    VALUE "Translation", 0x409, 1200\n'
                '  END\n'
                'END\n' % (_v4, _v4, meta["description"], _ver, meta["exe"],
                           meta["product"], _ver)
            )
        obj = os.path.join(stage, "icon_res.o")
        r = subprocess.run([windres, "icon.rc", "-O", "coff", "-o", "icon_res.o"],
                           cwd=stage, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
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
    # 启动器源码拷进 stage、全程用相对路径编译：msys2 的 ld 打不开含中文的
    # 绝对输出路径（报 cannot open output file），而 stage 的父目录路径里的中文
    # 只出现在 cwd 里，对 ld 是透明的。
    shutil.copyfile(os.path.join(HERE, "launcher.cpp"),
                    os.path.join(stage, "launcher.cpp"))
    # 安全版用独立的运行时目录与互斥体：两版可同机共存，
    # 且个人版的运行目录里不会残留 sec/ 代码（见 launcher.cpp 顶部宏说明）。
    defines = []
    if edition == "secure":
        defines = ['-DDL_RUNTIME_SUBDIR=L"runtime-secure"',
                   '-DDL_MUTEX_NAME=L"DropLinkSingleton_Secure_1"']
    else:
        # 个人版：旧 runtime 目录迁移 + 清掉任何残留的 dlcore/sec
        defines = ['-DDL_MIGRATE_LEGACY=1', '-DDL_STRIP_SEC=1']
    cmd = [gxx, "-O2", "-mwindows", "-municode", "-static",
           "-static-libgcc", "-static-libstdc++", "-s"] + defines + ["launcher.cpp"]
    if obj:
        cmd.append("icon_res.o")
    cmd += ["-lshlwapi", "-lshell32", "-lole32", "-lcomctl32", "-lgdi32",
            "-o", "launcher.exe"]
    log("编译启动器…")
    r = subprocess.run(cmd, cwd=stage, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
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
    # 发布自检：不通过就构建失败（退出码非 0），不允许「警告后继续」
    selfcheck_payload(out_path, payload, edition, version)
    write_sums(os.path.dirname(out_path))
    print("BUILD_ID=" + build_id)
    print("BUILD_EDITION=" + edition)


if __name__ == "__main__":
    main()
