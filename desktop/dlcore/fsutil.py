# -*- coding: utf-8 -*-
"""安全基线与文件系统工具：路径校验、命名、列目录、回收站。

由 server.py 拆分而来（2026-11-04）。纯搬移，不改逻辑。
"""
from __future__ import annotations

import ctypes
import gzip
import hashlib
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
from .config import *
from .config import is_secure

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

    个人使用模式（is_secure()=False）下只做最基本的合法性检查，
    恢复加固前的「手机可浏览整台电脑」行为。
    """
    if not isinstance(p, str) or not p.strip():
        raise ValueError("路径不能为空")
    if "\x00" in p:
        raise ValueError("路径无效")
    p = os.path.abspath(p)
    if not is_secure():
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
    if not is_secure():
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


