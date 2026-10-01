# -*- coding: utf-8 -*-
"""全量体检：把每个笔记本每个分区里的「未命名页」数一遍，并读回正文。

只读，不删。用来回答「到底还有没有导入失败留下的空页」这种问题 ——
只看一个笔记本容易漏，而删漏了、删错了都不可逆。

**关键**：标题是「未命名」不等于页面是空的。OneNote 的「快速笔记」里
随手新建的页就长这样，有些人也确实会建了页不写标题直接写正文。
所以这里会把每页的正文**真读一遍**，分成两类报：

    真空页（没标题也没正文）  —— 才可能是工具留下的
    有正文（只是没标题）      —— 是你自己的东西，不能按"清理空页"处理

    python tools/audit_blank_pages.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import Config                     # noqa: E402
from core.proc import force_utf8_console           # noqa: E402
from tools.cleanup_blank_pages import (            # noqa: E402
    BLANK_TITLES, is_recycle_section, page_stats,
)
from writers.com import ComWriter                  # noqa: E402


def main() -> int:
    force_utf8_console()
    cfg = Config.load()
    writer = ComWriter(cfg, log=lambda level, msg: None, step=lambda t: None)

    notebooks = writer.list_notebooks()
    truly_blank: list[str] = []      # 没标题、没正文、也没图 —— 唯一谈得上"清理"的一类
    media_only: list[str] = []       # 没标题、没正文，但有图 / 笔迹
    with_text: list[str] = []        # 没标题，但有正文
    total_pages = 0
    errors: list[str] = []

    for nb in notebooks:
        try:
            secs = writer.list_sections(nb.id)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{nb.name}: 列分区失败 {e}")
            continue
        for sec in secs:
            if is_recycle_section(sec.name):
                continue
            try:
                pages = writer.list_pages_full(sec.id)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{nb.name}/{sec.name}: 列页面失败 {e}")
                continue
            total_pages += len(pages)
            unnamed = [p for p in pages
                       if (p.get("title") or "").strip().lower() in BLANK_TITLES]
            for p in unnamed:
                try:
                    xml = writer.get_page_xml(p["id"], retries=2)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{nb.name}/{sec.name}/{p['id'][-12:]}: 读正文失败 {e}")
                    continue
                body_len, media = page_stats(xml)
                when = (p.get("created") or "").replace("T", " ")[:19]
                line = (f"{nb.name} / {sec.name}   {when}   "
                        f"id=…{p['id'][-24:]}   正文 {body_len} 字 · 媒体 {media} 个")
                if body_len >= 8:
                    with_text.append(line)
                elif media > 0:
                    media_only.append(line)
                else:
                    truly_blank.append(line)

    print("=" * 72)
    print(f"扫了 {len(notebooks)} 个笔记本 / {total_pages} 页")
    print("=" * 72)

    print(f"\n【A】真空页：没标题、没正文、也没图 —— 共 {len(truly_blank)} 页")
    print("     只有这一类才谈得上当「空页」清理。")
    for line in truly_blank:
        print("   " + line)
    if not truly_blank:
        print("    （没有）")

    print(f"\n【B】没标题，但页面里有图 / 笔迹 —— 共 {len(media_only)} 页（不能删）")
    for line in media_only:
        print("   " + line)
    if not media_only:
        print("    （没有）")

    print(f"\n【C】没标题，但有正文 —— 共 {len(with_text)} 页（不能删）")
    for line in with_text:
        print("   " + line)
    if not with_text:
        print("    （没有）")

    for e in errors:
        print(f"\n  ! {e}")
    print()
    print("[OK] 只读体检完成，没有改动任何页面。" if not errors else "[注意] 上面有读取失败的条目")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



if __name__ == "__main__":
    raise SystemExit(main())
