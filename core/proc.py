# -*- coding: utf-8 -*-
"""静默地跑子进程。

为什么要单独拎出来：每次调用 PowerShell 桥（COM 通道）或起一个子 Python
（跑自检），Windows 都会新建一个控制台窗口。界面上看起来就是「蓝色窗口
一闪而过」——用户会以为后台在乱跑东西。

用 CREATE_NO_WINDOW + STARTF_USESHOWWINDOW/SW_HIDE 把它压掉，
把「正在进行哪一步」交给日志去说（见 writers/com.py 的 _run）。
"""
from __future__ import annotations

import os
import subprocess
import sys

# Windows: 不新建控制台窗口
CREATE_NO_WINDOW = 0x08000000
SW_HIDE = 0


def silent_kwargs() -> dict:
    """传给 subprocess.run / Popen 的「别弹窗」参数；非 Windows 返回空字典。"""
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = SW_HIDE
    return {"creationflags": CREATE_NO_WINDOW, "startupinfo": si}


def decode_output(raw: bytes | str | None) -> str:
    """子进程输出解码。

    子进程可能按 UTF-8 输出（我们自己设了 PYTHONIOENCODING），
    也可能按中文 Windows 默认的 GBK 输出（PowerShell 5.1 就是），
    挨个试一遍，别让用户看到一屏乱码。
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8-sig", "utf-8", "cp936", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def child_env(**extra: str) -> dict:
    """给子进程用的环境变量：强制 UTF-8 输出，parent 的变量照抄。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.update({k: v for k, v in extra.items() if v is not None})
    return env


def force_utf8_console() -> None:
    """把本进程的 stdout / stderr 切到 UTF-8。

    中文 Windows 上 Python 默认按 GBK 编码输出，日志里只要出现一个
    「\ufffd」（替换字符），print 就直接 UnicodeEncodeError 把自己崩掉——
    自检脚本正好踩过这个坑。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
