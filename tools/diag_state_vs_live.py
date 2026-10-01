# -*- coding: utf-8 -*-
"""只读诊断：状态库记的 page_id 与 OneNote 分区现场清单对不对得上。

只读，不改任何东西，也不创建页面。

    python tools/diag_state_vs_live.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from core.config import Config  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402


def main() -> int:
    cfg = Config.load()
    pipe = Pipeline(cfg, log=lambda m, lv="INFO": print(f"  [{lv}] {m}"))
    items = pipe.scan()
    print(f"\n源目录：{cfg.vault_path}")
    print(f"目标：笔记本「{cfg.notebook_name}」 / 分区「{cfg.section_fixed}」")
    print(f"扫描到 {len(items)} 篇\n")

    ok = pipe.inspect_targets(items)
    print(f"\ninspect_targets 返回：{ok}\n")

    key = cfg.section_fixed.strip().lower()
    live = pipe.section_live_ids.get(key)
    pages = pipe.section_pages.get(key, {})

    print("=" * 68)
    for it in items:
        rec = pipe.state.get(it.rel)
        pid = (rec.page_id if rec else "") or ""
        # OneNote 的 page id 前缀都是一样的 GUID，只有尾段有区分度
        short = ("..." + pid[-6:]) if pid else "-"
        if live is None:
            verdict = "分区清单没读到，无法判定"
        elif not pid:
            verdict = "状态库无记录"
        elif pid in live:
            verdict = "现场还在 ✓"
        else:
            verdict = "现场已不存在 ✗（会被判 skip，永远重建不了）"
        print(f"{it.rel}")
        print(f"    本地动作 : {it.action}  ({it.reason})")
        print(f"    标题     : {it.title}")
        print(f"    记忆 id  : {short}")
        print(f"    现场判定 : {verdict}")
        print(f"    分区已扫 : {it.section_scanned}  同名命中: {bool(it.matched_page_id)}")
        print()

    if pages:
        print("-" * 68)
        print(f"分区「{cfg.section_fixed}」现场 {len(pages)} 个标题：")
        for t, pid in sorted(pages.items()):
            print(f"    ...{pid[-6:]}  {t}")
    pipe.state.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
