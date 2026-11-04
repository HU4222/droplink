# -*- coding: utf-8 -*-
"""图像相关：二维码 PNG、缩略图、应用图标。

由 server.py 拆分而来（2026-11-04）。纯搬移，不改逻辑。
"""
from __future__ import annotations

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
from .fsutil import *  # 路径与常量

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


