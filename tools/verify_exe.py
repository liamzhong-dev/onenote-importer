# -*- coding: utf-8 -*-
"""对**打包后的 exe** 做界面验收：启动 → 逐页截图 → 退出。

    python tools/verify_exe.py                    # 用 dist/ 里的默认产物
    python tools/verify_exe.py --out shots_exe    # 指定截图目录
    python tools/verify_exe.py --exe 路径.exe

为什么单独写一个：`tools/smoke_ui.py` 是在**源码进程内**自检的，它验证不了
「打出来的 exe 里界面是不是同一个样子」——比如 bridge 没进包、字体没打进去、
`sys._MEIPASS` 定位错，这些只有真跑 exe 才暴露。

两个关键手法：
  · **隔离配置**：给子进程一个临时 APPDATA，绝不碰用户真实的 config.json。
  · **真实点击，事后还原光标**：试过 PostMessage 投 WM_LBUTTONDOWN，Tk 不认
    （它按**真实光标位置**判定点到哪个控件，不是按消息里的坐标），页面根本不切。
    所以这里用 SetCursorPos + mouse_event 发真实输入，跑完把光标放回原处。
    副作用是这几秒里光标会自己动，别在跑的时候抢鼠标。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from tools.screenshot import capture, force_visible, find_window  # noqa: E402
from ui.theme import setup_dpi_awareness  # noqa: E402

USER32 = ctypes.windll.user32
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
SW_RESTORE = 9

NAV_FALLBACK = {          # 探测失败时的兜底：150% DPI、标准档实测值
    "sync":   (15, 122, 318, 56),
    "photos": (15, 184, 318, 55),
    "auth":   (15, 245, 318, 55),
    "adv":    (15, 306, 318, 55),
}
PAGE_TITLE = {"sync": "导入日记", "photos": "导入照片",
              "auth": "账户与通道", "adv": "高级设置"}


def client_origin(hwnd: int) -> tuple[int, int]:
    """客户区左上角在屏幕上的坐标。"""
    p = wintypes.POINT(0, 0)
    USER32.ClientToScreen(hwnd, ctypes.byref(p))
    return p.x, p.y


def click_screen(x: int, y: int) -> None:
    """在屏幕坐标 (x, y) 发一次真实左键单击。"""
    USER32.SetCursorPos(int(x), int(y))
    time.sleep(0.18)
    USER32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.06)
    USER32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def probe_nav(ui_scale: float, font_scale: float) -> dict[str, tuple[int, int, int, int]]:
    """在源码里按同样的缩放建一次壳，量出导航项在**客户区**里的位置。

    为什么不写死一张坐标表：侧边栏每个控件的位置都乘了 ui.px()，换个缩放档位
    整列都会挪。写死的话，在「大」「特大」档下点击会落到别的控件上，
    表现成「截图和上一页一模一样」（这个坑真踩过）。布局是确定性的，
    所以按同一套参数建一遍就能量准，而且以后改布局也不用回来修这张表。
    """
    import app as A

    orig = A.load_ui_prefs
    A.load_ui_prefs = lambda: (float(ui_scale), float(font_scale))
    try:
        a = A.SyncApp()
        try:
            a.update_idletasks()
            a.update()
            wx, wy = a.winfo_rootx(), a.winfo_rooty()
            nav = {}
            for key, item in a.nav_items.items():
                nav[key] = (item.winfo_rootx() - wx, item.winfo_rooty() - wy,
                            item.winfo_width(), item.winfo_height())
            return nav
        finally:
            a.destroy()
    finally:
        A.load_ui_prefs = orig


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=str(HERE / "dist" / "OneNote日记工具" / "OneNote日记工具.exe"))
    ap.add_argument("--out", default=str(HERE / "sample-output" / "exe-shots"))
    ap.add_argument("--ui-scale", type=float, default=None, help="先往隔离配置里种一个界面比例再启动")
    ap.add_argument("--font-scale", type=float, default=None, help="先往隔离配置里种一个字体比例再启动")
    ap.add_argument("--keep", action="store_true", help="跑完不关 exe（留给人工看）")
    args = ap.parse_args()

    exe = Path(args.exe)
    if not exe.exists():
        print(f"NG 找不到 exe：{exe}\n   先跑：python pack_exe.py")
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    setup_dpi_awareness()

    us = args.ui_scale if args.ui_scale is not None else 1.0
    fs = args.font_scale if args.font_scale is not None else 1.0

    # 探测和 exe 必须共用同一个隔离目录：探测进程读的是 os.environ["APPDATA"]，
    # 而 appdata_dir() 是调用时才读环境变量，所以这里改完立刻生效。
    saved_appdata = os.environ.get("APPDATA")
    env = dict(os.environ)
    env["APPDATA"] = tempfile.mkdtemp(prefix="exeverify-")
    os.environ["APPDATA"] = env["APPDATA"]
    # 缩放偏好是启动时读的，所以想验「exe 认不认这个设置」只能先种文件、再启动，
    # 不能启动之后再去点界面那排档位。
    prefs = Path(env["APPDATA"]) / "OneNoteDiaryImporter" / "ui.json"
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text(json.dumps({"ui_scale": us, "font_scale": fs}), encoding="utf-8")
    if args.ui_scale is not None or args.font_scale is not None:
        print(f"已种入缩放偏好：界面 {us} / 字体 {fs}")
    try:
        nav = probe_nav(us, fs)
        print(f"导航项坐标（按 {us}/{fs} 探测）："
              + "  ".join(f"{k}=({v[0]},{v[1]} {v[2]}x{v[3]})" for k, v in nav.items()))
    except Exception as e:                                    # noqa: BLE001
        print(f"导航坐标探测失败（{e}），退回标准档实测值 —— 非标准档可能点不中")
        nav = NAV_FALLBACK
    os.environ["APPDATA"] = saved_appdata

    print(f"启动 {exe.name}（隔离 APPDATA：{env['APPDATA']}）")
    proc = subprocess.Popen([str(exe)], env=env, cwd=str(exe.parent),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)

    ok = True
    saved_cursor = wintypes.POINT()
    USER32.GetCursorPos(ctypes.byref(saved_cursor))
    try:
        time.sleep(10)
        if proc.poll() is not None:
            print(f"NG 进程已经退出（退出码 {proc.returncode}）—— exe 起不来")
            return 1

        # 窗口标题是 "OneNote 日记工具"；screenshot.py 默认关键字是旧的，这里显式传
        hwnd = find_window("OneNote 日记工具")
        if not hwnd:
            print("NG 没找到窗口 —— exe 活着但界面没建出来")
            return 1
        force_visible(hwnd)

        rect = wintypes.RECT()
        USER32.GetWindowRect(hwnd, ctypes.byref(rect))
        ox, oy = client_origin(hwnd)
        print(f"窗口句柄 {hwnd}  外框 {rect.right - rect.left}x{rect.bottom - rect.top}"
              f"  客户区原点 ({ox}, {oy})")

        seen: dict[str, bytes] = {}
        try:
            USER32.SetForegroundWindow(hwnd)   # 不置前的话真实点击可能落到别的窗口
        except Exception:
            pass
        for key in ("sync", "photos", "auth", "adv"):
            dst = out / f"exe-{key}-{PAGE_TITLE[key]}.png"
            prev = next(iter(seen.values()), None)
            data = None
            for attempt in range(1, 4):
                # 每次重新量客户区原点：窗口可能在两轮之间被系统挪过位置
                ox, oy = client_origin(hwnd)
                x, y, w, h = nav[key]
                # 点 x+30 而不是正中：正中会落到标签右边那块空白上。
                # 图标和文字才是真正绑了 <Button-1> 的子控件，按人手的习惯点它更稳。
                click_screen(ox + x + 30, oy + y + h // 2)
                time.sleep(1.2)          # 等 Tk 重绘完再抓，否则会抓到上一页
                force_visible(hwnd)
                cw, ch = capture(hwnd, dst)
                data = dst.read_bytes()
                if data != prev:         # 和上一页不一样 = 真的切过去了
                    break
                print(f"      「{PAGE_TITLE[key]}」第 {attempt} 次点击没反应，重试")
            size_kb = len(data) / 1024
            # 同一张图 = 那一页压根没切过去（点空了），不只是"看着像"
            dup = next((k for k, v in seen.items() if v == data), None)
            seen[key] = data
            if dup:
                print(f"  NG  {PAGE_TITLE[key]:<6} 与「{PAGE_TITLE[dup]}」截图完全相同 —— 页面没切过去")
                ok = False
            elif size_kb <= 8:
                print(f"  NG  {PAGE_TITLE[key]:<6} -> {dst.name}  {cw}x{ch}  {size_kb:.0f} KB（基本是空图）")
                ok = False
            else:
                print(f"  OK  {PAGE_TITLE[key]:<6} -> {dst.name}  {cw}x{ch}  {size_kb:.0f} KB")

        # 顺便确认 exe 进程确实没有控制台黑窗
        classes = []
        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def cb(h, _l):
            p = wintypes.DWORD()
            USER32.GetWindowThreadProcessId(h, ctypes.byref(p))
            if p.value == proc.pid:
                b = ctypes.create_unicode_buffer(256)
                USER32.GetClassNameW(h, b, 256)
                classes.append(b.value)
            return True
        USER32.EnumWindows(cb, 0)
        consoles = [c for c in classes if c in ("ConsoleWindowClass", "PseudoConsoleWindow")]
        print(f"  {'NG' if consoles else 'OK'}  窗口类：{classes}")
        if consoles:
            ok = False
    finally:
        USER32.SetCursorPos(saved_cursor.x, saved_cursor.y)
        if not args.keep:
            proc.terminate()
            time.sleep(1.5)
            if proc.poll() is None:
                proc.kill()

    print("\n" + ("[OK] exe 界面验收通过" if ok else "[NG] exe 界面验收有问题"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
