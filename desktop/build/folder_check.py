# -*- coding: utf-8 -*-
"""文件夹传输自检：多级目录 / 空文件夹 / 大文件 / 中断 / 路径穿越。

用法：python build/folder_check.py [port]
"""
import http.client
import json
import os
import shutil
import sys
import time
import urllib.parse

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8848
PIN = os.environ.get("DROPLINK_PIN", "12345678")
COOKIE = ""
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   <- " + detail) if detail else ""))


def call(method, path, body=None, headers=None, timeout=120):
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


st, body, hd = call("POST", "/api/login", {"pin": PIN})
if st != 200:
    print("登录失败（测试实例需以 --pin %s 启动）：%s" % (PIN, st))
    sys.exit(2)
COOKIE = (hd.get("Set-Cookie") or "").split(";")[0]
st, body, _ = call("GET", "/api/session")
RECV = json.loads(body)["recv_dir"]
ROOT_NAME = "_folder_test"
ROOT = os.path.join(RECV, ROOT_NAME)
shutil.rmtree(ROOT, ignore_errors=True)

# 期望的目录树：3 级嵌套 + 两个空目录 + 中文/空格名
TREE = {
    "说明.txt": b"hello folder",
    "子目录A/文件1.txt": b"file one",
    "子目录A/深层 目录/文件2.txt": b"nested two",
    "子目录A/深层 目录/更深处/深文件.bin": os.urandom(1024),
    "空文件夹": None,
    "子目录A/空子目录": None,
}
BIG_REL = "子目录A/大文件.bin"
BIG_SIZE = 50 * 1024 * 1024

print("服务 %d，接收目录 %s" % (PORT, RECV))

print("\n[1] 多级目录 + 空文件夹 + 大文件")
ok_all = True
for rel, data in TREE.items():
    if data is None:
        st, body, _ = call("POST", "/api/mkdir", {"rel": "%s/%s" % (ROOT_NAME, rel)})
        if st != 200:
            ok_all = False
            print("    空目录失败:", rel, st, body[:80])
        continue
    name = rel.split("/")[-1]
    sub = rel.rsplit("/", 1)[0] if "/" in rel else ""
    q = {"name": name}
    if sub:
        q["rel"] = "%s/%s" % (ROOT_NAME, sub)
    else:
        q["rel"] = ROOT_NAME
    st, body, _ = call("POST", "/api/upload?" + urllib.parse.urlencode(q), data,
                       headers={"Content-Type": "application/octet-stream"})
    good = st == 200 and json.loads(body).get("ok")
    if not good:
        ok_all = False
        print("    上传失败:", rel, st, body[:80])
check("逐文件上传成功", ok_all)

# 大文件（50 MB）走同一通道
q = {"name": "大文件.bin", "rel": "%s/%s" % (ROOT_NAME, "子目录A")}
t0 = time.time()
st, body, _ = call("POST", "/api/upload?" + urllib.parse.urlencode(q), os.urandom(BIG_SIZE),
                   headers={"Content-Type": "application/octet-stream"}, timeout=300)
dt = time.time() - t0
check("50 MB 大文件上传", st == 200 and json.loads(body).get("size") == BIG_SIZE,
      "status=%s %.1f MB/s" % (st, BIG_SIZE / 1048576 / max(dt, 1e-6)))

print("\n[2] 目录结构还原")
tree_ok = True
for rel, data in list(TREE.items()) + [(BIG_REL, b"")]:
    p = os.path.join(ROOT, *rel.split("/"))
    if data is None:
        if not os.path.isdir(p):
            tree_ok = False
            print("    缺少空目录:", rel)
    else:
        if not os.path.isfile(p):
            tree_ok = False
            print("    缺少文件:", rel)
        elif data and os.path.getsize(p) != len(data):
            tree_ok = False
            print("    大小不符:", rel, os.path.getsize(p), len(data))
check("多级目录与空文件夹全部还原", tree_ok)
bigp = os.path.join(ROOT, *BIG_REL.split("/"))
check("大文件落盘大小正确", os.path.isfile(bigp) and os.path.getsize(bigp) == BIG_SIZE)

n_dirs = sum(1 for r, d, f in os.walk(ROOT) for _ in d)
check("目录层级数量正确", n_dirs == 5, "实际 %d 个子目录" % n_dirs)

print("\n[3] 兼容性与边界")
st, body, _ = call("POST", "/api/upload?" + urllib.parse.urlencode({"name": "普通文件.txt"}), b"single")   # 无 rel：旧行为
check("不带 rel 的单文件上传仍可用", st == 200 and os.path.isfile(os.path.join(RECV, "普通文件.txt")))
try:
    os.remove(os.path.join(RECV, "普通文件.txt"))
except OSError:
    pass

st, body, _ = call("POST", "/api/upload?name=x.txt&rel=" + urllib.parse.quote("../../evil"),
                   b"attack")
escaped = os.path.exists(os.path.join(os.path.dirname(RECV), "evil", "x.txt"))
check("rel 路径穿越被拦截", st in (200, 403) and not escaped, "status=%s" % st)

st, body, _ = call("POST", "/api/mkdir", {"rel": "../../evil_dir"})
check("mkdir 路径穿越被拦截", st in (200, 403) and not os.path.exists(
    os.path.join(os.path.dirname(RECV), "evil_dir")), "status=%s" % st)

# 中断：声明 10MB 只发 1MB，然后断开
try:
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    c.putrequest("POST", "/api/upload?name=%E4%B8%AD%E6%96%AD.bin&rel=" + ROOT_NAME)
    c.putheader("Content-Length", str(10 * 1024 * 1024))
    c.endheaders()
    c.send(b"x" * (1024 * 1024))
    c.sock.close()
except Exception:
    pass
time.sleep(1.5)
leftovers = [n for n in os.listdir(ROOT) if n.endswith(".part") or n.startswith(".dl_")]
check("中断后不留半截文件", not leftovers, ",".join(leftovers))
check("中断文件未被当成完成", not os.path.exists(os.path.join(ROOT, "中断.bin")))

print("\n[4] 打包下载保持结构")
st, body, _ = call("POST", "/api/zip", {"paths": [ROOT]}, timeout=300)
import io
import zipfile
names = []
if st == 200 and body[:2] == b"PK":
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        names = z.namelist()
check("打包下载包含子目录条目",
      any("子目录A/" in n for n in names) and any("空文件夹" in n for n in names),
      "条目 %d" % len(names))

# 清理
shutil.rmtree(ROOT, ignore_errors=True)
for n in os.listdir(RECV):
    if n.startswith("evil") or n == "普通文件.txt":
        try:
            os.remove(os.path.join(RECV, n))
        except OSError:
            pass

print("\n================ 结论 ================")
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  - " + f)
sys.exit(1 if FAIL else 0)
