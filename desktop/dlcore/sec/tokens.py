# -*- coding: utf-8 -*-
"""逐文件一次性 token —— 第二轮实现，对应清单 S-04。

token 只在「用户已确认的那次传输」内有效，绑定会话 + 目标文件名，
用一次即作废，不能复用到另一个文件或另一次会话。

本轮（骨架期）只保留接口签名。
"""

from __future__ import annotations

AVAILABLE = False


def issue(session_id: str, rel_path: str) -> str:
    """为本次会话里的一个目标文件签发一次性 token。"""
    raise NotImplementedError("S-04 逐文件一次性 token：第二轮实现")


def consume(session_id: str, token: str, rel_path: str) -> bool:
    """校验并核销 token；重复使用、跨文件、跨会话一律返回 False。"""
    raise NotImplementedError("S-04 逐文件一次性 token：第二轮实现")


def purge_expired() -> int:
    """清理过期/已用 token，返回清理条数。"""
    raise NotImplementedError("S-04 逐文件一次性 token：第二轮实现")
