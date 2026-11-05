# -*- coding: utf-8 -*-
"""
DropLink 互传 —— 原生桌面窗口（纯 ctypes 调用 Win32，无需 tkinter / 第三方库）

不弹黑色控制台窗口，双击 EXE 直接出现这个界面：
  二维码 + 手机访问地址 + 配对码 + 服务状态 + 常用按钮 + 托盘驻留
"""

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

# ---------------------------------------------------------------- 常量

WS_OVERLAPPED = 0x00000000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_VISIBLE = 0x10000000
WS_CLIPCHILDREN = 0x02000000
WS_TABSTOP = 0x00010000
WS_CHILD = 0x40000000
BS_DEFPUSHBUTTON = 0x0001
BS_PUSHBUTTON = 0x0000
SS_CENTER = 0x0001

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_COMMAND = 0x0111
WM_TIMER = 0x0113
WM_SIZE = 0x0005
WM_SETFONT = 0x0030
WM_CTLCOLORSTATIC = 0x0138
WM_USER = 0x0400
WM_TRAY = WM_USER + 1
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_GETMINMAXINFO = 0x0024
WM_PRINTCLIENT = 0x0318
WM_LBUTTONUP = 0x0202

HWND_MESSAGE = -3  # 只收消息、不显示的窗口

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

IDI_APPLICATION = 32512
IMAGE_ICON = 1
LR_SHARED = 0x00008000

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
TPM_LEFTALIGN = 0x0000
TPM_RETURNCMD = 0x0100
TPM_RIGHTBUTTON = 0x0002

SW_HIDE = 0
SW_SHOW = 5
SW_SHOWNORMAL = 1

TRANSPARENT = 1
DT_CENTER = 0x00000001
DT_VCENTER = 0x00000004
DT_SINGLELINE = 0x00000020
DT_NOPREFIX = 0x00000800
FW_BOLD = 700
FW_NORMAL = 400
DEFAULT_CHARSET = 1
CLEARTYPE_QUALITY = 5

COLOR_BTNTEXT = 18

# CW_USEDEFAULT 是 (int)0x80000000，写成 0x7FFFFFFF 会让窗口跑到屏幕外 (32767,32767)
CW_USEDEFAULT = -2147483648

ID_OPEN_UI = 101
ID_OPEN_DIR = 102
ID_COPY_URL = 103
ID_QUIT = 104

TITLE = "DropLink 互传"


def rgb(r, g, b):
    return (b << 16) | (g << 8) | r


C_BG = rgb(255, 255, 255)
C_TEXT = rgb(15, 23, 42)
C_TEXT2 = rgb(100, 116, 139)
C_MUTED = rgb(148, 163, 184)
C_BRAND = rgb(79, 70, 229)
C_OK = rgb(16, 185, 129)
C_LINE = rgb(226, 232, 240)
C_PANEL = rgb(248, 250, 252)
C_BTN = rgb(255, 255, 255)
C_BTN_TEXT = rgb(51, 65, 85)
C_PRIMARY = rgb(79, 70, 229)
C_PRIMARY_TEXT = rgb(255, 255, 255)

# ---------------------------------------------------------------- 库

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

try:
    ctypes.WinDLL("shcore", use_last_error=True).SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
                              ctypes.c_ulonglong, ctypes.c_longlong)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT), ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE), ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wintypes.HDC), ("fErase", wintypes.BOOL),
                ("rcPaint", wintypes.RECT), ("fRestore", wintypes.BOOL),
                ("fIncUpdate", wintypes.BOOL), ("rgbReserved", ctypes.c_char * 32)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256), ("uTimeout", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON),
    ]


class MINMAXINFO(ctypes.Structure):
    _fields_ = [("ptReserved", wintypes.POINT), ("ptMaxSize", wintypes.POINT),
                ("ptMaxPosition", wintypes.POINT), ("ptMinTrackSize", wintypes.POINT),
                ("ptMaxTrackSize", wintypes.POINT)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                ("wParam", ctypes.c_ulonglong), ("lParam", ctypes.c_longlong),
                ("time", wintypes.DWORD), ("pt", wintypes.POINT)]


# 资源 ID（IDI_APPLICATION / IDC_ARROW）走 MAKEINTRESOURCE：
# 必须以「整数指针值」传入，绝不能当成字符串指针，否则会直接访问违规崩溃。
user32.LoadIconW.restype = wintypes.HICON
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = wintypes.HANDLE
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]

# 从运行时目录的 icon.ico 加载应用图标（紫色渐变），保证窗口/托盘/EXE 图标风格统一
user32.LoadImageW.restype = wintypes.HANDLE
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT]

_APP_ICON = None
_APP_ICON_SMALL = None


def _load_app_icon(small=False):
    """加载 app/icon.ico（由打包器生成，多尺寸 16~256）。

    与手机 App 使用同一张图，保证桌面端窗口/任务栏/托盘图标风格统一。
    small=True 时按系统小图标尺寸（16/20/24px）取，托盘和标题栏更清晰。
    失败返回 None，调用方回退系统默认图标。
    """
    global _APP_ICON, _APP_ICON_SMALL
    cached = _APP_ICON_SMALL if small else _APP_ICON
    if cached:
        return cached
    try:
        ico = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
        if os.path.isfile(ico):
            # IMAGE_ICON=1, LR_LOADFROMFILE=0x10, LR_DEFAULTSIZE=0x20
            if small:
                cx = user32.GetSystemMetrics(49) or 16   # SM_CXSMICON
                cy = user32.GetSystemMetrics(50) or 16   # SM_CYSMICON
                h = user32.LoadImageW(None, ico, 1, cx, cy, 0x0010)
            else:
                h = user32.LoadImageW(None, ico, 1, 0, 0, 0x0010 | 0x0020)
            if h:
                if small:
                    _APP_ICON_SMALL = h
                else:
                    _APP_ICON = h
                return h
    except Exception:
        pass
    return None

user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                   wintypes.HANDLE, wintypes.HINSTANCE, ctypes.c_void_p]
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.DefWindowProcW.restype = ctypes.c_longlong
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_ulonglong, ctypes.c_longlong]
user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.SendMessageW.restype = ctypes.c_longlong
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_ulonglong, ctypes.c_longlong]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_ulonglong, ctypes.c_longlong]
user32.SetTimer.restype = ctypes.c_ulonglong
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_ulonglong, wintypes.UINT, ctypes.c_void_p]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UpdateWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
try:
    user32.GetDpiForSystem.restype = wintypes.UINT
except Exception:
    pass
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.InvalidateRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT), wintypes.BOOL]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.CreatePopupMenu.restype = wintypes.HANDLE
user32.AppendMenuW.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_ulonglong,
                               wintypes.LPCWSTR]
user32.TrackPopupMenu.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.DestroyMenu.argtypes = [wintypes.HANDLE]
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.EmptyClipboard.argtypes = []
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.CloseClipboard.argtypes = []
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.BeginPaint.restype = wintypes.HDC
user32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HANDLE]
user32.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
                             ctypes.POINTER(wintypes.RECT), wintypes.UINT]
user32.DrawTextW.restype = ctypes.c_int
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL

gdi32.CreateFontW.restype = wintypes.HFONT
gdi32.CreateSolidBrush.restype = wintypes.HANDLE
gdi32.CreateCompatibleBitmap.restype = wintypes.HANDLE
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.SelectObject.restype = wintypes.HANDLE
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.GetStockObject.restype = wintypes.HANDLE
gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.Ellipse.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

LOWORD = lambda v: v & 0xFFFF
HIWORD = lambda v: (v >> 16) & 0xFFFF


def dpi_scale():
    try:
        d = user32.GetDpiForSystem()
        if 96 <= d <= 480:
            return d / 96.0
    except Exception:
        pass
    return 1.0


# ---------------------------------------------------------------- 主窗口

class DropLinkWindow:
    W = 420
    H = 640

    def __init__(self, info):
        self.info = info
        self.url = (info["urls"][0] if info.get("urls") else
                    (info.get("local_url") or f"http://127.0.0.1:{info['port']}"))
        self.s = dpi_scale()
        self.hwnd = None
        self.tray_added = False
        self.running = True
        self.qr_bitmap = None
        self.qr_size = 0
        self.qr_url = ""
        self.started = time.time()
        self.fonts = {}
        self.brushes = {}
        self.buttons = []
        self.msg_toast = ""
        self.msg_until = 0
        self._build_qr()

    # ---- 二维码 ----

    def _build_qr(self):
        try:
            import qrmini
            grid = qrmini.qr_matrix(self.url, border=2)
        except Exception:
            self.qr_grid = None
            return
        self.qr_grid = grid
        self.qr_url = self.url

    def _make_qr_bitmap(self, hdc, px):
        """把二维码预渲染成位图，避免每秒重画上千次 FillRect。"""
        grid = self.qr_grid
        if not grid:
            return None
        n = len(grid)
        bmp = gdi32.CreateCompatibleBitmap(hdc, px, px)
        mem = gdi32.CreateCompatibleDC(hdc)
        old = gdi32.SelectObject(mem, bmp)
        brush_white = gdi32.CreateSolidBrush(C_BG)
        brush_black = gdi32.CreateSolidBrush(rgb(17, 24, 39))
        r = wintypes.RECT(0, 0, px, px)
        user32.FillRect(mem, ctypes.byref(r), brush_white)
        cell = px / float(n)
        for y in range(n):
            yy0 = int(y * cell)
            yy1 = max(yy0 + 1, int((y + 1) * cell))
            for x in range(n):
                if not grid[y][x]:
                    continue
                xx0 = int(x * cell)
                xx1 = max(xx0 + 1, int((x + 1) * cell))
                rc = wintypes.RECT(xx0, yy0, xx1, yy1)
                user32.FillRect(mem, ctypes.byref(rc), brush_black)
        gdi32.DeleteObject(brush_white)
        gdi32.DeleteObject(brush_black)
        gdi32.SelectObject(mem, old)
        gdi32.DeleteDC(mem)
        return bmp

    # ---- 字体 / 画刷 ----

    def font(self, size, bold=False, face="Microsoft YaHei UI"):
        key = (size, bold, face)
        if key not in self.fonts:
            self.fonts[key] = gdi32.CreateFontW(
                int(-size * self.s), 0, 0, 0, FW_BOLD if bold else FW_NORMAL,
                0, 0, 0, DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY, 0, face)
        return self.fonts[key]

    def brush(self, color):
        if color not in self.brushes:
            self.brushes[color] = gdi32.CreateSolidBrush(color)
        return self.brushes[color]

    def text(self, hdc, s, x, y, w, h, color, size, bold=False, align=DT_CENTER,
             face="Microsoft YaHei UI"):
        old = gdi32.SelectObject(hdc, self.font(size, bold, face))
        gdi32.SetTextColor(hdc, color)
        gdi32.SetBkMode(hdc, TRANSPARENT)
        rc = wintypes.RECT(int(x), int(y), int(x + w), int(y + h))
        user32.DrawTextW(hdc, s, -1, ctypes.byref(rc), align | DT_NOPREFIX)
        gdi32.SelectObject(hdc, old)

    # ---- 创建 ----

    def create(self, hidden=False):
        hinst = kernel32.GetModuleHandleW(None)
        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
        # 回调对象必须长期持有，否则被 GC 后窗口过程会直接崩
        self.wndproc = WNDPROC(self._proc)
        cls.lpfnWndProc = self.wndproc
        cls.hInstance = hinst
        cls.lpszClassName = "DropLinkWnd"
        cls.hIcon = _load_app_icon() or user32.LoadIconW(None, IDI_APPLICATION)
        cls.hIconSm = _load_app_icon(small=True) or cls.hIcon
        cls.hCursor = user32.LoadCursorW(None, 32512)  # IDC_ARROW
        cls.hbrBackground = self.brush(C_BG)
        self.wndclass = cls
        if not user32.RegisterClassExW(ctypes.byref(cls)):
            raise RuntimeError("注册窗口类失败")

        if hidden:
            # 只驻留托盘：创建一个不显示的 message-only 窗口来收消息
            self.hwnd = user32.CreateWindowExW(
                0, "DropLinkWnd", TITLE, 0,
                0, 0, 0, 0, HWND_MESSAGE, None, hinst, None)
            if not self.hwnd:
                raise RuntimeError("创建托盘窗口失败")
            return self.hwnd

        style = WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX | WS_CLIPCHILDREN
        # 布局用的是客户区坐标，先按风格换算成外框尺寸，
        # 否则标题栏会占掉一截，底部的按钮会被挤出可视区。
        w, h = int(self.W * self.s), int(self.H * self.s)
        rc = wintypes.RECT(0, 0, w, h)
        user32.AdjustWindowRect.argtypes = [ctypes.POINTER(wintypes.RECT),
                                            wintypes.DWORD, wintypes.BOOL]
        user32.AdjustWindowRect(ctypes.byref(rc), style, False)
        self.hwnd = user32.CreateWindowExW(
            0, "DropLinkWnd", TITLE, style,
            CW_USEDEFAULT, CW_USEDEFAULT,
            rc.right - rc.left, rc.bottom - rc.top,
            None, None, hinst, None)
        if not self.hwnd:
            raise RuntimeError("创建窗口失败")

        self._fit_client()
        self._center()
        self._make_buttons()
        user32.ShowWindow(self.hwnd, SW_SHOW)
        user32.UpdateWindow(self.hwnd)
        user32.SetTimer(self.hwnd, 1, 1000, None)
        return self.hwnd

    def _fit_client(self):
        """把外框撑到「客户区正好等于设计尺寸」，避免底部按钮被标题栏挤走。"""
        c = wintypes.RECT()
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetClientRect(self.hwnd, ctypes.byref(c))
        tw, th = int(self.W * self.s), int(self.H * self.s)
        dw, dh = tw - c.right, th - c.bottom
        if dw or dh:
            o = wintypes.RECT()
            user32.GetWindowRect(self.hwnd, ctypes.byref(o))
            self.frame_w = (o.right - o.left) + dw
            self.frame_h = (o.bottom - o.top) + dh
            user32.SetWindowPos(self.hwnd, 0, 0, 0, self.frame_w, self.frame_h,
                                0x0002)  # SWP_NOMOVE（尺寸要生效，不能加 NOSIZE）
        else:
            o = wintypes.RECT()
            user32.GetWindowRect(self.hwnd, ctypes.byref(o))
            self.frame_w, self.frame_h = o.right - o.left, o.bottom - o.top

    def _center(self):
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        rc = wintypes.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(rc))
        w = rc.right - rc.left
        h = rc.bottom - rc.top
        user32.SetWindowPos(self.hwnd, 0, (sw - w) // 2, max(0, (sh - h) // 2 - 20),
                            0, 0, 0x0001 | 0x0002)  # SWP_NOSIZE | SWP_NOZORDER

    def _make_buttons(self):
        s = self.s
        hw, uifont = self.hwnd, self.font(12)
        specs = [
            (ID_OPEN_UI, "打开电脑界面", int(40 * s), int(500 * s), int(340 * s), int(38 * s)),
            (ID_OPEN_DIR, "打开接收文件夹", int(40 * s), int(546 * s), int(340 * s), int(34 * s)),
            (ID_QUIT, "停止服务并退出", int(40 * s), int(588 * s), int(340 * s), int(30 * s)),
        ]
        for cid, label, x, y, w, h in specs:
            b = user32.CreateWindowExW(0, "BUTTON", label,
                                       WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_PUSHBUTTON,
                                       x, y, w, h, hw, cid,
                                       kernel32.GetModuleHandleW(None), None)
            user32.SendMessageW(b, WM_SETFONT, uifont, 1)
            self.buttons.append(b)
        # 地址行旁的小按钮：复制
        cp = user32.CreateWindowExW(0, "BUTTON", "复制地址",
                                    WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_PUSHBUTTON,
                                    int(160 * s), int(336 * s), int(100 * s), int(26 * s),
                                    hw, ID_COPY_URL, kernel32.GetModuleHandleW(None), None)
        user32.SendMessageW(cp, WM_SETFONT, self.font(11), 1)
        self.buttons.append(cp)

    # ---- 托盘 ----

    def add_tray(self):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = _load_app_icon(small=True) or user32.LoadIconW(None, IDI_APPLICATION)
        nid.szTip = TITLE + " · 服务运行中"
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        self.tray_added = True

    def del_tray(self):
        if not self.tray_added:
            return
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        self.tray_added = False

    def tray_menu(self):
        m = user32.CreatePopupMenu()
        user32.AppendMenuW(m, MF_STRING, ID_OPEN_UI, "打开电脑界面")
        user32.AppendMenuW(m, MF_STRING, ID_OPEN_DIR, "打开接收文件夹")
        user32.AppendMenuW(m, MF_STRING, ID_COPY_URL, "复制地址")
        user32.AppendMenuW(m, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(m, MF_STRING, ID_QUIT, "退出")
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(m, TPM_LEFTALIGN | TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                    pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, 0, 0, 0)
        user32.DestroyMenu(m)
        return cmd

    # ---- 动作 ----

    def copy_url(self):
        try:
            ctypes.windll.user32.OpenClipboard(self.hwnd)
            ctypes.windll.user32.EmptyClipboard()
            kernel32.GlobalAlloc.restype = wintypes.HANDLE
            kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
            h = kernel32.GlobalAlloc(0x0042, (len(self.url) + 1) * 2)  # GMEM_MOVEABLE|ZEROINIT
            kernel32.GlobalLock.restype = ctypes.c_void_p
            kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
            kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
            p = kernel32.GlobalLock(h)
            ctypes.memmove(p, self.url.encode("utf-16-le"), len(self.url) * 2)
            kernel32.GlobalUnlock(h)
            ctypes.windll.user32.SetClipboardData(13, h)  # CF_UNICODETEXT
            ctypes.windll.user32.CloseClipboard()
            self.flash("地址已复制")
        except Exception:
            self.flash("复制失败")

    def flash(self, msg):
        self.msg_toast = msg
        self.msg_until = time.time() + 2.5
        user32.InvalidateRect(self.hwnd, None, True)

    def open_ui(self):
        base = self.info.get("local_url") or f"http://127.0.0.1:{self.info['port']}"
        try:
            import server
            tok = self.info.get("local_token") or ""
            suffix = ("?k=" + tok) if tok else ""
            server.open_app_window(f"{base}/{suffix}")
        except Exception:
            import webbrowser
            tok = self.info.get("local_token") or ""
            suffix = ("?k=" + tok) if tok else ""
            webbrowser.open(f"{base}/{suffix}")

    def open_dir(self):
        try:
            os.startfile(self.info["recv_dir"])
        except Exception:
            pass

    def quit(self):
        self.running = False
        self.del_tray()
        user32.DestroyWindow(self.hwnd)

    # ---- 绘制 ----

    def paint(self, hdc=None):
        """hdc 为空时走标准 WM_PAINT；否则绘制到指定 DC（供 WM_PRINTCLIENT /
        PrintWindow / 系统缩略图使用）。"""
        if hdc is None:
            ps = PAINTSTRUCT()
            hdc = user32.BeginPaint(self.hwnd, ctypes.byref(ps))
            try:
                self.paint_into(hdc)
            finally:
                user32.EndPaint(self.hwnd, ctypes.byref(ps))
        else:
            self.paint_into(hdc)

    def paint_into(self, hdc):
        s = self.s
        W = int(self.W * s)
        gray = gdi32.CreateSolidBrush(C_PANEL)
        line = gdi32.CreatePen(0, 1, C_LINE)

        # 二维码底板
        qr_px = int(196 * s)
        qx = (W - qr_px) // 2
        qy = int(74 * s)
        rc = wintypes.RECT(qx - int(10 * s), qy - int(10 * s),
                           qx + qr_px + int(10 * s), qy + qr_px + int(10 * s))
        user32.FillRect(hdc, ctypes.byref(rc), gray)

        self.text(hdc, TITLE, 0, int(22 * s), W, int(30 * s), C_TEXT, 20, True)
        self.text(hdc, "同一个 Wi-Fi，手机电脑互传文件", 0, int(50 * s), W, int(18 * s),
                  C_TEXT2, 11)

        if self.qr_grid:
            if self.qr_bitmap is None or self.qr_url != self.url:
                if self.qr_bitmap:
                    gdi32.DeleteObject(self.qr_bitmap)
                self._build_qr()
                self.qr_bitmap = self._make_qr_bitmap(hdc, qr_px)
            if self.qr_bitmap:
                mem = gdi32.CreateCompatibleDC(hdc)
                old = gdi32.SelectObject(mem, self.qr_bitmap)
                gdi32.BitBlt(hdc, qx, qy, qr_px, qr_px, mem, 0, 0, 0x00CC0020)  # SRCCOPY
                gdi32.SelectObject(mem, old)
                gdi32.DeleteDC(mem)
        else:
            self.text(hdc, "（二维码生成失败）", qx, qy + qr_px // 2 - 10, qr_px, 20,
                      C_MUTED, 11)

        y = qy + qr_px + int(26 * s)
        self.text(hdc, "手机浏览器打开下面的地址", 0, y, W, int(18 * s), C_TEXT2, 11)
        self.text(hdc, self.url, 0, y + int(20 * s), W, int(24 * s), C_BRAND, 16, True,
                  face="Consolas")

        y2 = y + int(74 * s)
        self.text(hdc, "配 对 码", 0, y2, W, int(18 * s), C_TEXT2, 11)
        self.text(hdc, self.info["pin"], 0, y2 + int(18 * s), W, int(38 * s),
                  C_TEXT, 28, True, face="Consolas")

        # 状态
        try:
            import server
            n = len(server.STATE.sessions)
        except Exception:
            n = 0
        sy = y2 + int(66 * s)
        dot = int(6 * s)
        dx = W // 2 - int(78 * s)
        old_br = gdi32.SelectObject(hdc, self.brush(C_OK))
        old_pen = gdi32.SelectObject(hdc, gdi32.GetStockObject(8))  # NULL_PEN
        gdi32.Ellipse(hdc, dx, sy + int(5 * s), dx + dot, sy + int(5 * s) + dot)
        gdi32.SelectObject(hdc, old_br)
        gdi32.SelectObject(hdc, old_pen)
        self.text(hdc, f"服务已启动 · 已配对 {n} 台设备", dx + int(14 * s), sy,
                  int(200 * s), int(18 * s), C_TEXT2, 11, align=DT_CENTER)

        # 底部提示
        tip = self.msg_toast if time.time() < self.msg_until else "关闭此窗口即停止服务"
        self.text(hdc, tip, 0, int(462 * s), W, int(18 * s),
                  C_BRAND if self.msg_toast and time.time() < self.msg_until else C_MUTED, 11)

        gdi32.DeleteObject(gray)
        gdi32.DeleteObject(line)

    # ---- 消息 ----

    def _proc(self, hwnd, msg, wp, lp):
        if msg == WM_PAINT:
            self.paint()
            return 0
        if msg == WM_PRINTCLIENT:
            # 让 PrintWindow / 系统缩略图 / 截图工具也能拿到自绘内容
            self.paint(wp)
            return 0
        if msg == WM_ERASEBKGND:
            return 1
        if msg == WM_TIMER:
            user32.InvalidateRect(hwnd, None, True)
            return 0
        if msg == WM_COMMAND:
            cid = LOWORD(wp)
            if cid == ID_OPEN_UI:
                self.open_ui()
            elif cid == ID_OPEN_DIR:
                self.open_dir()
            elif cid == ID_COPY_URL:
                self.copy_url()
            elif cid == ID_QUIT:
                self.quit()
            return 0
        if msg == WM_TRAY:
            # 无主窗口（托盘模式）时，左键单击直接打开电脑端界面
            if LOWORD(lp) in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self.open_ui()
            elif LOWORD(lp) == WM_RBUTTONUP:
                cmd = self.tray_menu()
                if cmd == ID_OPEN_UI:
                    self.open_ui()
                elif cmd == ID_OPEN_DIR:
                    self.open_dir()
                elif cmd == ID_COPY_URL:
                    self.copy_url()
                elif cmd == ID_QUIT:
                    self.quit()
            return 0
        if msg == WM_SIZE:
            if wp == 1:  # SIZE_MINIMIZED
                user32.ShowWindow(hwnd, SW_HIDE)
                if not self.tray_added:
                    self.add_tray()
            return 0
        if msg == WM_GETMINMAXINFO:
            # 注意用「外框」尺寸，否则会把 _fit_client 撑开的客户区又压回去
            mmi = ctypes.cast(lp, ctypes.POINTER(MINMAXINFO)).contents
            fw = getattr(self, "frame_w", 0) or int(self.W * self.s)
            fh = getattr(self, "frame_h", 0) or int(self.H * self.s)
            mmi.ptMinTrackSize.x = fw
            mmi.ptMinTrackSize.y = fh
            mmi.ptMaxTrackSize.x = fw
            mmi.ptMaxTrackSize.y = fh
            return 0
        if msg == WM_CLOSE:
            self.quit()
            return 0
        if msg == WM_DESTROY:
            self.del_tray()
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def run(self):
        m = MSG()
        while self.running and user32.GetMessageW(ctypes.byref(m), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(m))
            user32.DispatchMessageW(ctypes.byref(m))
        return 0


def run(info):
    w = DropLinkWindow(info)
    w.create()
    w.add_tray()
    return w.run()


def run_tray(info):
    """只驻留右下角托盘（不弹任何窗口），提供退出 / 重新打开界面的入口。"""
    w = DropLinkWindow(info)
    w.create(hidden=True)
    w.add_tray()
    return w.run()
