#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DropLink 互传 —— 局域网直连文件互传（电脑 <-> 手机）

· 电脑运行本脚本，手机连同一个 Wi-Fi（或电脑开热点）后用浏览器打开地址
· 全程走局域网，不经互联网，不消耗手机流量
· 手机可浏览/下载/预览电脑文件，也可把照片、文件传到电脑
· 电脑可生成二维码，手机扫码直接下载某个文件

仅使用 Python 标准库；qrcode / pillow 存在时自动启用二维码与缩略图。
"""

from __future__ import annotations

import argparse
import ctypes
import gzip
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import webbrowser
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# 基础常量
# --------------------------------------------------------------------------

APP_NAME = "DropLink 互传"
APP_SHORT = "DropLink"
VERSION = "1.3.1"

# 个人使用模式（默认）：恢复安全加固前的宽松行为——
# 本机请求免配对、可浏览任意目录、6 位配对码、上传不写 MOTW。
# 需要重新打开加固：启动时加 --secure，或设置环境变量 DROPLINK_SECURE=1。
SECURE_MODE = os.environ.get("DROPLINK_SECURE") == "1"

# Windows 下隐藏 netsh/powershell 子进程的控制台窗口，避免启动/诊断时闪黑窗
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(APP_DIR, "web")
VENDOR_DIR = os.path.join(APP_DIR, "vendor")
CACHE_DIR = os.path.join(APP_DIR, ".cache", "thumbs")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

if os.path.isdir(VENDOR_DIR) and VENDOR_DIR not in sys.path:
    sys.path.insert(0, VENDOR_DIR)

mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("image/avif", ".avif")
mimetypes.add_type("image/heic", ".heic")
mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/quicktime", ".mov")
mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("audio/mpeg", ".mp3")
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("application/pdf", ".pdf")

# 正在进行的下载（供 /api/live 实时读取速率）
_LIVE = {}
_LIVE_LOCK = threading.Lock()

# 静态资源内存缓存：(路径, mtime_ns, 大小) -> (原文, gzip 后或 None)
# 目的是避免每次刷新页面都把 app.js/styles.css 重新读盘 + 重新压缩。
_STATIC_CACHE = {}


def _static_asset(path, st, compressible):
    key = (path, st.st_mtime_ns, st.st_size)
    hit = _STATIC_CACHE.get(key)
    if hit is not None:
        return hit
    with open(path, "rb") as f:
        raw = f.read()
    gz = None
    if compressible and len(raw) > 1024:
        try:
            gz = gzip.compress(raw, 6)
            if len(gz) >= len(raw):
                gz = None
        except Exception:
            gz = None
    if len(_STATIC_CACHE) > 16:
        _STATIC_CACHE.clear()
    _STATIC_CACHE[key] = (raw, gz)
    return raw, gz

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif",
             ".svg", ".ico", ".avif", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".3gp", ".ts"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".amr", ".opus"}
TEXT_EXT = {".txt", ".md", ".markdown", ".json", ".log", ".csv", ".tsv", ".xml",
            ".ini", ".cfg", ".conf", ".py", ".js", ".ts", ".html", ".htm", ".css",
            ".yaml", ".yml", ".bat", ".ps1", ".sh", ".c", ".cpp", ".h", ".java",
            ".sql", ".toml", ".env", ".gitignore"}
ARCHIVE_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso"}
DOC_EXT = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".wps", ".et", ".dps", ".pdf"}

SKIP_NAMES = {
    "$RECYCLE.BIN", "System Volume Information", "pagefile.sys", "swapfile.sys",
    "hiberfil.sys", "DumpStack.log", "DumpStack.log.tmp", "desktop.ini",
    "Config.Msi", "MSOCache", "Recovery", "Boot", "BOOTSECT.BAK", "$WinREAgent",
}

CHUNK = 512 * 1024

# ---- 安全基线（安全评估报告 DL-01 / DL-03 / DL-09）-------------------------

# 本机界面令牌：只有程序自己打开的窗口才带这个值。
# 「来源是 127.0.0.1」不再等于「已认证」——安卓同机应用、DNS 重绑定页面都拿不到它。
# 测试时可用环境变量指定，方便自动化验证；正常运行每次随机。
LOCAL_TOKEN = os.environ.get("DROPLINK_LOCAL_TOKEN") or secrets.token_urlsafe(24)

# 会话：30 天绝对过期 + 7 天不活跃过期（Cookie 同步为 30 天）
SESSION_ABS_TTL = 30 * 86400
SESSION_IDLE_TTL = 7 * 86400
MAX_SESSIONS = 64
MAX_FAIL_IPS = 4096
MAX_PEERS = 256

# Host 白名单缓存（网卡 IP 会变，10 秒刷新）
_HOST_CACHE = {"ts": 0.0, "hosts": set()}
_HOST_CACHE_LOCK = threading.Lock()

# 受保护的系统位置：不允许通过 /api/* 读写，避免被当成「以当前用户身份运行的
# 系统文件/SMB 代理」。产品只承诺共享用户自己的文件。
_SENSITIVE_ROOTS = None


def _sensitive_roots():
    global _SENSITIVE_ROOTS
    if _SENSITIVE_ROOTS is not None:
        return _SENSITIVE_ROOTS
    roots = []

    def add(p):
        if not p:
            return
        try:
            p = os.path.abspath(p)
        except Exception:
            return
        if p and p not in roots:
            roots.append(p)

    win = os.environ.get("WINDIR") or r"C:\Windows"
    add(win)
    add(os.path.join(win, "System32"))
    add(os.path.join(win, "SysWOW64"))
    for key in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        add(os.environ.get(key, ""))
    add(os.environ.get("APPDATA", ""))
    add(os.environ.get("LOCALAPPDATA", ""))
    home = os.path.expanduser("~")
    add(os.path.join(home, ".ssh"))
    add(os.path.join(home, ".gnupg"))
    add(os.path.join(home, "AppData"))
    # 启动项 / 开始菜单：写进去等于持久化，绝不通过接口暴露
    appdata = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    add(os.path.join(appdata, "Microsoft", "Windows", "Start Menu"))
    add(os.path.join(os.environ.get("ProgramData", ""), "Microsoft", "Windows", "Start Menu"))
    _SENSITIVE_ROOTS = roots
    return roots


def looks_unc(p: str) -> bool:
    """UNC / Win32 设备命名空间：\\\\server、\\\\?\\C:\\、\\\\.\\PhysicalDrive0。"""
    return p.replace("/", "\\").startswith("\\\\")


def has_ads(p: str) -> bool:
    """NTFS 备用数据流：C:\\a.txt:hidden（盘符之后不允许再出现冒号）。"""
    _drive, tail = os.path.splitdrive(p)
    return ":" in tail


def check_path(p: str) -> str:
    """统一的路径入口：拒绝 UNC/设备名、ADS、含 NUL 的路径和受保护系统目录。

    个人使用模式（SECURE_MODE=False）下只做最基本的合法性检查，
    恢复加固前的「手机可浏览整台电脑」行为。
    """
    if not isinstance(p, str) or not p.strip():
        raise ValueError("路径不能为空")
    if "\x00" in p:
        raise ValueError("路径无效")
    p = os.path.abspath(p)
    if not SECURE_MODE:
        return p
    if looks_unc(p):
        raise ValueError("不允许访问网络或设备路径")
    if has_ads(p):
        raise ValueError("不允许访问数据流路径")
    for root in _sensitive_roots():
        if inside(p, root):
            raise PermissionError("该位置受保护，不能在手机端访问")
    return p


# 可能被执行的文件类型：落盘后补 MOTW（Zone.Identifier），让 SmartScreen 提示来源
RISKY_EXT = {".exe", ".dll", ".scr", ".com", ".pif", ".cpl", ".bat", ".cmd",
             ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".hta", ".lnk",
             ".reg", ".msi", ".jar", ".url", ".msc", ".gadget"}


def mark_risky_file(path: str) -> bool:
    """给可执行文件打 Internet 标记（修复 DL-06：Python 落盘默认不带 MOTW）。"""
    if not SECURE_MODE:
        return False
    ext = os.path.splitext(path)[1].lower()
    if ext not in RISKY_EXT:
        return False
    if os.name != "nt":
        return True
    try:
        with open(path + ":Zone.Identifier", "w", encoding="ascii", newline="") as f:
            f.write("[ZoneTransfer]\r\nZoneId=3\r\nHostUrl=http://droplink.local/\r\n")
        return True
    except OSError:
        return False



# --------------------------------------------------------------------------
# Windows 路径工具
# --------------------------------------------------------------------------

CSIDL = {
    "Desktop": 0x0010,
    "Documents": 0x0005,
    "Pictures": 0x0027,
    "Music": 0x000B,
    "Videos": 0x000E,
}


def known_folder(name: str) -> str:
    """取 Windows 已知文件夹（桌面/下载/文档/图片/视频/音乐）。"""
    if os.name == "nt":
        csidl = CSIDL.get(name)
        if csidl is not None:
            try:
                buf = ctypes.create_unicode_buffer(512)
                if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0 and buf.value:
                    return buf.value
            except Exception:
                pass
        if name == "Downloads":
            p = os.path.join(os.path.expanduser("~"), "Downloads")
            if os.path.isdir(p):
                return p
    return os.path.join(os.path.expanduser("~"), name)


def list_drives():
    if os.name != "nt":
        return ["/"]
    out = []
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if mask & (1 << i):
                p = f"{chr(65 + i)}:\\"
                if os.path.exists(p):
                    out.append(p)
    except Exception:
        for c in "CDEFGHIJ":
            p = f"{c}:\\"
            if os.path.exists(p):
                out.append(p)
    return out


def quick_roots():
    out = []
    seen = set()
    for label, key in (("桌面", "Desktop"), ("下载", "Downloads"), ("文档", "Documents"),
                       ("图片", "Pictures"), ("视频", "Videos"), ("音乐", "Music")):
        p = known_folder(key)
        if os.path.isdir(p) and p.lower() not in seen:
            seen.add(p.lower())
            out.append({"label": label, "path": p, "icon": key.lower()})
    for d in list_drives():
        if d.lower() not in seen:
            seen.add(d.lower())
            out.append({"label": d.rstrip("\\"), "path": d, "icon": "drive"})
    return out


def kind_of(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in TEXT_EXT:
        return "text"
    if ext == ".pdf":
        return "pdf"
    if ext in ARCHIVE_EXT:
        return "archive"
    if ext in DOC_EXT:
        return "doc"
    return "other"


def natkey(s: str):
    return [(0, int(t)) if t.isdigit() else (1, t.lower()) for t in re.split(r"(\d+)", s) if t]


BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# 双向控制符 / 零宽字符：可把 invoice<U+202E>fdp.exe 显示成 invoiceexe.pdf
INVISIBLE_CHARS = re.compile("[\u202a-\u202e\u2066-\u2069\u200b-\u200f\ufeff]")
# Windows 设备名：CON / NUL / COM1 … 落盘会变成设备或虚假成功
RESERVED_NAMES = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.I)


def has_invisible(s: str) -> bool:
    return bool(INVISIBLE_CHARS.search(s or ""))


def safe_name(name: str, fallback: str = "file") -> str:
    name = unicodedata.normalize("NFC", str(name or ""))
    name = name.strip().replace("\\", "/")
    name = name.split("/")[-1]
    name = INVISIBLE_CHARS.sub("", name)
    name = BAD_CHARS.sub("_", name).strip(" .")
    if RESERVED_NAMES.match(name):
        name = "_" + name
    return (name or fallback)[:180]


def strict_name(raw, fallback: str = "") -> str:
    """上传 / 新建 / 重命名专用：可疑名字直接拒绝，而不是悄悄改写。"""
    s = unicodedata.normalize("NFC", str(raw or ""))
    s = s.replace("\\", "/").split("/")[-1].strip()
    if has_invisible(s):
        raise ValueError("文件名包含不可见字符")
    s = BAD_CHARS.sub("_", s).strip(" .")
    if not s:
        if fallback:
            return fallback
        raise ValueError("名称不能为空")
    if RESERVED_NAMES.match(s):
        raise ValueError("不能使用系统保留名称")
    return s[:180]


def unique_path(folder: str, name: str) -> str:
    base, ext = os.path.splitext(name)
    cand = os.path.join(folder, name)
    i = 1
    while os.path.exists(cand):
        cand = os.path.join(folder, f"{base} ({i}){ext}")
        i += 1
    return cand


def claim_path(tmp_path: str, folder: str, name: str) -> str:
    """把写好的临时文件原子地落到一个不冲突的最终文件名（并发上传不会互相覆盖）。

    旧做法是先建一个 0 字节占位文件再替换：占位期间别人能在目录里看到一个空文件，
    上传中断时这个空文件还会残留一会儿。改成「先写隐藏临时文件，最后 rename 抢名字」，
    Windows 上 os.rename 在目标已存在时会抛 FileExistsError，抢占依然互斥，
    而且整个过程比原来少一次删除。
    """
    base, ext = os.path.splitext(name)
    for i in range(10000):
        cand = os.path.join(folder, name if i == 0 else f"{base} ({i}){ext}")
        if os.path.exists(cand):
            continue
        try:
            os.rename(tmp_path, cand)
            return cand
        except FileExistsError:
            continue
        except OSError:
            # 目标不存在但 rename 仍失败（跨卷、被占用等）：最后试一次替换
            if os.path.exists(cand):
                continue
            try:
                os.replace(tmp_path, cand)
                return cand
            except OSError:
                break
    raise RuntimeError("无法分配文件名")

def inside(path: str, parent: str) -> bool:
    """用 realpath 比较：junction / 符号链接解析到外面时判定为「不在里面」。"""
    try:
        p = os.path.normcase(os.path.realpath(path))
        q = os.path.normcase(os.path.realpath(parent))
        return p == q or p.startswith(q.rstrip("\\/") + os.sep)
    except Exception:
        return False


def lexical_inside(path: str, parent: str) -> bool:
    """只看字符串路径（不解析 junction）——用于判断「这个链接本身」在不在目录里。"""
    try:
        p = os.path.normcase(os.path.abspath(path))
        q = os.path.normcase(os.path.abspath(parent))
        return p == q or p.startswith(q.rstrip("\\/") + os.sep)
    except Exception:
        return False


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def lan_urls(port: int):
    ips = []
    # 1) UDP connect 不发送任何数据包，可拿到默认出口网卡地址
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    # 2) 枚举本机所有 IPv4
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass

    def score(ip):
        if ip.startswith("192.168."):
            return 0
        if ip.startswith("10."):
            return 1
        if ip.startswith("172."):
            return 2
        return 3

    ips = [ip for ip in ips if not ip.startswith("169.254.")]
    ips.sort(key=score)
    return [f"http://{ip}:{port}" for ip in ips]


def allowed_hosts(port: int) -> set:
    """Host 白名单：本机名、回环、本机所有网卡 IP。

    这是阻断 DNS 重绑定的关键——攻击者域名解析到 127.0.0.1 后，
    请求里的 Host 仍是 evil.example.com，会被这里拒掉（DL-01）。
    """
    now = time.time()
    with _HOST_CACHE_LOCK:
        if _HOST_CACHE["hosts"] and now - _HOST_CACHE["ts"] < 10:
            return set(_HOST_CACHE["hosts"])
    hosts = {"127.0.0.1", "localhost", "::1", "[::1]"}
    try:
        hosts.add(socket.gethostname().lower())
        fqdn = socket.getfqdn().lower()
        if fqdn:
            hosts.add(fqdn)
    except Exception:
        pass
    for u in lan_urls(port):
        try:
            h = urllib.parse.urlparse(u).hostname
            if h:
                hosts.add(h.lower())
        except Exception:
            pass
    with _HOST_CACHE_LOCK:
        _HOST_CACHE["ts"] = now
        _HOST_CACHE["hosts"] = hosts
    return set(hosts)


def recycle(paths):
    """删除到回收站（可恢复）。失败返回 False，不做永久删除。"""
    if os.name != "nt":
        return False
    try:
        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", ctypes.c_void_p),
                ("wFunc", ctypes.c_uint),
                ("pFrom", ctypes.c_wchar_p),
                ("pTo", ctypes.c_wchar_p),
                ("fFlags", ctypes.c_ushort),
                ("fAnyOperationsAborted", ctypes.c_int),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", ctypes.c_wchar_p),
            ]

        ops = SHFILEOPSTRUCTW()
        ops.wFunc = 3  # FO_DELETE
        raw = "\0".join(os.path.abspath(p) for p in paths) + "\0\0"
        buf = ctypes.create_unicode_buffer(raw, size=len(raw) + 1)
        ops.pFrom = ctypes.cast(buf, ctypes.c_wchar_p)
        ops.fFlags = 0x0040 | 0x0010 | 0x0004  # ALLOWUNDO | NOCONFIRMATION | SILENT
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(ops))
        if rc == 0 and not ops.fAnyOperationsAborted:
            return True
        # 某些环境（非 Explorer 会话、被安全软件挂钩）会返回非 0，
        # 但文件其实已经进了回收站 —— 以「文件是否真的不在了」为准，
        # 绝不因此改成永久删除。
        return all(not os.path.exists(p) for p in paths)
    except Exception:
        return False


# --------------------------------------------------------------------------
# 防火墙（UAC 提权执行 netsh）
# --------------------------------------------------------------------------

def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _runas_bat(lines):
    """把 netsh 命令写进临时 bat，用 UAC 提权执行。返回 (是否执行, 退出码)。

    用 mkstemp 生成随机文件名（修复 DL-11：固定路径存在 TOCTOU，
    同用户的恶意程序可以预置内容借 UAC 执行）。
    """
    try:
        fd, tmp = tempfile.mkstemp(prefix="droplink_fw_", suffix=".cmd")
        os.close(fd)
    except OSError as exc:
        return False, f"写临时文件失败：{exc}"
    try:
        with open(tmp, "w", encoding="ascii", newline="\r\n") as f:
            f.write("@echo off\r\n")
            for ln in lines:
                f.write(ln + "\r\n")
    except OSError as exc:
        return False, f"写临时文件失败：{exc}"
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("fMask", ctypes.c_ulong),
            ("hwnd", ctypes.c_void_p),
            ("lpVerb", ctypes.c_wchar_p),
            ("lpFile", ctypes.c_wchar_p),
            ("lpParameters", ctypes.c_wchar_p),
            ("lpDirectory", ctypes.c_wchar_p),
            ("nShow", ctypes.c_int),
            ("hInstApp", ctypes.c_void_p),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", ctypes.c_wchar_p),
            ("hkeyClass", ctypes.c_void_p),
            ("dwHotKey", ctypes.c_ulong),
            ("hIcon", ctypes.c_void_p),
            ("hProcess", ctypes.c_void_p),
        ]

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = "cmd.exe"
    sei.lpParameters = '/c ""%s""' % tmp
    sei.nShow = 0  # SW_HIDE
    try:
        ok = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei))
    except Exception as exc:
        return False, f"调用失败：{exc}"
    if not ok:
        # 返回 False 多为用户点了「取消」（拒绝 UAC）
        return False, "已取消"

    code = -1
    if sei.hProcess:
        # 最多等 30 秒；用户若迟迟不点 UAC 确认框，视为已提交（避免请求一直挂着）
        r = ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, 30000)
        if r == 0x00000102:  # WAIT_TIMEOUT
            ctypes.windll.kernel32.TerminateProcess(sei.hProcess, 1)
            code = 0
        else:
            c = ctypes.c_ulong(0)
            ctypes.windll.kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(c))
            code = int(c.value)
        ctypes.windll.kernel32.CloseHandle(sei.hProcess)
    try:
        os.unlink(tmp)
    except OSError:
        pass
    return True, code


def _has_fw_rule() -> bool:
    """查询 DropLink 的防火墙入站规则是否存在（查询不需要管理员）。"""
    try:
        r = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=DropLink Port"],
            capture_output=True, timeout=6, creationflags=CREATE_NO_WINDOW)
        return r.returncode == 0 and b"DropLink Port" in r.stdout
    except Exception:
        return False


def _fw_rule_detail():
    """返回 DropLink 规则的配置细节：profile / 端口 / 是否启用。"""
    try:
        r = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=DropLink Port"],
            capture_output=True, timeout=8, creationflags=CREATE_NO_WINDOW)
        if r.returncode != 0:
            return None
        # netsh 输出编码因系统「活动代码页」而异：先试 UTF-8，再回退 GBK
        text = None
        for enc in ("utf-8", "gbk"):
            cand = r.stdout.decode(enc, "replace")
            if "配置文件" in cand or "Profiles" in cand:
                text = cand
                break
        if text is None:
            text = r.stdout.decode("utf-8", "replace")
        out = {}
        for line in text.splitlines():
            line = line.strip()
            if "配置文件" in line or "Profiles" in line:
                out["profiles"] = line.split(":", 1)[-1].strip()
            elif "本地端口" in line or "LocalPort" in line:
                out["port"] = line.split(":", 1)[-1].strip()
            elif "已启用" in line or "Enabled" in line:
                out["enabled"] = line.split(":", 1)[-1].strip()
        return out or None
    except Exception:
        return None


def device_name_of(ua: str) -> str:
    """从 User-Agent 粗略识别设备名，用于手机未上报名称时的兜底。"""
    if not ua:
        return "手机设备"
    if "Android" in ua:
        m = re.search(r"Android[\s/]+([\d.]+)", ua)
        return "安卓 " + (m.group(1) if m else "设备")
    if "iPhone" in ua or "iPad" in ua:
        return "苹果设备"
    if "Windows" in ua:
        return "Windows 设备"
    return "手机设备"


def _net_profiles():
    """用 PowerShell 查询本机各网络连接的类型（0 公用 / 1 专用 / 2 域）。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "$OutputEncoding=[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
             "Get-NetConnectionProfile | ForEach-Object { $_.Name + ';' + [int]$_.NetworkCategory }"],
            capture_output=True, timeout=12, creationflags=CREATE_NO_WINDOW)
        if r.returncode != 0:
            return None
        text = r.stdout.decode("utf-8", "replace")
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line or ";" not in line:
                continue
            name, _, cat_s = line.rpartition(";")
            try:
                cat = int(cat_s.strip())
            except ValueError:
                cat = 0
            out.append({"name": name.strip() or "网络", "cat": cat})
        return out if out else None
    except Exception:
        return None


def _all_ips():
    """列出本机所有 IPv4 地址及网卡名（含虚拟网卡），供「网络切换」选择。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "$OutputEncoding=[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
             "Get-NetIPAddress -AddressFamily IPv4 | ForEach-Object { "
             "$_.IPAddress + ';' + $_.InterfaceAlias + ';' + $_.PrefixLength }"],
            capture_output=True, timeout=12, creationflags=CREATE_NO_WINDOW)
        if r.returncode != 0:
            return None
        text = r.stdout.decode("utf-8", "replace")
        out = []
        seen = set()
        for line in text.splitlines():
            line = line.strip()
            if not line or ";" not in line:
                continue
            parts = line.split(";")
            ip = parts[0].strip()
            alias = parts[1].strip() if len(parts) > 1 else ""
            if not ip or ip.startswith("127.") or ip.startswith("169.254."):
                continue
            if ip in seen:
                continue
            seen.add(ip)
            # 虚拟网卡常见关键词，用于前端标注
            out.append({"ip": ip, "alias": alias})
        return out
    except Exception:
        return None


# --------------------------------------------------------------------------
# 全局状态
# --------------------------------------------------------------------------

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.sessions = {}   # token -> {"ts": 首次, "last": 最近, "ip": ..., "ua": ...}
        self.fails = {}
        # 8 位配对码：空间从 10^6 提到 10^8，配合限速后暴力成本提高 100 倍
        self.pin = "".join(secrets.choice("0123456789")
                           for _ in range(8 if SECURE_MODE else 6))
        self.started = time.time()
        self.clip = []
        self.transfers = []
        self.shares = {}
        self.peers = {}  # ip -> {"ts": 接入时间, "ua": 用户代理, "name": 设备名}
        self.pending = []  # 待接收推送：{"id","name","path","size","targets":["ip"|"all"],"ack":{ip:ts},"ts"}
        self.send_dir = ""  # 电脑端推送文件的暂存目录
        self.config = {}
        self.recv_dir = ""
        self.load()

    def load(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                self.config = json.load(f) or {}
        except Exception:
            self.config = {}
        recv = str(self.config.get("recv_dir") or "")
        if not recv or not os.path.isdir(recv):
            recv = os.path.join(known_folder("Desktop"), "DropLink 接收")
        try:
            os.makedirs(recv, exist_ok=True)
        except Exception:
            recv = tempfile.mkdtemp(prefix="DropLink_")
        self.recv_dir = os.path.abspath(recv)
        self.config["recv_dir"] = self.recv_dir
        # 电脑端「发送到手机」的暂存目录（隐藏）
        self.send_dir = os.path.join(self.recv_dir, ".droplink-send")
        try:
            os.makedirs(self.send_dir, exist_ok=True)
        except Exception:
            self.send_dir = tempfile.mkdtemp(prefix="DropLink_send_")
        self.save()

    def save(self):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def add_clip(self, text: str, src: str):
        item = {"id": secrets.token_hex(6), "text": text[:20000],
                "ts": time.time(), "from": src}
        with self.lock:
            self.clip.insert(0, item)
            del self.clip[80:]
        return item

    def add_transfer(self, **kw):
        """记一条传输。2 秒内同方向、同文件、同大小的重复请求视为同一次（修复 BUG-09）。

        WebView / 浏览器经常先发一次探测请求再发正式请求，导致「一次下载两条记录」。
        """
        now = time.time()
        with self.lock:
            for it in self.transfers[:5]:
                if (it.get("name") == kw.get("name")
                        and it.get("size") == kw.get("size")
                        and it.get("direction") == kw.get("direction")
                        and now - it.get("ts", 0) < 2):
                    return it
            item = {"id": secrets.token_hex(6), "ts": now, **kw}
            self.transfers.insert(0, item)
            del self.transfers[100:]
        return item

    # ---- 会话（修复 DL-03：过期、吊销、上限）----

    def new_session(self, ip: str, ua: str) -> str:
        token = secrets.token_urlsafe(24)
        now = time.time()
        with self.lock:
            self.sessions[token] = {"ts": now, "last": now, "ip": ip, "ua": ua[:120]}
            # 超出上限时踢掉最久未使用的
            if len(self.sessions) > MAX_SESSIONS:
                old = sorted(self.sessions.items(), key=lambda kv: kv[1]["last"])
                for k, _v in old[: len(self.sessions) - MAX_SESSIONS]:
                    self.sessions.pop(k, None)
        return token

    def session_valid(self, token: str) -> bool:
        if not token:
            return False
        if not SECURE_MODE:
            # 加固前：会话不过期，重启服务才失效
            with self.lock:
                return token in self.sessions
        now = time.time()
        with self.lock:
            rec = self.sessions.get(token)
            if not rec:
                return False
            if now - rec["ts"] > SESSION_ABS_TTL or now - rec["last"] > SESSION_IDLE_TTL:
                self.sessions.pop(token, None)
                return False
            rec["last"] = now
            return True

    def revoke_session(self, token: str):
        with self.lock:
            self.sessions.pop(token, None)

    def revoke_all(self) -> int:
        with self.lock:
            n = len(self.sessions)
            self.sessions.clear()
            return n

    def cleanup(self):
        """定期清理过期会话、失败的登录记录、离线设备，避免长期运行内存增长。"""
        now = time.time()
        with self.lock:
            dead = [k for k, v in self.sessions.items()
                    if now - v["ts"] > SESSION_ABS_TTL or now - v["last"] > SESSION_IDLE_TTL]
            for k in dead:
                self.sessions.pop(k, None)
            if len(self.fails) > MAX_FAIL_IPS:
                for k in sorted(self.fails, key=lambda i: self.fails[i]["ts"])[: len(self.fails) - MAX_FAIL_IPS]:
                    self.fails.pop(k, None)
            if len(self.peers) > MAX_PEERS:
                keep = sorted(self.peers.items(), key=lambda kv: kv[1]["ts"])[-MAX_PEERS:]
                self.peers = dict(keep)
            for k in [k for k, v in self.shares.items() if v["exp"] < now]:
                self.shares.pop(k, None)


STATE = State()

# 推送暂存（.droplink-send）的保留策略：过期即删、总量封顶，避免磁盘被历史副本占满
SEND_TTL = 24 * 3600
SEND_MAX_BYTES = 2 << 30


def purge_send_cache():
    """清理过期的推送暂存文件和目录里的孤儿文件，返回 (删除数, 释放字节)。"""
    now = time.time()
    freed = 0
    removed = 0
    with STATE.lock:
        keep = []
        for it in STATE.pending:
            path = it.get("path") or ""
            if not path or not os.path.exists(path):
                continue
            if now - float(it.get("ts") or 0) > SEND_TTL:
                try:
                    freed += os.path.getsize(path)
                    os.unlink(path)
                except OSError:
                    pass
                removed += 1
                continue
            keep.append(it)
        total = sum(int(it.get("size") or 0) for it in keep)
        while keep and total > SEND_MAX_BYTES:
            it = keep.pop(0)
            total -= int(it.get("size") or 0)
            try:
                freed += os.path.getsize(it["path"])
                os.unlink(it["path"])
            except OSError:
                pass
            removed += 1
        STATE.pending[:] = keep
        alive = {os.path.basename(it["path"]) for it in keep}

    # 目录里不属于任何待接收记录的孤儿文件（超过 1 天才清，避免误删正在写入的）
    try:
        with os.scandir(STATE.send_dir) as it:
            for e in it:
                try:
                    if not e.is_file() or e.name in alive:
                        continue
                    st = e.stat()
                    if now - st.st_mtime < 3600:   # 1 小时宽限，避开正在写入的新文件
                        continue
                    freed += st.st_size
                    os.unlink(e.path)
                    removed += 1
                except OSError:
                    continue
    except OSError:
        pass
    return removed, freed


# --------------------------------------------------------------------------
# 可选能力：二维码 / 缩略图 / 图标
# --------------------------------------------------------------------------

def qr_png(text: str, box: int = 8, border: int = 2):
    try:
        import qrcode
    except Exception:
        try:
            import qrmini
            return qrmini.qr_png_bytes(text, scale=max(2, box), border=border)
        except Exception:
            return None
    try:
        qr = qrcode.QRCode(version=None,
                           error_correction=qrcode.constants.ERROR_CORRECT_M,
                           box_size=box, border=border)
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#111827", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def has_pillow() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except Exception:
        return False


_QR_OK = None


def qr_available() -> bool:
    global _QR_OK
    if _QR_OK is None:
        _QR_OK = qr_png("droplink") is not None
    return _QR_OK


# 缩略图解码限流：一次只让 3 张图同时解码，避免大相册首屏把 CPU 打满
_THUMB_SEM = threading.Semaphore(3)


def thumb_bytes(path: str, width: int = 320):
    try:
        from PIL import Image, ImageOps
    except Exception:
        return None
    locked = _THUMB_SEM.acquire(timeout=15)
    try:
        # 用内置 zlib 校验和（避免为了 hashlib 拖进 5MB 的 libcrypto）
        key = "%08x" % (zlib.crc32(f"{path}|{os.path.getmtime(path)}|{width}".encode("utf-8"))
                        & 0xFFFFFFFF)
        os.makedirs(CACHE_DIR, exist_ok=True)
        cached = os.path.join(CACHE_DIR, key + ".jpg")
        if os.path.exists(cached):
            with open(cached, "rb") as f:
                return f.read()
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((width, width * 3))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            out = io.BytesIO()
            im.save(out, "JPEG", quality=80, optimize=True)
            data = out.getvalue()
        with open(cached, "wb") as f:
            f.write(data)
        return data
    except Exception:
        return None
    finally:
        if locked:
            _THUMB_SEM.release()


_ICON_CACHE = {}


def icon_png(size: int = 192):
    """统一的应用图标：无论装没装 Pillow，都返回 qrmini 生成的同一张图。

    这保证 favicon、PWA 清单图标、`/api/icon`、界面左上角 logo 与 EXE/安卓图标完全一致
    （紫色圆角底 + 左白下箭头 + 右白上箭头），而不是之前 Pillow 单独画的那套「上箭头 + 托盘」。
    """
    if size in _ICON_CACHE:
        return _ICON_CACHE[size]
    try:
        import qrmini
        data = qrmini.icon_png_bytes(size)
        _ICON_CACHE[size] = data
        return data
    except Exception:
        return None


# --------------------------------------------------------------------------
# HTTP 处理
# --------------------------------------------------------------------------

class Server(ThreadingHTTPServer):
    """更高的连接队列 + 守护线程：多台手机同时连、批量并发时不丢连接。"""
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"DropLink/{VERSION}"
    sys_version = ""
    # 读写超时：慢速连接（Slowloris）会在 30 秒内被断开，修复 X-07
    timeout = 30
    # 并发处理上限：再多就只能排队，避免几百条连接把线程/内存吃光
    _slots = threading.BoundedSemaphore(64)

    # ---------------- 基础设施 ----------------

    def handle(self):
        if not Handler._slots.acquire(timeout=10):
            try:
                self._send(503, b"busy", "text/plain; charset=utf-8", {"Retry-After": "5"})
            except Exception:
                pass
            self.close_connection = True
            return
        try:
            super().handle()
        finally:
            Handler._slots.release()

    def log_message(self, fmt, *args):
        pass

    def version_string(self):
        return self.server_version

    def _is_local(self):
        ip = self.client_address[0]
        return ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _host_header(self):
        h = (self.headers.get("Host") or "").strip()
        if not h or "@" in h or "/" in h or "\\" in h:
            return ""
        if h.startswith("["):
            end = h.find("]")
            return h[1:end].lower() if end > 0 else ""
        return (h.rsplit(":", 1)[0] if ":" in h else h).lower()

    def _host_ok(self):
        if not SECURE_MODE:
            return True
        h = self._host_header()
        return bool(h) and h in allowed_hosts(self.server.server_port)

    def _origin_ok(self):
        if not SECURE_MODE:
            return True
        o = (self.headers.get("Origin") or "").strip()
        if not o:
            return True
        if o.lower() == "null":
            return False
        try:
            u = urllib.parse.urlparse(o)
        except Exception:
            return False
        if u.scheme not in ("http", "https"):
            return False
        return (u.hostname or "").lower() in allowed_hosts(self.server.server_port)

    def _fetch_ok(self):
        if not SECURE_MODE:
            return True
        # 浏览器跨站请求一律拒绝；curl/原生 App 不带这个头，放行后仍要过令牌/会话
        sfs = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        return sfs in ("", "same-origin", "none")

    def _token_ok(self):
        tok = self.headers.get("X-DL-Token") or ""
        return bool(tok) and secrets.compare_digest(tok, LOCAL_TOKEN)

    def _cookie_token(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k.strip() == "dlsess":
                return v.strip()
        return ""

    def _authed(self):
        # 个人使用模式：恢复「本机请求直接放行」的旧行为
        if not SECURE_MODE and self._is_local():
            return True
        # Host/Origin/Sec-Fetch 是硬门槛：过不了就等同于未登录（阻断 DNS 重绑定与 CSRF）
        if not (self._host_ok() and self._origin_ok() and self._fetch_ok()):
            return False
        if self._token_ok():
            return True                 # 程序自己打开的界面
        return STATE.session_valid(self._cookie_token())

    def _local_ui(self):
        """改设置 / 开程序 / 调防火墙：必须是本机、来源合法，并且已认证（令牌或配对会话）。"""
        return (self._is_local() and self._host_ok() and self._origin_ok()
                and self._fetch_ok() and self._authed())

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if SECURE_MODE:
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
            # 禁止被 iframe、禁止内联脚本执行（修复 DL-08 点击劫持与同源内联脚本）
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
                "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if getattr(self, "command", None) != "HEAD" and body:
            self.wfile.write(body)

    def _server_error(self, exc):
        """500 只回一个错误号，异常文本写日志（修复 X-08 信息回显）。"""
        eid = secrets.token_hex(4)
        try:
            print(f"[错误 {eid}] {type(exc).__name__}: {exc}")
        except Exception:
            pass
        try:
            self._fail(500, f"服务器内部错误（{eid}）", close=True)
        except Exception:
            pass

    def _json(self, obj, code=200, extra=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        headers = dict(extra or {})
        accept = (self.headers.get("Accept-Encoding") or "").lower()
        # 大目录列表很占带宽：超过 2KB 就压缩（小响应压了没意义）
        if code == 200 and len(body) > 2048 and "gzip" in accept:
            body = gzip.compress(body, 5)
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
        self._send(code, body, "application/json; charset=utf-8", headers)

    def _fail(self, code, msg, close=False, **kw):
        if close:
            self.close_connection = True
        self._json({"ok": False, "error": msg, **kw}, code)

    def _need_auth(self):
        if self._authed():
            return True
        self._json({"ok": False, "error": "需要配对码", "need_login": True}, 401)
        return False

    def _body(self, limit: int = 1 << 20) -> bytes:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return b""
        if n > limit:
            raise ValueError("请求体过大")
        data = b""
        while len(data) < n:
            chunk = self.rfile.read(min(CHUNK, n - len(data)))
            if not chunk:
                break
            data += chunk
        return data

    @staticmethod
    def _no_duplicate_keys(pairs):
        """拒绝重复键（修复 X-09：{"pin":"bad","pin":"123456"} 的解析歧义）。"""
        out = {}
        for k, v in pairs:
            if k in out:
                raise ValueError("重复字段")
            out[k] = v
        return out

    def _json_body(self):
        raw = self._body()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8", "replace"),
                          object_pairs_hook=self._no_duplicate_keys)

    # ---------------- 静态资源 ----------------

    def _serve_web(self, rel: str):
        """静态资源：ETag 协商缓存 + gzip。首次加载省 70% 传输，二次加载直接 304。"""
        rel = urllib.parse.unquote(rel)
        target = os.path.abspath(os.path.join(WEB_DIR, rel))
        if not inside(target, WEB_DIR) or not os.path.isfile(target):
            return self._fail(404, "文件不存在")
        try:
            st = os.stat(target)
        except OSError:
            return self._fail(404, "文件不存在")

        etag = '"%x-%x"' % (st.st_mtime_ns, st.st_size)
        if (self.headers.get("If-None-Match") or "").strip() == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return

        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        compressible = (ctype.startswith("text/")
                        or ctype in ("application/javascript", "application/json",
                                     "application/manifest+json", "image/svg+xml"))
        if compressible and "charset" not in ctype:
            ctype += "; charset=utf-8"

        raw, gz = _static_asset(target, st, compressible)
        accept = (self.headers.get("Accept-Encoding") or "").lower()
        headers = {"ETag": etag, "Cache-Control": "no-cache", "Vary": "Accept-Encoding"}
        if gz is not None and "gzip" in accept:
            headers["Content-Encoding"] = "gzip"
            self._send(200, gz, ctype, headers)
        else:
            self._send(200, raw, ctype, headers)

    # ---------------- 文件发送（支持 Range） ----------------

    def _serve_file(self, path: str, inline=False, download_name=None):
        try:
            size = os.path.getsize(path)
        except OSError:
            return self._fail(404, "文件不存在")
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") and "charset" not in ctype:
            ctype += "; charset=utf-8"

        name = download_name or os.path.basename(path)
        ascii_name = name.encode("ascii", "ignore").decode("ascii") or "download"
        ascii_name = ascii_name.replace('"', "_").replace("\\", "_")
        # HTML / SVG 一律当附件下载：直接在服务源里打开会变成同源脚本执行（DL-08）
        if ctype.split(";")[0] in ("text/html", "image/svg+xml", "application/xhtml+xml"):
            inline = False
        disp = "inline" if inline else "attachment"
        disposition = f'{disp}; filename="{ascii_name}"; filename*=UTF-8\'\'{urllib.parse.quote(name)}'

        start, end = 0, max(0, size - 1)
        partial = False
        rng = (self.headers.get("Range") or "").strip()
        m = re.match(r"^bytes=(\d*)-(\d*)$", rng)
        if m and size > 0:
            s_txt, e_txt = m.group(1), m.group(2)
            if s_txt == "" and e_txt != "":
                back = int(e_txt)
                start = max(0, size - back)
                end = size - 1
            else:
                start = int(s_txt or 0)
                end = int(e_txt) if e_txt else size - 1
            if start >= size:
                return self._send(416, b"", ctype, {"Content-Range": f"bytes */{size}"})
            end = min(end, size - 1)
            if end < start:
                # bytes=5-1 这种倒序区间按 RFC 9110 应返回 416（修复 X-10）
                return self._send(416, b"", ctype, {"Content-Range": f"bytes */{size}"})
            partial = True

        length = end - start + 1 if size else 0
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", disposition)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD" or length == 0:
            return

        # 登记为「进行中」，让本机 App 能看到实时速率（以前只有发起方页面自己知道）
        live_id = secrets.token_hex(4)
        live = {"name": os.path.basename(path), "total": length, "sent": 0,
                "start": time.time(), "peer": self.client_address[0]}
        with _LIVE_LOCK:
            _LIVE[live_id] = live
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    buf = f.read(min(CHUNK, left))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    left -= len(buf)
                    live["sent"] += len(buf)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        finally:
            with _LIVE_LOCK:
                _LIVE.pop(live_id, None)

    def _dl_page(self, filepath: str, token: str):
        """手机扫分享二维码后，先展示下载确认页（文件名 + 大小 + 下载按钮 + 位置说明）。"""
        name = os.path.basename(filepath)
        try:
            size = os.path.getsize(filepath)
        except OSError:
            size = 0
        if size >= 1 << 30:
            human = f"{size / (1 << 30):.2f} GB"
        elif size >= 1 << 20:
            human = f"{size / (1 << 20):.1f} MB"
        elif size >= 1 << 10:
            human = f"{size / (1 << 10):.0f} KB"
        else:
            human = f"{size} B"
        esc_name = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        html = (
            "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>下载文件</title><style>"
            "*{box-sizing:border-box}body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;margin:0;"
            "background:#f4f5fb;color:#0f172a;display:flex;align-items:center;justify-content:center;"
            "min-height:100vh;padding:16px;overflow-x:hidden}"
            ".card{background:#fff;border-radius:22px;padding:34px 26px;width:100%;max-width:min(420px,100vw - 32px);"
            "box-shadow:0 12px 44px rgba(15,23,42,.10);text-align:center}"
            ".ico{width:66px;height:66px;border-radius:19px;background:linear-gradient(135deg,#4f46e5,#7c3aed);"
            "color:#fff;display:flex;align-items:center;justify-content:center;margin:0 auto 18px}"
            ".ico svg{width:30px;height:30px}"
            "h1{font-size:16px;font-weight:600;color:#64748b;margin:0 0 8px}"
            ".name{font-size:16px;font-weight:650;word-break:break-all;margin:0 0 6px;line-height:1.4}"
            ".meta{font-size:13px;color:#94a3b8;margin:0 0 26px}"
            ".btn{display:block;width:100%;padding:15px;border:none;border-radius:15px;"
            "background:linear-gradient(135deg,#4f46e5,#7c3aed);color:#fff;font-size:16px;font-weight:650;"
            "text-decoration:none;cursor:pointer}"
            ".btn:active{transform:scale(.98)}"
            ".tip{font-size:12px;color:#94a3b8;margin:18px 0 0;line-height:1.7}"
            "@media (prefers-color-scheme:dark){body{background:#0b1220;color:#e2e8f0}"
            ".card{background:#111827}.name{color:#e2e8f0}.meta,.tip{color:#7c8aa0}}"
            "</style></head><body><div class='card'>"
            "<div class='ico'><svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.2'"
            " stroke-linecap='round' stroke-linejoin='round'><path d='M12 3v11'/><path d='m7 10 5 5 5-5'/>"
            "<path d='M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2'/></svg></div>"
            f"<h1>来自电脑的文件</h1>"
            f"<p class='name'>{esc_name}</p>"
            f"<p class='meta'>{human} · 点击下方按钮下载</p>"
            f"<a class='btn' href='/dl/{token}/file'>下载</a>"
            "<p class='tip'>文件会保存到手机浏览器的「下载」目录。<br>"
            "如需改保存位置，请在浏览器菜单的「下载」设置里修改。</p>"
            "</div></body></html>"
        )
        return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

    # ---------------- 路由 ----------------

    def do_GET(self):
        try:
            self._route_get()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001
            self._server_error(exc)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        try:
            self._route_post()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001
            self._server_error(exc)

    def _parts(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        return urllib.parse.unquote(parsed.path), {k: v[0] for k, v in qs.items()}

    def _route_get(self):
        # Host / Origin 先过一遍：DNS 重绑定的请求会带着攻击者的域名，这里直接拒绝
        if not self._host_ok():
            self.close_connection = True
            return self._send(421, b"", "text/plain; charset=utf-8")
        if not (self._origin_ok() and self._fetch_ok()):
            self.close_connection = True
            return self._fail(403, "请求来源不被允许")

        path, q = self._parts()

        if path in ("/", "/index.html"):
            return self._serve_web("index.html")
        if path.startswith("/static/"):
            return self._serve_web(path[len("/static/"):])
        if path == "/favicon.ico":
            png = icon_png(64)
            if png:
                return self._send(200, png, "image/png", {"Cache-Control": "max-age=86400"})
            return self._send(204, b"")
        if path == "/api/icon":
            try:
                size = int(q.get("size") or 192)
            except (TypeError, ValueError):
                return self._fail(400, "参数无效")
            size = max(48, min(512, size))
            png = icon_png(size)
            if not png:
                return self._fail(404, "未安装 Pillow")
            return self._send(200, png, "image/png", {"Cache-Control": "max-age=86400"})
        if path == "/manifest.webmanifest":
            icons = []
            if icon_png(192):
                icons = [
                    {"src": "/api/icon?size=192", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                    {"src": "/api/icon?size=512", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
                ]
            manifest = {
                "name": APP_NAME, "short_name": APP_SHORT, "start_url": "/",
                "display": "standalone", "background_color": "#f4f6fb",
                "theme_color": "#4f46e5", "orientation": "portrait-primary",
                "icons": icons,
            }
            return self._send(200, json.dumps(manifest, ensure_ascii=False),
                              "application/manifest+json; charset=utf-8")
        if path == "/qr.png":
            text = q.get("text") or ""
            if len(text.encode("utf-8", "ignore")) > 512:
                return self._fail(414, "二维码内容过长")
            png = qr_png(text) if text else None
            if not png:
                return self._fail(404, "二维码不可用")
            return self._send(200, png, "image/png", {"Cache-Control": "no-store"})
        if path.startswith("/dl/"):
            rest = path[4:].strip("/")
            is_file = rest.endswith("/file")
            token = rest[:-5] if is_file else rest
            item = STATE.shares.get(token)
            if not item or item["exp"] < time.time():
                return self._fail(404, "链接已失效")
            if is_file:
                return self._serve_file(item["path"])
            return self._dl_page(item["path"], token)

        # ---- API ----
        if path == "/api/ping":
            return self._json({"ok": True, "ts": time.time()})
        if path == "/api/session":
            # 未认证不返回任何主机信息（修复 DL-05：以前未授权就能拿到 PIN/路径/内网 IP）
            if not self._need_auth():
                return None
            return self.api_session()
        if not self._need_auth():
            return None
        if path == "/api/logout":
            return self.api_logout()
        if path == "/api/sessions":
            return self.api_sessions()
        if path == "/api/list":
            return self.api_list(q)
        if path == "/api/roots":
            return self._json({"ok": True, "roots": quick_roots(), "recv_dir": STATE.recv_dir})
        if path == "/api/download":
            return self.api_download(q)
        if path == "/api/zip":
            return self.api_zip_get(q)
        if path == "/api/thumb":
            return self.api_thumb(q)
        if path == "/api/preview":
            return self.api_preview(q)
        if path == "/api/search":
            return self.api_search(q)
        if path == "/api/clip":
            with STATE.lock:
                items = list(STATE.clip)
            return self._json({"ok": True, "items": items})
        if path == "/api/transfers":
            with STATE.lock:
                items = list(STATE.transfers)
            return self._json({"ok": True, "items": items})
        if path == "/api/config":
            return self._json({"ok": True, "recv_dir": STATE.recv_dir})
        if path == "/api/diag":
            return self.api_diag()
        if path == "/api/networks":
            return self.api_networks()
        if path == "/api/live":
            return self.api_live()
        if path == "/api/peers":
            return self.api_peers()
        if path == "/api/inbox":
            return self.api_inbox()
        return self._fail(404, "接口不存在")

    def _route_post(self):
        if not self._host_ok():
            self.close_connection = True
            return self._send(421, b"", "text/plain; charset=utf-8")
        if not (self._origin_ok() and self._fetch_ok()):
            self.close_connection = True
            return self._fail(403, "请求来源不被允许")

        path, q = self._parts()

        if path == "/api/login":
            return self.api_login()
        if path == "/api/upload":
            return self.api_upload(q)
        if not self._need_auth():
            return None
        if path == "/api/logout":
            return self.api_logout()
        if path == "/api/sessions/revoke":
            return self.api_sessions_revoke()
        if path == "/api/clip":
            return self.api_clip_add()
        if path == "/api/clip/delete":
            return self.api_clip_del()
        if path == "/api/mkdir":
            return self.api_mkdir()
        if path == "/api/rename":
            return self.api_rename()
        if path == "/api/delete":
            return self.api_delete()
        if path == "/api/zip":
            return self.api_zip()
        if path == "/api/share":
            return self.api_share()
        if path == "/api/send":
            return self.api_send(q)
        if path == "/api/inbox/ack":
            return self.api_inbox_ack()
        if path == "/api/config":
            return self.api_set_config()
        if path == "/api/open":
            return self.api_open()
        if path == "/api/firewall/set":
            return self.api_firewall_set()
        if path == "/api/firewall/remove":
            return self.api_firewall_remove()
        if path == "/api/open-hotspot":
            return self.api_open_hotspot()
        return self._fail(404, "接口不存在")

    # ---------------- API 实现 ----------------

    def api_session(self):
        urls = lan_urls(self.server.server_port)
        local_ui = self._local_ui()
        info = {
            "ok": True,
            "authed": True,
            "local": local_ui,
            "device": socket.gethostname(),
            "platform": f"{os.name}-{sys.platform}",
            "app": APP_NAME,
            "version": VERSION,
            "recv_dir": STATE.recv_dir,
            "uptime": int(time.time() - STATE.started),
            "lan_urls": urls,
            "qr_target": urls[0] if urls else "",
            "qr_available": qr_available(),
            "thumb_available": has_pillow(),
            "pin_length": len(STATE.pin),
        }
        # 配对码只给「本机 + 令牌」的界面，任何网络请求都拿不到（修复 DL-05）
        if local_ui:
            info["pin"] = STATE.pin
        extra = None
        # 本机界面也换一个短会话 Cookie：<img>/<a> 这类请求带不了自定义头，
        # 有了 Cookie 才能正常显示缩略图、直接点下载。
        if self._token_ok() and not self._cookie_token():
            tok = STATE.new_session(self.client_address[0], "local-ui")
            extra = {"Set-Cookie":
                     f"dlsess={tok}; Path=/; Max-Age={SESSION_ABS_TTL}; HttpOnly; SameSite=Strict"}
        self._json(info, extra=extra)

    def api_login(self):
        try:
            data = self._json_body()
        except Exception:
            return self._fail(400, "请求格式错误", close=True)
        ip = self.client_address[0]
        now = time.time()
        rec = STATE.fails.get(ip)
        if rec and rec["count"] >= 5:
            # 指数退避：连续失败后锁 60s → 120s → 240s（上限 5 分钟）
            wait = min(300, 60 * (2 ** max(0, rec["count"] - 5)))
            if now - rec["ts"] < wait:
                return self._fail(429, "尝试次数过多，请稍后再试")
        pin = data.get("pin")
        # 严格 schema：只接受字符串，拒绝数字/数组/对象与重复键（修复 X-09）
        if not isinstance(pin, str) or not (4 <= len(pin) <= 12) or not pin.isdigit():
            return self._fail(400, "配对码格式不正确")
        if not secrets.compare_digest(pin, STATE.pin):
            if not rec or now - rec["ts"] > 300:
                STATE.fails[ip] = {"count": 1, "ts": now}
            else:
                rec["count"] += 1
                rec["ts"] = now
            return self._fail(403, "配对码不正确")
        ua = (self.headers.get("User-Agent") or "")[:120]
        token = STATE.new_session(ip, ua)
        STATE.fails.pop(ip, None)
        name = str(data.get("name") or "").strip()[:40]
        with STATE.lock:
            STATE.peers[ip] = {"ts": now, "ua": ua, "name": name or device_name_of(ua)}
        STATE.cleanup()
        print(f"[接入] {name or ip} 已配对成功")
        cookie = f"dlsess={token}; Path=/; Max-Age={SESSION_ABS_TTL}; HttpOnly; SameSite=Strict"
        self._json({"ok": True, "device": socket.gethostname()}, extra={"Set-Cookie": cookie})

    def api_logout(self):
        tok = self._cookie_token()
        if tok:
            STATE.revoke_session(tok)
        cookie = "dlsess=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"
        self._json({"ok": True}, extra={"Set-Cookie": cookie})

    def api_sessions(self):
        if not self._local_ui():
            return self._fail(403, "只有电脑本机可以查看")
        now = time.time()
        with STATE.lock:
            items = [{
                "token": k[:6] + "…",
                "ip": v.get("ip"),
                "ua": v.get("ua"),
                "age": int(now - v["ts"]),
                "idle": int(now - v["last"]),
            } for k, v in STATE.sessions.items()]
        items.sort(key=lambda x: x["idle"])
        return self._json({"ok": True, "sessions": items})

    def api_sessions_revoke(self):
        if not self._local_ui():
            return self._fail(403, "只有电脑本机可以操作")
        try:
            data = self._json_body()
        except Exception:
            return self._fail(400, "请求格式错误", close=True)
        n = STATE.revoke_all() if data.get("all") else 0
        return self._json({"ok": True, "revoked": n})

    def _read_target(self, raw: str):
        """统一的读路径入口；不合法时已经回了 4xx，返回 None。"""
        try:
            return check_path(raw)
        except ValueError as exc:
            self._fail(400, str(exc))
        except PermissionError as exc:
            self._fail(403, str(exc))
        return None

    def api_list(self, q):
        target = self._read_target(q.get("path") or STATE.recv_dir)
        if not target:
            return None
        if not os.path.isdir(target):
            return self._fail(404, "文件夹不存在或无法访问")
        entries = []
        try:
            with os.scandir(target) as it:
                for e in it:
                    if e.name in SKIP_NAMES or e.name.startswith("$"):
                        continue
                    try:
                        is_dir = e.is_dir(follow_symlinks=False)
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    entries.append({
                        "name": e.name,
                        "path": e.path,
                        "dir": is_dir,
                        "size": 0 if is_dir else st.st_size,
                        "mtime": st.st_mtime,
                        "kind": "folder" if is_dir else kind_of(e.name),
                        "ext": "" if is_dir else os.path.splitext(e.name)[1].lower().lstrip("."),
                        "hidden": e.name.startswith("."),
                    })
        except PermissionError:
            return self._fail(403, "没有权限访问这个文件夹")
        except OSError:
            return self._fail(500, "读取目录失败")

        entries.sort(key=lambda x: (not x["dir"], natkey(x["name"])))

        # 目录内容签名 → ETag：内容没变就直接 304，省掉序列化和传输
        sig = zlib.crc32(("|".join(
            "%s:%s:%.0f" % (e["name"], e["size"], e["mtime"]) for e in entries)).encode("utf-8", "replace"))
        etag = '"L%d-%08x"' % (len(entries), sig & 0xFFFFFFFF)
        if (self.headers.get("If-None-Match") or "").strip() == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return

        parent = os.path.dirname(target.rstrip("\\/"))
        if not parent or not os.path.isdir(parent) or parent == target:
            parent = None
        disk = {}
        try:
            usage = shutil.disk_usage(target)
            disk = {"total": usage.total, "free": usage.free}
        except Exception:
            pass
        self._json({
            "ok": True,
            "path": target,
            "name": os.path.basename(target.rstrip("\\/")) or target,
            "parent": parent,
            "entries": entries,
            "roots": quick_roots(),
            "recv_dir": STATE.recv_dir,
            "writable": inside(target, STATE.recv_dir),
            "disk": disk,
        }, extra={"ETag": etag, "Cache-Control": "no-cache"})

    def api_download(self, q):
        p = self._read_target(q.get("path") or "")
        if not p:
            return None
        if not os.path.isfile(p):
            return self._fail(404, "文件不存在")
        if q.get("log") == "1" and self.command != "HEAD":
            try:
                STATE.add_transfer(direction="down", name=os.path.basename(p),
                                   size=os.path.getsize(p), peer=self.client_address[0])
            except Exception:
                pass
        self._serve_file(p, inline=q.get("inline") == "1")

    def api_thumb(self, q):
        p = self._read_target(q.get("path") or "")
        if not p:
            return None
        if not os.path.isfile(p):
            return self._fail(404, "文件不存在")
        try:
            width = int(q.get("w") or 320)
        except (TypeError, ValueError):
            return self._fail(400, "参数无效")
        data = thumb_bytes(p, max(64, min(1600, width)))
        if data:
            return self._send(200, data, "image/jpeg", {"Cache-Control": "max-age=86400"})
        # 无 Pillow 或解码失败：回退原图
        return self._serve_file(p, inline=True)

    def api_preview(self, q):
        p = self._read_target(q.get("path") or "")
        if not p:
            return None
        if not os.path.isfile(p):
            return self._fail(404, "文件不存在")
        size = os.path.getsize(p)
        limit = 256 * 1024
        try:
            with open(p, "rb") as f:
                raw = f.read(limit)
        except OSError:
            return self._fail(500, "读取失败")
        if b"\x00" in raw[:4096]:
            return self._fail(415, "该文件不是文本文件")
        for enc in ("utf-8", "gb18030", "utf-16"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", "replace")
        self._json({"ok": True, "text": text, "truncated": size > limit, "size": size})

    def api_search(self, q):
        base = self._read_target(q.get("path") or STATE.recv_dir)
        if not base:
            return None
        kw = (q.get("q") or "").strip().lower()
        if not kw or not os.path.isdir(base):
            return self._json({"ok": True, "entries": [], "truncated": False})
        results = []
        scanned = 0
        deadline = time.time() + 8
        truncated = False
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_NAMES and not d.startswith("$")]
            if root.count(os.sep) - base.count(os.sep) >= 6:
                dirs[:] = []
            for name in dirs + files:
                if name in SKIP_NAMES:
                    continue
                scanned += 1
                if scanned > 40000 or time.time() > deadline:
                    truncated = True
                    break
                if kw in name.lower():
                    full = os.path.join(root, name)
                    try:
                        st = os.stat(full)
                        is_dir = os.path.isdir(full)
                    except OSError:
                        continue
                    results.append({
                        "name": name, "path": full, "dir": is_dir,
                        "size": 0 if is_dir else st.st_size, "mtime": st.st_mtime,
                        "kind": "folder" if is_dir else kind_of(name),
                        "ext": "" if is_dir else os.path.splitext(name)[1].lower().lstrip("."),
                        "rel": os.path.relpath(full, base),
                    })
                    if len(results) >= 300:
                        truncated = True
                        break
            if truncated:
                break
        results.sort(key=lambda x: (x["rel"].count(os.sep), x["name"].lower()))
        self._json({"ok": True, "entries": results, "truncated": truncated})

    def api_clip_add(self):
        try:
            data = self._json_body()
        except Exception:
            return self._fail(400, "请求格式错误", close=True)
        text = str(data.get("text") or "").strip()
        # 只有分隔线（—— / ----）也视为空内容（BUG-05）
        if not text or re.fullmatch(r"[—\-–_\s]+", text):
            return self._fail(400, "内容为空")
        src = str(data.get("from") or "手机")[:20]
        item = STATE.add_clip(text, src)
        self._json({"ok": True, "item": item})

    def api_clip_del(self):
        try:
            data = self._json_body()
        except Exception:
            return self._fail(400, "请求格式错误", close=True)
        cid = str(data.get("id") or "")
        with STATE.lock:
            STATE.clip = [c for c in STATE.clip if c["id"] != cid]
        self._json({"ok": True})

    def api_mkdir(self):
        try:
            data = self._json_body()
        except Exception:
            return self._fail(400, "请求格式错误", close=True)

        # 文件夹传输：按相对路径一次性建好多级目录（空文件夹也靠这个还原）
        rel = str(data.get("rel") or "").replace("\\", "/").strip("/")
        if rel:
            try:
                parts = [strict_name(p) for p in rel.split("/") if p and p not in (".", "..")]
            except ValueError as exc:
                return self._fail(400, str(exc))
            parts = [p for p in parts if p]
            if not parts:
                return self._fail(400, "路径无效")
            target = os.path.abspath(os.path.join(STATE.recv_dir, *parts))
            if not inside(target, STATE.recv_dir):
                return self._fail(403, "只能传到接收目录")
            try:
                os.makedirs(target, exist_ok=True)   # 幂等：已存在也算成功
            except OSError:
                return self._fail(500, "创建文件夹失败")
            return self._json({"ok": True, "path": target, "rel": "/".join(parts)})

        parent = os.path.abspath(str(data.get("path") or STATE.recv_dir))
        try:
            name = strict_name(str(data.get("name") or ""))
        except ValueError as exc:
            return self._fail(400, str(exc))
        if not inside(parent, STATE.recv_dir):
            return self._fail(403, "只能在接收目录内新建文件夹")
        target = unique_path(parent, name)
        try:
            os.makedirs(target, exist_ok=False)
        except OSError:
            return self._fail(500, "创建文件夹失败")
        self._json({"ok": True, "path": target})

    def api_rename(self):
        try:
            data = self._json_body()
        except Exception:
            return self._fail(400, "请求格式错误", close=True)
        src = os.path.abspath(str(data.get("path") or ""))
        try:
            name = strict_name(str(data.get("name") or ""))
        except ValueError as exc:
            return self._fail(400, str(exc))
        if not os.path.exists(src):
            return self._fail(404, "文件不存在")
        if not inside(os.path.dirname(src), STATE.recv_dir):
            return self._fail(403, "只能在接收目录内重命名")
        dst = os.path.join(os.path.dirname(src), name)
        if os.path.exists(dst) and os.path.normcase(dst) != os.path.normcase(src):
            return self._fail(409, "同名文件已存在")
        try:
            os.rename(src, dst)
        except OSError:
            return self._fail(500, "重命名失败")
        self._json({"ok": True, "path": dst})

    def api_delete(self):
        try:
            data = self._json_body()
        except Exception:
            return self._fail(400, "请求格式错误", close=True)
        raw = [os.path.abspath(str(p)) for p in (data.get("paths") or []) if p]
        if not raw:
            return self._fail(400, "缺少路径")
        if len(raw) > 500:
            return self._fail(400, "一次删除的文件过多")
        # 去重 + 父子同选时只保留父目录（修复 X-28：父+子同时提交会占住线程）
        seen = set()
        paths = []
        for p in raw:
            k = os.path.normcase(p)
            if k not in seen:
                seen.add(k)
                paths.append(p)
        paths = [p for p in paths
                 if not any(os.path.normcase(p) != os.path.normcase(q) and lexical_inside(p, q)
                            for q in paths)]
        paths = [p for p in paths if os.path.exists(p)]
        if not paths:
            return self._fail(404, "没有可删除的文件")
        # 接收目录本身不能删（修复 X-02：以前 inside(recv, recv) 为真，整个目录会进回收站）
        recv_norm = os.path.normcase(os.path.abspath(STATE.recv_dir))
        if any(os.path.normcase(p) == recv_norm for p in paths):
            return self._fail(403, "不能删除接收目录本身")
        # 与 rename / mkdir 保持一致：只允许改动接收目录内的文件，
        # 避免配对设备（或盗用配对码者）删除系统里的任意文件。
        # 这里用字符串路径判断，junction 本身可以删（删链接不会动到目标）。
        outside = [p for p in paths if not lexical_inside(p, STATE.recv_dir)]
        if outside:
            return self._fail(403, "只能删除接收目录内的文件；其它位置请在电脑上操作",
                              blocked=len(outside))
        # 父目录若被 junction 指到外面，删除会作用到目标，直接拒绝
        for p in paths:
            parent = os.path.dirname(p)
            if os.path.exists(parent) and not inside(parent, STATE.recv_dir):
                return self._fail(403, "该路径指向接收目录之外，已拒绝")
        ok = recycle(paths)
        if not ok:
            return self._fail(500, "删除失败（已取消，不会永久删除）")
        self._json({"ok": True, "count": len(paths)})

    def api_zip(self):
        try:
            data = self._json_body()
        except Exception:
            return self._fail(400, "请求格式错误", close=True)
        return self._zip_paths(data.get("paths") or [])

    def api_zip_get(self, q):
        """GET 版本：安卓 WebView 无法用 fetch 接住 blob 下载，只能走导航交给 DownloadListener。"""
        raw = q.get("paths") or "[]"
        try:
            paths = json.loads(raw)
        except Exception:
            paths = [raw]
        if not isinstance(paths, list):
            paths = [paths]
        return self._zip_paths(paths)

    def _zip_paths(self, raw_paths):
        raw = [str(p) for p in raw_paths if p]
        paths = []
        for p in raw:
            try:
                paths.append(check_path(p))
            except ValueError as exc:
                return self._fail(400, str(exc))
            except PermissionError as exc:
                return self._fail(403, str(exc))
        paths = [p for p in paths if os.path.exists(p)]
        if not paths:
            return self._fail(404, "没有可打包的文件")
        max_bytes = 8 << 30      # 单次打包上限 8 GB，避免塞满临时盘（修复 DL-07）
        max_files = 20000
        total = 0
        count = 0
        tmp = tempfile.NamedTemporaryFile(prefix="droplink_", suffix=".zip", delete=False)
        tmp.close()
        try:
            with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
                for p in paths[:300]:
                    if os.path.isfile(p):
                        size = os.path.getsize(p)
                        total += size
                        count += 1
                        if total > max_bytes or count > max_files:
                            raise OverflowError("打包内容过大")
                        z.write(p, arcname=os.path.basename(p))
                    elif os.path.isdir(p):
                        base = os.path.dirname(p.rstrip("\\/"))
                        for root, dirs, files in os.walk(p):
                            dirs[:] = [d for d in dirs if d not in SKIP_NAMES]
                            # 目录条目必须显式写入，否则空文件夹在解压后会丢失
                            if not files and not dirs:
                                rel_dir = os.path.relpath(root, base).replace("\\", "/") + "/"
                                try:
                                    z.writestr(zipfile.ZipInfo(rel_dir), b"")
                                except OSError:
                                    pass
                            for f in files:
                                if f in SKIP_NAMES:
                                    continue
                                fp = os.path.join(root, f)
                                try:
                                    size = os.path.getsize(fp)
                                except OSError:
                                    continue
                                total += size
                                count += 1
                                if total > max_bytes or count > max_files:
                                    raise OverflowError("打包内容过大")
                                try:
                                    z.write(fp, os.path.relpath(fp, base))
                                except OSError:
                                    continue
            name = f"DropLink_{time.strftime('%Y%m%d_%H%M%S')}.zip"
            self._serve_file(tmp.name, download_name=name)
        except OverflowError:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            return self._fail(413, "打包内容过大，请分批下载")
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    def api_share(self):
        try:
            data = self._json_body()
        except Exception:
            return self._fail(400, "请求格式错误", close=True)
        p = str(data.get("path") or "")
        try:
            p = check_path(p)
        except ValueError as exc:
            return self._fail(400, str(exc))
        except PermissionError as exc:
            return self._fail(403, str(exc))
        if not os.path.isfile(p):
            return self._fail(404, "文件不存在")
        token = secrets.token_urlsafe(12).replace("-", "").replace("_", "")
        STATE.shares[token] = {"path": p, "exp": time.time() + 86400}
        STATE.cleanup()
        # 用户可指定用哪个网络 IP 生成链接（多网卡时避免选错网段）
        host = str(data.get("host") or "").strip()
        allow = allowed_hosts(self.server.server_port)
        if host:
            bare = host.rsplit(":", 1)[0] if ":" in host and not host.endswith("]") else host
            if bare.strip("[]").lower() not in allow:
                return self._fail(400, "不允许使用该地址生成分享链接")
            if ":" not in host:
                host = f"{host}:{self.server.server_port}"
        else:
            host = self.headers.get("Host") or ""
            bare = (host.rsplit(":", 1)[0] if ":" in host else host).strip("[]").lower()
            if not host or bare not in allow or bare in ("127.0.0.1", "localhost", "::1"):
                urls = lan_urls(self.server.server_port)
                host = urls[0].replace("http://", "") if urls else f"127.0.0.1:{self.server.server_port}"
        self._json({"ok": True, "url": f"http://{host}/dl/{token}"})

    def api_set_config(self):
        if not self._local_ui():
            return self._fail(403, "只有电脑本机可以修改设置")
        try:
            data = self._json_body()
        except Exception:
            return self._fail(400, "请求格式错误", close=True)
        target = str(data.get("recv_dir") or "").strip().strip('"')
        if not target:
            return self._fail(400, "路径不能为空")
        target = os.path.abspath(target)
        if looks_unc(target):
            return self._fail(400, "接收目录必须是本地路径")
        if has_ads(target):
            return self._fail(400, "路径无效")
        drive, tail = os.path.splitdrive(target)
        if tail in ("\\", "/"):
            return self._fail(400, "不能把盘根设为接收目录")
        for root in _sensitive_roots():
            if inside(target, root):
                return self._fail(403, "不能把系统目录设为接收目录")
        try:
            os.makedirs(target, exist_ok=True)
        except OSError:
            return self._fail(400, "无法创建该目录")
        real = os.path.realpath(target)
        for root in _sensitive_roots():
            if inside(real, root):
                return self._fail(403, "不能把系统目录设为接收目录")
        STATE.recv_dir = target
        STATE.config["recv_dir"] = target
        STATE.save()
        self._json({"ok": True, "recv_dir": target})

    def api_open(self):
        if not self._local_ui():
            return self._fail(403, "只有电脑本机可以打开文件夹")
        try:
            data = self._json_body()
        except Exception:
            return self._fail(400, "请求格式错误", close=True)
        p = str(data.get("path") or STATE.recv_dir)
        if os.path.isfile(p):
            os.startfile(os.path.dirname(p))  # noqa: S606
        elif os.path.isdir(p):
            os.startfile(p)  # noqa: S606
        else:
            return self._fail(404, "路径不存在")
        self._json({"ok": True})

    def api_firewall_set(self):
        if not self._local_ui():
            return self._fail(403, "只有电脑本机可以设置防火墙")
        lines = [
            'netsh advfirewall firewall delete rule name="DropLink Port" >nul 2>&1',
            'netsh advfirewall firewall add rule name="DropLink Port" dir=in action=allow protocol=TCP localport=8848-8867 profile=any >nul 2>&1',
        ]
        done, code = _runas_bat(lines)
        if not done:
            return self._json({"ok": False, "cancelled": True, "msg": str(code)})
        if code != 0:
            return self._json({"ok": False, "msg": f"执行失败（退出码 {code}）"})
        return self._json({"ok": True, "msg": "防火墙规则已添加"})

    def api_firewall_remove(self):
        if not self._local_ui():
            return self._fail(403, "只有电脑本机可以设置防火墙")
        lines = [
            'netsh advfirewall firewall delete rule name="DropLink Port" >nul 2>&1',
            'netsh advfirewall firewall delete rule name="DropLink Program" >nul 2>&1',
            'netsh advfirewall firewall delete rule name="DropLink 8848" >nul 2>&1',
        ]
        done, code = _runas_bat(lines)
        if not done:
            return self._json({"ok": False, "cancelled": True, "msg": str(code)})
        return self._json({"ok": True, "msg": "防火墙规则已删除"})

    def api_diag(self):
        if not self._local_ui():
            return self._fail(403, "只有电脑本机可以诊断")
        return self._json({
            "ok": True,
            "urls": lan_urls(self.server.server_port),
            "fw_rule": _has_fw_rule(),
            "fw_detail": _fw_rule_detail(),
            "profiles": _net_profiles(),
        })

    def api_networks(self):
        if not self._local_ui():
            return self._fail(403, "只有电脑本机可以查看")
        return self._json({
            "ok": True,
            "port": self.server.server_port,
            "networks": _all_ips(),
        })

    def api_live(self):
        """正在进行的下载（本机发起的发送），带实时速率。"""
        if not self._authed():
            return self._fail(401, "需要配对码")
        now = time.time()
        items = []
        with _LIVE_LOCK:
            for v in _LIVE.values():
                el = max(0.2, now - v["start"])
                items.append({
                    "name": v["name"], "total": v["total"], "sent": v["sent"],
                    "speed": int(v["sent"] / el), "peer": v["peer"],
                    "elapsed": round(el, 1),
                })
        return self._json({"ok": True, "items": items})

    def api_peers(self):
        if not self._local_ui():
            return self._fail(403, "只有电脑本机可以查看")
        with STATE.lock:
            peers = [{"ip": k, "ts": v["ts"], "name": v.get("name") or device_name_of(v.get("ua", ""))}
                     for k, v in STATE.peers.items()]
        peers.sort(key=lambda x: -x["ts"])
        return self._json({"ok": True, "peers": peers})

    def api_send(self, q):
        """电脑端向指定手机（或多台）推送文件。body 为文件内容，query 带 name/targets。"""
        if not self._local_ui():
            return self._fail(403, "只有电脑本机可以发送")
        name = safe_name(q.get("name") or "file")
        targets_raw = str(q.get("targets") or "all").strip()
        targets = [t.strip() for t in targets_raw.split(",") if t.strip()]
        if not targets or targets == ["all"]:
            targets = ["all"]

        send_id = secrets.token_hex(6)
        dest = os.path.join(STATE.send_dir, f"{send_id}_{name}")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        received = 0
        try:
            with open(dest, "wb") as f:
                while received < length:
                    chunk = self.rfile.read(min(CHUNK, length - received))
                    if not chunk:
                        break
                    f.write(chunk)
                    received += len(chunk)
        except Exception as exc:
            return self._fail(500, f"接收文件失败：{exc}")
        if received == 0:
            os.unlink(dest)
            return self._fail(400, "文件为空")

        size = os.path.getsize(dest)
        item = {
            "id": send_id, "name": name, "path": dest, "size": size,
            "targets": targets, "ack": {}, "ts": time.time(),
        }
        with STATE.lock:
            STATE.pending.append(item)
        try:
            purge_send_cache()   # 顺手回收过期暂存，避免无限增长
        except Exception:
            pass
        self._json({"ok": True, "id": send_id, "name": name, "size": size, "targets": targets})

    def api_inbox(self):
        """手机端查询发给自己的待接收文件。"""
        if not self._authed():
            self.close_connection = True
            return self._fail(401, "需要配对码")
        ip = self.client_address[0]
        out = []
        with STATE.lock:
            for item in STATE.pending:
                if ip in item.get("ack", {}):
                    continue
                if item["targets"] != ["all"] and ip not in item["targets"]:
                    continue
                out.append({
                    "id": item["id"], "name": item["name"], "size": item["size"],
                    "url": f"/api/download?path={urllib.parse.quote(item['path'])}",
                })
        return self._json({"ok": True, "items": out})

    def api_inbox_ack(self):
        """手机确认已接收（下载完成后调用）。"""
        if not self._authed():
            self.close_connection = True
            return self._fail(401, "需要配对码")
        try:
            data = self._json_body()
        except Exception:
            return self._fail(400, "请求格式错误", close=True)
        ip = self.client_address[0]
        item_id = str(data.get("id") or "")
        with STATE.lock:
            for item in STATE.pending:
                if item["id"] == item_id:
                    item.setdefault("ack", {})[ip] = time.time()
                    break
        self._json({"ok": True})

    def api_open_hotspot(self):
        if not self._local_ui():
            return self._fail(403, "只有电脑本机可以操作")
        try:
            os.startfile("ms-settings:network-mobilehotspot")  # noqa: S606
        except Exception as exc:
            return self._fail(500, f"无法打开设置：{exc}")
        self._json({"ok": True})

    # ---------------- 上传 ----------------

    def api_upload(self, q):
        if not self._authed():
            self.close_connection = True
            return self._fail(401, "需要配对码")

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self.close_connection = True
            return self._fail(411, "缺少文件长度")
        if length > 64 * 1024 ** 3:
            self.close_connection = True
            return self._fail(413, "文件过大")

        rel = str(q.get("rel") or "").replace("\\", "/")
        try:
            parts = [strict_name(p) for p in rel.split("/") if p and p not in (".", "..")]
            name = strict_name(q.get("name") or (parts[-1] if parts else "file"), "file")
        except ValueError as exc:
            self.close_connection = True
            return self._fail(400, str(exc))
        # rel 既可能是「目录」（配合 name），也可能是「目录/文件名」（浏览器 webkitRelativePath）。
        # 只有最后一段与文件名一致时才把它当文件名去掉，否则全部当目录。
        sub_dirs = parts[:-1] if (parts and parts[-1] == name) else parts

        dest_dir = os.path.join(STATE.recv_dir, *sub_dirs) if sub_dirs else STATE.recv_dir
        dest_dir = os.path.abspath(dest_dir)
        if not inside(dest_dir, STATE.recv_dir):
            self.close_connection = True
            return self._fail(403, "只能上传到接收目录")
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError:
            self.close_connection = True
            return self._fail(500, "无法创建目录")
        # 目录建好后再确认一次：已有 junction 会把写入引到接收目录之外（修复 X-03）
        if not inside(dest_dir, STATE.recv_dir):
            self.close_connection = True
            return self._fail(403, "该目录指向接收目录之外")

        try:
            free = shutil.disk_usage(dest_dir).free
        except OSError:
            free = 0
        if free and free < length + (256 << 20):
            self.close_connection = True
            return self._fail(507, "磁盘剩余空间不足")

        tmp_path = os.path.join(dest_dir, f".dl_{secrets.token_hex(4)}.part")
        started = time.time()
        written = 0
        try:
            with open(tmp_path, "wb") as f:
                left = length
                while left > 0:
                    buf = self.rfile.read(min(CHUNK, left))
                    if not buf:
                        break
                    f.write(buf)
                    written += len(buf)
                    left -= len(buf)
                f.flush()
                os.fsync(f.fileno())
        except Exception:  # noqa: BLE001
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            self.close_connection = True
            return self._fail(500, "写入文件失败")

        if written != length:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            self.close_connection = True
            return self._fail(400, f"文件不完整（{written}/{length}）")

        try:
            target = claim_path(tmp_path, dest_dir, name)   # 原子抢名，防并发覆盖（X-04）
        except RuntimeError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return self._fail(500, "无法分配文件名")
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return self._fail(500, "保存文件失败")

        risky = mark_risky_file(target)

        mtime = self.headers.get("X-File-Mtime")
        if mtime:
            try:
                ts = float(mtime)
                if ts > 1e12:
                    ts /= 1000.0
                os.utime(target, (ts, ts))
            except Exception:
                pass

        elapsed = max(0.001, time.time() - started)
        STATE.add_transfer(direction="up", name=os.path.basename(target), size=length,
                           peer=self.client_address[0], speed=length / elapsed)
        print(f"[接收] {os.path.basename(target)}  {human_size(length)}  "
              f"{human_size(int(length / elapsed))}/s")

        self._json({"ok": True, "saved": target, "name": os.path.basename(target),
                    "size": length, "elapsed": round(elapsed, 2),
                    "risky": risky})


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------

def open_app_window(url: str) -> bool:
    """优先用 Edge / Chrome 的 app 模式打开，看起来像一个独立应用窗口。"""
    bases = [
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    rels = [
        r"Microsoft\Edge\Application\msedge.exe",
        r"Google\Chrome\Application\chrome.exe",
    ]
    for base in bases:
        if not base:
            continue
        for rel in rels:
            exe = os.path.join(base, rel)
            if not os.path.isfile(exe):
                continue
            try:
                subprocess.Popen(
                    [exe, f"--app={url}", "--window-size=1200,880", "--no-first-run"],
                    creationflags=0x00000008 | CREATE_NO_WINDOW,  # DETACHED_PROCESS | 不弹控制台
                    close_fds=True,
                )
                return True
            except Exception:
                continue
    try:
        webbrowser.open(url)
        return True
    except Exception:
        return False


def print_banner(port: int, urls):
    line = "=" * 62
    print()
    print(line)
    print(f"  {APP_NAME}  v{VERSION}   ·   同一个 Wi-Fi，手机电脑互传文件")
    print(line)
    print(f"  电脑打开：  http://127.0.0.1:{port}")
    if urls:
        print(f"  手机打开：  {urls[0]}")
        for extra in urls[1:]:
            print(f"              {extra}")
    else:
        print("  手机打开：  未检测到局域网地址（请先连接 Wi-Fi 或开热点）")
    print(f"  配对码  ：  {STATE.pin}      （手机首次打开需输入）")
    print(f"  接收目录：  {STATE.recv_dir}")
    print(line)
    print("  提示：手机连同一个 Wi-Fi / 电脑热点即可，不经过互联网，不耗手机流量。")
    print("        手机浏览器菜单里「添加到主屏幕」，可当 App 用。")
    print("        关闭此窗口或按 Ctrl+C 即停止服务。")
    print(line)
    if urls:
        art = qr_ascii(urls[0])
        if art:
            print()
            print("  手机可以直接扫这个二维码连接：")
            print()
            for ln in art:
                print("    " + ln)
    print()


def qr_ascii(text: str):
    """把二维码渲染成终端里的字符画（半块字符，两行合并成一行）。"""
    try:
        import qrmini
        grid = qrmini.qr_matrix(text, border=2)
    except Exception:
        return None
    try:
        enc = getattr(sys.stdout, "encoding", "") or ""
        if "utf" not in enc.lower():
            return None
    except Exception:
        return None
    # 每两个模块合成一个字符行，保持方块比例
    pad = [False] * len(grid[0])
    rows = list(grid) + ([pad] if len(grid) % 2 else [])
    out = []
    for i in range(0, len(rows), 2):
        top, bot = rows[i], rows[i + 1]
        line = []
        for x in range(len(top)):
            a, b = bool(top[x]), bool(bot[x])
            line.append("█" if (a and b) else "▀" if a else "▄" if b else " ")
        out.append("".join(line))
    return out


def start_server(port: int = 8848, pin: str = "", recv: str = "",
                 background: bool = True):
    """启动服务。background=True 时在工作线程里跑，立刻返回运行信息。"""
    if pin:
        lo, hi = (8, 12) if SECURE_MODE else (4, 8)
        if not (pin.isdigit() and lo <= len(pin) <= hi):
            raise RuntimeError("配对码必须是 %d–%d 位数字" % (lo, hi))
        STATE.pin = pin
    if recv:
        target = os.path.abspath(recv)
        try:
            os.makedirs(target, exist_ok=True)
            STATE.recv_dir = target
            STATE.config["recv_dir"] = target
            STATE.save()
        except OSError as exc:
            print(f"接收目录不可用：{exc}")

    # 启动时清理推送暂存，避免历史副本长期占磁盘
    try:
        n, freed = purge_send_cache()
        if n:
            print(f"[清理] 推送暂存移除 {n} 个文件，释放 {freed / 1048576:.1f} MB")
    except Exception:
        pass

    # 清掉上次中断留下的临时文件（超过 24 小时的 .dl_*.part），避免长期占空间
    def _sweep_parts():
        try:
            deadline = time.time() - 86400
            for dirpath, _dirs, files in os.walk(STATE.recv_dir):
                for fn in files:
                    if fn.startswith(".dl_") and fn.endswith(".part"):
                        fp = os.path.join(dirpath, fn)
                        try:
                            if os.path.getmtime(fp) < deadline:
                                os.unlink(fp)
                        except OSError:
                            pass
        except Exception:
            pass

    threading.Thread(target=_sweep_parts, daemon=True).start()

    httpd = None
    for offset in range(0, 20):
        try:
            httpd = Server(("0.0.0.0", port + offset), Handler)
            port = port + offset
            break
        except OSError:
            continue
    if httpd is None:
        raise RuntimeError("端口被占用，无法启动")

    httpd.daemon_threads = True
    info = {
        "httpd": httpd,
        "port": port,
        "urls": lan_urls(port),
        "pin": STATE.pin,
        "local_token": LOCAL_TOKEN,
        "recv_dir": STATE.recv_dir,
    }
    # 定期清理过期会话/失败记录，避免长期运行内存增长（DL-09）
    def _janitor():
        while True:
            time.sleep(300)
            try:
                STATE.cleanup()
            except Exception:
                pass
    threading.Thread(target=_janitor, daemon=True).start()
    if background:
        threading.Thread(target=httpd.serve_forever,
                         kwargs={"poll_interval": 0.3}, daemon=True).start()
    return info


def main():
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--port", type=int, default=8848, help="监听端口，默认 8848")
    ap.add_argument("--pin", default="", help="固定配对码（默认每次随机）")
    ap.add_argument("--recv", default="", help="指定接收目录")
    ap.add_argument("--no-browser", action="store_true", help="启动时不自动打开浏览器")
    ap.add_argument("--version", "-V", action="version",
                    version="%s %s · HU4222" % (APP_NAME, VERSION),
                    help="输出版本号与作者后退出")
    ap.add_argument("--secure", action="store_true",
                    help="启用安全加固（Host/Origin 校验、本机令牌、敏感目录限制、8 位配对码）")
    args = ap.parse_args()
    global SECURE_MODE
    if args.secure:
        SECURE_MODE = True

    try:
        info = start_server(args.port, args.pin, args.recv, background=True)
    except RuntimeError as exc:
        print(exc)
        return 1

    port, urls = info["port"], info["urls"]
    print_banner(port, urls)

    if not args.no_browser:
        def _open():
            time.sleep(0.8)
            open_app_window(f"http://127.0.0.1:{port}/?k={LOCAL_TOKEN}")
        threading.Thread(target=_open, daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        info["httpd"].shutdown()
        info["httpd"].server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
