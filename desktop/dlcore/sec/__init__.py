# -*- coding: utf-8 -*-
"""安全版专用模块包（只随安全版打包）。

第二轮实现，对应《DROP LINK 安全加固清单》：

    tls.py         S-01  TLS 传输加密（自签证书）
    trust.py       S-02  证书指纹 = 设备身份 + 信任列表
    approval.py    S-03  传输前人工确认（元数据先行）
    tokens.py      S-04  逐文件一次性 token
    pathguard.py   S-06  写入侧路径强约束（realpath / UNC / ADS / 软链 / 保留名）

三条纪律（与 docs/EDITIONS.md 第二节一致）：

1. dlcore/ 下的共用模块（config / state / fsutil / winenv / media / api）
   **不得 import 本包**；只有 server.py 入口与 httpd.py 的 edition 分支
   可以按需加载。
2. 个人版构建（build_exe.py --edition personal）不打包本目录，
   所以本包内不得放「共用模块会依赖」的代码。
3. 版本差异只通过 config.EDITION / config.is_secure_edition() 判断，
   不在业务代码里散落 edition 字符串。

骨架进度（1.5.0）：
  * 第二轮 A：certgen.py（纯标准库 ECDSA P-256 自签证书）+ tls.py（服务端 TLS）；
    安全版实际监听 https://IP:8848，/api/device 返回证书指纹。
  * 第二轮 B：trust.py（设备登记 + 首次配对审批 + 信任列表撤销）；
    /api/trust*、/api/login/status 只在安全版存在，个人版 404。
  * 第三轮：approval.py（S-03 传输前确认 + S-04 逐文件一次性 token）；
    /api/prepare-upload、/api/upload/status、/api/approve* 只在安全版存在。
  * 仍未实现：pathguard.py（S-06 写入侧路径约束）；tokens.py 保留为占位
    （token 逻辑已并入 approval.py）。
"""

from __future__ import annotations

# 包级开关：只有全部子模块可用才置 True（写入路径约束还没做）。
AVAILABLE = False


def available() -> bool:
    """本包是否具备完整的安全实现（当前仍为 False，TLS 部分见 tls.AVAILABLE）。"""
    return AVAILABLE
