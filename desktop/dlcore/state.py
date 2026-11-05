# -*- coding: utf-8 -*-
"""运行期状态：会话、剪贴板、传输记录、配置。

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
from .config import is_secure
from .fsutil import known_folder  # auto-added
from .fsutil import *  # 常量与工具（MAX_SESSIONS 等）

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
                           for _ in range(8 if is_secure() else 6))
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

    def new_session(self, ip: str, ua: str, device_id: str = "") -> str:
        token = secrets.token_urlsafe(24)
        now = time.time()
        with self.lock:
            self.sessions[token] = {"ts": now, "last": now, "ip": ip, "ua": ua[:120],
                                    "device_id": device_id}
            # 超出上限时踢掉最久未使用的
            if len(self.sessions) > MAX_SESSIONS:
                old = sorted(self.sessions.items(), key=lambda kv: kv[1]["last"])
                for k, _v in old[: len(self.sessions) - MAX_SESSIONS]:
                    self.sessions.pop(k, None)
        return token

    def session_valid(self, token: str) -> bool:
        if not token:
            return False
        if not is_secure():
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

    def revoke_device_sessions(self, device_id: str) -> int:
        """撤销某台已信任设备时，把它所有会话立刻作废（下一次请求 401）。"""
        if not device_id:
            return 0
        with self.lock:
            dead = [k for k, v in self.sessions.items() if v.get("device_id") == device_id]
            for k in dead:
                self.sessions.pop(k, None)
            return len(dead)

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


