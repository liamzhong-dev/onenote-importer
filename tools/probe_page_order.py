# -*- coding: utf-8 -*-
"""OneNote 目标分区体检（只读）：笔记本有哪些分区、目标分区里现在有多少页。

「导入完找不到文件」多半是两件事：
    ① 根本没写进去 —— 看状态库就知道（预览模式跑多少次都不会动 OneNote）；
    ② 写进去了，但没在预想的位置 —— 看这里列出的页面顺序，再看导入日志里的
       「第 N–M 页」提示。

这个脚本只读不改，连一页都不会动：
    python tools/probe_page_order.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import Config  # noqa: E402
from core.proc import force_utf8_console  # noqa: E402
from writers.com import ComWriter  # noqa: E402

force_utf8_console()

RECYCLE_NAMES = ("删除的页面", "Deleted Pages")


def show_history(cfg: Config) -> None:
    """状态库是最诚实的：dry_run=1 那几次从来没碰过 OneNote。"""
    path = Path(cfg.state_db_path())
    print("同步记账：")
    if not path.exists():
        print("  · 还没有状态库，说明一次都还没跑过")
        return
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    pages = list(con.execute("SELECT COUNT(*) AS n FROM pages"))[0]["n"]
    print(f"  · 已记账页面 {pages} 条"
          + ("（0 条 = OneNote 里从来没被写入过任何页面）" if pages == 0 else ""))
    for r in con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 5"):
        flag = "预览（未写入）" if r["dry_run"] else "真实写入"
        print(f"  · {r['started_at']}  {flag}  新建 {r['created']} / 更新 {r['updated']}"
              f" / 跳过 {r['skipped']} / 失败 {r['failed']}  通道={r['writer']}")


def show_sections(writer: ComWriter, nb, target: str) -> None:
    print("\n现有分区与页面数：")
    sections = [s for s in writer.list_sections(nb.id) if s.name not in RECYCLE_NAMES]
    for sec in sections:
        pages = writer.list_pages(sec.id)
        mark = "  ← 目标分区" if sec.name.strip().lower() == target.strip().lower() else ""
        print(f"  · 「{sec.name}」{len(pages)} 页{mark}")
        if mark and pages:
            print(f"        最上面 3 页：{'、'.join(t for _, t in pages[:3])}")
            print(f"        最下面 3 页：{'、'.join(t for _, t in pages[-3:])}")


def main() -> int:
    cfg = Config.load()
    print(f"笔记本：{cfg.notebook_name}")
    if cfg.section_mode == "fixed":
        print(f"分区方式：固定写入「{cfg.section_fixed}」")
    else:
        print(f"分区方式：按模板「{cfg.section_template}」自动分区")
    print(f"预览模式（dry_run）：{'开 —— 跑多少次都不会写进 OneNote' if cfg.dry_run else '关'}"
          f"　标题模板：{cfg.title_template}")
    show_history(cfg)

    writer = ComWriter(cfg, log=lambda level, msg: print(f"    [{level}] {msg}"))
    probe = writer.probe()
    print(f"\nCOM 通道：{'可用' if probe.available else '不可用 — ' + probe.reason}")
    if not probe.available:
        return 1
    nb = writer.find_notebook(cfg.notebook_name)
    if nb is None:
        print(f"[NG] OneNote 里找不到笔记本「{cfg.notebook_name}」")
        return 1
    show_sections(writer, nb, cfg.section_fixed)
    print("\n（本工具只读；导入后日志里的「第 N–M 页」就是新页在该分区里的位置）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
