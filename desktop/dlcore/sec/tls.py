# -*- coding: utf-8 -*-
"""把服务端套上 TLS —— 第二轮 A，对应清单 S-01。

只做安全版：个人版一行都不走这里（由 server.py 的 is_secure_edition() 决定）。
证书本身由同目录的 certgen.py 用纯标准库生成，本模块只负责：
  * 构造 SSLContext（TLS 1.2 起步、只留 ECDSA + AEAD 套件）；
  * 把已绑定的监听 socket 包成 TLS 套接字（http.server 的标准做法）。
"""

from __future__ import annotations

import ssl

AVAILABLE = True

# TLS 1.2 只保留 ECDSA + AEAD；TLS 1.3 的套件由 OpenSSL 默认值决定（都安全）
_CIPHERS = ("ECDHE-ECDSA-AES256-GCM-SHA384:"
            "ECDHE-ECDSA-AES128-GCM-SHA256:"
            "ECDHE-ECDSA-CHACHA20-POLY1305")


def make_context(cert_path: str = "", key_path: str = "") -> ssl.SSLContext:
    """构造服务端 SSLContext；不传路径时用 certgen 的当前证书。"""
    if not cert_path or not key_path:
        from . import certgen
        info = certgen.ensure_cert(verbose=False)
        cert_path = cert_path or info["cert_path"]
        key_path = key_path or info["key_path"]

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.options |= ssl.OP_NO_COMPRESSION
        ctx.options |= ssl.OP_CIPHER_SERVER_PREFERENCE
    except AttributeError:
        pass
    try:
        ctx.set_ciphers(_CIPHERS)
    except ssl.SSLError:
        # 某些 OpenSSL 构建没有 ChaCha20，退到只用 AES-GCM
        ctx.set_ciphers("ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-ECDSA-AES128-GCM-SHA256")
    ctx.load_cert_chain(cert_path, key_path)
    try:
        ctx.set_alpn_protocols(["http/1.1"])
    except NotImplementedError:
        pass
    return ctx


def wrap_server(server, context):
    """把 ThreadingHTTPServer 的监听 socket 换成 TLS（保持对象不变）。"""
    server.socket = context.wrap_socket(server.socket, server_side=True)
    return server
