# DropLink 互传

电脑 ⇄ 手机，同一个 Wi-Fi 内直接互传文件。不经过互联网、不上传云端、不消耗手机流量。

- **电脑端**：单文件 `DropLink.exe`（内嵌 Python 运行时，双击即用），另附完整源码。
- **手机端**：原生 Android App（Kotlin + WebView UI），也支持手机浏览器直接打开电脑地址。

## 功能

| 端 | 功能 |
|---|---|
| 电脑 | 启动即显示二维码 / 局域网地址 / 6 位配对码；托盘常驻；可换端口、改接收目录、诊断防火墙 |
| 手机 | 浏览电脑文件（含各磁盘）、在线预览、下载、上传文件 / 照片 / 整个文件夹 |
| 双向 | 传输中心实时速率与进度、剪贴板互发、批量打包下载、电脑推送文件到手机 |
| 其他 | 深浅主题、PWA「添加到主屏幕」、零依赖二维码、断点续传、10 GB 大文件实测 |

## 目录结构

```
desktop/                电脑端（Python 3，仅标准库）
├─ server.py            HTTP 服务 + 文件/剪贴板/推送 API
├─ app_main.py          EXE 入口（起服务 + 托盘 + 打开界面）
├─ gui.py               原生窗口 / 托盘（ctypes 调 Win32）
├─ qrmini.py            零依赖二维码与图标生成
├─ web/                 网页界面（HTML/CSS/JS，无外部依赖）
└─ build/               打包脚本、自检脚本、启动器源码

android/                手机端（Kotlin，Gradle 8.7，minSdk 26）
└─ app/src/main/        HttpServer / WebView / 扫码 / 热点

tools/gen_launcher_icons.py   生成安卓启动图标（仓库不存二进制）
```

## 快速开始

### 电脑端（源码运行）

```bash
cd desktop
python server.py            # 需要 Python 3.9+
```

浏览器打开 `http://127.0.0.1:8848`，手机连同一 Wi-Fi 后扫窗口里的二维码即可。

### 电脑端（打包 EXE）

需要 MinGW-w64（`g++`、`windres`）与 Python 3：

```bash
cd desktop
python build/build_exe.py -o DropLink.exe
```

### 手机端

仓库不包含二进制图标，构建前先生成（与电脑端共用同一套图标算法）：

```bash
python tools/gen_launcher_icons.py
```

然后用 Android Studio 打开 `android/`，或命令行：

```bash
cd android
gradle assembleRelease
```

签名通过环境变量注入，仓库不含密钥：

```
DROPLINK_KEYSTORE / DROPLINK_STORE_PASSWORD / DROPLINK_KEY_ALIAS / DROPLINK_KEY_PASSWORD
```

## 自检

`desktop/build/` 下有五套脚本，测试实例建议用固定配对码启动：

```bash
python server.py --port 8899 --pin 12345678 --no-browser
python build/e2e_exe.py 8899            # 功能回归
python build/security_check.py 8899     # 越权/穿越/暴力破解
python build/folder_check.py 8899       # 文件夹传输
python build/bulk_check.py 8899         # 10 GB / 海量小文件
python build/hardening_check.py 8899    # 加固专项（需 --secure 模式）
```

接收目录可用 `DROPLINK_RECV` 指定，默认是 `~/Desktop/DropLink 接收`。

## 安全模式

默认是**个人使用模式**：本机免配对、可浏览整机、6 位配对码，方便自用。

如果要在不可信网络里使用，启动时加 `--secure`（或设 `DROPLINK_SECURE=1`），会启用：

- Host / Origin / Sec-Fetch 校验（阻断 DNS 重绑定与 CSRF）
- 本机界面一次性令牌
- 屏蔽系统目录（Windows / Program Files / AppData / 启动项）
- 拒绝 UNC、NTFS 数据流、目录联接穿越
- 8 位配对码、会话过期、上传可执行文件打 MOTW

## 说明

本项目按「够用就好」的原则只依赖 Python 标准库与 AndroidX/CameraX，没有云端组件。
未指定开源许可证，如需商用或二次分发请先联系作者。
