# -*- coding: utf-8 -*-
"""自签证书生成（纯标准库）—— 第二轮 A，对应清单 S-01。

为什么不用第三方的 cryptography / 系统 openssl：
  * 打包脚本把 site-packages 排除在 payload 之外，第三方包进不了 EXE；
  * 目标机器没有 openssl 命令，也不能要求用户装。

所以这里用纯 Python 实现 ECDSA P-256（secp256r1）+ 最小可用的 X.509 v3 编码：
  * 密钥：随机标量 d，公钥 Q = d·G（仿射坐标，约几十毫秒）；
  * 证书：手写 DER（TBSCertificate + ECDSA-SHA256 签名 + PEM）；
  * 私钥：PKCS#8（OpenSSL 3 与传统 EC 私钥都能读）。
选 P-256 而不是 RSA-2048：纯 Python 生成 RSA 密钥要十几秒，P-256 是百毫秒级。

只做本轮需要的：自签、10 年有效、SAN 覆盖 localhost + 本机 IPv4。
不做证书轮换、不做 CA 链、不做信任列表（S-02 下一轮）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import ssl
import subprocess
import threading
import time

try:
    from dlcore.logsafe import log
except Exception:  # 以脚本方式直接运行（python dlcore/sec/certgen.py）时
    def log(m):
        print(m)
from datetime import datetime, timedelta, timezone

AVAILABLE = True

# --------------------------------------------------------------------------
# P-256（secp256r1）曲线参数
# --------------------------------------------------------------------------

_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = _P - 3
_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_G = (_GX, _GY)

_OID_EC_PUBKEY = "1.2.840.10045.2.1"
_OID_PRIME256V1 = "1.2.840.10045.3.1.7"
_OID_ECDSA_SHA256 = "1.2.840.10045.4.3.2"
_OID_CN = "2.5.4.3"
_OID_BASIC_CONSTRAINTS = "2.5.29.19"
_OID_KEY_USAGE = "2.5.29.15"
_OID_EXT_KEY_USAGE = "2.5.29.37"
_OID_SUBJECT_ALT_NAME = "2.5.29.17"
_OID_SERVER_AUTH = "1.3.6.1.5.5.7.3.1"

_VALID_DAYS = 3650  # 10 年

_LOCK = threading.Lock()
_CACHE = None


# --------------------------------------------------------------------------
# DER 编码（X.509 的最小集合）
# --------------------------------------------------------------------------

def _der_len(n: int) -> bytes:
    if n < 0x80:
        return bytes((n,))
    body = b""
    while n:
        body = bytes((n & 0xFF,)) + body
        n >>= 8
    return bytes((0x80 | len(body),)) + body


def _tlv(tag: int, body: bytes) -> bytes:
    return bytes((tag,)) + _der_len(len(body)) + body


def _seq(*items: bytes) -> bytes:
    return _tlv(0x30, b"".join(items))


def _set(*items: bytes) -> bytes:
    return _tlv(0x31, b"".join(items))


def _int(v: int) -> bytes:
    if v < 0:
        raise ValueError("只支持非负整数")
    body = v.to_bytes(max(1, (v.bit_length() + 7) // 8), "big")
    if body[0] & 0x80:  # 正数要补一个 0x00，避免被读成负数
        body = b"\x00" + body
    return _tlv(0x02, body)


def _bool(v: bool) -> bytes:
    return _tlv(0x01, b"\xff" if v else b"\x00")


def _octets(b: bytes) -> bytes:
    return _tlv(0x04, b)


def _bit_string(b: bytes, unused: int = 0) -> bytes:
    return _tlv(0x03, bytes((unused,)) + b)


def _utf8(s: str) -> bytes:
    return _tlv(0x0C, s.encode("utf-8"))


def _utc_time(dt: datetime) -> bytes:
    return _tlv(0x17, dt.strftime("%y%m%d%H%M%SZ").encode("ascii"))


def _oid(dotted: str) -> bytes:
    parts = [int(x) for x in dotted.split(".")]
    body = bytes((40 * parts[0] + parts[1],))
    for v in parts[2:]:
        chunk = [v & 0x7F]
        v >>= 7
        while v:
            chunk.insert(0, (v & 0x7F) | 0x80)
            v >>= 7
        body += bytes(chunk)
    return _tlv(0x06, body)


def _explicit(n: int, body: bytes) -> bytes:
    return _tlv(0xA0 + n, body)


def _name(cn: str) -> bytes:
    return _seq(_set(_seq(_oid(_OID_CN), _utf8(cn))))


def _extension(oid: str, value: bytes, critical: bool = False) -> bytes:
    parts = [_oid(oid)]
    if critical:
        parts.append(_bool(True))
    parts.append(_octets(value))
    return _seq(*parts)


# --------------------------------------------------------------------------
# ECDSA P-256
# --------------------------------------------------------------------------

def _add(p1, p2):
    """仿射坐标点加；None 表示无穷远点。"""
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % _P == 0:
            return None
        lam = (3 * x1 * x1 + _A) * pow(2 * y1, -1, _P) % _P
    else:
        lam = (y2 - y1) * pow(x2 - x1, -1, _P) % _P
    x3 = (lam * lam - x1 - x2) % _P
    y3 = (lam * (x1 - x3) - y1) % _P
    return (x3, y3)


def _mul(k: int, point):
    result = None
    addend = point
    while k:
        if k & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        k >>= 1
    return result


def _generate_keypair():
    d = secrets.randbelow(_N - 1) + 1
    q = _mul(d, _G)
    if q is None:  # 理论上不可能
        raise RuntimeError("ECDSA 公钥计算失败")
    return d, q


def _sign(d: int, message: bytes):
    """ECDSA-SHA256；返回 (r, s)。随机 k，签名后自校验一次。"""
    e = int.from_bytes(hashlib.sha256(message).digest(), "big")
    for _ in range(16):
        k = secrets.randbelow(_N - 1) + 1
        point = _mul(k, _G)
        r = point[0] % _N
        if r == 0:
            continue
        s = (pow(k, -1, _N) * (e + d * r)) % _N
        if s == 0:
            continue
        if not _verify_rs((_mul(d, _G)), e, r, s):
            raise RuntimeError("ECDSA 自校验失败")
        return r, s
    raise RuntimeError("ECDSA 签名失败（重试 16 次）")


def _verify_rs(q, e: int, r: int, s: int) -> bool:
    if not (1 <= r < _N and 1 <= s < _N):
        return False
    w = pow(s, -1, _N)
    point = _add(_mul(e * w % _N, _G), _mul(r * w % _N, q))
    return point is not None and point[0] % _N == r


def _sig_der(r: int, s: int) -> bytes:
    return _seq(_int(r), _int(s))


# --------------------------------------------------------------------------
# 密钥 / 证书序列化
# --------------------------------------------------------------------------

def _sec1_key(d: int, q) -> bytes:
    priv = d.to_bytes(32, "big")
    pub = b"\x04" + q[0].to_bytes(32, "big") + q[1].to_bytes(32, "big")
    return _seq(
        _int(1),
        _octets(priv),
        _explicit(0, _oid(_OID_PRIME256V1)),
        _explicit(1, _bit_string(pub)),
    )


def _pkcs8_key(d: int, q) -> bytes:
    alg = _seq(_oid(_OID_EC_PUBKEY), _oid(_OID_PRIME256V1))
    return _seq(_int(0), alg, _octets(_sec1_key(d, q)))


def _pem(label: str, der: bytes) -> str:
    b64 = base64.b64encode(der).decode("ascii")
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return "-----BEGIN %s-----\n%s\n-----END %s-----\n" % (label, "\n".join(lines), label)


def _general_names(sans) -> bytes:
    names = []
    for kind, value in sans:
        if kind == "dns":
            names.append(_tlv(0x82, value.encode("ascii")))
        elif kind == "ip":
            names.append(_tlv(0x87, socket.inet_aton(value)))
        else:
            raise ValueError("不支持的 SAN 类型：%r" % (kind,))
    return _seq(*names)


def build_certificate(d: int, q, sans, cn: str = "DropLink", days: int = _VALID_DAYS) -> bytes:
    """生成自签 X.509 v3 证书（DER 字节）。"""
    now = datetime.now(timezone.utc)
    not_before = now - timedelta(days=1)   # 容忍客户端时钟偏差
    not_after = now + timedelta(days=days)
    serial = secrets.randbits(127) or 1

    alg_sig = _seq(_oid(_OID_ECDSA_SHA256))
    alg_key = _seq(_oid(_OID_EC_PUBKEY), _oid(_OID_PRIME256V1))
    spki = _seq(alg_key, _bit_string(b"\x04" + q[0].to_bytes(32, "big") + q[1].to_bytes(32, "big")))

    extensions = [
        _extension(_OID_BASIC_CONSTRAINTS, _seq(), critical=True),          # CA:FALSE
        _extension(_OID_KEY_USAGE, _bit_string(b"\x80", 7), critical=True),  # digitalSignature
        _extension(_OID_EXT_KEY_USAGE, _seq(_oid(_OID_SERVER_AUTH))),        # serverAuth
        _extension(_OID_SUBJECT_ALT_NAME, _general_names(sans)),
    ]

    tbs = _seq(
        _explicit(0, _int(2)),        # v3
        _int(serial),
        alg_sig,
        _name(cn),                    # issuer = subject（自签）
        _seq(_utc_time(not_before), _utc_time(not_after)),
        _name(cn),
        spki,
        _explicit(3, _seq(*extensions)),
    )
    r, s = _sign(d, tbs)
    return _seq(tbs, alg_sig, _bit_string(_sig_der(r, s)))


# --------------------------------------------------------------------------
# 指纹
# --------------------------------------------------------------------------

def fingerprint_of(cert_der: bytes) -> str:
    """证书 DER 的 SHA-256，形如 AB:CD:EF:…（大写、冒号分组）。"""
    digest = hashlib.sha256(cert_der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def short_fingerprint(fingerprint: str) -> str:
    """A1:B2:C3:D4:… -> A1B2-C3D4（前 4 字节，肉眼比对用）。"""
    hex_only = fingerprint.replace(":", "")
    return "%s-%s" % (hex_only[:4], hex_only[4:8])


# --------------------------------------------------------------------------
# 本机地址与默认目录
# --------------------------------------------------------------------------

def _is_lan_ipv4(ip: str) -> bool:
    try:
        first = socket.inet_aton(ip)[0]
    except OSError:
        return False
    if first == 0 or first == 127 or first >= 224:
        return False
    if ip.startswith("169.254."):   # APIPA 链路本地地址，随插拔变化，不进 SAN
        return False
    return True


def local_ipv4() -> list:
    """本机所有局域网 IPv4（去重、排序，保证 SAN 稳定）。"""
    found = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("8.8.8.8", 80))   # 不实际发包，只为拿到默认出口地址
        found.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except Exception:
        pass
    return sorted({ip for ip in found if _is_lan_ipv4(ip)})


def _sans(extra=None) -> list:
    items = [("dns", "localhost"), ("ip", "127.0.0.1")]
    for ip in local_ipv4():
        items.append(("ip", ip))
    for item in extra or ():
        kind, _, value = item.partition(":")
        if value:
            items.append((kind.lower(), value))
    # 去重且保持顺序
    out = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def _sans_plain(sans) -> list:
    return [value for _kind, value in sans]


def _app_dir() -> str:
    """dlcore/sec/certgen.py -> 应用根目录（源码下是工程根，EXE 下是 <runtime>/app）。"""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _running_from_payload() -> bool:
    """EXE 里 APP_DIR 是 <runtime>/app，它的上一层有 pythonw.exe。"""
    return os.path.isfile(os.path.join(os.path.dirname(_app_dir()), "pythonw.exe"))


def default_cert_dir() -> str:
    """证书目录：环境变量 > 便携数据目录 > EXE 用户目录 > 源码目录。

    DROPLINK_DATA_DIR 是启动器的数据根（默认 %LOCALAPPDATA%\\DropLink）。
    便携/U 盘模式把它指到随身目录时，证书（含私钥）也跟着走，不写回用户 profile。
    """
    env = os.environ.get("DROPLINK_CERT_DIR")
    if env:
        return os.path.abspath(env)
    data = os.environ.get("DROPLINK_DATA_DIR")
    if data:
        return os.path.join(os.path.abspath(data), "secure", "certs")
    if _running_from_payload():
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "DropLink", "secure", "certs")
    return os.path.join(_app_dir(), ".certs")


# --------------------------------------------------------------------------
# 生成 / 复用
# --------------------------------------------------------------------------

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _pair_loads(cert_path: str, key_path: str) -> bool:
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_path, key_path)
        return True
    except (ssl.SSLError, OSError, ValueError):
        return False


def _read_pem_der(path: str) -> bytes:
    with open(path, "r", encoding="ascii") as f:
        text = f.read()
    body = []
    inside = False
    for line in text.splitlines():
        if line.startswith("-----BEGIN"):
            inside = True
            continue
        if line.startswith("-----END"):
            break
        if inside:
            body.append(line.strip())
    return base64.b64decode("".join(body))


def _current_user_sid() -> str:
    """当前登录 SID。

    不能直接用 %USERNAME% 拼 icacls：同名账号存在于多个计算机/域时（改过机器名
    的环境尤其常见），icacls 会解析到另一个账户，把私钥锁给错的人。
    """
    try:
        from ..config import CREATE_NO_WINDOW
        r = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                           capture_output=True, timeout=10,
                           creationflags=CREATE_NO_WINDOW)
        text = r.stdout.decode("utf-8", "replace").replace('"', "")
        for part in text.replace("\r", "\n").replace(",", "\n").split("\n"):
            part = part.strip()
            if part.startswith("S-1-"):
                return part
    except Exception:
        pass
    return ""


def _restrict_key(path: str) -> None:
    """私钥只给当前用户读；失败不致命，记一行。

    分两步：先给当前 SID 显式授权（失败就完全不动 ACL，避免把继承权限删掉后
    谁都进不去），成功后再移除继承。任何一步失败都只记日志，不影响证书使用。
    """
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return
    sid = _current_user_sid()
    principal = ("*" + sid) if sid else (os.environ.get("USERNAME") or "")
    if not principal:
        return
    try:
        from ..config import CREATE_NO_WINDOW
        r = subprocess.run(["icacls", path, "/grant:r", principal + ":F"],
                           capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW)
        if r.returncode != 0:
            log("[证书] 私钥权限收紧失败（不影响使用）")
            return
        r = subprocess.run(["icacls", path, "/inheritance:r"],
                           capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW)
        if r.returncode != 0:
            log("[证书] 私钥继承权限未移除（不影响使用）")
    except Exception:
        log("[证书] 私钥权限收紧失败（不影响使用）")


def _write_atomic(path: str, data: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(data)
    os.replace(tmp, path)


def _info(cd, cert_path, key_path, sans, cert_der, generated, elapsed) -> dict:
    fp = fingerprint_of(cert_der)
    meta_path = os.path.join(cd, "meta.json")
    try:
        meta = json.loads(open(meta_path, encoding="utf-8").read())
        not_after = meta.get("not_after")
    except Exception:
        not_after = ""
    return {
        "cert_dir": cd,
        "cert_path": cert_path,
        "key_path": key_path,
        "sans": _sans_plain(sans),
        "fingerprint": fp,
        "fingerprint_short": short_fingerprint(fp),
        "not_after": not_after,
        "generated": generated,
        "elapsed": elapsed,
    }


def _try_reuse(cd, sans):
    cert_path = os.path.join(cd, "cert.pem")
    key_path = os.path.join(cd, "key.pem")
    meta_path = os.path.join(cd, "meta.json")
    for p in (cert_path, key_path, meta_path):
        if not os.path.isfile(p):
            return None
    try:
        meta = json.loads(open(meta_path, encoding="utf-8").read())
        if meta.get("sans") != _sans_plain(sans):
            return None
        if meta.get("cert_sha256") != _sha256_file(cert_path):
            return None            # 证书被改过（或被别的构建覆盖）
        if meta.get("key_sha256") != _sha256_file(key_path):
            return None            # 私钥被换过 / 与证书不配对
        not_after = datetime.strptime(meta["not_after"], "%Y-%m-%dT%H:%M:%SZ")
        not_after = not_after.replace(tzinfo=timezone.utc)
        if not_after <= datetime.now(timezone.utc) + timedelta(days=1):
            return None            # 已过期或快过期
        if not _pair_loads(cert_path, key_path):
            return None
        return _info(cd, cert_path, key_path, sans, _read_pem_der(cert_path), False, 0.0)
    except Exception:
        return None


def _generate(cd, sans, cn, verbose):
    t0 = time.time()
    if verbose:
        log("[证书] 未找到可用证书，正在生成（ECDSA P-256，约 1-3 秒）…")
    d, q = _generate_keypair()
    der = build_certificate(d, q, sans, cn=cn)
    cert_path = os.path.join(cd, "cert.pem")
    key_path = os.path.join(cd, "key.pem")
    _write_atomic(key_path, _pem("PRIVATE KEY", _pkcs8_key(d, q)))
    _write_atomic(cert_path, _pem("CERTIFICATE", der))
    _restrict_key(key_path)

    fp = fingerprint_of(der)
    not_after = (datetime.now(timezone.utc) + timedelta(days=_VALID_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = {
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sans": _sans_plain(sans),
        "not_after": not_after,
        "fingerprint": fp,
        "cert_sha256": _sha256_file(cert_path),
        "key_sha256": _sha256_file(key_path),
        "generator": "dlcore.sec.certgen/1",
    }
    _write_atomic(os.path.join(cd, "meta.json"),
                  json.dumps(meta, ensure_ascii=False, indent=2))
    if not _pair_loads(cert_path, key_path):
        raise RuntimeError("刚生成的证书无法被 ssl 加载（cert=%s）" % cert_path)
    elapsed = time.time() - t0
    if verbose:
        log("[证书] 已生成：指纹 %s，有效期至 %s，用时 %.2f 秒"
              % (short_fingerprint(fp), not_after, elapsed))
    return _info(cd, cert_path, key_path, sans, der, True, elapsed)


def ensure_cert(cert_dir: str = None, extra_sans=None, verbose: bool = True) -> dict:
    """确保证书可用并返回信息；首次生成、之后复用，损坏/过期/SAN 变化时重建。

    返回：
        {"cert_dir", "cert_path", "key_path", "sans": [...],
         "fingerprint": "AB:CD:…", "fingerprint_short": "A1B2-C3D4",
         "not_after": "2036-11-05T…Z", "generated": bool, "elapsed": float}
    """
    global _CACHE
    cd = os.path.abspath(cert_dir) if cert_dir else default_cert_dir()
    sans = _sans(extra_sans)
    with _LOCK:
        if _CACHE is not None and _CACHE["cert_dir"] == cd and _CACHE["sans"] == _sans_plain(sans):
            return _CACHE
        os.makedirs(cd, exist_ok=True)
        info = _try_reuse(cd, sans)
        if info is not None:
            if verbose:
                log("[证书] 复用已有证书：指纹 %s，有效期至 %s"
                      % (info["fingerprint_short"], info["not_after"]))
        else:
            info = _generate(cd, sans, "DropLink", verbose)
        _CACHE = info
        return info


def current() -> dict:
    """本次进程启动时加载/生成的证书信息；没有就是 None。

    /api/device 用这个而不是再调 ensure_cert：避免中途 IP 变化触发重建，
    导致「报出去的指纹」和「正在服务的证书」不是同一张。
    """
    return _CACHE


if __name__ == "__main__":
    result = ensure_cert()
    print("cert:", result["cert_path"])
    print("key :", result["key_path"])
    print("sans:", result["sans"])
    print("fingerprint:", result["fingerprint"])
