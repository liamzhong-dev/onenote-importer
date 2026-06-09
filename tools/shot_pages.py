# -*- coding: utf-8 -*-
"""离线抓图工具：把界面的三个页面各截一张，用于视觉走查。

用法（在项目根目录）：
    python tools/shot_pages.py [输出目录]
不依赖任何第三方库，全部走 Win32 GDI。
"""
from __future__ import annotations

import ctypes
import importlib.util
import sys
import time
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))


def load_shot():
    spec = importlib.util.spec_from_file_location("_shot", HERE / "screenshot.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def root_hwnd(win) -> int:
    """把 Tk 给的窗口句柄提升到顶层窗口（带标题栏那个）。"""
    u = ctypes.windll.user32
    hwnd = win.winfo_id()
    return u.GetAncestor(wintypes.HWND(hwnd), 2) or hwnd   # GA_ROOT = 2


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "doc" / "shots"
    out_dir.mkdir(parents=True, exist_ok=True)
    shot = load_shot()

    import app as A

    win = A.SyncApp()
    win.update_idletasks()
    win.update()
    # 等启动时的通道探测跑完（后台线程 + 队列回传），否则账户页会是空的
    for _ in range(16):
        win.update()
        time.sleep(0.25)

    pages = [
        ("sync", "01-导入页.png"),
        ("auth", "02-账户与通道页.png"),
        ("adv", "03-高级设置页.png"),
    ]
    hwnd = root_hwnd(win)
    written = []
    for key, name in pages:
        win._show_page(key)
        win.update_idletasks()
        win.update()
        time.sleep(0.5)
        win._show_page(key)
        win.update()
        w, h = shot.capture(hwnd, out_dir / name)
        written.append(f"{name} {w}x{h}")

    win.destroy()
    print("已生成：")
    for line in written:
        print("  ", line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
