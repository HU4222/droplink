# -*- coding: utf-8 -*-
"""设备信任 + 首次配对审批 —— 第二轮 B，对应方案 S-02 的落地版。

为什么不是「证书指纹固定」：
    客户端是浏览器 / 安卓 WebView，拿不到对端证书 DER，做不了 pinning。
    这里改成对 DropLink 真正有效的三件事：
        1. 设备登记：客户端首次访问生成 device_id（localStorage），登录时上报；
        2. 首次配对挂起：未登记设备登录只拿到 request_id，必须等电脑本机点「批准」；
        3. 信任列表可撤销：撤销后该设备已有会话立即失效，重新登录回到待批准。
威胁模型：同一 Wi-Fi 下的人拿到/猜中配对码，也读不到整台电脑——除非你在他面前点了批准。

设备表落盘在 <应用目录>/config/trusted_devices.json（随配置目录走，EXE 升级不丢）；
待批准队列只在内存里，服务重启即作废（可接受）。

运行期可用 DROPLINK_TRUST_FILE 指定设备表路径、DROPLINK_APPROVAL_TTL 调审批超时（测试用）。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time

AVAILABLE = True

PENDING_TTL = float(os.environ.get("DROPLINK_APPROVAL_TTL") or 300)
_MAX_PENDING = 32

_LOCK = threading.RLock()
_devices = {}      # device_id -> record
_pending = {}      # request_id -> record
_loaded = False

_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def valid_device_id(device_id: str) -> bool:
    return bool(_DEVICE_ID_RE.match(device_id or ""))


def _app_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def trust_path() -> str:
    env = os.environ.get("DROPLINK_TRUST_FILE")
    if env:
        return os.path.abspath(env)
    return os.path.join(_app_dir(), "config", "trusted_devices.json")


def _now() -> float:
    return time.time()


def _load_locked() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        with open(trust_path(), "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        for item in data.get("devices") or []:
            if not isinstance(item, dict):
                continue
            did = str(item.get("device_id") or "")
            if valid_device_id(did):
                item.setdefault("first_seen", _now())
                item.setdefault("last_seen", item["first_seen"])
                _devices[did] = item
    except (OSError, ValueError):
        pass


def _save_locked() -> None:
    path = trust_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "devices": list(_devices.values())},
                      f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def _short(device_id: str) -> str:
    return (device_id or "")[:8]


def _device_view(d: dict) -> dict:
    return {
        "device_id": d.get("device_id", ""),
        "device_id_short": _short(d.get("device_id", "")),
        "name": d.get("name") or "未知设备",
        "platform": d.get("platform") or "",
        "first_seen": d.get("first_seen", 0),
        "last_seen": d.get("last_seen", 0),
        "last_ip": d.get("last_ip") or "",
        "auto_accept": bool(d.get("auto_accept")),
    }


def set_auto_accept(device_id: str, enabled: bool) -> bool:
    """该设备的「自动接受传输」开关（默认关，落盘）。"""
    with _LOCK:
        _load_locked()
        rec = _devices.get(device_id)
        if rec is None:
            return False
        rec["auto_accept"] = bool(enabled)
        _save_locked()
        return True


def list_devices() -> list:
    with _LOCK:
        _load_locked()
        items = [_device_view(d) for d in _devices.values()]
    items.sort(key=lambda x: x.get("last_seen") or 0, reverse=True)
    return items


def get(device_id: str):
    with _LOCK:
        _load_locked()
        rec = _devices.get(device_id)
        return dict(rec) if rec else None


def is_trusted(device_id: str) -> bool:
    return get(device_id) is not None


def _trust_locked(device_id: str, name: str, platform: str, ua: str, ip: str) -> dict:
    now = _now()
    rec = _devices.get(device_id)
    if rec is None:
        rec = {
            "device_id": device_id,
            "name": (name or "未知设备")[:40],
            "platform": (platform or "")[:40],
            "ua": (ua or "")[:120],
            "first_seen": now,
            "last_seen": now,
            "last_ip": ip,
            "trusted_at": now,
        }
        _devices[device_id] = rec
    else:
        if name:
            rec["name"] = name[:40]
        if platform:
            rec["platform"] = platform[:40]
        rec["last_seen"] = now
        rec["last_ip"] = ip
    return rec


def trust(device_id: str, name: str = "", platform: str = "", ua: str = "", ip: str = "") -> dict:
    """直接登记一台设备（审批通过时调用）。"""
    with _LOCK:
        _load_locked()
        rec = _trust_locked(device_id, name, platform, ua, ip)
        _save_locked()
        return dict(rec)


def touch(device_id: str, ip: str) -> None:
    with _LOCK:
        _load_locked()
        rec = _devices.get(device_id)
        if rec is None:
            return
        rec["last_seen"] = _now()
        rec["last_ip"] = ip
        _save_locked()


def revoke(device_id: str) -> bool:
    with _LOCK:
        _load_locked()
        if device_id not in _devices:
            return False
        _devices.pop(device_id, None)
        _save_locked()
        return True


# --------------------------------------------------------------------------
# 待批准队列（内存）
# --------------------------------------------------------------------------

def _prune_locked(now: float) -> None:
    # 过期后保留 10 分钟「墓碑」，让轮询方能拿到 status="expired" 而不是 404
    dead = [k for k, v in _pending.items() if now >= v["expires_at"] + 600]
    for k in dead:
        _pending.pop(k, None)


def request_pair(device_id: str, name: str, platform: str, ua: str, ip: str) -> dict:
    """登记一次待批准请求；同一设备重复登录复用同一个 request_id。"""
    with _LOCK:
        _load_locked()
        now = _now()
        _prune_locked(now)
        for rec in _pending.values():
            if rec["device_id"] == device_id and rec["status"] == "pending":
                return dict(rec)
        if len(_pending) >= _MAX_PENDING:
            oldest = sorted(_pending.values(), key=lambda r: r["first_seen"])[0]
            _pending.pop(oldest["request_id"], None)
        rid = secrets.token_hex(8)
        rec = {
            "request_id": rid,
            "device_id": device_id,
            "name": (name or "未知设备")[:40],
            "platform": (platform or "")[:40],
            "ua": (ua or "")[:120],
            "ip": ip,
            "first_seen": now,
            "expires_at": now + PENDING_TTL,
            "status": "pending",
            "session_token": "",
        }
        _pending[rid] = rec
        return dict(rec)


def pending_list() -> list:
    with _LOCK:
        now = _now()
        _prune_locked(now)
        items = []
        for r in _pending.values():
            if r["status"] != "pending":
                continue
            if now >= r["expires_at"]:
                r["status"] = "expired"      # 超时的当场标记，不再出现在待批准列表
                continue
            items.append({
                "request_id": r["request_id"],
                "device_id_short": _short(r["device_id"]),
                "name": r["name"],
                "platform": r["platform"],
                "ip": r["ip"],
                "first_seen": r["first_seen"],
                "expires_in": max(0, int(r["expires_at"] - now)),
            })
    items.sort(key=lambda x: x["first_seen"])
    return items


def status(request_id: str):
    """请求状态：pending / approved / rejected / expired。

    过期不再直接删记录：保留 10 分钟墓碑，客户端能明确拿到 expired（P2-1）。
    """
    with _LOCK:
        now = _now()
        _prune_locked(now)
        rec = _pending.get(request_id)
        if rec is None:
            return None
        if rec["status"] == "pending" and now >= rec["expires_at"]:
            rec["status"] = "expired"
        return dict(rec)


def approve(request_id: str):
    """批准：写入信任列表，返回请求记录（含 ip/ua/device_id）供发会话。"""
    with _LOCK:
        _load_locked()
        now = _now()
        _prune_locked(now)
        rec = _pending.get(request_id)
        if rec is None:
            return None
        if rec["status"] == "pending" and now >= rec["expires_at"]:
            rec["status"] = "expired"
        if rec["status"] != "pending":
            return None
        rec["status"] = "approved"
        rec["approved_at"] = now
        _trust_locked(rec["device_id"], rec["name"], rec["platform"], rec["ua"], rec["ip"])
        _save_locked()
        return dict(rec)


def reject(request_id: str) -> bool:
    with _LOCK:
        now = _now()
        _prune_locked(now)
        rec = _pending.get(request_id)
        if rec is None:
            return False
        if rec["status"] == "pending" and now >= rec["expires_at"]:
            rec["status"] = "expired"
        if rec["status"] != "pending":
            return False
        rec["status"] = "rejected"
        rec["rejected_at"] = now
        return True


def attach_session(request_id: str, token: str) -> None:
    with _LOCK:
        rec = _pending.get(request_id)
        if rec is not None:
            rec["session_token"] = token
