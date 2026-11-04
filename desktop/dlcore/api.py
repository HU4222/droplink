# -*- coding: utf-8 -*-
"""接口实现层（ApiMixin）：/api/* 的全部方法。

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
from .fsutil import *
from .media import *
from .winenv import *
from .state import STATE
from .state import purge_send_cache
from .config import _LIVE, _LIVE_LOCK  # 共享运行期状态（原地修改，不重新绑定）
from .winenv import _all_ips, _fw_rule_detail, _has_fw_rule, _net_profiles, _runas_bat  # 私有工具，星号导入不包含下划线名
from .fsutil import _sensitive_roots
from .fsutil import SESSION_ABS_TTL

class ApiMixin:
    """所有 /api/* 的实现。依赖 Handler 的 _json/_fail/_serve_file 等传输层方法。"""

    def api_session(self):
        urls = lan_urls(self.server.server_port)
        local_ui = self._local_ui()
        info = {
            "ok": True,
            "authed": True,
            "local": local_ui,
            "device": socket.gethostname(),
            "platform": f"{os.name}-{sys.platform}",
            "app": APP_NAME,
            "version": VERSION,
            "recv_dir": STATE.recv_dir,
            "uptime": int(time.time() - STATE.started),
            "lan_urls": urls,
            "qr_target": urls[0] if urls else "",
            "qr_available": qr_available(),
            "thumb_available": has_pillow(),
            "pin_length": len(STATE.pin),
        }
        # 配对码只给「本机 + 令牌」的界面，任何网络请求都拿不到（修复 DL-05）
        if local_ui:
            info["pin"] = STATE.pin
        extra = None
        # 本机界面也换一个短会话 Cookie：<img>/<a> 这类请求带不了自定义头，
        # 有了 Cookie 才能正常显示缩略图、直接点下载。
        if self._token_ok() and not self._cookie_token():
            tok = STATE.new_session(self.client_address[0], "local-ui")
            extra = {"Set-Cookie":
                     f"dlsess={tok}; Path=/; Max-Age={SESSION_ABS_TTL}; HttpOnly; SameSite=Strict"}
        self._json(info, extra=extra)

    def api_login(self):
        try:
            data = self._json_body()
        except Exception:
            return self._fail(400, "请求格式错误", close=True)
        ip = self.client_address[0]
        now = time.time()
        rec = STATE.fails.get(ip)
        if rec and rec["count"] >= 5:
            # 指数退避：连续失败后锁 60s → 120s → 240s（上限 5 分钟）
            wait = min(300, 60 * (2 ** max(0, rec["count"] - 5)))
            if now - rec["ts"] < wait:
                return self._fail(429, "尝试次数过多，请稍后再试")
        pin = data.get("pin")
        # 严格 schema：只接受字符串，拒绝数字/数组/对象与重复键（修复 X-09）
        if not isinstance(pin, str) or not (4 <= len(pin) <= 12) or not pin.isdigit():
            return self._fail(400, "配对码格式不正确")
        if not secrets.compare_digest(pin, STATE.pin):
            if not rec or now - rec["ts"] > 300:
                STATE.fails[ip] = {"count": 1, "ts": now}
            else:
                rec["count"] += 1
                rec["ts"] = now
            return self._fail(403, "配对码不正确")
        ua = (self.headers.get("User-Agent") or "")[:120]
        token = STATE.new_session(ip, ua)
        STATE.fails.pop(ip, None)
        name = str(data.get("name") or "").strip()[:40]
        with STATE.lock:
            STATE.peers[ip] = {"ts": now, "ua": ua, "name": name or device_name_of(ua)}
        STATE.cleanup()
        print(f"[接入] {name or ip} 已配对成功")
        cookie = f"dlsess={token}; Path=/; Max-Age={SESSION_ABS_TTL}; HttpOnly; SameSite=Strict"
        self._json({"ok": True, "device": socket.gethostname()}, extra={"Set-Cookie": cookie})

    def api_logout(self):
        tok = self._cookie_token()
        if tok:
            STATE.revoke_session(tok)
        cookie = "dlsess=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"
        self._json({"ok": True}, extra={"Set-Cookie": cookie})

    def api_sessions(self):
        if not self._local_ui():
            return self._fail(403, "只有电脑本机可以查看")
        now = time.time()
        with STATE.lock:
            items = [{
                "token": k[:6] + "…",
                "ip": v.get("ip"),
                "ua": v.get("ua"),
                "age": int(now - v["ts"]),
                "idle": int(now - v["last"]),
            } for k, v in STATE.sessions.items()]
        items.sort(key=lambda x: x["idle"])
        return self._json({"ok": True, "sessions": items})

    def api_sessions_revoke(self):
        if not self._local_ui():
            return self._fail(403, "只有电脑本机可以操作")
        try:
            data = self._json_body()
        except Exception:
            return self._fail(400, "请求格式错误", close=True)
        n = STATE.revoke_all() if data.get("all") else 0
        return self._json({"ok": True, "revoked": n})

    def _read_target(self, raw: str):
        """统一的读路径入口；不合法时已经回了 4xx，返回 None。"""
        try:
            return check_path(raw)
        except ValueError as exc:
            self._fail(400, str(exc))
        except PermissionError as exc:
            self._fail(403, str(exc))
        return None

    def api_list(self, q):
        target = self._read_target(q.get("path") or STATE.recv_dir)
        if not target:
            return None
        if not os.path.isdir(target):
            return self._fail(404, "文件夹不存在或无法访问")
        entries = []
        try:
            with os.scandir(target) as it:
                for e in it:
                    if e.name in SKIP_NAMES or e.name.startswith("$"):
                        continue
                    try:
                        is_dir = e.is_dir(follow_symlinks=False)
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    entries.append({
                        "name": e.name,
                        "path": e.path,
                        "dir": is_dir,
                        "size": 0 if is_dir else st.st_size,
                        "mtime": st.st_mtime,
                        "kind": "folder" if is_dir else kind_of(e.name),
                        "ext": "" if is_dir else os.path.splitext(e.name)[1].lower().lstrip("."),
                        "hidden": e.name.startswith("."),
                    })
        except PermissionError:
            return self._fail(403, "没有权限访问这个文件夹")
        except OSError:
            return self._fail(500, "读取目录失败")

        entries.sort(key=lambda x: (not x["dir"], natkey(x["name"])))

        # 目录内容签名 → ETag：内容没变就直接 304，省掉序列化和传输
        sig = zlib.crc32(("|".join(
            "%s:%s:%.0f" % (e["name"], e["size"], e["mtime"]) for e in entries)).encode("utf-8", "replace"))
        etag = '"L%d-%08x"' % (len(entries), sig & 0xFFFFFFFF)
        if (self.headers.get("If-None-Match") or "").strip() == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return

        parent = os.path.dirname(target.rstrip("\\/"))
        if not parent or not os.path.isdir(parent) or parent == target:
            parent = None
        disk = {}
        try:
            usage = shutil.disk_usage(target)
            disk = {"total": usage.total, "free": usage.free}
        except Exception:
            pass
        self._json({
            "ok": True,
            "path": target,
            "name": os.path.basename(target.rstrip("\\/")) or target,
            "parent": parent,
            "entries": entries,
            "roots": quick_roots(),
            "recv_dir": STATE.recv_dir,
            "writable": inside(target, STATE.recv_dir),
            "disk": disk,
        }, extra={"ETag": etag, "Cache-Control": "no-cache"})

    def api_download(self, q):
        p = self._read_target(q.get("path") or "")
        if not p:
            return None
        if not os.path.isfile(p):
            return self._fail(404, "文件不存在")
        if q.get("log") == "1" and self.command != "HEAD":
            try:
                STATE.add_transfer(direction="down", name=os.path.basename(p),
                                   size=os.path.getsize(p), peer=self.client_address[0])
            except Exception:
                pass
        self._serve_file(p, inline=q.get("inline") == "1")

    def api_thumb(self, q):
        p = self._read_target(q.get("path") or "")
        if not p:
            return None
        if not os.path.isfile(p):
            return self._fail(404, "文件不存在")
        try:
            width = int(q.get("w") or 320)
        except (TypeError, ValueError):
            return self._fail(400, "参数无效")
        data = thumb_bytes(p, max(64, min(1600, width)))
        if data:
            return self._send(200, data, "image/jpeg", {"Cache-Control": "max-age=86400"})
        # 无 Pillow 或解码失败：回退原图
        return self._serve_file(p, inline=True)

    def api_sha256(self, q):
        """返回文件的大小与 SHA-256，供客户端下载完成后校验（1.4.0）。

        大文件按 1 MB 分块读，内存占用恒定；不缓存结果——同一路径的内容可能被替换，
        缓存会让「校验通过」变成假阳性。
        """
        p = self._read_target(q.get("path") or "")
        if not p:
            return None
        if not os.path.isfile(p):
            return self._fail(404, "文件不存在")
        h = hashlib.sha256()
        size = 0
        try:
            with open(p, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
                    size += len(chunk)
        except OSError:
            return self._fail(500, "读取文件失败")
        return self._json({"ok": True, "size": size, "sha256": h.hexdigest(),
                           "name": os.path.basename(p)})

    def api_preview(self, q):
        p = self._read_target(q.get("path") or "")
        if not p:
            return None
        if not os.path.isfile(p):
            return self._fail(404, "文件不存在")
        size = os.path.getsize(p)
        limit = 256 * 1024
        try:
            with open(p, "rb") as f:
                raw = f.read(limit)
        except OSError:
            return self._fail(500, "读取失败")
        if b"\x00" in raw[:4096]:
            return self._fail(415, "该文件不是文本文件")
        for enc in ("utf-8", "gb18030", "utf-16"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", "replace")
        self._json({"ok": True, "text": text, "truncated": size > limit, "size": size})

    def api_search(self, q):
        base = self._read_target(q.get("path") or STATE.recv_dir)
        if not base:
            return None
        kw = (q.get("q") or "").strip().lower()
        if not kw or not os.path.isdir(base):
            return self._json({"ok": True, "entries": [], "truncated": False})
        results = []
        scanned = 0
        deadline = time.time() + 8
        truncated = False
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_NAMES and not d.startswith("$")]
            if root.count(os.sep) - base.count(os.sep) >= 6:
                dirs[:] = []
            for name in dirs + files:
                if name in SKIP_NAMES:
                    continue
                scanned += 1
                if scanned > 40000 or time.time() > deadline:
                    truncated = True
                    break
                if kw in name.lower():
                    full = os.path.join(root, name)
                    try:
                        st = os.stat(full)
                        is_dir = os.path.isdir(full)
                    except OSError:
                        continue
                    results.append({
                        "name": name, "path": full, "dir": is_dir,
                        "size": 0 if is_dir else st.st_size, "mtime": st.st_mtime,
                        "kind": "folder" if is_dir else kind_of(name),
                        "ext": "" if is_dir else os.path.splitext(name)[1].lower().lstrip("."),
                        "rel": os.path.relpath(full, base),
                    })
                    if len(results) >= 300:
                        truncated = True
                        break
            if truncated:
                break
        results.sort(key=lambda x: (x["rel"].count(os.sep), x["name"].lower()))
        self._json({"ok": True, "entries": results, "truncated": truncated})

    def api_clip_add(self):
        try:
            data = self._json_body()
        except Exception:
            return self._fail(400, "请求格式错误", close=True)
        text = str(data.get("text") or "").strip()
        # 只有分隔线（—— / ----）也视为空内容（BUG-05）
        if not text or re.fullmatch(r"[—\-–_\s]+", text):
            return self._fail(400, "内容为空")
        src = str(data.get("from") or "手机")[:20]
        item = STATE.add_clip(text, src)
        self._json({"ok": True, "item": item})

    def api_clip_del(self):
        try:
            data = self._json_body()
        except Exception:
            return self._fail(400, "请求格式错误", close=True)
        cid = str(data.get("id") or "")
        with STATE.lock:
            STATE.clip = [c for c in STATE.clip if c["id"] != cid]
        self._json({"ok": True})

    def api_mkdir(self):
        try:
            data = self._json_body()
        except Exception:
            return self._fail(400, "请求格式错误", close=True)

        # 文件夹传输：按相对路径一次性建好多级目录（空文件夹也靠这个还原）
        rel = str(data.get("rel") or "").replace("\\", "/").strip("/")
        if rel:
            try:
                parts = [strict_name(p) for p in rel.split("/") if p and p not in (".", "..")]
            except ValueError as exc:
                return self._fail(400, str(exc))
            parts = [p for p in parts if p]
            if not parts:
                return self._fail(400, "路径无效")
            target = os.path.abspath(os.path.join(STATE.recv_dir, *parts))
            if not inside(target, STATE.recv_dir):
                return self._fail(403, "只能传到接收目录")
            try:
                os.makedirs(target, exist_ok=True)   # 幂等：已存在也算成功
            except OSError:
                return self._fail(500, "创建文件夹失败")
            return self._json({"ok": True, "path": target, "rel": "/".join(parts)})

        parent = os.path.abspath(str(data.get("path") or STATE.recv_dir))
        try:
            name = strict_name(str(data.get("name") or ""))
        except ValueError as exc:
            return self._fail(400, str(exc))
        if not inside(parent, STATE.recv_dir):
            return self._fail(403, "只能在接收目录内新建文件夹")
        target = unique_path(parent, name)
        try:
            os.makedirs(target, exist_ok=False)
        except OSError:
            return self._fail(500, "创建文件夹失败")
        self._json({"ok": True, "path": target})

    def api_rename(self):
        try:
            data = self._json_body()
        except Exception:
            return self._fail(400, "请求格式错误", close=True)
        src = os.path.abspath(str(data.get("path") or ""))
        try:
            name = strict_name(str(data.get("name") or ""))
        except ValueError as exc:
            return self._fail(400, str(exc))
        if not os.path.exists(src):
            return self._fail(404, "文件不存在")
        if not inside(os.path.dirname(src), STATE.recv_dir):
            return self._fail(403, "只能在接收目录内重命名")
        dst = os.path.join(os.path.dirname(src), name)
        if os.path.exists(dst) and os.path.normcase(dst) != os.path.normcase(src):
            return self._fail(409, "同名文件已存在")
        try:
            os.rename(src, dst)
        except OSError:
            return self._fail(500, "重命名失败")
        self._json({"ok": True, "path": dst})

    def api_delete(self):
        try:
            data = self._json_body()
        except Exception:
            return self._fail(400, "请求格式错误", close=True)
        raw = [os.path.abspath(str(p)) for p in (data.get("paths") or []) if p]
        if not raw:
            return self._fail(400, "缺少路径")
        if len(raw) > 500:
            return self._fail(400, "一次删除的文件过多")
        # 去重 + 父子同选时只保留父目录（修复 X-28：父+子同时提交会占住线程）
        seen = set()
        paths = []
        for p in raw:
            k = os.path.normcase(p)
            if k not in seen:
                seen.add(k)
                paths.append(p)
        paths = [p for p in paths
                 if not any(os.path.normcase(p) != os.path.normcase(q) and lexical_inside(p, q)
                            for q in paths)]
        paths = [p for p in paths if os.path.exists(p)]
        if not paths:
            return self._fail(404, "没有可删除的文件")
        # 接收目录本身不能删（修复 X-02：以前 inside(recv, recv) 为真，整个目录会进回收站）
        recv_norm = os.path.normcase(os.path.abspath(STATE.recv_dir))
        if any(os.path.normcase(p) == recv_norm for p in paths):
            return self._fail(403, "不能删除接收目录本身")
        # 与 rename / mkdir 保持一致：只允许改动接收目录内的文件，
        # 避免配对设备（或盗用配对码者）删除系统里的任意文件。
        # 这里用字符串路径判断，junction 本身可以删（删链接不会动到目标）。
        outside = [p for p in paths if not lexical_inside(p, STATE.recv_dir)]
        if outside:
            return self._fail(403, "只能删除接收目录内的文件；其它位置请在电脑上操作",
                              blocked=len(outside))
        # 父目录若被 junction 指到外面，删除会作用到目标，直接拒绝
        for p in paths:
            parent = os.path.dirname(p)
            if os.path.exists(parent) and not inside(parent, STATE.recv_dir):
                return self._fail(403, "该路径指向接收目录之外，已拒绝")
        ok = recycle(paths)
        if not ok:
            return self._fail(500, "删除失败（已取消，不会永久删除）")
        self._json({"ok": True, "count": len(paths)})

    def api_zip(self):
        try:
            data = self._json_body()
        except Exception:
            return self._fail(400, "请求格式错误", close=True)
        return self._zip_paths(data.get("paths") or [])

    def api_zip_get(self, q):
        """GET 版本：安卓 WebView 无法用 fetch 接住 blob 下载，只能走导航交给 DownloadListener。"""
        raw = q.get("paths") or "[]"
        try:
            paths = json.loads(raw)
        except Exception:
            paths = [raw]
        if not isinstance(paths, list):
            paths = [paths]
        return self._zip_paths(paths)

    def _zip_paths(self, raw_paths):
        raw = [str(p) for p in raw_paths if p]
        paths = []
        for p in raw:
            try:
                paths.append(check_path(p))
            except ValueError as exc:
                return self._fail(400, str(exc))
            except PermissionError as exc:
                return self._fail(403, str(exc))
        paths = [p for p in paths if os.path.exists(p)]
        if not paths:
            return self._fail(404, "没有可打包的文件")
        max_bytes = 8 << 30      # 单次打包上限 8 GB，避免塞满临时盘（修复 DL-07）
        max_files = 20000
        total = 0
        count = 0
        tmp = tempfile.NamedTemporaryFile(prefix="droplink_", suffix=".zip", delete=False)
        tmp.close()
        try:
            with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
                for p in paths[:300]:
                    if os.path.isfile(p):
                        size = os.path.getsize(p)
                        total += size
                        count += 1
                        if total > max_bytes or count > max_files:
                            raise OverflowError("打包内容过大")
                        z.write(p, arcname=os.path.basename(p))
                    elif os.path.isdir(p):
                        base = os.path.dirname(p.rstrip("\\/"))
                        for root, dirs, files in os.walk(p):
                            dirs[:] = [d for d in dirs if d not in SKIP_NAMES]
                            # 目录条目必须显式写入，否则空文件夹在解压后会丢失
                            if not files and not dirs:
                                rel_dir = os.path.relpath(root, base).replace("\\", "/") + "/"
                                try:
                                    z.writestr(zipfile.ZipInfo(rel_dir), b"")
                                except OSError:
                                    pass
                            for f in files:
                                if f in SKIP_NAMES:
                                    continue
                                fp = os.path.join(root, f)
                                try:
                                    size = os.path.getsize(fp)
                                except OSError:
                                    continue
                                total += size
                                count += 1
                                if total > max_bytes or count > max_files:
                                    raise OverflowError("打包内容过大")
                                try:
                                    z.write(fp, os.path.relpath(fp, base))
                                except OSError:
                                    continue
            name = f"DropLink_{time.strftime('%Y%m%d_%H%M%S')}.zip"
            self._serve_file(tmp.name, download_name=name)
        except OverflowError:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            return self._fail(413, "打包内容过大，请分批下载")
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    def api_share(self):
        try:
            data = self._json_body()
        except Exception:
            return self._fail(400, "请求格式错误", close=True)
        p = str(data.get("path") or "")
        try:
            p = check_path(p)
        except ValueError as exc:
            return self._fail(400, str(exc))
        except PermissionError as exc:
            return self._fail(403, str(exc))
        if not os.path.isfile(p):
            return self._fail(404, "文件不存在")
        token = secrets.token_urlsafe(12).replace("-", "").replace("_", "")
        STATE.shares[token] = {"path": p, "exp": time.time() + 86400}
        STATE.cleanup()
        # 用户可指定用哪个网络 IP 生成链接（多网卡时避免选错网段）
        host = str(data.get("host") or "").strip()
        allow = allowed_hosts(self.server.server_port)
        if host:
            bare = host.rsplit(":", 1)[0] if ":" in host and not host.endswith("]") else host
            if bare.strip("[]").lower() not in allow:
                return self._fail(400, "不允许使用该地址生成分享链接")
            if ":" not in host:
                host = f"{host}:{self.server.server_port}"
        else:
            host = self.headers.get("Host") or ""
            bare = (host.rsplit(":", 1)[0] if ":" in host else host).strip("[]").lower()
            if not host or bare not in allow or bare in ("127.0.0.1", "localhost", "::1"):
                urls = lan_urls(self.server.server_port)
                host = urls[0].replace("http://", "") if urls else f"127.0.0.1:{self.server.server_port}"
        self._json({"ok": True, "url": f"http://{host}/dl/{token}"})

    def api_set_config(self):
        if not self._local_ui():
            return self._fail(403, "只有电脑本机可以修改设置")
        try:
            data = self._json_body()
        except Exception:
            return self._fail(400, "请求格式错误", close=True)
        target = str(data.get("recv_dir") or "").strip().strip('"')
        if not target:
            return self._fail(400, "路径不能为空")
        target = os.path.abspath(target)
        if looks_unc(target):
            return self._fail(400, "接收目录必须是本地路径")
        if has_ads(target):
            return self._fail(400, "路径无效")
        drive, tail = os.path.splitdrive(target)
        if tail in ("\\", "/"):
            return self._fail(400, "不能把盘根设为接收目录")
        for root in _sensitive_roots():
            if inside(target, root):
                return self._fail(403, "不能把系统目录设为接收目录")
        try:
            os.makedirs(target, exist_ok=True)
        except OSError:
            return self._fail(400, "无法创建该目录")
        real = os.path.realpath(target)
        for root in _sensitive_roots():
            if inside(real, root):
                return self._fail(403, "不能把系统目录设为接收目录")
        STATE.recv_dir = target
        STATE.config["recv_dir"] = target
        STATE.save()
        self._json({"ok": True, "recv_dir": target})

    def api_open(self):
        if not self._local_ui():
            return self._fail(403, "只有电脑本机可以打开文件夹")
        try:
            data = self._json_body()
        except Exception:
            return self._fail(400, "请求格式错误", close=True)
        p = str(data.get("path") or STATE.recv_dir)
        if os.path.isfile(p):
            os.startfile(os.path.dirname(p))  # noqa: S606
        elif os.path.isdir(p):
            os.startfile(p)  # noqa: S606
        else:
            return self._fail(404, "路径不存在")
        self._json({"ok": True})

    def api_firewall_set(self):
        if not self._local_ui():
            return self._fail(403, "只有电脑本机可以设置防火墙")
        lines = [
            'netsh advfirewall firewall delete rule name="DropLink Port" >nul 2>&1',
            'netsh advfirewall firewall add rule name="DropLink Port" dir=in action=allow protocol=TCP localport=8848-8867 profile=any >nul 2>&1',
        ]
        done, code = _runas_bat(lines)
        if not done:
            return self._json({"ok": False, "cancelled": True, "msg": str(code)})
        if code != 0:
            return self._json({"ok": False, "msg": f"执行失败（退出码 {code}）"})
        return self._json({"ok": True, "msg": "防火墙规则已添加"})

    def api_firewall_remove(self):
        if not self._local_ui():
            return self._fail(403, "只有电脑本机可以设置防火墙")
        lines = [
            'netsh advfirewall firewall delete rule name="DropLink Port" >nul 2>&1',
            'netsh advfirewall firewall delete rule name="DropLink Program" >nul 2>&1',
            'netsh advfirewall firewall delete rule name="DropLink 8848" >nul 2>&1',
        ]
        done, code = _runas_bat(lines)
        if not done:
            return self._json({"ok": False, "cancelled": True, "msg": str(code)})
        return self._json({"ok": True, "msg": "防火墙规则已删除"})

    def api_diag(self):
        if not self._local_ui():
            return self._fail(403, "只有电脑本机可以诊断")
        return self._json({
            "ok": True,
            "urls": lan_urls(self.server.server_port),
            "fw_rule": _has_fw_rule(),
            "fw_detail": _fw_rule_detail(),
            "profiles": _net_profiles(),
        })

    def api_networks(self):
        if not self._local_ui():
            return self._fail(403, "只有电脑本机可以查看")
        return self._json({
            "ok": True,
            "port": self.server.server_port,
            "networks": _all_ips(),
        })

    def api_live(self):
        """正在进行的下载（本机发起的发送），带实时速率。"""
        if not self._authed():
            return self._fail(401, "需要配对码")
        now = time.time()
        items = []
        with _LIVE_LOCK:
            for v in _LIVE.values():
                el = max(0.2, now - v["start"])
                items.append({
                    "name": v["name"], "total": v["total"], "sent": v["sent"],
                    "speed": int(v["sent"] / el), "peer": v["peer"],
                    "elapsed": round(el, 1),
                })
        return self._json({"ok": True, "items": items})

    def api_peers(self):
        if not self._local_ui():
            return self._fail(403, "只有电脑本机可以查看")
        with STATE.lock:
            peers = [{"ip": k, "ts": v["ts"], "name": v.get("name") or device_name_of(v.get("ua", ""))}
                     for k, v in STATE.peers.items()]
        peers.sort(key=lambda x: -x["ts"])
        return self._json({"ok": True, "peers": peers})

    def api_send(self, q):
        """电脑端向指定手机（或多台）推送文件。body 为文件内容，query 带 name/targets。"""
        if not self._local_ui():
            return self._fail(403, "只有电脑本机可以发送")
        name = safe_name(q.get("name") or "file")
        targets_raw = str(q.get("targets") or "all").strip()
        targets = [t.strip() for t in targets_raw.split(",") if t.strip()]
        if not targets or targets == ["all"]:
            targets = ["all"]

        send_id = secrets.token_hex(6)
        dest = os.path.join(STATE.send_dir, f"{send_id}_{name}")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        received = 0
        try:
            with open(dest, "wb") as f:
                while received < length:
                    chunk = self.rfile.read(min(CHUNK, length - received))
                    if not chunk:
                        break
                    f.write(chunk)
                    received += len(chunk)
        except Exception as exc:
            return self._fail(500, f"接收文件失败：{exc}")
        if received == 0:
            os.unlink(dest)
            return self._fail(400, "文件为空")

        size = os.path.getsize(dest)
        item = {
            "id": send_id, "name": name, "path": dest, "size": size,
            "targets": targets, "ack": {}, "ts": time.time(),
        }
        with STATE.lock:
            STATE.pending.append(item)
        try:
            purge_send_cache()   # 顺手回收过期暂存，避免无限增长
        except Exception:
            pass
        self._json({"ok": True, "id": send_id, "name": name, "size": size, "targets": targets})

    def api_inbox(self):
        """手机端查询发给自己的待接收文件。"""
        if not self._authed():
            self.close_connection = True
            return self._fail(401, "需要配对码")
        ip = self.client_address[0]
        out = []
        with STATE.lock:
            for item in STATE.pending:
                if ip in item.get("ack", {}):
                    continue
                if item["targets"] != ["all"] and ip not in item["targets"]:
                    continue
                out.append({
                    "id": item["id"], "name": item["name"], "size": item["size"],
                    "url": f"/api/download?path={urllib.parse.quote(item['path'])}",
                })
        return self._json({"ok": True, "items": out})

    def api_inbox_ack(self):
        """手机确认已接收（下载完成后调用）。"""
        if not self._authed():
            self.close_connection = True
            return self._fail(401, "需要配对码")
        try:
            data = self._json_body()
        except Exception:
            return self._fail(400, "请求格式错误", close=True)
        ip = self.client_address[0]
        item_id = str(data.get("id") or "")
        with STATE.lock:
            for item in STATE.pending:
                if item["id"] == item_id:
                    item.setdefault("ack", {})[ip] = time.time()
                    break
        self._json({"ok": True})

    def api_open_hotspot(self):
        if not self._local_ui():
            return self._fail(403, "只有电脑本机可以操作")
        try:
            os.startfile("ms-settings:network-mobilehotspot")  # noqa: S606
        except Exception as exc:
            return self._fail(500, f"无法打开设置：{exc}")
        self._json({"ok": True})

    # ---------------- 上传 ----------------

    def api_upload(self, q):
        if not self._authed():
            self.close_connection = True
            return self._fail(401, "需要配对码")

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self.close_connection = True
            return self._fail(411, "缺少文件长度")
        if length > 64 * 1024 ** 3:
            self.close_connection = True
            return self._fail(413, "文件过大")

        rel = str(q.get("rel") or "").replace("\\", "/")
        try:
            parts = [strict_name(p) for p in rel.split("/") if p and p not in (".", "..")]
            name = strict_name(q.get("name") or (parts[-1] if parts else "file"), "file")
        except ValueError as exc:
            self.close_connection = True
            return self._fail(400, str(exc))
        # rel 既可能是「目录」（配合 name），也可能是「目录/文件名」（浏览器 webkitRelativePath）。
        # 只有最后一段与文件名一致时才把它当文件名去掉，否则全部当目录。
        sub_dirs = parts[:-1] if (parts and parts[-1] == name) else parts

        dest_dir = os.path.join(STATE.recv_dir, *sub_dirs) if sub_dirs else STATE.recv_dir
        dest_dir = os.path.abspath(dest_dir)
        if not inside(dest_dir, STATE.recv_dir):
            self.close_connection = True
            return self._fail(403, "只能上传到接收目录")
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError:
            self.close_connection = True
            return self._fail(500, "无法创建目录")
        # 目录建好后再确认一次：已有 junction 会把写入引到接收目录之外（修复 X-03）
        if not inside(dest_dir, STATE.recv_dir):
            self.close_connection = True
            return self._fail(403, "该目录指向接收目录之外")

        try:
            free = shutil.disk_usage(dest_dir).free
        except OSError:
            free = 0
        if free and free < length + (256 << 20):
            self.close_connection = True
            return self._fail(507, "磁盘剩余空间不足")

        tmp_path = os.path.join(dest_dir, f".dl_{secrets.token_hex(4)}.part")
        started = time.time()
        written = 0
        try:
            with open(tmp_path, "wb") as f:
                left = length
                while left > 0:
                    buf = self.rfile.read(min(CHUNK, left))
                    if not buf:
                        break
                    f.write(buf)
                    written += len(buf)
                    left -= len(buf)
                f.flush()
                os.fsync(f.fileno())
        except Exception:  # noqa: BLE001
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            self.close_connection = True
            return self._fail(500, "写入文件失败")

        if written != length:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            self.close_connection = True
            return self._fail(400, f"文件不完整（{written}/{length}）")

        try:
            target = claim_path(tmp_path, dest_dir, name)   # 原子抢名，防并发覆盖（X-04）
        except RuntimeError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return self._fail(500, "无法分配文件名")
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return self._fail(500, "保存文件失败")

        risky = mark_risky_file(target)

        mtime = self.headers.get("X-File-Mtime")
        if mtime:
            try:
                ts = float(mtime)
                if ts > 1e12:
                    ts /= 1000.0
                os.utime(target, (ts, ts))
            except Exception:
                pass

        elapsed = max(0.001, time.time() - started)
        STATE.add_transfer(direction="up", name=os.path.basename(target), size=length,
                           peer=self.client_address[0], speed=length / elapsed)
        print(f"[接收] {os.path.basename(target)}  {human_size(length)}  "
              f"{human_size(int(length / elapsed))}/s")

        self._json({"ok": True, "saved": target, "name": os.path.basename(target),
                    "size": length, "elapsed": round(elapsed, 2),
                    "risky": risky})

