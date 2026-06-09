# -*- coding: utf-8 -*-
"""真实写入试验：在**临时分区**里建一页、写内容、读回核对，最后删干净。

为什么要有这个工具：OneNote 的 COM 写入是「写错一个字整页被拒」的严格接口，
假通道测不出 schema 对不对。2026-09-21 那次「提示导入成功、实际一个字没写进去」
就是 <one:Title> 的结构写错了，只有真跑一次才会暴露。

它只碰自己建的分区（名字带 zz- 前缀、明确写着「可删」），
跑完会把页面和分区都删掉，不会动你的任何笔记本内容。
"""
from __future__ import annotations

import base64
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import Config                                  # noqa: E402
from core.onenote_html import MediaPart, RenderedPage            # noqa: E402
from core.proc import force_utf8_console                         # noqa: E402
from writers.com import ComWriter, build_page_xml                # noqa: E402

SCRATCH = "zz-导入写入自检-可删"

# 4x4 纯色 PNG，用来验证 <one:Image> 那条路径
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAYAAACp8Z5+AAAAF0lEQVQImWNkYGD4z0AEYBxVSF+"
    "FjAwMDAwAOh0D/wAAAABJRU5ErkJggg=="
)

BODY = """<html><head><title>t</title></head><body>
<div data-id="content-root">
<h2>标题二</h2>
<p>普通段落，带 <strong>加粗</strong> 与 <em>斜体</em>。</p>
<ul>
  <li>一级项 A</li>
  <li>一级项 B
    <ul><li>二级项 B1</li><li>二级项 B2</li></ul>
  </li>
</ul>
<ol><li>有序一</li><li>有序二</li></ol>
<table>
  <tr><th>列一</th><th>列二</th></tr>
  <tr><td>a</td><td>b</td></tr>
</table>
<blockquote>引用一行</blockquote>
<pre>code line</pre>
<hr/>
<p><img src="name:imageBlock0" width="64" /></p>
<p>结尾一句。</p>
</div></body></html>"""


def visible_text(xml: str) -> str:
    """回读到的 XML 里「人眼能看到的文字」，标签和空白全部去掉。

    为什么不直接 `"一级项 A" in xml`：OneNote 会把同一句话按样式拆成好几个
    <span>（写着写着字体断了就断一次），还会在标签内部换行。于是原文
    「一级项 A」在回读里长成 `一级项</span><span ...> A` —— 按原样找永远找不到，
    明明写进去了却报「没写进去」。比「连起来读是什么」才靠谱。
    """
    text = "\n".join(re.findall(r"<!\[CDATA\[(.*?)\]\]>", xml, flags=re.S))
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", "", text)


def has(haystack: str, needle: str) -> bool:
    """在归一化后的文字里找 needle —— needle 里的空白也要一起归一化。

    否则 `has(seen, "code line")` 永远找不到「code line」（归一化后是 "codeline"）。
    """
    return re.sub(r"\s+", "", needle) in haystack


def main() -> int:
    force_utf8_console()
    ok = True

    def say(mark: str, text: str):
        print(f"  {mark} {text}")

    cfg = Config.load()
    writer = ComWriter(cfg, log=lambda level, msg: print(f"      [{level}] {msg}"),
                       step=lambda t: print(f"      … {t}"))

    nb = writer.find_notebook(cfg.notebook_name)
    if nb is None:
        names = []
        try:
            names = [n.name for n in writer.list_notebooks()]
        except Exception:  # noqa: BLE001 — 列不出来不影响主判断，只少一点提示信息
            pass
        say("NG", f"找不到笔记本「{cfg.notebook_name}」")
        if names:
            say("--", "本机 OneNote 里的笔记本：" + "、".join(names))
        say("--", "先在界面里选好目标笔记本（或把它的名字填进配置）再跑这个自检。")
        return 1
    say("OK", f"目标笔记本：{nb.name}")

    # 先看看有没有上次跑崩留下的临时分区
    stale = writer.find_section(nb.id, SCRATCH)
    if stale:
        say("--", "发现上次残留的临时分区，先清掉")
        writer.delete_page(stale.id)

    sec = writer.ensure_section(nb.id, SCRATCH)
    say("OK", f"临时分区已就绪：{sec.name}")

    tmpdir = Path(tempfile.mkdtemp(prefix="onenote-scratch-"))
    img = tmpdir / "dot.png"
    img.write_bytes(TINY_PNG)

    page = RenderedPage(
        title="自检页面（可删）",
        html=BODY,
        media=[MediaPart("imageBlock0", img, "image/png", "image", len(TINY_PNG))],
    )

    page_id = ""
    try:
        page_id = writer.create_page(sec.id, page)
        say("OK", f"新建页面成功：{page_id}")

        back = writer.get_page_xml(page_id) or ""
        dump = ROOT / "sample-output" / "scratch-readback.xml"
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_text(back, encoding="utf-8")
        seen = visible_text(back)
        checks = [
            ("标题写进去了", has(seen, "自检页面（可删）")),
            ("段落写进去了", has(seen, "普通段落")),
            ("加粗保留", "font-weight" in back),
            ("斜体保留", "font-style:italic" in back),
            ("列表写进去了", has(seen, "一级项 A") and has(seen, "一级项 B")),
            ("二级列表用了嵌套结构", "<one:OEChildren>" in back and has(seen, "二级项 B1")),
            ("有序列表写进去了", has(seen, "有序一") and has(seen, "有序二")),
            ("表格写进去了", has(seen, "列一") and has(seen, "列二")),
            ("引用写进去了", has(seen, "引用一行")),
            ("代码块写进去了", has(seen, "code line")),
            ("图片写进去了", "<one:Image" in back and "<one:Data>" in back),
            ("没有残留的 indentation 属性", "indentation=" not in back),
            ("只保留一条 outline（没有多出空行）", back.count("<one:Outline") == 1),
        ]
        for name, good in checks:
            say("OK" if good else "NG", name)
            ok = ok and good
        if not all(g for _, g in checks):
            i = back.find("<one:Outline")
            say("--", f"回读到的正文段（完整内容见 {dump}）：\n{back[i:i + 2200]}")

        # 更新路径也要过一遍：替换内容后旧文字必须消失
        page2 = RenderedPage(title="自检页面（可删）", html=BODY.replace("结尾一句。", "改过的一句话。"), media=[])
        writer.update_page_content(page_id, page2)
        back2 = writer.get_page_xml(page_id) or ""
        seen2 = visible_text(back2)
        upd = [("更新后新内容在", has(seen2, "改过的一句话")),
               ("更新后旧内容已替换", not has(seen2, "结尾一句")),
               ("更新后仍只有一条 outline", back2.count("<one:Outline") == 1)]
        for name, good in upd:
            say("OK" if good else "NG", name)
            ok = ok and good

        # 读不到底稿时**必须拒绝写入**，不能硬写。
        # 硬写的后果：新 outline 没有 objectID，UpdatePageContent 只会把它追加进去，
        # 页面上就多出第二条大纲、旧内容还留着 —— 正是要修掉的那个 bug。
        # 这条路径以前用桥里的 set-page（先按 objectID 删大纲再写）兜底，
        # 实测发现根本删不掉：DeleteHierarchy 只接受笔记本/分区/页面这类结构对象，
        # 页面里的 outline 它管不着。所以现在改成明确报错、页面原样不动。
        real_get = writer.get_page_xml
        writer.get_page_xml = lambda pid: None          # type: ignore[assignment]
        raised = ""
        try:
            writer.update_page_content(page_id, RenderedPage(
                title="自检页面（可删）",
                html=BODY.replace("结尾一句。", "不该被写进去的一句。"), media=[]))
        except Exception as e:  # noqa: BLE001
            raised = f"{type(e).__name__}: {e}"
        finally:
            writer.get_page_xml = real_get              # type: ignore[assignment]
        back3 = writer.get_page_xml(page_id) or ""
        seen3 = visible_text(back3)
        fb = [("兜底路径：拿不到底稿时拒绝写入", bool(raised)),
              ("兜底路径：没把不该写的写进去", not has(seen3, "不该被写进去的一句")),
              ("兜底路径：页面内容原样保留", has(seen3, "改过的一句话")),
              ("兜底路径：仍然只有一条 outline", back3.count("<one:Outline") == 1)]
        for name, good in fb:
            say("OK" if good else "NG", name)
            ok = ok and good
        if raised:
            say("--", f"拒绝写入时抛的错：{raised}")
    finally:
        if page_id:
            try:
                writer.delete_page(page_id)
                say("--", "自检页面已删除")
            except Exception as e:  # noqa: BLE001
                say("NG", f"删除自检页面失败：{e}")
                ok = False
        try:
            writer.delete_page(sec.id)
            say("--", "临时分区已删除")
        except Exception as e:  # noqa: BLE001
            say("NG", f"删除临时分区失败：{e}")
            ok = False

    left = [s.name for s in writer.list_sections(nb.id)]
    if SCRATCH in left:
        say("NG", "临时分区还在，请手动删掉")
        ok = False
    else:
        say("OK", "环境已还原，笔记本里没留下任何自检痕迹")

    print("\n" + ("全部通过：COM 写入链路可用" if ok else "有失败项，见上面 NG"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
