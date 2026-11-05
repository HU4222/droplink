# -*- coding: utf-8 -*-
"""传输前人工确认 + 逐文件一次性 token —— 第三轮，对应清单 S-03 / S-04。

安全版的上传改成两阶段：

    阶段一（元数据）
        POST /api/prepare-upload  {device_id, files:[{name,size,rel,sha256?}]}
        → 200 {session_id, state:"pending", expires_in}
        此时不接收任何字节，接收目录里不会出现文件（含 .part）。

    阶段二（逐文件）
        PC 本机接受后，每个文件签发一次性 token；
        POST /api/upload?session_id&file_id&token 才允许写入。
        token 绑定 session+file，用一次即作废；跨文件/重放/过期一律 403。

自动接受：信任表里按设备一个开关（默认关）。开了之后该设备的 prepare 直接进入
accepted 并下发 token；开关状态落盘、在信任列表里展示。

会话只放内存（服务重启即作废）；pending 默认 5 分钟过期，accepted 后给 30 分钟上传窗口。
"""

from __future__ import annotations

import os
import secrets
import threading
import time

AVAILABLE = True

PENDING_TTL = float(os.environ.get("DROPLINK_TRANSFER_TTL") or 300)   # 等待本机确认
UPLOAD_TTL = float(os.environ.get("DROPLINK_UPLOAD_TTL") or 1800)     # 接受后上传窗口
MAX_FILES = 200

_LOCK = threading.RLock()
_sessions = {}   # session_id -> record


def _now() -> float:
    return time.time()


def _prunable(rec, now: float) -> bool:
    """够不够格删掉这条会话记录（P3-1）。

    两个条件都满足才删：过期墓碑已过 10 分钟，且没有任何文件还在 uploading。
    正在写的会话如果被 prune，上传完成时就找不到会话了，会误报失败。
    """
    if now < rec["expires_at"] + 600:
        return False
    return not any(f["status"] == "uploading" for f in rec["files"])


def _prune(now: float) -> None:
    # 过期后保留 10 分钟墓碑：客户端能拿到 state="expired" 而不是「查无此单」
    dead = [k for k, v in _sessions.items() if _prunable(v, now)]
    for k in dead:
        _sessions.pop(k, None)


def _issued_tokens(rec) -> None:
    for f in rec["files"]:
        if f["status"] == "accepted" and not f["token"]:
            f["token"] = secrets.token_hex(16)


def _expire_if_needed(rec, now: float) -> None:
    if rec["state"] in ("pending", "accepted") and now >= rec["expires_at"]:
        rec["state"] = "expired"
        for f in rec["files"]:
            if f["status"] in ("pending", "accepted"):
                f["status"] = "expired"
            f["token"] = ""


def _file_view(f, with_token: bool) -> dict:
    out = {
        "file_id": f["file_id"],
        "name": f["name"],
        "size": f["size"],
        "rel": f.get("rel", ""),
        "status": f["status"],
    }
    if with_token and f.get("token"):
        out["token"] = f["token"]
    return out


def view(rec, with_tokens: bool = False) -> dict:
    now = _now()
    _expire_if_needed(rec, now)
    return {
        "session_id": rec["session_id"],
        "state": rec["state"],
        "device_name": rec.get("device_name") or "",
        "device_id_short": (rec.get("device_id") or "")[:8],
        "ip": rec.get("ip") or "",
        "files": [_file_view(f, with_tokens) for f in rec["files"]],
        "file_count": len(rec["files"]),
        "total_bytes": rec.get("total_bytes", 0),
        "expires_in": max(0, int(rec["expires_at"] - now)),
        "auto": bool(rec.get("auto")),
        "created": rec.get("created", 0),
    }


def prepare(device_id: str, device_name: str, ip: str, files: list,
            auto_accept: bool = False) -> dict:
    """登记一次待确认传输（不落盘）；auto_accept 时直接进入 accepted 并签发 token。"""
    now = _now()
    sid = secrets.token_hex(8)
    items = []
    for raw in files[:MAX_FILES]:
        name = str(raw.get("name") or "").strip()[:200]
        if not name:
            continue
        try:
            size = int(raw.get("size") or 0)
        except (TypeError, ValueError):
            size = -1
        if size < 0:
            continue
        items.append({
            "file_id": secrets.token_hex(6),
            "name": name,
            "size": size,
            "rel": str(raw.get("rel") or "")[:400],
            "sha256": str(raw.get("sha256") or "").strip().lower()[:64],
            "status": "pending",
            "token": "",
        })
    rec = {
        "session_id": sid,
        "device_id": device_id,
        "device_name": device_name[:40],
        "ip": ip,
        "files": items,
        "total_bytes": sum(f["size"] for f in items),
        "state": "pending",
        "created": now,
        "expires_at": now + PENDING_TTL,
        "auto": False,
    }
    with _LOCK:
        _prune(now)
        _sessions[sid] = rec
        if auto_accept and items:
            rec["state"] = "accepted"
            rec["auto"] = True
            rec["accepted_at"] = now
            rec["expires_at"] = now + UPLOAD_TTL
            for f in items:
                f["status"] = "accepted"
            _issued_tokens(rec)
        return view(rec, with_tokens=auto_accept)


def pending() -> list:
    with _LOCK:
        now = _now()
        _prune(now)
        out = []
        for rec in _sessions.values():
            _expire_if_needed(rec, now)
            if rec["state"] == "pending":
                out.append(view(rec))
    out.sort(key=lambda r: r["created"])
    return out


def status(session_id: str):
    with _LOCK:
        now = _now()
        _prune(now)
        rec = _sessions.get(session_id)
        if rec is None:
            return None
        _expire_if_needed(rec, now)
        return rec


def accept(session_id: str, file_ids=None):
    """本机接受；file_ids 给定时只接受其中列出的文件，其余标记 rejected。"""
    with _LOCK:
        rec = status(session_id)
        if rec is None or rec["state"] != "pending":
            return None
        wanted = set(file_ids or [])
        for f in rec["files"]:
            if wanted and f["file_id"] not in wanted:
                f["status"] = "rejected"
            else:
                f["status"] = "accepted"
        rec["state"] = "accepted"
        rec["accepted_at"] = _now()
        rec["expires_at"] = _now() + UPLOAD_TTL
        _issued_tokens(rec)
        return rec


def reject(session_id: str) -> bool:
    with _LOCK:
        rec = status(session_id)
        if rec is None or rec["state"] != "pending":
            return False
        rec["state"] = "rejected"
        for f in rec["files"]:
            f["status"] = "rejected"
        return True


def cancel(session_id: str) -> bool:
    with _LOCK:
        rec = status(session_id)
        if rec is None or rec["state"] not in ("pending", "accepted"):
            return False
        rec["state"] = "cancelled"
        for f in rec["files"]:
            if f["status"] in ("pending", "accepted", "uploading"):
                f["status"] = "cancelled"
            f["token"] = ""
        return True


def consume(session_id: str, file_id: str, token: str):
    """校验并核销一次性 token。返回 (rec, file) 或 (None, 原因)。"""
    with _LOCK:
        rec = status(session_id)
        if rec is None:
            return None, "上传会话不存在或已过期"
        if rec["state"] != "accepted":
            return None, "这次传输未获批准"
        for f in rec["files"]:
            if f["file_id"] != file_id:
                continue
            if not f.get("token") or not secrets.compare_digest(f["token"], token or ""):
                return None, "token 无效（跨文件或已作废）"
            if f["status"] != "accepted":
                return None, "token 已使用"
            f["token"] = ""
            f["status"] = "uploading"
            return rec, f
    return None, "文件不在这次传输里"


def active_uploads(session_id: str) -> int:
    """同一会话里正在写的文件数（S-07：最多 2 个并发，超了直接 429，不排队）。"""
    with _LOCK:
        rec = _sessions.get(session_id)
        if rec is None:
            return 0
        return sum(1 for f in rec["files"] if f["status"] == "uploading")


def release(session_id: str, file_id: str):
    """把一次「一个字节都没读」的失败还回去（P3-3），返回**新** token；失败返回 None。

    口径（用户拍板）：旧 token 立即作废，换发新 token；文件状态回 accepted。
    只在该文件 status == "uploading" 时生效；不延长 expires_at。
    断线 / 校验不符这类「已经写了半截」的失败不走这里——那种 token 不还。
    """
    with _LOCK:
        rec = _sessions.get(session_id)
        if rec is None:
            return None
        for f in rec["files"]:
            if f["file_id"] == file_id and f["status"] == "uploading":
                f["status"] = "accepted"
                f["token"] = secrets.token_hex(16)
                return f["token"]
    return None


def finish(session_id: str, file_id: str, ok: bool = True) -> None:
    with _LOCK:
        rec = _sessions.get(session_id)
        if rec is None:
            return
        for f in rec["files"]:
            if f["file_id"] == file_id:
                f["status"] = "done" if ok else "failed"
        if ok and all(x["status"] in ("done", "rejected", "expired") for x in rec["files"]):
            rec["state"] = "completed"


def clear() -> None:
    with _LOCK:
        _sessions.clear()
