# -*- coding: utf-8 -*-
"""生成 10 GiB 测速文件（真实写入，非稀疏文件）。"""
import hashlib
import os
import time

TARGET = os.environ.get("DROPLINK_TARGET") or os.path.join(
    os.path.expanduser("~"), "Desktop", "10GB-测速文件.bin")
SIZE = 10 * (1 << 30)          # 10 GiB = 10,737,418,240 B
BLOCK = 8 << 20                # 8 MiB

if os.path.exists(TARGET):
    print("目标已存在，先删除：", TARGET)
    os.remove(TARGET)

buf = os.urandom(BLOCK)        # 不可压缩的随机内容，避免任何压缩/去重优化影响测速
written = 0
t0 = time.time()
last = 0
h = hashlib.sha256()

with open(TARGET, "wb", buffering=0) as f:
    while written < SIZE:
        n = min(BLOCK, SIZE - written)
        chunk = buf if n == BLOCK else buf[:n]
        f.write(chunk)
        h.update(chunk)
        written += n
        if written - last >= (1 << 30):
            last = written
            el = max(0.001, time.time() - t0)
            print("  %.1f / 10.0 GiB   %.0f MB/s" % (written / (1 << 30), written / el / 1e6), flush=True)
    f.flush()
    os.fsync(f.fileno())

el = max(0.001, time.time() - t0)
size = os.path.getsize(TARGET)
print()
print("完成：", TARGET)
print("大小：%d 字节（%.2f GiB）" % (size, size / (1 << 30)))
print("写入：%.1f 秒，平均 %.0f MB/s" % (el, size / el / 1e6))
print("SHA-256：", h.hexdigest())
