# DropLink 分版说明书（个人版 / 安全版）

> 状态：1.5.0，2026-11-05。
> 本文件是**对外说明书**，按当前实现更新；`EDITIONS-PLAN.md` 是最初的内部方案基线（不是发布文档）。
> 已落地：TLS、证书指纹、设备信任与首次配对审批、传输前确认与逐文件一次性 token、
> 授权语义（S-05）、写入侧路径加强（S-06）、分接口限流（S-07）、日志脱敏（S-10）、
> 配额与残留清理（S-11）、Web 面加固（S-12/S-13）、安全默认值（S-14）。
> 未落地：代码签名与 SBOM（S-15 的部分），见第五、九节与 `SECURITY-CHECKLIST.md`。

---

## 一、怎么选

| | 个人版 `DropLink.exe` | 安全版 `DropLink-Secure.exe` |
| --- | --- | --- |
| 传输 | 明文 HTTP（仅局域网） | TLS（自签证书，端口不变 8848） |
| 首次连接 | 输入配对码即进 | 输入配对码 → 电脑本机点「批准」才发会话 |
| 配对码 | 6 位 | 8 位 |
| 适合 | 自己家、自己的设备之间，图快 | 办公室 / 公共 Wi-Fi / 多人共用网络 |

两版**必须同族**：混装的表现是「连不上」或「证书/协议对不上」，这是刻意的，不是 bug。

---

## 二、代码怎么分

一条代码线，构建期分版（`--edition personal|secure`），不做两条 git 分支。

```
dlcore/            共用：config / state / fsutil / winenv / media / api / httpd
dlcore/sec/        只随安全版打包（个人版 payload 里不存在）
  certgen.py       S-01 纯标准库 ECDSA P-256 自签证书
  tls.py           S-01 服务端 SSLContext / 监听 socket 包装
  trust.py         S-02 设备登记 + 首次配对审批 + 信任列表
  approval.py      S-03/S-04 传输前确认 + 逐文件一次性 token
  tokens.py        S-04 逐文件 token（已并入 approval.py，保留占位文件）
  pathguard.py     S-06 写入侧路径约束（逐段体检 / 保留名 / ADS / 链接 / 越界）
```

三条硬纪律：

1. `dlcore/sec/` 只能由 `server.py` 入口和 `httpd.py` 的 edition 分支按需 import，
   共用模块不得依赖它。
2. 安全版专属路由在个人版里**不注册**（404），不暴露「这里本来有个接口」。
3. 协议差异只由 `config.EDITION` / `is_secure_edition()` 和 `Server.scheme` 决定，
   前端通过 `/api/session` 的 `scheme` 与 `window.__EDITION__` 取。

个人版 payload：不含 `dlcore/sec/`、不含 `_ssl.pyd`/OpenSSL，`import ssl` 直接失败。
安全版 payload：`sec/` 七个文件 + `_ssl.pyd` + `libssl-3-x64.dll` + `libcrypto-3-x64.dll`。

---

## 三、安全版已实现

### 1. TLS（S-01）

- 证书：ECDSA P-256（secp256r1），X.509 v3，自签，10 年，随机序列号；
  `CA:FALSE`、`keyUsage=digitalSignature`、`EKU=serverAuth`；
  SAN = `localhost` + `127.0.0.1` + 本机所有局域网 IPv4。
- 生成：纯 Python（无 `cryptography`、不依赖系统 openssl）；首次约 0.1 秒。
- 复用/重建：证书与私钥、SAN、有效期都校验；文件被改、私钥丢失、IP 变化、过期都会重建。
- 服务：`TLS 1.2` 起，TLS 1.2 只留 ECDSA + AEAD 套件；监听 `https://IP:8848`。
- 私钥权限：Windows 用 `icacls` 只授权当前用户 SID（失败只记日志，不影响使用）。
- 证书目录：`%LOCALAPPDATA%\DropLink\secure\certs`；便携模式随 `DROPLINK_DATA_DIR`。

### 2. 证书指纹（只读）

- `GET /api/device` → 完整指纹（SHA-256，冒号分组）、短指纹（`A1B2-C3D4`）、
  `not_after`、`sans`；启动横幅也打印短指纹。
- 指纹只供**肉眼核对**。浏览器 / WebView 拿不到对端证书 DER，做不了 pinning；
  安卓原生层可行但当前安卓端是 WebView，成本远大于收益，因此不做。

### 3. 设备信任与首次配对审批（S-02 落地版）

为什么不是「客户端固定指纹」：客户端是浏览器，拿不到证书 DER。
改成对 DropLink 真正有效的三件事：设备登记、首次配对挂起、信任列表可撤销。

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/api/login` | POST | 安全版要求 `device_id`；已信任 → 200 发会话；新设备 → **202 pending** |
| `/api/login/status` | GET | pending 202 / approved 200 + 会话 Cookie / rejected 403 / 过期 200 `{"status":"expired"}` |
| `/api/trust` | GET | 已信任设备列表（仅本机） |
| `/api/trust/pending` | GET | 待批准队列（仅本机） |
| `/api/trust/approve` | POST | 批准并登记（仅本机） |
| `/api/trust/reject` | POST | 拒绝（仅本机） |
| `/api/trust/revoke` | POST | 撤销并立即断开该设备所有会话（仅本机） |

- `device_id`：客户端首次访问生成的 UUID，存 localStorage。
- 待批准队列在内存，5 分钟未处理自动作废（过期返回 `state: "expired"`）；信任表落盘 `config/trusted_devices.json`。
- 审批类接口只认 `127.0.0.1`（外加 Host/Origin/Sec-Fetch 校验，防恶意网页跨站代点）。
- 撤销后该设备的会话立即失效，下一次请求 401，重新登录回到待批准。

### 4. 传输前确认 + 逐文件一次性 token（S-03 / S-04）

安全版上传分两阶段，**接受之前不接收任何字节**（接收目录里连 `.part` 都不会出现）：

| 接口 | 方法 | 谁可以调 | 说明 |
| --- | --- | --- | --- |
| `/api/prepare-upload` | POST | 已配对设备 | 只提交元数据（文件列表/大小），返回 `pending` 或（自动接受时）`accepted` + token |
| `/api/upload/status` | GET | 发起设备 | 轮询自己那次申请；`accepted` 时下发逐文件 token |
| `/api/approve/pending` | GET | 仅本机 | 待确认传输列表（设备、文件、总大小、来源 IP、剩余时间） |
| `/api/approve` | POST | 仅本机 | `{session_id, action:"accept"\|"reject", file_ids?}` |
| `/api/approve/cancel` | POST | 本机或发起设备 | 取消这次传输 |
| `/api/trust/auto-accept` | POST | 仅本机 | 按设备开关「自动接受」，落盘 |

- 接受后每个文件签发一个一次性 `token`，绑定 `session_id + file_id`；
  上传时必须带 `?session_id=&file_id=&token=`，跨文件、重放、过期一律 403。
- 上传字节数必须与申请时声明一致，不一致直接断开并记日志。
- 待确认 5 分钟过期；接受后给 30 分钟上传窗口。
- 自动接受默认关；开启后只对**这台已信任设备**生效。

---

## 四、还没做

| 条目 | 现状 |
| --- | --- |
| S-07 分接口限流（接口已留 429 `RATE_LIMITED` + `Retry-After`，真正限流未做） | 未做 |
| S-10 全量日志脱敏、S-15 发布完整性等 | 未做 |

---

## 五、默认值与失败行为

- 安全版：`SECURE_MODE=True`（不可关）、TLS 不可关、8 位配对码、本机审批、传文件前本机确认。
- 个人版：明文、6 位配对码、本机免登录、上传不弹确认（与 1.4.x 一致）。
- **失败即关闭**：安全版拿不到证书 → 拒绝启动；`dlcore/httpd.py` 损坏 → 拒绝启动
  （不会退回明文诊断服务，退出码 2）；个人版仍可降级启动诊断页。

---

## 六、构建与产物

```bash
python build/build_exe.py --edition personal -o dist/DropLink.exe
python build/build_exe.py --edition secure   -o dist/DropLink-Secure.exe
```

发布基线是仓库根目录的 **`VERSION`** 文件（一行版本号，如 `1.5.0`）。构建会先做三条断言：
`config.VERSION` 必须等于 `VERSION`，且 `README.md`、本文件都必须出现同一版本号；
任一不一致 → 构建直接失败（退出码非 0）。构建成功后自动写 `dist/SHA256SUMS.txt` 并自校验。

| | 个人版 | 安全版 |
| --- | --- | --- |
| 体积 | ≈14.9 MB | ≈24.8 MB（OpenSSL 运行时约 9.8 MB） |
| 运行时目录 | `%LOCALAPPDATA%\DropLink\runtime-personal` | `…\runtime-secure` |
| 单实例互斥体 | `DropLinkSingleton_Personal_1` | `DropLinkSingleton_Secure_1` |
| 旧 `runtime` 目录 | 首次启动迁移到 `runtime-personal` 并清掉残留 `sec/` | 不迁移，重新释放 |

---

## 七、环境变量（便携 / 测试用）

| 变量 | 作用 |
| --- | --- |
| `DROPLINK_EDITION` | 源码运行时的版本身份（构建产物以烤入值为准） |
| `DROPLINK_DATA_DIR` | 数据根（默认 `%LOCALAPPDATA%\DropLink`），证书目录随它走 |
| `DROPLINK_CERT_DIR` | 直接指定证书目录 |
| `DROPLINK_TRUST_FILE` | 直接指定信任表路径 |
| `DROPLINK_APPROVAL_TTL` | 待批准超时秒数（默认 300） |
| `DROPLINK_TRANSFER_TTL` | 待确认传输超时秒数（默认 300） |
| `DROPLINK_UPLOAD_TTL` | 接受后上传窗口秒数（默认 1800） |
| `DROPLINK_RUNTIME_DIR` | 直接指定运行时释放目录 |
| `DROPLINK_SECURE` | 个人版打开加固开关（安全版始终开） |

---

## 八、状态码与错误码（仅安全版）

安全版的失败响应统一为：

```json
{"ok": false, "error": "中文说明", "code": "AUTH_REQUIRED"}
```

`error` 保持原文（前端直接显示），`code` 供客户端分支。**个人版没有 `code` 字段**，
状态码也保持 1.4.x 语义（如配对码错仍是 403），方便老客户端继续用。

| 场景 | 状态码 | `code` |
| --- | --- | --- |
| 没带配对码 / 会话无效 | 401 | `AUTH_REQUIRED` |
| PIN 错 | 401 | `AUTH_BAD_PIN` |
| 非本机调本机专属接口 | 403 | `LOCAL_ONLY` |
| 已登录但无权访问该会话 | 403 | `FORBIDDEN` |
| 被接收方拒绝后继续上传 | 403 | `TRANSFER_REJECTED` |
| 接口不存在 | 404 | `NO_ROUTE` |
| 会话/申请从来没有过（含墓碑后被清理） | 404 | `NOT_FOUND` |
| 会话曾经存在但已过期/已取消 | 410 | `GONE` |
| 状态冲突（对已接受的会话再 approve、重复 cancel） | 409 | `STATE_CONFLICT` |
| token 跨文件/重放/伪造 | 403 | `TOKEN_INVALID` |
| `Content-Length` 与申请尺寸不符 | 422 | `SIZE_MISMATCH` |
| sha256 与声明不符（申请时带了 sha） | 422 | `CHECKSUM_MISMATCH` |
| 文件名非法（保留名/ADS/越界/不可见字符） | 422 | `INVALID_NAME` |
| 磁盘不足 | 507 | `DISK_FULL` |
| 限流（本轮只留接口，S-07 才真正限） | 429 + `Retry-After` | `RATE_LIMITED` |

补充约定：

- 尺寸不符时，服务端**一个字节都没读**，会换发一枚新 token 放进 422 响应体
  （`file_id` + `token`），旧 token 立即作废；断线/校验不符这类「已写了一半」的失败不还 token。
- 排队中的上传会话如果还有文件在写，不会被清理（墓碑清理只删完全空闲的过期会话）。

---

## 九、限流、日志脱敏与配额（S-07 / S-10 / S-11）

### 9.1 分接口限流（仅安全版）

令牌桶，维度 `(接口类别, 客户端 IP)`。默认值（**突发 = 速率**，保证「打满就限」）：

| 类别 | 速率 | 突发 | 环境变量 |
| --- | --- | --- | --- |
| list / search | 50 次/秒 | 50 | `DROPLINK_RL_LIST` |
| preview / thumb | 30 次/秒 | 30 | `DROPLINK_RL_PREVIEW` |
| upload | 20 次/秒 | 20 | `DROPLINK_RL_UPLOAD` |
| login | 沿用 5 次失败后的 60/120/240 秒退避（上限 300） | — | — |

- 环境变量格式 `速率,突发`，例如 `DROPLINK_RL_LIST=50,100`。
- 超限：`429` + `code="RATE_LIMITED"` + `Retry-After: <向上取整秒数>`。
- **本机回环（127.0.0.1 / ::1）不做 QPS 限流**，但登录退避照旧。
- 同一上传会话同时最多 2 个文件在写；第三个直接 429，不排队。
- 表有界：每 5 分钟清一次 10 分钟未访问的 key，最多 4096 个 key；淘汰不等于放行。

### 9.2 日志脱敏

- 路径：只留 `…/文件名#文件名sha256前8位`（哈希的是文件名，不读文件内容）。
- 每行事件日志带本地时间前缀 `HH:MM:SS `（启动横幅不带）。
- IP：局域网 IP 打码（`192.168.1.*`、IPv6 前 4 组 + `::*`）；`127.0.0.1` / `::1` 原样保留。
- PIN / token：显示为 `***`（`--selfcheck` 与日志都不出现）。
- 异常文本：`safe_exc()` 把绝对路径替换成同样的脱敏形式。
- 启动横幅在终端里显示完整信息；输出被重定向到日志/管道时自动脱敏（PIN、接收目录、局域网地址）。

### 9.3 配额与残留清理

- 配额：`DROPLINK_QUOTA_GB`（默认 0 = 不限；测试可用 `DROPLINK_QUOTA_MB`）。
  接收目录已用空间 + 本次上传 > 配额 → `507 DISK_FULL`，文案区分「配额已满」与「磁盘空间不足」。
- `.part` 清理：启动扫一次 + 每小时一次；只清理本程序生成的 `.dl_<8位小写hex>.part`
  （严格正则 `.dl_[0-9a-f]{8}\.part`），用户自己的 `.part` 文件不动；超过 24 小时才删，删了记一行脱敏日志。
- 上传中断（客户端断开）：立即删 `.part`、文件状态置 `failed`、token 不复活。
- 任何失败（507/400/断线）都不会留下 0 字节文件。

---

## 十、Web 面加固（S-12 / S-13）

### 10.1 恶意输入（S-12）

- **目录深度**：安全版上传路径最多 32 段（不含文件名本身），超过 → `422 INVALID_NAME`「目录层级过深（>32）」。
  个人版保持 1.4.x 行为，不设上限。
- **可执行内容强制下载**（仅安全版）：接收目录里命中
  `html / htm / xhtml / svg / svgz / mht / mhtml / xml / xsl` 的文件，下载响应固定
  `Content-Disposition: attachment` + `Content-Type: application/octet-stream`，
  不允许在接收端源上以 `text/html` 打开。个人版保持既有行为（HTML/SVG 仍是附件，但类型按扩展名）。
- **不解压**：本产品只「打包下载」多个文件，**没有**解压/展开用户压缩包的逻辑，
  因此不存在 zip 炸弹路径；上传的 `.zip` 只当普通文件保存。
- **MIME 一致性**：所有下载响应带 `X-Content-Type-Options: nosniff`；预览接口只回
  `application/json` 的 `{"text": ...}`（最多 256 KB），不回 HTML。

### 10.2 会话与请求来源（S-13）

- **Cookie**：安全版 `Set-Cookie` 追加 `; Secure`（TLS 全链路）；个人版不加（HTTP 下加了会失效）。
- **Origin / Sec-Fetch**：
  - 安全版：所有 `POST` 与 `GET` 都校验，失败 `403 BAD_ORIGIN`（个人版仍为 `403` 无 code）。
  - 个人版：只对会改文件系统的 `POST`（upload / delete / mkdir / rename / clip / clip/delete）校验；
    `GET` 与 `/api/login` 保持宽松。
  - **缺头一律放行**（老 Android WebView 不发 `Sec-Fetch-*`）。
- **Host**：两版都校验，非本机名/回环/本机网卡 IP → `421`。安全版在 Host 与证书 SAN
  不一致时额外记一行脱敏日志（不拒绝，避免误伤局域网 IP 访问）。
- **响应头**：两版都有 `X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、
  `X-Frame-Options: DENY`；安全版另有 `Permissions-Policy` 与严格 `CSP`。
- **错误回包排空上限**：关连接前的请求体排空最多 1 MB / 1 秒，慢速大包不会长期占住线程；
  正常客户端（错误回包时 body 很小）不受影响。

---

## 十一、安全默认值（S-14）

| 项 | 个人版 | 安全版 |
| --- | --- | --- |
| 传输加密 | 无（HTTP） | TLS 1.3（不可关） |
| 配对码 | 6 位 | 8 位 |
| 设备审批 | 无 | 有（首次连接必须本机批准） |
| 传输前确认 | 无（直传） | 有（默认开） |
| 自动接受 | 不适用 | 默认关 |
| 分接口限流 | 无 | 有（见 9.1） |
| 配额 | 不限 | 默认不限（可配） |
| 日志脱敏 | 有 | 有 |

安全版首次打开 Web 首页时，顶部出现一条**一次性**说明「本机已启用：TLS 加密 · 传输前确认 · 设备审批」，
点「知道了」后写入 `localStorage`（键名带版本号，升级后会再出现一次）。它只是一条提示，
不是可以关闭安全功能的开关；个人版不出现。

---

## 十二、边界与已知限制

1. **两版不互通**：安全版要求 TLS，个人版客户端连不上是预期行为。
2. **自签证书必然弹警告**：浏览器首次打开会提示证书不受信任，用启动横幅的短指纹核对后继续。
3. **`device_id` 不是密码学身份**：它由客户端生成、可伪造。真正的门槛是「配对码 + 本机人工批准」；
   要做到不可伪造，需要给设备发客户端证书（原生端才做得到）。
4. **安全版降级 = 拒绝启动**：这是刻意选择；模块损坏时请重装，而不是期待它继续跑。
5. **安全版 UI 的浏览器端交互**未做自动化测试，接口与页面元素已按下发内容校验。
