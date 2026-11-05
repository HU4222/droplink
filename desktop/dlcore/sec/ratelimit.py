# -*- coding: utf-8 -*-
"""分接口限流（令牌桶）—— S-07，仅安全版使用。

维度是 (bucket, key)：bucket 是接口类别，key 是客户端 IP。
个人版不 import 这个模块；本机回环地址在调用侧直接豁免（见 httpd._qps_ok）。

表有界：每 5 分钟清一次 10 分钟没访问的 key；超过 4096 个 key 按「最久未用」淘汰。
淘汰≠放行——淘汰后仍然按新桶计算，不会因为表满就漏过限流。
"""

from __future__ import annotations

import os
import threading
import time

AVAILABLE = True

MAX_KEYS = 4096
IDLE_TTL = 600.0          # 10 分钟没访问就清
SWEEP_EVERY = 300.0       # 每 5 分钟清一次

_LOCK = threading.Lock()
_BUCKETS = {}             # (bucket, key) -> {"tokens": float, "ts": float, "seen": float}
_LAST_SWEEP = 0.0


def _env_pair(name: str, rate: float, burst: int):
    raw = os.environ.get(name)
    if not raw:
        return rate, burst
    try:
        parts = raw.replace(" ", "").split(",")
        r = float(parts[0])
        b = int(float(parts[1])) if len(parts) > 1 else burst
        if r > 0 and b > 0:
            return r, b
    except (TypeError, ValueError):
        pass
    return rate, burst


def limits(bucket: str):
    """各接口类别的 (rate, burst)，环境变量可覆盖。

    默认 burst == rate：正常打开一个文件夹只发 1–2 个 list，等于速率的突发完全够；
    只有收紧到这个口径，「单 IP 每秒 50 次」才会真的触发限流（P2-1）。
    """
    if bucket in ("list", "search"):
        return _env_pair("DROPLINK_RL_LIST", 50.0, 50)
    if bucket in ("preview", "thumb"):
        return _env_pair("DROPLINK_RL_PREVIEW", 30.0, 30)
    if bucket == "upload":
        return _env_pair("DROPLINK_RL_UPLOAD", 20.0, 20)
    return _env_pair("DROPLINK_RL_DEFAULT", 50.0, 50)


def _sweep(now: float) -> None:
    global _LAST_SWEEP
    if now - _LAST_SWEEP < SWEEP_EVERY:
        return
    _LAST_SWEEP = now
    dead = [k for k, v in _BUCKETS.items() if now - v["seen"] > IDLE_TTL]
    for k in dead:
        _BUCKETS.pop(k, None)


def _evict(now: float) -> None:
    while len(_BUCKETS) > MAX_KEYS:
        old = min(_BUCKETS.items(), key=lambda kv: kv[1]["seen"])
        _BUCKETS.pop(old[0], None)


def allow(bucket: str, key: str, rate: float = None, burst: int = None):
    """返回 (是否放行, 需要等待的秒数)。"""
    if rate is None or burst is None:
        r, b = limits(bucket)
        rate = rate if rate is not None else r
        burst = burst if burst is not None else b
    now = time.time()
    with _LOCK:
        _sweep(now)
        rec = _BUCKETS.get((bucket, key))
        if rec is None:
            rec = {"tokens": float(burst), "ts": now, "seen": now}
            _BUCKETS[(bucket, key)] = rec
            _evict(now)
        else:
            rec["tokens"] = min(float(burst), rec["tokens"] + (now - rec["ts"]) * rate)
            rec["ts"] = now
            rec["seen"] = now
        if rec["tokens"] >= 1.0:
            rec["tokens"] -= 1.0
            return True, 0.0
        wait = (1.0 - rec["tokens"]) / rate if rate > 0 else 1.0
        return False, wait


def table_size() -> int:
    with _LOCK:
        return len(_BUCKETS)


def reset() -> None:
    global _LAST_SWEEP
    with _LOCK:
        _BUCKETS.clear()
        _LAST_SWEEP = 0.0
