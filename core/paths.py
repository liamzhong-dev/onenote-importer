# -*- coding: utf-8 -*-
"""资源定位：找到随程序一起分发的文件（bridge 下的 PowerShell 桥脚本等）。

为什么需要这一层：源码运行时 `__file__` 指向项目目录，但用 PyInstaller
打包成 exe 之后，源码被塞进压缩包（onefile）或 `_internal` 子目录（onedir），
`__file__` 的父目录不再是项目根 —— 继续用它找 `bridge/xxx.ps1` 会直接找不到。

规则：
    · 源码运行           → 项目根目录（`core/paths.py` 的上两级）
    · 打包后             → `sys._MEIPASS`（打包进去的资源就在这儿）
                          找不到再退到 exe 所在目录（支持「把 bridge/ 放在 exe 旁边」）
"""
from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打出来的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """程序资源根目录。"""
    if not is_frozen():
        return Path(__file__).resolve().parent.parent

    base = getattr(sys, "_MEIPASS", None)
    if base:
        p = Path(base)
        if (p / "bridge").is_dir():
            return p
    return Path(sys.executable).resolve().parent


def bridge_script(name: str) -> Path:
    """定位 `bridge/<name>`；打包后失效时回退到源码树里的那份（开发机上常见）。"""
    p = app_root() / "bridge" / name
    if p.exists():
        return p
    fallback = Path(__file__).resolve().parent.parent / "bridge" / name
    return fallback if fallback.exists() else p
