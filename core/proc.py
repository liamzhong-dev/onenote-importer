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


def run_script(script, timeout: int = 600) -> tuple[int, str, str]:
    """跑一个自检脚本，返回 (退出码, stdout, stderr)。

    源码运行时起子进程，脚本崩了也带不走界面。打包成 exe 之后这条路就不通了：
    窗口版 exe 没有控制台，子进程写出来的东西会被整个丢掉（实测输出 0 字节），
    所以打包版直接在本进程里跑，输出用 StringIO 接回来。
    """
    if getattr(sys, "frozen", False):
        return _run_inprocess(script)
    proc = subprocess.run([sys.executable, str(script)], capture_output=True,
                          env=child_env(), timeout=timeout, **silent_kwargs())
    return proc.returncode, decode_output(proc.stdout), decode_output(proc.stderr)


def _run_inprocess(script) -> tuple[int, str, str]:
    """在本进程里按 __main__ 跑一个脚本，把它打出来的东西接回来。"""
    import contextlib
    import io
    import runpy
    import traceback as tb

    out, err = io.StringIO(), io.StringIO()
    saved_argv = sys.argv
    sys.argv = [str(script)]
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                runpy.run_path(str(script), run_name="__main__")
                return 0, out.getvalue(), err.getvalue()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
                return code, out.getvalue(), err.getvalue()
            except BaseException:  # noqa: BLE001 — 自检脚本崩了不该把界面一起带走
                tb.print_exc()
                return 1, out.getvalue(), err.getvalue()
    finally:
        sys.argv = saved_argv


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
