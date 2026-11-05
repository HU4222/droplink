# -*- coding: utf-8 -*-
"""Windows 环境与网络：管理员、防火墙规则、网卡地址、设备识别。

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
from .config import *
from .fsutil import *  # 路径与常量

def lan_urls(port: int, scheme: str = "http"):
    """本机局域网访问地址；scheme 由调用方按版本给（安全版 https）。"""
    ips = []
    # 1) UDP connect 不发送任何数据包，可拿到默认出口网卡地址
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    # 2) 枚举本机所有 IPv4
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass

    def score(ip):
        if ip.startswith("192.168."):
            return 0
        if ip.startswith("10."):
            return 1
        if ip.startswith("172."):
            return 2
        return 3

    ips = [ip for ip in ips if not ip.startswith("169.254.")]
    ips.sort(key=score)
    prefix = (scheme or "http").rstrip(":/") + "://"
    return [f"{prefix}{ip}:{port}" for ip in ips]


_HOST_CACHE = {"ts": 0.0, "hosts": set()}
_HOST_CACHE_LOCK = threading.Lock()


def allowed_hosts(port: int) -> set:
    """Host 白名单：本机名、回环、本机所有网卡 IP。

    这是阻断 DNS 重绑定的关键——攻击者域名解析到 127.0.0.1 后，
    请求里的 Host 仍是 evil.example.com，会被这里拒掉（DL-01）。
    """
    now = time.time()
    with _HOST_CACHE_LOCK:
        if _HOST_CACHE["hosts"] and now - _HOST_CACHE["ts"] < 10:
            return set(_HOST_CACHE["hosts"])
    hosts = {"127.0.0.1", "localhost", "::1", "[::1]"}
    try:
        hosts.add(socket.gethostname().lower())
        fqdn = socket.getfqdn().lower()
        if fqdn:
            hosts.add(fqdn)
    except Exception:
        pass
    for u in lan_urls(port):
        try:
            h = urllib.parse.urlparse(u).hostname
            if h:
                hosts.add(h.lower())
        except Exception:
            pass
    with _HOST_CACHE_LOCK:
        _HOST_CACHE["ts"] = now
        _HOST_CACHE["hosts"] = hosts
    return set(hosts)



# --------------------------------------------------------------------------
# 防火墙（UAC 提权执行 netsh）
# --------------------------------------------------------------------------

def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _runas_bat(lines):
    """把 netsh 命令写进临时 bat，用 UAC 提权执行。返回 (是否执行, 退出码)。

    用 mkstemp 生成随机文件名（修复 DL-11：固定路径存在 TOCTOU，
    同用户的恶意程序可以预置内容借 UAC 执行）。
    """
    try:
        fd, tmp = tempfile.mkstemp(prefix="droplink_fw_", suffix=".cmd")
        os.close(fd)
    except OSError as exc:
        return False, f"写临时文件失败：{exc}"
    try:
        with open(tmp, "w", encoding="ascii", newline="\r\n") as f:
            f.write("@echo off\r\n")
            for ln in lines:
                f.write(ln + "\r\n")
    except OSError as exc:
        return False, f"写临时文件失败：{exc}"
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("fMask", ctypes.c_ulong),
            ("hwnd", ctypes.c_void_p),
            ("lpVerb", ctypes.c_wchar_p),
            ("lpFile", ctypes.c_wchar_p),
            ("lpParameters", ctypes.c_wchar_p),
            ("lpDirectory", ctypes.c_wchar_p),
            ("nShow", ctypes.c_int),
            ("hInstApp", ctypes.c_void_p),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", ctypes.c_wchar_p),
            ("hkeyClass", ctypes.c_void_p),
            ("dwHotKey", ctypes.c_ulong),
            ("hIcon", ctypes.c_void_p),
            ("hProcess", ctypes.c_void_p),
        ]

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = "cmd.exe"
    sei.lpParameters = '/c ""%s""' % tmp
    sei.nShow = 0  # SW_HIDE
    try:
        ok = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei))
    except Exception as exc:
        return False, f"调用失败：{exc}"
    if not ok:
        # 返回 False 多为用户点了「取消」（拒绝 UAC）
        return False, "已取消"

    code = -1
    if sei.hProcess:
        # 最多等 30 秒；用户若迟迟不点 UAC 确认框，视为已提交（避免请求一直挂着）
        r = ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, 30000)
        if r == 0x00000102:  # WAIT_TIMEOUT
            ctypes.windll.kernel32.TerminateProcess(sei.hProcess, 1)
            code = 0
        else:
            c = ctypes.c_ulong(0)
            ctypes.windll.kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(c))
            code = int(c.value)
        ctypes.windll.kernel32.CloseHandle(sei.hProcess)
    try:
        os.unlink(tmp)
    except OSError:
        pass
    return True, code


def _has_fw_rule() -> bool:
    """查询 DropLink 的防火墙入站规则是否存在（查询不需要管理员）。"""
    try:
        r = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=DropLink Port"],
            capture_output=True, timeout=6, creationflags=CREATE_NO_WINDOW)
        return r.returncode == 0 and b"DropLink Port" in r.stdout
    except Exception:
        return False


def _fw_rule_detail():
    """返回 DropLink 规则的配置细节：profile / 端口 / 是否启用。"""
    try:
        r = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=DropLink Port"],
            capture_output=True, timeout=8, creationflags=CREATE_NO_WINDOW)
        if r.returncode != 0:
            return None
        # netsh 输出编码因系统「活动代码页」而异：先试 UTF-8，再回退 GBK
        text = None
        for enc in ("utf-8", "gbk"):
            cand = r.stdout.decode(enc, "replace")
            if "配置文件" in cand or "Profiles" in cand:
                text = cand
                break
        if text is None:
            text = r.stdout.decode("utf-8", "replace")
        out = {}
        for line in text.splitlines():
            line = line.strip()
            if "配置文件" in line or "Profiles" in line:
                out["profiles"] = line.split(":", 1)[-1].strip()
            elif "本地端口" in line or "LocalPort" in line:
                out["port"] = line.split(":", 1)[-1].strip()
            elif "已启用" in line or "Enabled" in line:
                out["enabled"] = line.split(":", 1)[-1].strip()
        return out or None
    except Exception:
        return None


def device_name_of(ua: str) -> str:
    """从 User-Agent 粗略识别设备名，用于手机未上报名称时的兜底。"""
    if not ua:
        return "手机设备"
    if "Android" in ua:
        m = re.search(r"Android[\s/]+([\d.]+)", ua)
        return "安卓 " + (m.group(1) if m else "设备")
    if "iPhone" in ua or "iPad" in ua:
        return "苹果设备"
    if "Windows" in ua:
        return "Windows 设备"
    return "手机设备"


def _net_profiles():
    """用 PowerShell 查询本机各网络连接的类型（0 公用 / 1 专用 / 2 域）。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "$OutputEncoding=[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
             "Get-NetConnectionProfile | ForEach-Object { $_.Name + ';' + [int]$_.NetworkCategory }"],
            capture_output=True, timeout=12, creationflags=CREATE_NO_WINDOW)
        if r.returncode != 0:
            return None
        text = r.stdout.decode("utf-8", "replace")
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line or ";" not in line:
                continue
            name, _, cat_s = line.rpartition(";")
            try:
                cat = int(cat_s.strip())
            except ValueError:
                cat = 0
            out.append({"name": name.strip() or "网络", "cat": cat})
        return out if out else None
    except Exception:
        return None


def _all_ips():
    """列出本机所有 IPv4 地址及网卡名（含虚拟网卡），供「网络切换」选择。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "$OutputEncoding=[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
             "Get-NetIPAddress -AddressFamily IPv4 | ForEach-Object { "
             "$_.IPAddress + ';' + $_.InterfaceAlias + ';' + $_.PrefixLength }"],
            capture_output=True, timeout=12, creationflags=CREATE_NO_WINDOW)
        if r.returncode != 0:
            return None
        text = r.stdout.decode("utf-8", "replace")
        out = []
        seen = set()
        for line in text.splitlines():
            line = line.strip()
            if not line or ";" not in line:
                continue
            parts = line.split(";")
            ip = parts[0].strip()
            alias = parts[1].strip() if len(parts) > 1 else ""
            if not ip or ip.startswith("127.") or ip.startswith("169.254."):
                continue
            if ip in seen:
                continue
            seen.add(ip)
            # 虚拟网卡常见关键词，用于前端标注
            out.append({"ip": ip, "alias": alias})
        return out
    except Exception:
        return None


