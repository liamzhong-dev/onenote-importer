# -*- coding: utf-8 -*-
"""只读检查：状态库记着的页面，在 OneNote 里的实际正文有多少字。

只读。用来发现「页面在、但内容是空的」这种情况 —— 光看标题在不在是发现不了的。

    python tools/diag_page_content.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from core.config import Config  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402

TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


def visible(xml: str) -> str:
    txt = TAG.sub(" ", xml)
    txt = txt.replace("&nbsp;", " ").replace("&amp;", "&")
    txt = txt.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return WS.sub(" ", txt).strip()


def main() -> int:
    cfg = Config.load()
    pipe = Pipeline(cfg, log=lambda m, lv="INFO": None)
    items = pipe.scan()
    pipe.inspect_targets(items)
    writer = pipe.connect()

    print(f"分区「{cfg.section_fixed}」—— 逐页读回正文\n" + "=" * 68)
    for it in items:
        rec = pipe.state.get(it.rel)
        pid = (rec.page_id if rec else "") or ""
        tail = pid[-6:] if pid else "-"
        print(f"{it.rel}   动作={it.action}")
        print(f"  标题   : {it.title}")
        print(f"  记忆id : ...{tail}")
        if not pid:
            print("  → 无记忆 id，跳过读取\n")
            continue
        try:
            xml = writer.get_page_xml(pid, retries=2)
        except Exception as e:
            print(f"  → 读不到内容：{type(e).__name__}: {e}\n")
            continue
        text = visible(xml)
        # 去掉标题本身，剩下的才是正文
        body = text.replace(it.title, "", 1).strip()
        # 本地源文件的可见字数，用来对比
        local_src = it.path.read_text(encoding="utf-8", errors="replace")
        local = WS.sub(" ", TAG.sub(" ", local_src)).strip()
        print(f"  页面可见字数: {len(text)}   去掉标题后: {len(body)}")
        print(f"  本地文件字数: {len(local)}")
        print(f"  页面开头: {text[:90]!r}")
        print(f"  页面结尾: {text[-60:]!r}")
        if len(body) < 20:
            print("  ⚠ 页面正文几乎是空的")
        print()
    pipe.state.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
