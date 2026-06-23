# -*- coding: utf-8 -*-
"""生成图标：doc/icon.ico（给 exe 用）和 doc/icon.png（给仓库/README 用）。

形状是用 Pillow 画出来的，颜色取界面主题里的蓝色，不依赖任何设计素材。
只有「重新生成图标」才需要跑它，工具本身零依赖：

    python -m pip install pillow
    python tools/make_icon.py

想换图标就改下面的颜色和形状参数，重跑一次即可。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

# ui/theme.py 里的 accent，保持一致
BG = (47, 107, 255, 255)
FG = (255, 255, 255, 255)

GRID = 1024                      # 设计坐标系，所有尺寸按它等比缩放
SIZES = (16, 24, 32, 48, 64, 128, 256)
SS = 4                           # 每个尺寸先按 4 倍画再缩回去，边缘才不毛


def _render(size: int) -> Image.Image:
    n = size * SS
    k = n / GRID
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def box(*v):
        return [x * k for x in v]

    # 圆角方底
    d.rounded_rectangle(box(0, 0, GRID - 1, GRID - 1), radius=176 * k, fill=BG)
    # 向下的箭头：竖杆 + 箭头
    d.rounded_rectangle(box(462, 200, 562, 590), radius=50 * k, fill=FG)
    d.polygon([(324 * k, 540 * k), (700 * k, 540 * k), (512 * k, 800 * k)], fill=FG)
    # 底部托盘横条
    d.rounded_rectangle(box(236, 848, 788, 920), radius=44 * k, fill=FG)
    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    doc = Path(__file__).resolve().parent.parent / "doc"
    doc.mkdir(parents=True, exist_ok=True)
    _render(256).save(doc / "icon.ico", format="ICO", sizes=[(s, s) for s in SIZES])
    _render(512).save(doc / "icon.png", format="PNG")
    print(f"已生成 {doc / 'icon.ico'}（{', '.join(str(s) for s in SIZES)}）")
    print(f"已生成 {doc / 'icon.png'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
