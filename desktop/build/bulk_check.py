# -*- coding: utf-8 -*-
"""大文件 / 海量小文件传输自检。

1) 10 GB 单文件：整流下载 + Range 抽检（首/中/尾）；
2) 1 MB 总量、4 KB 小文件 ×256 的文件夹：打包下载、逐文件上传/下载。
用法：python build/bulk_check.py [port] [--keep]
"""
import http.client
import json
import os
import shutil
import sys
import urllib.parse
import time

PORT = 8848
KEEP = "--keep" in sys.argv
for a in sys.argv[1:]:
    if a.isdigit():
        PORT = int(a)

BIG_GB = 10
SMALL_N = 256          # 256 × 4KB ≈ 1MB
SMALL_SIZE = 4096
PIN = os.environ.get("DROPLINK_PIN", "12345678")
COOKIE = ""
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   <- " + detail) if detail else ""))


def call(method, path, body=None, headers=None, timeout=600):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
    h = dict(headers or {})
    if COOKIE:
        h.setdefault("Cookie", COOKIE)
    payload = body
    if isinstance(body, (dict, list)):
        payload = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    c.request(method, path, body=payload, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data, dict(r.getheaders())


def stream_download(path, sink=None, headers=None, timeout=1800):
    """流式下载，返回 (状态码, 字节数, 耗时秒)。sink 为 None 时丢弃数据。"""
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
    hh = dict(headers or {})
    if COOKIE:
        hh.setdefault("Cookie", COOKIE)
    c.request("GET", path, headers=hh)
    r = c.getresponse()
    n = 0
    t0 = time.time()
    while True:
        chunk = r.read(1 << 20)
        if not chunk:
            break
        n += len(chunk)
        if sink is not None:
            sink.write(chunk)
    dt = time.time() - t0
    c.close()
    return r.status, n, dt


st, body, hd = call("POST", "/api/login", {"pin": PIN})
if st != 200:
    print("登录失败（测试实例需以 --pin %s 启动）：%s" % (PIN, st))
    sys.exit(2)
COOKIE = (hd.get("Set-Cookie") or "").split(";")[0]
st, body, _ = call("GET", "/api/session")
sess = json.loads(body)
RECV = sess["recv_dir"]
print("服务 %d，接收目录 %s" % (PORT, RECV))

# ---------------------------------------------------------------- 10 GB
print("\n[1] 10 GB 单文件传输")
big = os.path.join(RECV, "_bulk_10g.bin")
size_big = BIG_GB * (1 << 30)
t0 = time.time()
with open(big, "wb") as f:            # 稀疏文件，瞬间创建
    f.seek(size_big - 1)
    f.write(b"\0")
mk = time.time() - t0
real = os.path.getsize(big)
check("10 GB 测试文件就绪", real == size_big, "%.2f GB，%.2fs" % (real / (1 << 30), mk))

q = "/api/download?path=" + urllib.parse.quote(big)
t0 = time.time()
st, got, dt = stream_download(q + "&log=0")
speed = got / max(dt, 1e-6) / (1 << 20)
check("10 GB 整流下载完整", st == 200 and got == size_big,
      "status=%s 收到 %.2f GB，%.1f s，%.0f MB/s" % (st, got / (1 << 30), dt, speed))

for label, rng, expect in (("首", "bytes=0-1048575", 1 << 20),
                           ("中", "bytes=%d-%d" % (5 << 30, (5 << 30) + (1 << 20) - 1), 1 << 20),
                           ("尾", "bytes=%d-" % (size_big - (1 << 20)), 1 << 20)):
    st, got, dt = stream_download(q, headers={"Range": rng})
    check("10 GB %s段 Range 正确" % label, st == 206 and got == expect,
          "status=%s %d B" % (st, got))

q2 = "/api/download?path=" + urllib.parse.quote("x")
st, _, _ = call("GET", q2, headers={"Range": "bytes=0-1"})
check("越界文件 Range 不崩", st in (403, 404, 416), "status=%s" % st)

# ---------------------------------------------------------------- 小文件文件夹
print("\n[2] 1 MB 总量 / 4 KB 小文件 ×%d 的文件夹" % SMALL_N)
folder = os.path.join(RECV, "_bulk_small")
shutil.rmtree(folder, ignore_errors=True)
os.makedirs(folder, exist_ok=True)
blob = (b"A" * (SMALL_SIZE - 1)) + b"\n"
t0 = time.time()
for i in range(SMALL_N):
    with open(os.path.join(folder, "f%03d.txt" % i), "wb") as f:
        f.write(blob)
mk = time.time() - t0
total = SMALL_N * SMALL_SIZE
check("小文件文件夹就绪", os.path.isdir(folder),
      "%d 个 × 4 KB = %.2f MB，建模 %.2fs" % (SMALL_N, total / 1048576, mk))

t0 = time.time()
st, body, _ = call("GET", "/api/list?path=" + urllib.parse.quote(folder))
items = json.loads(body).get("entries", [])
dt = time.time() - t0
check("目录列表返回全部小文件", st == 200 and len(items) == SMALL_N,
      "%d 条，%.0f ms" % (len(items), dt * 1000))

etag = None
st, body, hd = call("GET", "/api/list?path=" + urllib.parse.quote(folder))
etag = hd.get("ETag")
st2, body2, _ = call("GET", "/api/list?path=" + urllib.parse.quote(folder),
                     headers={"If-None-Match": etag or ""})
check("目录列表支持 ETag 304", st2 == 304, "status=%s" % st2)

# 打包下载（zip 里应有 SMALL_N 个条目）
t0 = time.time()
st, body, _ = call("POST", "/api/zip", {"paths": [folder]}, headers={"Accept-Encoding": "identity"})
dt = time.time() - t0
import io
import zipfile
ok_zip = False
cnt = 0
if st == 200 and body[:2] == b"PK":
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        cnt = len(z.namelist())
        ok_zip = cnt == SMALL_N
check("文件夹打包下载完整", ok_zip, "status=%s 条目=%d 包体=%.2f MB 用时%.1fs"
      % (st, cnt, len(body) / 1048576, dt))

# 逐文件上传（模拟手机一次传一堆小文件）
t0 = time.time()
up_ok = 0
for i in range(SMALL_N):
    st, body, _ = call("POST", "/api/upload?name=up_%03d.txt" % i, blob,
                       headers={"Content-Type": "application/octet-stream"})
    if st == 200 and json.loads(body).get("ok"):
        up_ok += 1
dt = time.time() - t0
check("逐个上传 %d 个小文件" % SMALL_N, up_ok == SMALL_N,
      "%d/%d 成功，%.1f s（%.0f 个/秒）" % (up_ok, SMALL_N, dt, SMALL_N / max(dt, 1e-6)))

t0 = time.time()
st, got, dt = stream_download("/api/download?path=" +
                              urllib.parse.quote(os.path.join(RECV, "up_000.txt")))
check("小文件可正常下载", st == 200 and got == SMALL_SIZE, "%d B" % got)

# ---------------------------------------------------------------- 清理
if not KEEP:
    print("\n清理测试数据…")
    try:
        os.remove(big)
    except OSError:
        pass
    shutil.rmtree(folder, ignore_errors=True)
    # 上传可能因重名生成 "up_000 (1).txt"，一并清掉
    try:
        for name in os.listdir(RECV):
            if name.startswith("up_") and name.endswith(".txt"):
                try:
                    os.remove(os.path.join(RECV, name))
                except OSError:
                    pass
    except OSError:
        pass

print("\n================ 结论 ================")
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  - " + f)
sys.exit(1 if FAIL else 0)
