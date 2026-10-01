# -*- coding: utf-8 -*-
"""临时工具：从自产 PNG 里裁一块并放大，用来核对小字到底画没画出来。

只认 tools/screenshot.py 写出的那种 PNG（RGB / 每行 filter=0）。
    python tools/_crop.py 源图.png 输出.png x0 y0 x1 y1 [放大倍数]
"""
import sys
import zlib
from pathlib import Path


def read_png(p: Path):
    b = p.read_bytes()
    assert b[:8] == b"\x89PNG\r\n\x1a\n", "不是 PNG"
    i, w, h, idat = 8, 0, 0, b""
    while i < len(b):
        ln = int.from_bytes(b[i:i + 4], "big")
        tag = b[i + 4:i + 8]
        data = b[i + 8:i + 8 + ln]
        if tag == b"IHDR":
            w = int.from_bytes(data[0:4], "big")
            h = int.from_bytes(data[4:8], "big")
        elif tag == b"IDAT":
            idat += data
        i += 12 + ln
    raw = zlib.decompress(idat)
    stride = w * 3 + 1
    rows = []
    for y in range(h):
        assert raw[y * stride] == 0, f"第 {y} 行不是 filter 0"
        rows.append(raw[y * stride + 1:(y + 1) * stride])
    return w, h, rows


def write_png(path: Path, w: int, h: int, rgb_rows):
    raw = b"".join(b"\x00" + r for r in rgb_rows)

    def chunk(tag, data):
        return len(data).to_bytes(4, "big") + tag + data + zlib.crc32(tag + data).to_bytes(4, "big")

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", w.to_bytes(4, "big") + h.to_bytes(4, "big") + bytes((8, 2, 0, 0, 0)))
           + chunk(b"IDAT", zlib.compress(raw, 6))
           + chunk(b"IEND", b""))
    path.write_bytes(png)


def main():
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    x0, y0, x1, y1 = (int(v) for v in sys.argv[3:7])
    scale = int(sys.argv[7]) if len(sys.argv) > 7 else 2
    w, h, rows = read_png(src)
    x0, x1 = max(0, x0), min(w, x1)
    y0, y1 = max(0, y0), min(h, y1)
    out = []
    for y in range(y0, y1):
        row = rows[y]
        line = b""
        for x in range(x0, x1):
            px = row[x * 3:x * 3 + 3]
            line += px * scale
        for _ in range(scale):
            out.append(line)
    cw, ch = (x1 - x0) * scale, (y1 - y0) * scale
    write_png(dst, cw, ch, out)
    # 顺带报一下这块区域里有多少「深色像素」（文字应该产生不少）
    dark = 0
    for y in range(y0, y1):
        row = rows[y]
        for x in range(x0, x1):
            r, g, b = row[x * 3], row[x * 3 + 1], row[x * 3 + 2]
            if r < 120 and g < 120 and b < 120:
                dark += 1
    print(f"裁出 {cw}x{ch} → {dst}；原图 {w}x{h}；该区域深色像素 {dark}")


if __name__ == "__main__":
    main()
