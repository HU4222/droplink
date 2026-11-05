# -*- coding: utf-8 -*-
"""写入侧路径强约束 —— S-06，安全版专用。

个人版保持现有 fsutil.strict_name + inside() 的宽松口径；安全版在落盘前必须再过
这一层，拦住「目录段漏检 / 保留名 / ADS / 结尾点空格 / 符号链接 / 越界」这些
Windows 特有的坑。

三层关系：
    strict_name()      基础拦截（非法字符、保留名的一部分）
    pathguard.audit()  加严的静态体检（逐段）
    pathguard.resolve/check  真实路径归一 + 包含关系 + 重解析点

任何一层不干净都整单拒绝，不做「修一修继续写」。
"""

from __future__ import annotations

import os
import re
import unicodedata

AVAILABLE = True

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
MAX_SEGMENT_UNITS = 255
MAX_PATH_UNITS = 32767

_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "CONIN$", "CONOUT$",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}

# 零宽 / 双向控制：肉眼看不见，但会让「同名文件」实际指向别处
_INVISIBLE = {
    "\u200b", "\u200c", "\u200d", "\ufeff",
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
    "\u2066", "\u2067", "\u2068", "\u2069",
}

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')


def audit(name: str) -> list:
    """静态体检：返回问题列表（空列表 = 干净）。只检查「一段名字」，不含分隔符。"""
    issues = []
    if not isinstance(name, str) or not name:
        return ["文件名为空"]
    # NFC/NFD 两种写法在 NTFS 上指向同一个文件，统一成 NFC 再判
    name = unicodedata.normalize("NFC", name)
    if name in (".", ".."):
        return ["文件名不能是 . 或 .."]
    if len(name) > MAX_PATH_UNITS:
        issues.append("文件名过长")
    if len(name.encode("utf-16-le")) // 2 > MAX_SEGMENT_UNITS:
        issues.append("文件名超过 255 个字符")
    if "/" in name or "\\" in name:
        issues.append("文件名不能包含路径分隔符")
    if ":" in name:
        issues.append("文件名不能包含备用数据流（ADS）")
    bad = _BAD_CHARS.findall(name)
    if bad:
        issues.append("文件名包含非法字符")
    if name != name.rstrip(" ."):
        issues.append("文件名不能以点或空格结尾")
    for ch in name:
        if ch in _INVISIBLE:
            issues.append("文件名包含不可见字符")
            break
    stem = name.rstrip(" .").split(".")[0].upper()
    if stem in _RESERVED:
        issues.append("文件名是 Windows 保留名（%s）" % stem)
    return issues


def _inside(base: str, real: str) -> bool:
    """真实路径包含判断：必须用 commonpath + normcase，不能 startswith。

    不同盘符时 commonpath 会抛 ValueError，按「越界」处理。
    """
    try:
        common = os.path.commonpath([os.path.normcase(base), os.path.normcase(real)])
    except ValueError:
        return False
    return common == os.path.normcase(base)


def resolve_write_target(recv_dir: str, name: str) -> str:
    """把一段文件名解析成接收目录内的最终落盘路径（不做 IO）。"""
    issues = audit(name)
    if issues:
        raise ValueError("；".join(issues))
    base = os.path.realpath(recv_dir)
    target = os.path.abspath(os.path.join(base, name))
    real = os.path.realpath(target)
    if not _inside(base, real):
        raise ValueError("目标路径越出接收目录")
    return target


def _has_reparse(path: str, base: str) -> bool:
    """target 到 base（含）之间有没有符号链接 / 目录联接。"""
    current = os.path.abspath(path)
    base = os.path.abspath(base)
    while True:
        try:
            st = os.stat(current, follow_symlinks=False)
            attrs = getattr(st, "st_file_attributes", 0)
            if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                return True
        except OSError:
            pass
        if os.path.normcase(current) == os.path.normcase(base):
            return False
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def check_upload_path(recv_dir: str, path: str) -> str:
    """校验一个（可能还不存在的）写入路径，返回归一后的真实路径。"""
    base = os.path.realpath(recv_dir)
    real = os.path.realpath(path)
    if not _inside(base, real):
        raise ValueError("目标路径越出接收目录")
    drive, tail = os.path.splitdrive(real)
    if ":" in tail:
        raise ValueError("路径包含备用数据流（ADS）")
    if _has_reparse(real, base):
        raise ValueError("路径包含符号链接或目录联接")
    return real


def check_recv_dir(recv_dir: str) -> str:
    """启动期检查接收目录：安全版不接受 UNC / 网络路径。"""
    p = str(recv_dir or "")
    if p.replace("/", "\\").startswith("\\\\"):
        raise ValueError("安全版不允许把接收目录设在网络路径（UNC）上")
    return os.path.realpath(p)
