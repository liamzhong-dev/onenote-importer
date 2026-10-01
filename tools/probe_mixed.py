# -*- coding: utf-8 -*-
"""混合格式导入的端到端体检：md + txt + docx + html 一起扫、一起渲染。

全程用预览模式（dry_run），**不连 OneNote、不写任何页面**，
只验证「文件 → 计划 → OneNote 受限 HTML」这条链路是通的。

    python tools/probe_mixed.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="mixedprobe-"))
os.environ["APPDATA"] = str(TMP / "appdata")

from core import convert  # noqa: E402
from core.config import Config  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402
from tests.docx_fixture import build_mixed_vault  # noqa: E402  与自检共用同一份夹具

VAULT = TMP / "vault"


def build_vault() -> None:
    build_mixed_vault(VAULT)


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f'  {"OK " if ok else "NG "} {name}' + (f"  —— {detail}" if detail else ""))
    return ok


def make_cfg(formats) -> Config:
    c = Config()
    c.vault_path = str(VAULT)
    c.source_formats = formats
    c.include_globs = ["**/*.md"]      # 故意留成默认值，验它不会把别的格式挡掉
    c.exclude_globs = ["**/.obsidian/**", "**/模板/**"]
    c.notebook_name = "日记"
    c.title_template = "{title}"
    c.dry_run = True
    return c


def main() -> int:
    build_vault()
    ok = True

    # ---------------- 只开 md：行为必须和以前一模一样 ----------------
    print("== 只勾 Markdown ==")
    p = Pipeline(make_cfg(["md"]))
    items = p.scan()
    rels = sorted(Path(i.rel).name for i in items)
    print("   扫到:", rels)
    ok &= check("只扫到 md", rels == ["2026-09-01 md 篇.md"], str(rels))
    ok &= check("pdf 没被当来源", not any(r.endswith(".pdf") for r in rels))

    # ---------------- 四种全开 ----------------
    print("\n== md + txt + docx + html 全开 ==")
    p = Pipeline(make_cfg(["md", "txt", "docx", "html"]))
    items = p.scan()
    rels = sorted(Path(i.rel).name for i in items)
    print("   扫到:", rels)
    for ext in (".md", ".txt", ".docx", ".html"):
        ok &= check(f"扫到 {ext}", any(r.endswith(ext) for r in rels))
    ok &= check("pdf 依然没被当来源", not any(r.endswith(".pdf") for r in rels))
    ok &= check("默认的 **/*.md 没把其它格式挡掉", len(rels) == 4, f"{len(rels)} 个")
    ok &= check("全部标为新建", all(i.action == "create" for i in items))

    # ---------------- 每篇都渲染一遍 ----------------
    print("\n== 渲染成 OneNote 受限 HTML ==")
    wants = {
        ".md": ["正文里的 <b>加粗</b>"],
        ".txt": ["第一段。", "第二段。"],
        # 表格在 OneNote HTML 里是 <table>/<td>，不是 Markdown 的竖线语法
        ".docx": ["九月七日", "<b>很重要</b>", ">项目</th>", ">含|竖线</td>", "无序一"],
        ".html": ["正文标题", "<b>加粗</b>", "<li>条目甲</li>"],
    }
    for item in items:
        nf = next(n for n in p.index.notes if n.rel == item.rel)
        page, _ = p.render(nf)
        ext = Path(item.rel).suffix.lower()
        html = page.html
        print(f"   {ext:6s} 标题={page.title!r:22s} 体积={page.total_bytes() // 1024}KB "
              f"警告={page.warnings}")
        for needle in wants[ext]:
            ok &= check(f"{ext} 渲染含 {needle!r}", needle in html)
        ok &= check(f"{ext} 生成了页面标题", bool(page.title.strip()), repr(page.title))

    # ---------------- txt 的日期来自文件名 ----------------
    print("\n== 元信息推断 ==")
    from core.frontmatter import collect_meta, split_frontmatter
    t = convert.read_as_markdown(VAULT / "2026-09-02 txt 篇.txt")
    fm, body = split_frontmatter(t)
    meta = collect_meta(VAULT / "2026-09-02 txt 篇.txt", fm, body, ["date"], ["title"])
    ok &= check("txt 日期取自文件名", meta.date is not None and meta.date.strftime("%Y-%m-%d") == "2026-09-02",
                str(meta.date))
    ok &= check("txt 标题取自文件名", meta.title == "2026-09-02 txt 篇", meta.title)

    t = convert.read_as_markdown(VAULT / "2026-09-04 html 篇.html")
    fm, body = split_frontmatter(t)
    meta = collect_meta(VAULT / "2026-09-04 html 篇.html", fm, body, ["date"], ["title"])
    ok &= check("html 标题取自 <title>", meta.title == "七月十九日 · 某个网页", meta.title)
    ok &= check("html 日期取自文件名", meta.date is not None and meta.date.strftime("%Y-%m-%d") == "2026-09-04",
                str(meta.date))

    # ---------------- 坏文件不该拖垮整趟扫描 ----------------
    print("\n== 坏文件容错 ==")
    bad = VAULT / "2026-09-05 坏的.docx"
    bad.write_bytes(b"PK\x03\x04 broken")
    p = Pipeline(make_cfg(["md", "docx"]))
    items = p.scan()
    bad_items = [i for i in items if Path(i.rel).name == bad.name]
    # vault 里有 6 个文件，但只开了 md / docx：md + 好的 docx + 坏的 docx = 3
    ok &= check("坏 docx 没让扫描崩掉", len(items) == 3, f"共 {len(items)} 条")
    ok &= check("坏 docx 被标成失败", bool(bad_items) and bad_items[0].action == "fail",
                bad_items[0].action if bad_items else "没找到")
    ok &= check("失败原因写清楚了",
                bool(bad_items) and "读取失败" in bad_items[0].reason,
                bad_items[0].reason if bad_items else "")
    ok &= check("同一个坏文件不影响别的文件",
                any(Path(i.rel).name.endswith(".md") and i.action == "create" for i in items))

    print()
    print("[OK] 混合格式端到端体检全部通过" if ok else "[NG] 有断言没过，看上面的 NG 行")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
