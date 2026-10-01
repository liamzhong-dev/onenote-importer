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
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import Config                                  # noqa: E402
from core.onenote_html import MediaPart, RenderedPage            # noqa: E402
from core.proc import force_utf8_console                         # noqa: E402
from writers.base import PageGone, WriterError                    # noqa: E402
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
        # 签名要跟真实方法一致（带 **kw）：漏了的话会抛 TypeError，
        # 被 _page_xml 收进 StructureUnavailable —— 断言照样「通过」，
        # 但验的已经不是「读回是空」，而是「调用写错了」。别让它悄悄跑偏。
        writer.get_page_xml = lambda pid, **kw: None     # type: ignore[assignment]
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

        # 「页面被人在 OneNote 里删掉了」这种情况必须能被认出来。
        # 真实现场：日记页被人手删之后（它会挪进分区「删除的页面」），
        # 工具还拿着状态库里的旧 ID 去 GetPageContent，
        # OneNote 回 HRESULT 0x80042014（对象不存在），整篇判失败 ——
        # 点多少次都一样。现在这个错误必须翻译成 PageGone，
        # 上层才能「清掉记账 + 重新新建」。
        # 真机才测得到这条：假的通道编不出 OneNote 那个 HRESULT。
        ghost_id = ""
        try:
            ghost_id = writer.create_page(sec.id, RenderedPage(
                title="自检幽灵页（可删）",
                html="<html><head><title>g</title></head><body>"
                     "<div data-id='content-root'><p>马上会被删掉</p></div></body></html>",
                media=[]))
            writer.delete_page(ghost_id)
            time.sleep(1.0)      # 让 OneNote 把删除落实
            ghost_err: Exception | None = None
            try:
                writer.update_page_content(ghost_id, RenderedPage(
                    title="自检幽灵页（可删）", html="<p>不该被写进去</p>", media=[]))
            except Exception as e:  # noqa: BLE001
                ghost_err = e
            gc = [("页面被删后再改它：认成「页面已不存在」", isinstance(ghost_err, PageGone)),
                  ("该错误属于通道失败的一种，不再是无法处置的异常",
                   isinstance(ghost_err, WriterError))]
            # 再确认那一页是真没了（读都读不回来），不是我们看花了眼
            try:
                still = writer.get_page_xml(ghost_id)
                gc.append(("页面确实已经不在（读回也是空/报错）", not still))
            except PageGone:
                gc.append(("页面确实已经不在（读回也报「不存在」）", True))
            except Exception as e:  # noqa: BLE001
                gc.append((f"读回不该是别的错（实际 {type(e).__name__}: {e}）", False))
            for name, good in gc:
                say("OK" if good else "NG", name)
                ok = ok and good
            say("--", f"幽灵页返回的错：{type(ghost_err).__name__}: {ghost_err}")
            ghost_id = ""        # 已经删过了，收尾不用再删
        except Exception as e:  # noqa: BLE001
            say("NG", f"「页面被删」这条路径没能验证：{type(e).__name__}: {e}")
            ok = False

        # 「页面被删、本地文件又没改过」也必须能自己爬出来。
        # 状态库只记「上次写成功了」；页面被手删之后本地文件一个字没动 →
        # 哈希一致 → 旧代码判 skip → 压根不碰 OneNote → 那一页永远回不来，
        # 用户只能自己去回收站还原。这里拿真实通道 + 真实分区清单跑全链路。
        try:
            from core.pipeline import Pipeline

            vault = Path(tempfile.mkdtemp(prefix="onenote-scratch-vault-"))
            (vault / "2026-09-09.md").write_text(
                "# 冒烟\n\n这一篇用来验证「页面被删之后能自己重建」。\n", encoding="utf-8")

            pcfg = Config.load()
            pcfg.vault_path = str(vault)
            pcfg.section_mode = "fixed"
            pcfg.section_fixed = SCRATCH
            pcfg.title_template = "{M}月{D}日"
            pcfg.update_existing = True
            pcfg.throttle_ms = 0
            pcfg.state_path = str(vault / "state.sqlite3")

            pe = Pipeline(pcfg, log=lambda msg, level="INFO": None)
            pe.writer = writer
            items_e = pe.scan()
            pe.inspect_targets(items_e)
            s1 = pe.run(items_e, dry_run=False)
            recs = pe.state.all()
            first_pid = next(iter(recs.values())).page_id if recs else ""

            # 模拟「用户把这页删了」—— 它会挪进分区「删除的页面」
            if first_pid:
                writer.delete_page(first_pid)
                time.sleep(1.5)

            # 本地文件一字未改：这一刻本来就该判「跳过」
            was = [it.action for it in pe.scan()]

            # 再点一次「开始导入」：核对现场后就该发现那一页没了
            logs2: list[str] = []
            pe2 = Pipeline(pcfg, log=lambda msg, level="INFO": logs2.append(msg))
            pe2.writer = writer
            items2 = pe2.scan()
            pe2.inspect_targets(items2)
            after = [it.action for it in items2]
            s2 = pe2.run(items2, dry_run=False)
            recs2 = pe2.state.all()
            new_pid = next(iter(recs2.values())).page_id if recs2 else ""

            e2e = [
                ("端到端①：首次导入确实建了页面", s1["created"] == 1 and bool(first_pid)),
                ("端到端②：本地没改时先判成「跳过」", was == ["skip"]),
                ("端到端③：那一页被删后不再当「跳过」", after != ["skip"]),
                ("端到端④：自动重新新建且没记失败", s2["created"] == 1 and s2["failed"] == 0),
                ("端到端⑤：记账换成了新的页面 ID",
                 bool(new_pid) and new_pid != first_pid),
                ("端到端⑥：日志讲清了为什么不再跳过",
                 any("已不在 OneNote" in m for m in logs2)),
            ]
            for name, good in e2e:
                say("OK" if good else "NG", name)
                ok = ok and good
            if new_pid:
                writer.delete_page(new_pid)
                say("--", "端到端验证重建出来的页面已删除")
        except Exception as e:  # noqa: BLE001
            say("NG", f"「页面被删后自动重建」这条路径没能验证：{type(e).__name__}: {e}")
            ok = False
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
