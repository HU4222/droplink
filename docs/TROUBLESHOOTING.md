# Troubleshooting

## The phone cannot open the address

Work through these in order — each one fixes a real, common cause.

1. **Same network?** The phone must be on the same Wi-Fi as the PC. A phone on 5 GHz and a PC on 2.4 GHz usually still works (same router), but a *guest* network is isolated from the main one.
2. **AP isolation.** Many routers (and almost all public hotspots) block device-to-device traffic. The symptom: the address times out even though both devices show Wi-Fi. Fix: turn off "AP isolation" / "client isolation" in the router, or use the phone's hotspot instead.
3. **VPN / proxy.** A VPN on either device routes LAN traffic away. Turn it off, or allow local traffic in the VPN app.
4. **Windows Firewall.** The first launch asks for permission — if you dismissed it, re-add the rule from the PC window (`Firewall` button) or run `desktop/放行防火墙.bat`.
5. **Wrong address.** A PC with several adapters (Ethernet + Wi-Fi + virtual adapters from Docker/VMware) shows several addresses. Use the one starting with `192.168.`, `10.` or `172.16–31.` that matches the phone's subnet. The PC window labels them.

> **中文**：按顺序排查——① 是否同一个 Wi-Fi（访客网络是隔离的）；② 路由器是否开了 AP 隔离（公共热点几乎都开，现象是地址一直转圈）；③ 任一端开了 VPN 会把局域网流量带走；④ Windows 防火墙首次弹窗被关掉了，可从电脑窗口重新放行；⑤ 电脑有多张网卡（有线/无线/Docker 虚拟网卡）时会出现多个地址，要选和手机同网段的那个。

## "Pairing code incorrect"

The code is shown in the PC window and rotates when the server restarts. If you restarted the PC app, re-read the code. Five wrong attempts trigger a temporary lock (longer each time).

> **中文**：配对码显示在电脑窗口里，服务重启会重新生成；连续输错 5 次会临时锁定，且每次锁定时间递增。

## Downloads from the phone do nothing

On first use Android asks where to save. If you dismissed that picker, the next download asks again. If nothing at all happens, check that the app has the "All files access" permission — the Android 11+ setting is under *Settings → Apps → DropLink → Special access*.

> **中文**：安卓第一次下载会让你选保存目录，取消了就下次再问；如果完全没反应，检查「所有文件访问」权限是否给了（设置 → 应用 → DropLink → 特殊权限）。

## Upload leaves a `.part` file

A `.part` file means the transfer was interrupted. Both sides delete stale `.part` files older than 24 hours on startup; anything newer is kept so a resume is still possible. Just re-send the file.

> **中文**：出现 `.part` 说明传输中断了。两端都会在启动时清理超过 24 小时的残留，较新的会保留以便续传；直接重传即可。

## The page is blank / stuck on a spinner

- Hard-refresh the browser (Ctrl+F5) — the UI is cached aggressively.
- On Android, swipe the app away and reopen; if it persists, reinstall (the APK and the desktop share the same UI version, so a mismatch is unusual).
- If you use `--secure`, a token mismatch also produces an empty page; reopen from the PC window instead of a bookmarked URL.

> **中文**：浏览器强刷一次（Ctrl+F5）；安卓把应用划掉重开；开了 `--secure` 时用旧书签打开会因为没有令牌而空白，从电脑窗口重新打开即可。

## Port 8848 is already in use

DropLink increments the port automatically and shows the actual number in the window. To force one: `DropLink.exe --port 9000`.

> **中文**：端口被占用时程序会自动往后找可用端口，窗口里显示的是真实端口；也可以用 `--port` 指定。

## Antivirus flags the EXE

The executable is a self-extracting Python bundle without a code signature, which heuristic scanners dislike. Verify the SHA-256 published with each release, or build it yourself from source.

> **中文**：EXE 是未签名的自解压 Python 包，容易被启发式引擎误报。用发布页的 SHA-256 校验，或者直接从源码自己编译。

## Still stuck

Open an issue with: Windows/Android version, the addresses shown in the PC window, and what the phone displays. The PC window has a `Diagnostics` panel that copies the relevant details.

> **中文**：还不行就提 issue，附上系统版本、电脑窗口里显示的地址、手机端看到的内容；电脑窗口的「诊断」面板可以一键复制这些信息。
