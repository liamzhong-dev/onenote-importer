# -*- coding: utf-8 -*-
"""把导入失败留下的空页挑出来（默认只列，不动手）。

2026-09-21 13:55 那次导入，页面建出来了、内容一个字没写进去，
于是在 2026夏 里留下 9 个「未命名」空页。滚回机制已经补上（以后不会再留），
但已经产生的这批得清掉。

用法：
    python tools/cleanup_blank_pages.py                        # 只列出空页，什么都不删
    python tools/cleanup_blank_pages.py --notebook MyNotebook --all-sections
    python tools/cleanup_blank_pages.py --notebook MyNotebook --all-sections --yes   # 真删

**两道保险，都是踩过之后补的：**

1. `--after`：空页的标题都叫「未命名」，光看标题分不出「工具留下的」和
   「你自己建了没写的」。加一道时间过滤，只动那个时间点之后产生的。
2. **读一遍正文再决定**（2026-10-01 补）：标题为空**不足以**判定该删。
   实测 19 个笔记本里标题为空的有 15 页，其中 5 页里面**有一张图**——
   那是用户自己没写标题的页，只按标题筛会连它们一起删掉。
   现在会真读页面 XML，确认「没正文、也没图」才动手；读不回来一律不删。

**回收站分区（「删除的页面」）整段跳过**：那里面的页已经是删过的，
对它们调 delete_page 是**永久删除**，不是"再删一次"。

删掉的页面会进 OneNote 回收站，不是彻底消失。
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import Config                    # noqa: E402
from core.proc import force_utf8_console          # noqa: E402
from writers.com import ComWriter                 # noqa: E402

BLANK_TITLES = {"", "未命名", "无标题", "untitled", "new page", "新建页面"}

# OneNote 的「删除的页面」会作为一个分区被 list_sections 列出来。
# 它里面的页面已经是回收站里的东西了 —— 对它们再调 delete_page 是**永久删除**，
# 不是"再删一次"。清理空页绝不能碰这里，所以按名字整段跳过。
RECYCLE_SECTION_NAMES = {"删除的页面", "已删除页面", "deleted pages", "recycle bin", "trash", "ゴミ箱"}

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
# 内嵌媒体的 base64 是元素的**文本**（在 <one:Data> 标签里），剥标签剥不掉：
# 一张图能数出几十万字。所以先整段挖掉；用反向引用保证开闭标签同源。
_BINARY_BLOCK = re.compile(
    r"<(?P<ns>[\w]*:?)(?:Data|Binary)\b[^>]*>.*?</(?P=ns)(?:Data|Binary)>",
    re.S | re.I)
# 媒体元素本身（图片 / 手写笔迹 / 内嵌文件），要单独数
_MEDIA_EL = re.compile(r"<[\w]*:?(?:Image|InkDrawing|InkWord|MediaFile)\b", re.I)


def page_stats(xml: str) -> tuple[int, int]:
    """返回 (去掉标题后的可见正文字数, 媒体元素个数)。

    两个数必须分开看，缺一个都会误伤：
      · 不挖 base64 → 一张图 = 几十万字，把"有图没字"算成"写了七十万字"
      · 挖了不数媒体 → 又分不清"只剩一张图"和"真的什么都没有"
    """
    xml = xml or ""
    media = len(_MEDIA_EL.findall(xml))
    txt = _BINARY_BLOCK.sub(" ", xml)
    txt = _TAG.sub(" ", txt)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                 ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'")):
        txt = txt.replace(a, b)
    return max(0, len(_WS.sub(" ", txt).strip()) - 12), media


def is_recycle_section(name: str) -> bool:
    key = (name or "").strip().lower().replace(" ", "")
    return key in {n.replace(" ", "") for n in RECYCLE_SECTION_NAMES}


def really_blank(writer, page_id: str) -> tuple[bool, str]:
    """真去读一遍这一页，确认它**既没正文也没图**。

    只按标题判空是绝对不能直接删的：标题叫「未命名」的页里混着一大批
    "只有一张图"或者"忘了写标题"的页 —— 那是用户自己的东西。
    读不回来就一律当"不空"处理（宁可漏删）。
    """
    try:
        xml = writer.get_page_xml(page_id, retries=2)
    except Exception as e:  # noqa: BLE001
        return False, f"读不回正文（{type(e).__name__}），保守起见不删"
    text_len, media = page_stats(xml)
    if media:
        return False, f"里面有 {media} 个图/笔迹"
    if text_len >= 8:
        return False, f"里面有 {text_len} 个字的正文"
    return True, f"确实空（正文 {text_len} 字 · 媒体 {media} 个）"


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
    ap.add_argument("--all-sections", action="store_true",
                    help="扫这个笔记本下的所有分区（不指定 --section 时默认只看配置里那一个）")
    ap.add_argument("--after", default="", help="只处理这个时间之后创建的空页，如 2026-09-21 13:50")
    ap.add_argument("--yes", action="store_true", help="确认执行删除（不加则只列不动）")
    args = ap.parse_args()

    cfg = Config.load()
    nb_name = args.notebook or cfg.notebook_name
    if args.all_sections:
        # 显式要求全扫：不回落成配置里那一个分区，否则"全扫"其实是"只扫一个"
        sec_name = args.section
    else:
        sec_name = args.section or (cfg.section_fixed if cfg.section_mode == "fixed" else "")

    writer = ComWriter(cfg, log=lambda level, msg: None, step=lambda t: None)
    nb = writer.find_notebook(nb_name)
    if nb is None:
        print(f"找不到笔记本「{nb_name}」")
        return 1
    secs = [s for s in writer.list_sections(nb.id) if not sec_name or s.name == sec_name]
    skipped = [s for s in secs if is_recycle_section(s.name)]
    if skipped:
        print(f"跳过回收站分区：{'、'.join(s.name for s in skipped)}"
              f"（那里面的页面已经是删过的，再动就是永久删除）")
    secs = [s for s in secs if not is_recycle_section(s.name)]
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
        skipped_busy: list[tuple[dict, str]] = []
        for p in blanks:
            when = page_time(p.get("created", ""))
            if cutoff is not None and when is not None and when < cutoff:
                continue
            # 标题为空**不足以**判定该删 —— 必须真读一遍确认里面什么都没有。
            # 这一步是 2026-10-01 补的：此前只按标题筛，全量 --yes 会把
            # 「没写标题但里面有一张图/一段字」的页一起删掉。
            empty, why = really_blank(writer, p["id"])
            if not empty:
                skipped_busy.append((p, why))
                continue
            picked.append((p, when, why))

        print(f"\n分区「{sec.name}」共 {len(pages)} 页：标题为空的 {len(blanks)} 页；"
              f"其中**确认**是空页的 {len(picked)} 页"
              + (f"，有内容的 {len(skipped_busy)} 页已跳过" if skipped_busy else ""))
        for p, why in skipped_busy:
            print(f"     [留] {short_id(p['id'])} —— {why}")
        if not picked:
            continue
        for i, (p, when, why) in enumerate(picked, start=1):
            idx = next((n for n, q in enumerate(pages, 1) if q["id"] == p["id"]), 0)
            stamp = when.strftime("%m-%d %H:%M:%S") if when else f"（时间无法解析：{p.get('created')!r}）"
            print(f"  {i:2d}. 第 {idx} 页  {stamp}  id={short_id(p['id'])}  {why}")

        if not args.yes:
            print("   → 只列不删。确认无误后加 --yes 再跑一次。")
            continue
        for p, _, _ in picked:
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
