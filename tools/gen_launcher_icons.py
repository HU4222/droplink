# -*- coding: utf-8 -*-
"""生成安卓启动图标（mipmap-*/ic_launcher.png）。

图标与电脑端 EXE、网页 favicon 完全一致：都来自 desktop/qrmini.py 的
icon_png_bytes()（紫色圆角底 + 左白下箭头 + 右白上箭头）。
仓库不存二进制 PNG，构建安卓前跑一次本脚本即可。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "desktop"))
import qrmini  # noqa: E402

RES = os.path.join(ROOT, "android", "app", "src", "main", "res")
SIZES = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}

for bucket, size in SIZES.items():
    folder = os.path.join(RES, "mipmap-" + bucket)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "ic_launcher.png")
    with open(path, "wb") as f:
        f.write(qrmini.icon_png_bytes(size))
    print("写入", path, size, "px")
