# -*- coding: utf-8 -*-
"""把指定窗口渲染成 PNG（用于界面自检，无需第三方库）。

用法：
    python tools/screenshot.py 输出.png          # 按窗口标题找
    python tools/screenshot.py 输出.png 标题关键字

用 PrintWindow(PW_RENDERFULLCONTENT) 抓取，被别的窗口挡住也能正确渲染。
"""
from __future__ import annotations

import ctypes
import sys
import time
import zlib
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ui.theme import setup_dpi_awareness  # noqa: E402

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

PW_RENDERFULLCONTENT = 0x00000002
SW_RESTORE = 9
BI_RGB = 0
DIB_RGB_COLORS = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def find_window(keyword: str) -> int:
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _l):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if keyword in buf.value:
                    found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else 0


def write_png(path: Path, width: int, height: int, bgra: bytes, stride: int) -> None:
    rows = []
    for y in range(height):
        row = bgra[y * stride: y * stride + width * 4]
        rows.append(b"\x00" + bytes(b for i in range(0, len(row), 4) for b in (row[i + 2], row[i + 1], row[i])))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (len(data).to_bytes(4, "big") + tag + data
                + zlib.crc32(tag + data).to_bytes(4, "big"))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes((8, 2, 0, 0, 0)))
           + chunk(b"IDAT", zlib.compress(raw, 6))
           + chunk(b"IEND", b""))
    path.write_bytes(png)


def force_visible(hwnd: int, tries: int = 3) -> bool:
    """把窗口拉成「正常显示」。返回最终是否可见（非最小化）。"""
    for _ in range(max(1, tries)):
        if not user32.IsIconic(hwnd):
            return True
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.35)
    return not user32.IsIconic(hwnd)


def _wait(seconds: float, pump=None) -> None:
    """等一小会儿，同时（可选）把消息泵转起来。

    ⚠️ 光 time.sleep 是不够的：被测程序多半是在自己的事件回调里调 capture()，
    睡的这一段它**没法处理重绘消息**。窗口如果刚被拉回可见，PrintWindow 就会
    拿到上一帧的 DWM 缓存（实测踩过：切到「账户与通道」截出来还是「导入日记」页）。
    传 pump=tk 窗口的 update 进去，重绘才会在抓图之前跑完。
    """
    end = time.time() + seconds
    while time.time() < end:
        if pump is not None:
            try:
                pump()
            except Exception:
                pass
        time.sleep(0.03)


def capture(hwnd: int, out: Path, pump=None) -> tuple[int, int]:
    # ⚠️ 窗口最小化时 GetWindowRect 返回 (-32000, -32000)，PrintWindow 只能画出一条
    #    标题栏（实测踩过：图只有 1KB，内容是三个窗口按钮）。
    #    而且这台机器上窗口会被**反复**压回最小化 —— 拉回来之后隔一两秒又缩回去，
    #    所以置前后必须再确认一次，不能只 restore 一下就走。
    force_visible(hwnd)
    try:
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    _wait(0.25, pump)
    was_iconic = bool(user32.IsIconic(hwnd))
    force_visible(hwnd)
    # 上面这次如果**真的**做了还原动作，窗口还要整体重绘一遍才轮到抓图
    _wait(0.25 if was_iconic else 0.15, pump)

    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top

    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    user32.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT)

    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = w
    info.bmiHeader.biHeight = -h        # 负数 = 自上而下
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = BI_RGB
    stride = w * 4
    buf = ctypes.create_string_buffer(stride * h)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(info), DIB_RGB_COLORS)

    write_png(out, w, h, buf.raw, stride)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(hwnd, hdc)
    return w, h


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    out = Path(sys.argv[1])
    keyword = sys.argv[2] if len(sys.argv) > 2 else "导入工具"
    setup_dpi_awareness()
    hwnd = find_window(keyword)
    if not hwnd:
        print(f"没找到标题含「{keyword}」的窗口")
        return 1
    w, h = capture(hwnd, out)
    print(f"已保存 {out}（{w}x{h}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
