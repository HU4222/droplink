# -*- coding: utf-8 -*-
"""全局常量与运行模式开关。

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

# --------------------------------------------------------------------------
# 基础常量
# --------------------------------------------------------------------------

APP_NAME = "DropLink 互传"
APP_SHORT = "DropLink"
VERSION = "1.4.7"

# 个人使用模式（默认）：恢复安全加固前的宽松行为——
# 本机请求免配对、可浏览任意目录、6 位配对码、上传不写 MOTW。
# 需要重新打开加固：启动时加 --secure，或设置环境变量 DROPLINK_SECURE=1。
SECURE_MODE = os.environ.get("DROPLINK_SECURE") == "1"

# Windows 下隐藏 netsh/powershell 子进程的控制台窗口，避免启动/诊断时闪黑窗
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # dlcore/ 的上一层才是应用根
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


def set_secure(on: bool) -> None:
    """运行期切换个人模式 / 加固模式（--secure 或 DROPLINK_SECURE=1）。"""
    global SECURE_MODE
    SECURE_MODE = bool(on)


def is_secure() -> bool:
    """其它模块一律通过它读开关：直接 import 常量会拿到导入时的快照。"""
    return SECURE_MODE
