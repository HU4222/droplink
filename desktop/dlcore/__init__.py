# -*- coding: utf-8 -*-
"""DropLink 服务端内部模块包（带容错加载器）。

设计目标（1.4.2）
-----------------
**主模块**（server.py 与本文件）是唯一的硬依赖。除此之外，dlcore 下任何一个
模块被删掉、改名、写坏（语法错误）或导入时报异常，服务都必须仍然启动，
只是对应能力降级，并把「哪个模块坏了、坏在哪一行」说清楚。

做法
----
按依赖顺序逐个 import；任何一个失败，就构造一个「占位模块」塞进
sys.modules，使后面模块的 ``from .xxx import ...`` 依然成立。

占位模块的默认值刻意 **fail-closed**（失败即拒绝）：
  * fsutil 坏了 → 路径校验一律判定为不合法（拒绝访盘），而不是放行；
  * state 坏了  → 会话校验一律判为无效（无法配对登录），而不是免登录；
  * media 坏了  → 图标/二维码返回 None（前端显示占位），而不是伪造一张图。
宁可少一个功能，也不要在降级状态下多开一个口子。
"""
from __future__ import annotations

import importlib
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import types

# 加载顺序 = 依赖顺序：前面的模块被后面的模块 star-import
MODULES = ("config", "fsutil", "state", "winenv", "media", "api", "httpd")

# 模块名 -> {"state": "ok" | "missing" | "broken", "error": str}
STATUS = {}

# 配置模块坏掉时用的兜底版本号；正式版本在 dlcore/config.py，两处需一致
_FALLBACK_VERSION = "1.5.0"


class Placeholder:
    """「不可用」占位对象：可调用、可下标、可迭代，布尔恒为 False。

    用于填充占位模块里没列举到的名字，避免降级后再抛 NameError/AttributeError
    把整个请求打成 500。它不会假装成功——布尔为 False，长度 0，迭代为空。
    """

    __slots__ = ("_why",)

    def __init__(self, why: str = ""):
        object.__setattr__(self, "_why", why)

    def __call__(self, *a, **k):
        return self

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return self

    def __getitem__(self, key):
        return self

    def __setitem__(self, key, value):
        return None

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def __bool__(self):
        return False

    def __repr__(self):
        return "<不可用%s>" % ((": " + self._why) if self._why else "")


# --------------------------------------------------------------------------
# 各模块的兜底默认值
# --------------------------------------------------------------------------

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cfg_set_secure(on: bool) -> None:
    globals()["SECURE_MODE"] = bool(on)


def _cfg_is_secure() -> bool:
    return bool(globals().get("SECURE_MODE"))


def _cfg_is_secure_edition() -> bool:
    """配置模块坏掉时版本身份不可知：按个人版处理。

    个人版不注册任何安全版路由，降级状态下最坏结果是「安全版少三个接口」，
    而不是把预留路由暴露出去。
    """
    return False


def _fs_check_path(path: str):
    """fail-closed：安全基线不可用时拒绝任何路径。

    按真实 check_path 的契约**抛异常**而不是返回 None——调用方（api._read_target）
    靠 ValueError/PermissionError 来回应 4xx；返回 None 会让请求既不回包也不报错，
    客户端只能干等到超时。
    """
    raise PermissionError("路径校验模块（dlcore/fsutil.py）不可用，已拒绝访问")


def _fs_safe_name(name: str, fallback: str = "file") -> str:
    base = os.path.basename(str(name or "").replace("\\", "/"))
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", base).strip(" .")
    return base or fallback


def _fs_inside(path: str, parent: str) -> bool:
    """fail-closed：判断不了就当作「不在里面」。"""
    return False


def _fs_human_size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "0 B"


def _fs_known_folder(name: str) -> str:
    home = os.path.expanduser("~")
    table = {"desktop": "Desktop", "downloads": "Downloads", "documents": "Documents",
             "pictures": "Pictures", "videos": "Videos", "music": "Music", "home": ""}
    return os.path.join(home, table.get(str(name or "").lower(), "")) if name else home


def _local_ip():
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def _st_lan_urls(port: int):
    """网卡模块不可用时的降级实现：只靠一次 UDP 连接问出默认出口地址。"""
    ips = []
    try:
        ips.append(_local_ip())
    except Exception:
        pass
    urls = ["http://%s:%d" % (ip, port) for ip in ips]
    return urls or ["http://127.0.0.1:%d" % port]


class _MiniState:
    """state 模块不可用时的最小状态：字段齐全，但会话一律不通过。"""

    def __init__(self):
        self.pin = "%06d" % secrets.randbelow(10 ** 6)
        self.recv_dir = os.path.join(os.path.expanduser("~"), "Desktop", "DropLink 接收")
        self.send_dir = os.path.join(tempfile.gettempdir(), "droplink_send")
        self.config = {"recv_dir": self.recv_dir}
        self.sessions = {}
        self.shares = {}
        self.fails = {}
        self.peers = {}
        self.pending = {}
        self.transfers = []
        self.clip = ""
        self.started = time.time()
        self.lock = threading.RLock()

    def save(self):
        return None

    def cleanup(self):
        return None

    # 安全默认：判不出有效会话就是无效，绝不因为降级而免登录
    def session_valid(self, *a, **k):
        return None

    def new_session(self, *a, **k):
        return ""

    def revoke_session(self, *a, **k):
        return None

    def revoke_all(self):
        return None

    def add_clip(self, *a, **k):
        return None

    def add_transfer(self, *a, **k):
        return None


def _make_api_mixin():
    """api 模块不可用时的 ApiMixin：任何 api_* 调用都回 503，而不是 500 堆栈。"""

    class ApiMixin:
        def __getattr__(self, name):
            if not name.startswith("api_"):
                raise AttributeError(name)

            def _unavailable(*a, **k):
                msg = "该功能暂不可用：dlcore/api.py 加载失败，请重新安装或恢复该文件"
                try:
                    return self._fail(503, msg)
                except Exception:
                    return None

            return _unavailable

    return ApiMixin


FALLBACKS = {
    "config": {
        "APP_NAME": "DropLink 互传",
        "APP_SHORT": "DropLink",
        "VERSION": _FALLBACK_VERSION,
        "SECURE_MODE": os.environ.get("DROPLINK_SECURE") == "1",
        "CREATE_NO_WINDOW": 0x08000000 if os.name == "nt" else 0,
        "APP_DIR": _APP_DIR,
        "WEB_DIR": os.path.join(_APP_DIR, "web"),
        "VENDOR_DIR": os.path.join(_APP_DIR, "vendor"),
        "CACHE_DIR": os.path.join(_APP_DIR, ".cache", "thumbs"),
        "CONFIG_PATH": os.path.join(_APP_DIR, "config.json"),
        "IMAGE_EXT": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif",
                      ".svg", ".ico", ".avif", ".tif", ".tiff"},
        "VIDEO_EXT": {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".3gp", ".ts"},
        "AUDIO_EXT": {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".amr", ".opus"},
        "TEXT_EXT": {".txt", ".md", ".markdown", ".json", ".log", ".csv", ".tsv", ".xml",
                     ".ini", ".cfg", ".conf", ".py", ".js", ".ts", ".html", ".htm",
                     ".css", ".yaml", ".yml", ".bat", ".ps1", ".sh", ".c", ".cpp", ".h",
                     ".java", ".sql", ".toml", ".env", ".gitignore"},
        "ARCHIVE_EXT": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso"},
        "DOC_EXT": {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".wps", ".et",
                    ".dps", ".pdf"},
        "SKIP_NAMES": {"$RECYCLE.BIN", "System Volume Information", "pagefile.sys",
                       "swapfile.sys", "hiberfil.sys", "DumpStack.log", "desktop.ini"},
        "CHUNK": 512 * 1024,
        "_LIVE": {},
        "_LIVE_LOCK": threading.Lock(),
        "_STATIC_CACHE": {},
        "set_secure": _cfg_set_secure,
        "is_secure": _cfg_is_secure,
        "EDITION": "personal",
        "is_secure_edition": _cfg_is_secure_edition,
    },
    "fsutil": {
        "LOCAL_TOKEN": os.environ.get("DROPLINK_LOCAL_TOKEN") or secrets.token_urlsafe(24),
        "SESSION_ABS_TTL": 30 * 86400,
        "SESSION_IDLE_TTL": 7 * 86400,
        "MAX_SESSIONS": 64,
        "MAX_FAIL_IPS": 4096,
        "MAX_PEERS": 256,
        "RISKY_EXT": set(),
        "CSIDL": {},
        "BAD_CHARS": re.compile(r'[\\/:*?"<>|\x00-\x1f]'),
        "INVISIBLE_CHARS": re.compile("[\u200b-\u200f\ufeff]"),
        "RESERVED_NAMES": re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.I),
        "_sensitive_roots": lambda: [],
        "check_path": _fs_check_path,
        "looks_unc": lambda p: str(p or "").startswith("\\\\"),
        "has_ads": lambda p: False,
        "mark_risky_file": lambda p: False,
        "known_folder": _fs_known_folder,
        "list_drives": lambda: [],
        "quick_roots": lambda: [],
        "kind_of": lambda name: "file",
        "natkey": lambda s: str(s or "").lower(),
        "has_invisible": lambda s: False,
        "safe_name": _fs_safe_name,
        "strict_name": lambda raw, fallback="": "",
        "unique_path": lambda folder, name: os.path.join(folder, name),
        "claim_path": lambda tmp, folder, name: os.path.join(folder, name),
        "inside": _fs_inside,
        "lexical_inside": _fs_inside,
        "human_size": _fs_human_size,
        "recycle": lambda paths: 0,
    },
    "state": {
        "State": _MiniState,
        "STATE": _MiniState(),
        "SEND_TTL": 24 * 3600,
        "SEND_MAX_BYTES": 2 << 30,
        "purge_send_cache": lambda: (0, 0),
    },
    "winenv": {
        "lan_urls": _st_lan_urls,
        "allowed_hosts": lambda port: set(),
        "device_name_of": lambda ua: "未知设备",
        "_is_admin": lambda: False,
        "_runas_bat": lambda lines: False,
        "_has_fw_rule": lambda: False,
        "_fw_rule_detail": lambda: "网络模块不可用",
        "_net_profiles": lambda: [],
        "_all_ips": lambda: [],
        "_HOST_CACHE": {"ts": 0.0, "hosts": set()},
        "_HOST_CACHE_LOCK": threading.Lock(),
    },
    "media": {
        "qr_png": lambda text, box=8, border=2: None,
        "has_pillow": lambda: False,
        "qr_available": lambda: False,
        "thumb_bytes": lambda path, width=320: None,
        "icon_png": lambda size=192: None,
        "_ICON_CACHE": {},
        "_QR_OK": False,
        "_THUMB_SEM": threading.Semaphore(1),
    },
    "api": {
        "ApiMixin": _make_api_mixin(),
    },
    "httpd": {
        "Server": Placeholder("httpd 模块不可用"),
        "Handler": Placeholder("httpd 模块不可用"),
    },
}


def _make_stub(name: str, error: str) -> types.ModuleType:
    """构造占位模块：先填兜底默认值，其余名字按需返回 Placeholder。"""
    mod = types.ModuleType(__name__ + "." + name)
    mod.__doc__ = "占位模块：dlcore.%s 加载失败（%s）。" % (name, error)
    mod.__dl_degraded__ = True
    mod.__dl_error__ = error

    table = FALLBACKS.get(name) or {}
    for key, value in table.items():
        setattr(mod, key, value)
    # 星号导入只看 __all__ / 非下划线名，所以这里列全，避免 from .x import * 拿到空
    mod.__all__ = sorted(k for k in table if not k.startswith("_"))

    def _module_getattr(attr, _name=name):
        if attr.startswith("__"):
            raise AttributeError(attr)
        value = Placeholder("%s.%s" % (_name, attr))
        setattr(mod, attr, value)      # 缓存，别重复构造
        return value

    mod.__getattr__ = _module_getattr
    return mod


def _load(name: str) -> None:
    full = __name__ + "." + name
    try:
        importlib.import_module("." + name, __name__)
    except Exception as exc:          # SyntaxError / ImportError / 运行期异常都算损坏
        err = "%s: %s" % (type(exc).__name__, exc)
        line = ""
        try:
            tb = exc.__traceback__
            while tb is not None and tb.tb_next is not None:
                tb = tb.tb_next
            if tb is not None:
                line = "（%s:%s）" % (os.path.basename(tb.tb_frame.f_code.co_filename),
                                     tb.tb_lineno)
        except Exception:
            pass
        err = (err + line)[:220]
        state = "missing" if isinstance(exc, ModuleNotFoundError) else "broken"
        stub = _make_stub(name, err)
        sys.modules[full] = stub
        setattr(sys.modules[__name__], name, stub)
        STATUS[name] = {"state": state, "error": err}
    else:
        STATUS[name] = {"state": "ok", "error": ""}


def load_all() -> None:
    """按依赖顺序加载；已判定降级的模块不重复尝试。"""
    for name in MODULES:
        full = __name__ + "." + name
        mod = sys.modules.get(full)
        if mod is not None and getattr(mod, "__dl_degraded__", False):
            continue
        if STATUS.get(name, {}).get("state") == "ok":
            continue
        _load(name)


def ok(name: str) -> bool:
    """该模块是否正常加载。"""
    return STATUS.get(name, {}).get("state") == "ok"


def degraded_names():
    """所有降级模块名（正常时返回空列表）。"""
    return [n for n in MODULES if not ok(n)]


def describe():
    """完整状态，供诊断页与 /api/ping 使用。"""
    return [{"name": n, "state": STATUS.get(n, {}).get("state", "unknown"),
             "error": STATUS.get(n, {}).get("error", "")} for n in MODULES]


def health_line() -> str:
    """一行摘要，写日志用。"""
    bad = degraded_names()
    if not bad:
        return "模块全部就绪（%d 个）" % len(MODULES)
    return "降级运行：%d/%d 个模块不可用（%s）" % (len(bad), len(MODULES), "、".join(bad))


load_all()
