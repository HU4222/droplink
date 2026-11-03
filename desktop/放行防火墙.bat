@echo off
chcp 65001 >nul
title DropLink - 放行防火墙
net session >nul 2>&1
if errorlevel 1 (
  echo.
  echo   请右键点击本文件，选择「以管理员身份运行」。
  echo.
  pause
  exit /b 1
)

echo.
echo   正在为 DropLink 放行防火墙（端口 + 程序，覆盖专用/公用网络）...
echo.

netsh advfirewall firewall delete rule name="DropLink Port" >nul 2>&1
netsh advfirewall firewall add rule name="DropLink Port" dir=in action=allow protocol=TCP localport=8848-8867 profile=any

netsh advfirewall firewall delete rule name="DropLink Program" >nul 2>&1
netsh advfirewall firewall add rule name="DropLink Program" dir=in action=allow program="%LOCALAPPDATA%\DropLink\runtime\pythonw.exe" profile=any enable=yes

echo.
echo   完成。手机连同一个 Wi-Fi 后，打开地址就能连上了。
echo   若仍连不上，请参考「使用说明.md」里的常见问题。
echo.
pause
