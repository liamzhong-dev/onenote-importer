# -*- coding: utf-8 -*-
"""把导入失败留下的空页挑出来（默认只列，不动手）。

2026-09-21 13:55 那次导入，页面建出来了、内容一个字没写进去，
于是在 2026夏 里留下 9 个「未命名」空页。滚回机制已经补上（以后不会再留），
但已经产生的这批得清掉。

用法：
    python tools/cleanup_blank_pages.py                        # 只列出空页，什么都不删
    python tools/cleanup_blank_pages.py --after "2026-09-21 13:50"
    python tools/cleanup_blank_pages.py --after "2026-09-21 13:50" --yes   # 真删

为什么要求 --after：空页的标题都叫「未命名」，光看标题分不出「工具留下的」
和「你自己建了没写的」。加一道时间过滤，只动那个时间点之后产生的。
删掉的页面会进 OneNote 回收站，不是彻底消失。
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import Config                    # noqa: E402
from core.proc import force_utf8_console          # noqa: E402
from writers.com import ComWriter                 # noqa: E402

BLANK_TITLES = {"", "未命名", "无标题", "untitled", "new page", "新建页面"}


def parse_when(text: str) -> dt.datetime | None:
    if not text:
        return None
    cleaned = text.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m-%d %H:%M"):
        try:
            parsed = dt.datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        if fmt == "%m-%d %H:%M":
            parsed = parsed.replace(year=dt.date.today().year)
        return parsed
    return None


def page_time(raw: str) -> dt.datetime | None:
    """OneNote 的 dateTime 属性。写成 Z 结尾的多半是 UTC，这里统一成本地时间比。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    text = raw.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def short_id(pid: str, tail: int = 16) -> str:
    """页面 id 的前半段是**分区**的 GUID，同一分区里所有页面都长一样。

    直接 [:38] 截出来是一排看起来完全相同的 id，等于没给。真正能区分页面的是
    末尾那串数字，所以这里留尾巴。
    """
    pid = pid or ""
    return f"…{pid[-tail:]}" if len(pid) > tail else pid


def main() -> int:
    force_utf8_console()
    ap = argparse.ArgumentParser(description="列出（或删除）导入失败留下的空页")
    ap.add_argument("--notebook", default="", help="笔记本名，默认取配置里的")
    ap.add_argument("--section", default="", help="分区名，默认取配置里的目标分区")
    ap.add_argument("--after", default="", help="只处理这个时间之后创建的空页，如 2026-09-21 13:50")
    ap.add_argument("--yes", action="store_true", help="确认执行删除（不加则只列不动）")
    args = ap.parse_args()

    cfg = Config.load()
    nb_name = args.notebook or cfg.notebook_name
    sec_name = args.section or (cfg.section_fixed if cfg.section_mode == "fixed" else "")

    writer = ComWriter(cfg, log=lambda level, msg: None, step=lambda t: None)
    nb = writer.find_notebook(nb_name)
    if nb is None:
        print(f"找不到笔记本「{nb_name}」")
        return 1
    secs = [s for s in writer.list_sections(nb.id) if not sec_name or s.name == sec_name]
    if not secs:
        print(f"笔记本「{nb.name}」下没有分区「{sec_name}」")
        return 1

    cutoff = parse_when(args.after)
    if args.after and cutoff is None:
        print(f"看不懂 --after 的时间格式：{args.after}")
        return 1

    total_deleted = 0
    for sec in secs:
        pages = writer.list_pages_full(sec.id)
        blanks = [p for p in pages if (p.get("title") or "").strip().lower() in BLANK_TITLES]
        picked = []
        for p in blanks:
            when = page_time(p.get("created", ""))
            if cutoff is not None and when is not None and when < cutoff:
                continue
            picked.append((p, when))

        print(f"\n分区「{sec.name}」共 {len(pages)} 页，其中标题为空/未命名的 {len(blanks)} 页；"
              f"符合本次筛选的 {len(picked)} 页")
        if not picked:
            continue
        for i, (p, when) in enumerate(picked, start=1):
            idx = next((n for n, q in enumerate(pages, 1) if q["id"] == p["id"]), 0)
            stamp = when.strftime("%m-%d %H:%M:%S") if when else f"（时间无法解析：{p.get('created')!r}）"
            print(f"  {i:2d}. 第 {idx} 页  {stamp}  id={short_id(p['id'])}")

        if not args.yes:
            print("   → 只列不删。确认无误后加 --yes 再跑一次。")
            continue
        for p, _ in picked:
            try:
                writer.delete_page(p["id"])
                total_deleted += 1
                print(f"  已删除（进回收站）：{short_id(p['id'], 24)}")
            except Exception as e:  # noqa: BLE001
                print(f"  删除失败：{e}")

    if args.yes:
        print(f"\n共删除 {total_deleted} 页，可在 OneNote「删除的页面」里找回。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
