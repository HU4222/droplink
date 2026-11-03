# Build & package

Everything here is reproducible from a clean clone. No private keys or passwords are stored in the repository.

## Desktop (Windows)

### Requirements

- Python 3.9+ (3.13 recommended) on Windows 10/11.
- No `pip install` needed at runtime — the desktop side uses only the standard library. Pillow is optional and only enables JPEG thumbnails.

### Run from source

```bat
cd desktop
python server.py                  :: default port 8848, opens the UI
python server.py --port 9000 --recv "D:\Downloads" --no-browser
```

### Build the single-file EXE

```bat
cd desktop
python build/build_exe.py -o dist\DropLink.exe
```

The builder embeds the interpreter, `server.py`, `gui.py`, `qrmini.py` and `web/` into one self-extracting executable and stamps the icon from `qrmini.icon_png_bytes()`. Output is ~14 MB; nothing is written to the registry.

> **中文**：电脑端只依赖 Python 标准库（Pillow 仅用于生成 JPEG 缩略图，可缺省）。`python server.py` 直接跑源码；`python build/build_exe.py` 打成单文件 EXE，内嵌解释器和网页资源，约 14 MB，不写注册表。

## Android

### Requirements

- JDK 17
- Android SDK with `compileSdk 34` and build-tools 34
- The Gradle wrapper is included, so no separate Gradle install is needed

### Debug build

```bash
cd android
./gradlew assembleDebug
# app/build/outputs/apk/debug/app-debug.apk
```

### Release build

Signing values come from environment variables so no keystore ever lands in git:

```bash
export DROPLINK_KEYSTORE=/path/to/your.keystore
export DROPLINK_STORE_PASSWORD=…
export DROPLINK_KEY_ALIAS=droplink
export DROPLINK_KEY_PASSWORD=…

cd android
./gradlew assembleRelease
```

If `DROPLINK_KEYSTORE` is unset or missing, the release build falls back to the debug signing key — convenient for local testing, **not** acceptable for store distribution.

### Regenerate the launcher icons

```bash
python tools/gen_launcher_icons.py --out android/app/src/main/res
```

The script draws the same rounded-square icon used by the desktop EXE and the web favicon, at every mipmap density.

> **中文**：安卓端需要 JDK 17 + Android SDK（compileSdk 34），仓库自带 Gradle Wrapper，不用另装 Gradle。调试包 `./gradlew assembleDebug`；发布包通过四个环境变量注入签名，没配就退回 debug 签名（仅适合本地测试）。启动图标可以用 `tools/gen_launcher_icons.py` 重新生成，和电脑端 EXE、网页 favicon 是同一套画法。

## Web assets

`desktop/web/` is plain HTML/CSS/JS — no bundler, no npm. After editing, copy the three files into the Android app so both platforms stay in sync:

```bash
cp desktop/web/index.html desktop/web/app.js desktop/web/styles.css \
   android/app/src/main/assets/www/
```

> **中文**：网页界面没有构建步骤，改完把三个文件拷进安卓的 `assets/www/` 就完成两端同步。

## Self-test suites

`desktop/build/` contains the regression scripts used during development. They start against a running instance:

```bash
python build/e2e_exe.py       8890   # functional round-trip (22 checks)
python build/folder_check.py  8890   # folder upload / ZIP / abort handling (11)
python build/bulk_check.py    8890   # 10 GB ranges + 256 small files (12)
python build/security_check.py 8890  # runs against a --secure instance (54)
python build/hardening_check.py 8890 # runs against a --secure instance (38)
node  build/device_smoke.js         # Android UI smoke via WebView devtools
```

Set `DROPLINK_RECV` to the receive folder you want the tests to use. The Android smoke test needs a debug build (WebView debugging enabled) and `adb forward tcp:9333 localabstract:webview_devtools_remote_<pid>`.

> **中文**：`desktop/build/` 里是开发期用的回归脚本：功能 22 项、文件夹 11 项、大文件 12 项、安全 54 项、加固 38 项，以及一个通过 WebView 调试端口驱动的安卓 UI 冒烟脚本。跑之前用 `DROPLINK_RECV` 指定接收目录；安卓冒烟需要 debug 包并做一次 `adb forward`。
