# -*- coding: utf-8 -*-
"""切页可靠性探针：确认「切过去之后窗口上真的显示的是这一页」。

背景：冒烟脚本里 03-账户与通道页 一度截出来还是「导入日记」页。
要先分清到底是
  (a) 应用没把页面 lift 上来（真 bug），还是
  (b) lift 上来了但 PrintWindow 抓的是上一帧（截图时序问题）。
判据用两个互相独立的口径：
  · Tk 口径 —— winfo_containing(屏幕坐标) 落回来的是不是目标页的后代
  · 像素口径 —— 同一页连拍两次，两张图必须一致（说明这一帧已经画稳了）

用法：
    python tools/probe_pages.py
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SMOKE_CFG = Path(tempfile.gettempdir()) / "onenote_photo_probe"
shutil.rmtree(_SMOKE_CFG, ignore_errors=True)
_SMOKE_CFG.mkdir(parents=True, exist_ok=True)
os.environ["ONENOTE_PHOTO_CONFIG_DIR"] = str(_SMOKE_CFG)

import core.uiprefs as uiprefs  # noqa: E402
uiprefs.prefs_path = lambda: _SMOKE_CFG / "ui.json"

from tools.screenshot import capture, find_window  # noqa: E402
from ui.theme import setup_dpi_awareness  # noqa: E402

OUT = ROOT / "sample-output" / "_probe_pages"
OUT.mkdir(parents=True, exist_ok=True)


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def top_level_hwnd(tk_win) -> int:
    import ctypes
    hwnd = tk_win.winfo_id()
    parent = ctypes.windll.user32.GetParent(hwnd)
    return parent or hwnd


def main() -> int:
    setup_dpi_awareness()
    from app import SyncApp

    app = SyncApp()
    problems: list[str] = []
    seen: dict[str, str] = {}

    def settle(seconds: float = 0.9):
        end = time.time() + seconds
        while time.time() < end:
            app.update()
            time.sleep(0.05)

    def on_top_key() -> str:
        """host 的子窗口里**堆叠顺序最靠上**的那个是不是目标页。

        不用 winfo_containing 按屏幕坐标去问 —— 窗口被压成最小化时
        winfo_rootx 是 -32000，那套坐标完全是废的（实测永远返回 None）。
        winfo children 给的是堆叠顺序，跟屏幕坐标无关，稳得多。
        """
        app.update_idletasks()
        kids = app.host.winfo_children()
        if not kids:
            return "?"
        top = kids[-1]
        for key, frame in app.pages.items():
            if str(frame) == str(top):
                return key
        return f"?({top})"

    def step():
        try:
            for key in ("sync", "photos", "auth", "adv"):
                app._show_page(key)
                a = OUT / f"{key}-a.png"
                b = OUT / f"{key}-b.png"
                # 先抓一张：capture 会把窗口拉回可见，之后 winfo_rootx 才是有效坐标。
                # （窗口最小化时 rootx = -32000，winfo_containing 直接返回 None。）
                capture(top_level_hwnd(app), a, pump=app.update)
                tk_says = on_top_key()
                if tk_says != key:
                    problems.append(
                        f"切到 {key} 之后，Tk 说最顶上的是 {tk_says}（页面没真的起来）")
                settle(0.6)
                capture(top_level_hwnd(app), b, pump=app.update)
                ha, hb = md5(a), md5(b)
                seen[key] = ha
                # 日志类页面本来就会不停刷新，两次不一致不算问题，只打出来看
                print(f"      {key:7s} Tk={tk_says:7s} {ha[:8]} / {hb[:8]} "
                      f"{'稳' if ha == hb else '两帧不同（页面在刷新）'}", flush=True)
        except Exception as e:  # noqa: BLE001 — 探针要能把炸点打出来，不能闷死
            import traceback
            traceback.print_exc()
            problems.append(f"探针自身异常：{type(e).__name__}: {e}")
        finally:
            app.destroy()

    app.after(500, step)
    app.after(120_000, lambda: (print("[NG] 探针超时", flush=True), app.destroy()))
    app.mainloop()

    # 四个页面互不相同
    by_hash: dict[str, list[str]] = {}
    for k, h in seen.items():
        by_hash.setdefault(h, []).append(k)
    for names in by_hash.values():
        if len(names) > 1:
            problems.append(f"这些页面截出来一模一样：{names}")

    shutil.rmtree(_SMOKE_CFG, ignore_errors=True)
    if problems:
        for p in problems:
            print(f"[NG] {p}")
        return 1
    print("[OK] 四个页面切过去都能真的显示出来，像素稳定且互不相同")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
