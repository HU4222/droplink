# -*- coding: utf-8 -*-
"""DropLink 互传 —— 独立 EXE 入口。

双击 EXE 后：
  1. 在后台线程启动局域网服务；
  2. 弹出原生窗口，显示二维码 / 访问地址 / 配对码；
  3. 自动打开电脑端界面（Edge、Chrome 应用模式）；
  4. 关掉窗口即停止服务。
"""

import os
import sys
import time


def _redirect_output():
    """pythonw 下 sys.stdout 是 None，print 会直接崩，先接到日志文件。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        logdir = os.path.join(base, "DropLink")
        os.makedirs(logdir, exist_ok=True)
        f = open(os.path.join(logdir, "droplink.log"), "a", encoding="utf-8")
        f.write("\n===== %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        f.flush()
    except Exception:
        f = open(os.devnull, "w")
    sys.stdout = f
    sys.stderr = f


_redirect_output()

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import server  # noqa: E402


def _set_app_user_model_id():
    """让任务栏把本进程归到 DropLink 名下（含托盘通知分组），而不是 pythonw。"""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DropLink.App")
    except Exception:
        pass


def parse_args(argv):
    port, pin, recv, gui, browser, secure = 8848, "", "", True, True, False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1]); i += 2
        elif a == "--pin" and i + 1 < len(argv):
            pin = argv[i + 1]; i += 2
        elif a == "--recv" and i + 1 < len(argv):
            recv = argv[i + 1]; i += 2
        elif a == "--no-gui":
            gui = False; i += 1
        elif a == "--no-browser":
            browser = False; i += 1
        elif a == "--secure":
            secure = True; i += 1
        else:
            i += 1
    return port, pin, recv, gui, browser, secure


def main():
    _set_app_user_model_id()
    port, pin, recv, use_gui, use_browser, secure = parse_args(sys.argv[1:])
    if secure:
        server.SECURE_MODE = True
    try:
        info = server.start_server(port, pin, recv, background=True)
    except RuntimeError as exc:
        print(str(exc))
        return 1

    # 把实际端口写进运行时目录，供「再次双击 EXE」时重新打开界面用
    try:
        rt = os.path.dirname(HERE)
        with open(os.path.join(rt, ".port"), "w", encoding="ascii") as _f:
            _f.write(str(info["port"]))
    except Exception:
        pass

    if not use_gui:
        server.print_banner(info["port"], info["urls"])
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return 0

    if use_browser:
        def _open():
            time.sleep(0.6)
            try:
                # 唯一可见界面：直接进「连接信息」页（二维码 + 地址 + 配对码）
                # 带本机令牌，服务端才认这是程序自己打开的窗口（不是任意网页）
                server.open_app_window(
                    "http://127.0.0.1:%d/?k=%s#about" % (info["port"], info["local_token"]))
            except Exception:
                pass

        import threading
        threading.Thread(target=_open, daemon=True).start()

    try:
        import gui
        gui.run_tray(info)  # 只驻留托盘，不弹窗口；托盘右键可退出
    except Exception as exc:  # 托盘起不来也要让服务可用
        print("托盘启动失败：%r" % (exc,))
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    # 退出：尝试优雅停服务，但最多等 2 秒。有活跃传输时 shutdown 可能阻塞，
    # 若不设上限会导致 os._exit 迟迟不执行，pythonw 卡住、launcher 跟着残留。
    try:
        def _stop():
            try:
                info["httpd"].shutdown()
                info["httpd"].server_close()
            except Exception:
                pass
        import threading as _th
        _t = _th.Thread(target=_stop, daemon=True)
        _t.start()
        _t.join(2.0)
    except Exception:
        pass
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
