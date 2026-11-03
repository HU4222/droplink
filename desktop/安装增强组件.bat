@echo off
chcp 65001 >nul
title DropLink - 可选增强（图片缩略图）
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo.
echo   二维码已经内置，本步骤只用于开启「图片缩略图」。
echo   装不上也完全不影响正常使用，可以直接关掉这个窗口。
echo.
echo   正在安装 pillow 到本地 vendor 目录（约 3 MB）...
echo.

python -m pip install --upgrade --target "%~dp0vendor" pillow
if errorlevel 1 (
  echo.
  echo   安装失败，可能是网络不通。可改用国内镜像重试：
  echo   python -m pip install --upgrade --target "%~dp0vendor" pillow -i https://pypi.tuna.tsinghua.edu.cn/simple
  echo.
)
pause
