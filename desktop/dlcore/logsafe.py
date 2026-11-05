# -*- coding: utf-8 -*-
"""日志脱敏（两版共用，不依赖 dlcore.sec）。

原则：
  * 路径只留文件名 + 文件名哈希前缀（不读盘，不暴露目录结构）；
  * 局域网 IP 打码，本机回环地址原样保留（排障最需要）；
  * PIN / token 一律 mask；
  * 异常文本里的绝对路径换成同样的脱敏形式。

只用于「会落到 stdout / 日志」的打印，不要拿它处理要回给客户端的 JSON。
"""

from __future__ import annotations

import hashlib
import os
import re
import time


def stamp() -> str:
    """日志行时间前缀：本地时区 HH:MM:SS。"""
    return time.strftime("%H:%M:%S")


def log(msg: str) -> None:
    """统一的事件日志出口：加时间前缀并立即 flush（P2-3）。"""
    print("%s %s" % (stamp(), msg), flush=True)

_MASK = "***"
_LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
_ABS_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s\"'<>|]+")


def mask(s) -> str:
    """PIN、token、密钥之类：不保留任何长度信息。"""
    return _MASK if s not in (None, "") else ""


def _hash8(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8", "replace")).hexdigest()[:8]


def redact_path(p) -> str:
    """E:\\a\\b\\报告.pdf -> …/报告.pdf#a1b2c3d4（哈希的是文件名，不读文件内容）。"""
    if not p:
        return ""
    text = str(p).replace("\\", "/").rstrip("/")
    base = text.rsplit("/", 1)[-1]
    if not base:
        return _MASK
    return "…/%s#%s" % (base, _hash8(base))


def redact_ip(ip) -> str:
    """IPv4 -> 192.168.1.*；IPv6 -> 前 4 组 + ::*；回环地址原样返回。"""
    text = str(ip or "").strip()
    if not text:
        return ""
    if text in _LOOPBACK:
        return text
    if ":" in text:
        groups = [g for g in text.split(":") if g]
        return ":".join(groups[:4]) + "::*"
    parts = text.split(".")
    if len(parts) == 4:
        return ".".join(parts[:3]) + ".*"
    return text


def safe_exc(exc: BaseException) -> str:
    """异常文本脱敏：类型名保留，绝对路径换成 …/文件名#hash。"""
    try:
        text = "%s: %s" % (type(exc).__name__, exc)
    except Exception:
        text = "Exception"
    return _ABS_PATH_RE.sub(lambda m: redact_path(m.group(0)), text)
