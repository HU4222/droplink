# Security model

DropLink is a **LAN convenience tool**, not a hardened internet service. This document states exactly what it protects against, what it does not, and how to tighten it.

## Two profiles

The same binary ships two behaviours. The default is chosen for everyday personal use; `--secure` is for shared or untrusted networks.

| Behaviour | Default (personal) | `--secure` |
|---|---|---|
| Local UI | Opens without a pairing code | Requires a one-time token injected into the URL |
| Host header | Not validated | Whitelist of loopback + local interface IPs, otherwise `421` |
| `Origin` / `Sec-Fetch-Site` | Not validated | Cross-site requests `403` |
| Browser reach | Any folder on the machine | Windows, Program Files, AppData, `.ssh`, Startup blocked |
| `\\server\share`, `file:stream` | Allowed | Rejected (`400`) |
| Pairing code | 6 digits | 8 digits, exponential back-off on failures |
| Session lifetime | Until restart | 30 days, revoked on logout |
| Uploaded executables | No mark | Marked with `Zone.Identifier` (MOTW) |
| Response headers | Basic | CSP, `X-Frame-Options: DENY`, `Permissions-Policy` |

Start it with:

```bat
DropLink.exe --secure
:: or
set DROPLINK_SECURE=1 && DropLink.exe
```

> **中文**：同一个程序有两套行为。默认「个人模式」面向自己用：本机界面免配对、可以浏览整机、6 位配对码、局域网明文 HTTP。加 `--secure` 后开启加固模式：本机界面需要一次性令牌、校验 Host/Origin、屏蔽敏感目录、拒绝 UNC 与 NTFS 数据流、8 位配对码且失败退避、会话 30 天过期、可执行文件打「来自网络」标记、加安全响应头。

## What is protected

- **Other devices on the LAN** cannot read files without the pairing code.
- **Path traversal** is contained: uploaded and written paths are resolved and checked against the receive folder, and reparse points (junctions/symlinks) that escape it are rejected in both profiles.
- **Concurrent uploads** with the same name never overwrite each other — the final name is claimed atomically.
- **Deleting** always goes through the Recycle Bin; deleting the receive folder itself is refused.
- **Browser cross-site attacks** (CSRF, DNS rebinding) are blocked in `--secure`; in personal mode the local trust boundary is intentionally open.

> **中文**：局域网内的其它设备没有配对码读不到文件；路径越界（包括目录联接/符号链接）在两套模式下都会被拦；同名并发上传不会互相覆盖；删除一律走回收站且禁止删接收目录本身；`--secure` 下会阻断 CSRF 与 DNS 重绑定。

## What is NOT protected

- **Plaintext HTTP.** Anyone able to sniff the LAN (open Wi-Fi, ARP spoofing) can read traffic, including the pairing code. Use a trusted network or your phone's hotspot. TLS is not implemented.
- **A compromised PC.** The tool runs with your user rights; it cannot protect files your account can already read.
- **Malicious files.** DropLink moves bytes; it does not scan them. Executables received from someone else are still executables.
- **The pairing code is short.** Six digits is ~10⁶ combinations; the failure back-off slows brute force but does not make it cryptographic. Use `--secure` (8 digits) on shared networks.

> **中文**：不防护的部分要说清楚——全程明文 HTTP，能嗅探局域网的人可以读到配对码和文件内容（**未实现 TLS**）；电脑本身被攻破时无能为力；不查毒，传过来的 exe 依然是 exe；6 位配对码强度有限，共享网络请用 `--secure` 的 8 位。

## Recommended setup

1. Home network you control → default mode is fine.
2. Office / shared Wi-Fi → `--secure`, and prefer the phone hotspot.
3. Café / hotel / airport → do not use; tether from the phone instead.
4. Stop the server when done — closing the window kills the listener immediately.

> **中文**：家里自己可控的网络用默认模式即可；办公室或共享 Wi-Fi 建议开 `--secure`，或者干脆用手机热点；咖啡厅、酒店、机场这类网络不要用；用完关掉窗口，服务立刻停止监听。

## Reporting

Found a hole? Open a private security advisory on GitHub or contact the maintainer before filing a public issue.

> **中文**：发现安全问题请先走私密渠道（GitHub Security Advisory 或直接联系维护者），确认修复后再公开。
