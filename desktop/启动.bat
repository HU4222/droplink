@echo off
chcp 65001 >nul
title DropLink 互传
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "PYEXE="
where python >nul 2>nul
if not errorlevel 1 set "PYEXE=python"
if not defined PYEXE (
  where py >nul 2>nul
  if not errorlevel 1 set "PYEXE=py -3"
)
if not defined PYEXE (
  echo.
  echo   没有找到 Python，请先安装 Python 3.9 及以上版本：
  echo   https://www.python.org/downloads/
  echo   安装时记得勾选 "Add Python to PATH"。
  echo.
  pause
  exit /b 1
)
%PYEXE% server.py %*
if errorlevel 1 (
  echo.
  echo   服务已退出，错误码 %errorlevel%。
  pause
)
