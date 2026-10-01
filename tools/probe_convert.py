# -*- coding: utf-8 -*-
"""转换层体检：现场造一份 .docx / .txt / .html，再走一遍转换。

不依赖任何外部素材 —— 测试夹具是当场用 zipfile 现拼的 .docx
（见 tests/docx_fixture.py），所以这个脚本在任何机器上都能跑，
不会因为"样本文件找不到"而假绿。

    python tools/probe_convert.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# 配置目录指到临时区，免得写脏用户的 %APPDATA%
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="convertprobe-ap-")

from core import convert  # noqa: E402
from tests.docx_fixture import HTML, build_docx  # noqa: E402


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f'  {"OK " if ok else "NG "} {name}' + (f"  —— {detail}" if detail else ""))
    return ok


def main() -> int:
    td = Path(tempfile.mkdtemp(prefix="convertprobe-"))
    ok = True

    # ---------------------------------------------------------- 纯文本
    print("== 纯文本 .txt ==")
    p = td / "2026-09-07 日记.txt"
    p.write_bytes("第一行\n\n\n\n第二段\n带缩进的第三行\n".encode("gb18030"))
    txt = convert.read_as_markdown(p)
    print("   转换结果 repr:", repr(txt))
    ok &= check("GBK 编码能正确读出中文", "第二段" in txt,
                txt.splitlines()[1] if len(txt.splitlines()) > 1 else "")
    ok &= check("连续空行压成一个", "\n\n\n" not in txt)
    ok &= check("换行保留", "第一行" in txt and "第二段" in txt)

    # UTF-8 BOM
    p2 = td / "utf8bom.txt"
    p2.write_bytes("\ufeff带 BOM 的内容\n".encode("utf-8"))
    ok &= check("UTF-8 BOM 不会留下乱码",
                convert.read_as_markdown(p2).strip() == "带 BOM 的内容",
                repr(convert.read_as_markdown(p2).strip()))

    # txt 开头的 --- 只是分割线，不能被当成 frontmatter 吃掉内容
    p3 = td / "hr.txt"
    p3.write_bytes("---\n第一段\n---\n第二段\n".encode("utf-8"))
    t3 = convert.read_as_markdown(p3)
    ok &= check("txt 原样保留 --- 行", "第一段" in t3 and "第二段" in t3, repr(t3))

    # ---------------------------------------------------------- Word
    print("\n== Word .docx ==")
    dx = td / "日记.docx"
    build_docx(dx)
    md = convert.read_as_markdown(dx)
    print("   ---- 转换结果 ----")
    for ln in md.splitlines():
        print("   |", ln)
    print("   ------------------")
    ok &= check("一级标题（styleId=1）", "# 九月七日" in md)
    ok &= check("二级标题（outlineLvl）", "## 傍晚" in md)
    ok &= check("加粗", "**很重要**" in md)
    ok &= check("斜体", "*一点*" in md)
    ok &= check("删除线", "~~删掉的~~" in md)
    ok &= check("外链", "[链接](https://example.com/x)" in md)
    ok &= check("无序列表", "- 无序一" in md and "- 无序二" in md)
    ok &= check("有序列表重新从 1 开始", "1. 有序一" in md and "2. 有序二" in md)
    ok &= check("软换行", "换行在  \n下一行" in md or "换行在  \n" in md)
    ok &= check("表格表头", "| 项目 | 值 |" in md)
    ok &= check("表格分隔行", "| --- | --- |" in md)
    ok &= check("单元格竖线被转义", "含\\|竖线" in md)
    ok &= check("内容控件里的文字没丢", "内容控件里的字也要在" in md)
    img_paths = [ln for ln in md.splitlines() if ln.startswith("![")]
    ok &= check("图片被抽出来", bool(img_paths), img_paths[0] if img_paths else "没找到")
    if img_paths:
        real = img_paths[0][img_paths[0].index("(") + 1:-1]
        ok &= check("图片文件真实存在且非空",
                    Path(real).exists() and Path(real).stat().st_size > 0, real)
        ok &= check("图片没写进来源目录", str(td) not in real)

    # 坏 docx：改名冒充的
    print("\n== 容错 ==")
    bad = td / "假的.docx"
    bad.write_bytes(b"PK\x03\x04 not really a docx")
    try:
        convert.read_as_markdown(bad)
        ok &= check("坏 docx 抛 ConvertError", False, "居然没抛")
    except convert.ConvertError as e:
        ok &= check("坏 docx 抛 ConvertError", True, str(e))
    except Exception as e:  # noqa: BLE001
        ok &= check("坏 docx 抛 ConvertError", False, f"抛的是 {type(e).__name__}: {e}")

    # 超长正文要截断，否则巨大的网页会写出写不进去的 OneNote XML
    big = td / "巨长.txt"
    big.write_text("字" * (convert.MAX_TEXT_CHARS + 5000), encoding="utf-8")
    long_text = convert.read_as_markdown(big)
    ok &= check("超长正文被截断", len(long_text) < convert.MAX_TEXT_CHARS + 500,
                f"{len(long_text)} 字符")
    ok &= check("截断处有提示", "已截断" in long_text)

    # ---------------------------------------------------------- 网页
    print("\n== 网页 .html ==")
    hp = td / "page.html"
    hp.write_text(HTML, encoding="utf-8")
    hm = convert.read_as_markdown(hp)
    print("   ---- 转换结果 ----")
    for ln in hm.splitlines():
        print("   |", ln)
    print("   ------------------")
    ok &= check("title 变成一级标题", hm.startswith("# 七月十九日"))
    ok &= check("script 内容被丢掉", "var x" not in hm)
    ok &= check("style 内容被丢掉", "font:14px" not in hm)
    ok &= check("nav 被丢掉", "关于" not in hm)
    ok &= check("footer 被丢掉", "版权" not in hm)
    ok &= check("h1 保留", "## 正文标题" in hm or "# 正文标题" in hm)
    ok &= check("加粗", "**加粗**" in hm)
    ok &= check("斜体", "*斜体*" in hm)
    ok &= check("外链保留", "[外链](https://example.com/a)" in hm)
    ok &= check("站内相对链接只留文字", "站内链接" in hm and "](/relative)" not in hm)
    ok &= check("无序列表", "- 条目甲" in hm)
    ok &= check("有序列表", "1. 第一" in hm and "2. 第二" in hm)
    ok &= check("引用", "> 引用的一句话。" in hm)
    ok &= check("代码块", "```" in hm and 'print("hello")' in hm)
    ok &= check("分割线", "\n---\n" in hm or hm.rstrip().endswith("---"))
    ok &= check("表格", "| 列一 | 列二 |" in hm and "| a | b |" in hm)
    ok &= check("图片保留", "![照片](images/photo.jpg)" in hm)
    ok &= check("占位像素图被丢掉", "spacer.gif" not in hm)

    # ---------------------------------------------------------- 格式登记
    print("\n== 格式登记 ==")
    ok &= check("format_of(.md)", convert.format_of("a/b/c.md") == "md")
    ok &= check("format_of(.markdown)", convert.format_of("x.markdown") == "md")
    ok &= check("format_of(.docx)", convert.format_of("x.DOCX") == "docx")
    ok &= check("format_of(.html)", convert.format_of("x.htm") == "html")
    ok &= check("format_of 不认识的后缀", convert.format_of("x.pdf") is None)
    ok &= check("exts_for 全集",
                convert.exts_for(["md", "txt", "docx", "html"]) == set(convert.SOURCE_EXTS))
    ok &= check("exts_for 空值兜底成 md", convert.exts_for([]) == {".md", ".markdown"})
    ok &= check("normalize_formats 去非法值", convert.normalize_formats(["txt", "pdf", "TXT"]) == ["txt"])
    ok &= check("normalize_formats 空值兜底", convert.normalize_formats([]) == ["md"])

    print()
    print("[OK] 转换层体检全部通过" if ok else "[NG] 有断言没过，看上面的 NG 行")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
