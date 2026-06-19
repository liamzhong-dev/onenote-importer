# -*- coding: utf-8 -*-
"""无 OneNote 环境下的自检：用样例 Vault 跑完整渲染管线，把结果 HTML 落到 out/。

运行方式（不需要任何凭据，也不写 OneNote）：
    python tests/selftest.py
"""
from __future__ import annotations

import ast
import base64
import os
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import Config  # noqa: E402
from core.pipeline import CREATE, Pipeline, resolve_section_name, section_name_for  # noqa: E402
from core.proc import force_utf8_console, run_script, silent_kwargs  # noqa: E402
from writers.base import NotebookRef, OneNoteWriter, ProbeResult, SectionRef  # noqa: E402

# 必须在任何 print 之前：中文 Windows 默认 GBK，输出里带一个替换字符就整脚本崩
force_utf8_console()

# check_inspect_targets() 要复用 main() 建好的样例库
_SELFTMP_VAULT = ""

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

SAMPLE_WITH_FM = """---
date: 2026-09-07
title: 周末随笔
tags: [随笔, 生活]
mood: 平静
---

今天把拖了很久的一件事收尾了，**关键是**先把边界想清楚再动手。

> [!warning] 关于沉没成本
> 一件事情做久了，就会因为投入太多而舍不得放手。但其实沉没成本不该进决策。

- [x] 查一遍资料
- [ ] 写一份说明文档
  - 先列清单
  - 再约 `评审` 过一遍

1. 早上六点半醒
2. 开会到中午

| 项目 | 进度 |
| --- | --- |
| 甲 | 90% |
| 乙 | 40% |

```python
def why(sunk_cost):
    return sunk_cost not in decision
```

![[sea.png|420]]

今天 [[2026-09-06]] 提到的那件事，先放着。#随笔
"""

SAMPLE_PLAIN = """# 没有 frontmatter 的一天

早上醒来，窗外在下雨。

> 引用一句话试试
> 第二行还在引用里

- 列表一
  - 嵌套项
- 列表二

引用一条外部图：![alt](https://example.com/a.png)

还有一个不存在的附件：![[missing-doc.pdf]]
"""


def build_sample_vault(base: Path) -> Path:
    base = Path(base)
    if base.exists():
        shutil.rmtree(base)
    (base / "日记" / "2026" / "assets").mkdir(parents=True, exist_ok=True)
    (base / "日记" / "2026-09-07.md").write_text(SAMPLE_WITH_FM, encoding="utf-8")
    (base / "日记" / "2026" / "2026-09-08.md").write_text(SAMPLE_PLAIN, encoding="utf-8")
    (base / "日记" / "2026" / "assets" / "sea.png").write_bytes(PNG_1PX)
    (base / "日记" / "2026" / "assets" / "note.pdf").write_bytes(b"%PDF-1.4 fake")
    (base / ".obsidian").mkdir(exist_ok=True)
    return base


def check_section_strategy() -> list[str]:
    """分区策略自检：季节占位符、fixed 模式、跨年冬季归属。"""
    import datetime as dt

    from core.pipeline import resolve_section_name

    failed = []
    cases = [
        # (模板或 fixed 名, 日期, 期望)
        ("{YYYY}{season}", dt.datetime(2026, 6, 22), "2026夏"),
        ("{YYYY}{season}", dt.datetime(2026, 11, 3), "2026秋"),
        ("{YYYY}年{MM}月", dt.datetime(2026, 9, 7), "2026年09月"),
        # {YYYY} 取「那一天所在的自然年」：2026-01 就是 2026冬
        ("{YYYY}{season}", dt.datetime(2026, 1, 5), "2026冬"),
        # 跨年冬季要归到「冬天开始的那一年」时用 {season_year}
        ("{season_year}{season}", dt.datetime(2026, 1, 5), "2025冬"),
        ("{season_year}{season}", dt.datetime(2026, 2, 28), "2025冬"),
        ("{season_year}{season}", dt.datetime(2026, 12, 30), "2026冬"),   # 12 月是新一冬的开始
        ("{season_year}{season}", dt.datetime(2026, 3, 1), "2026春"),
    ]
    for tpl, date, want in cases:
        got = section_name_for(tpl, date, "x")
        print(f"  {'OK ' if got == want else 'NG '} {tpl} @ {date:%Y-%m-%d} → {got}")
        if got != want:
            failed.append(f"{tpl} @ {date} 得到 {got}，期望 {want}")

    # fixed 模式：无论日期是什么，都只返回那一个分区名
    cfg = Config()
    cfg.section_mode = "fixed"
    cfg.section_fixed = "2026夏"
    got = resolve_section_name(cfg, dt.datetime(2026, 9, 7), "x")
    print(f"  {'OK ' if got == '2026夏' else 'NG '} fixed 模式 → {got}")
    if got != "2026夏":
        failed.append(f"fixed 模式得到 {got}，期望 2026夏")

    # fixed 名为空时不要退化成空字符串，应该回落到模板
    cfg.section_fixed = ""
    cfg.section_template = "{YYYY}-{MM}"
    got = resolve_section_name(cfg, dt.datetime(2026, 9, 7), "x")
    print(f"  {'OK ' if got == '2026-09' else 'NG '} fixed 留空时回落模板 → {got}")
    if got != "2026-09":
        failed.append(f"fixed 留空时得到 {got}，期望回落成 2026-09")
    return failed


class FakeWriter(OneNoteWriter):
    """假的写入通道：模拟「笔记本里已有一个分区，分区里已经有几页」。

    用它验证「写进已有分区而不是另建」这条链路都不用连 OneNote。
    """

    name = "fake"

    def __init__(self, sections: dict[str, dict[str, str]]):
        # {分区名: {页面标题: page_id}}
        super().__init__(Config(), log=None)
        self.sections = sections
        self.created: list[str] = []
        self.created_pages: list[tuple[str, str]] = []   # (分区, 页面标题)
        self.updated: list[str] = []
        # 打开它就能模拟「通道说写成功了，其实页面上什么都没有」——
        # 用来验证写入后回读核对真的会发现问题。
        self.drop_pages = False

    def probe(self):
        return ProbeResult(True, "假通道")

    def list_notebooks(self):
        return [NotebookRef("nb-1", "日记")]

    def ensure_notebook(self, display_name):
        return NotebookRef("nb-1", display_name)

    def list_sections(self, notebook_id):
        return [SectionRef(f"sec-{n}", n) for n in self.sections]

    def ensure_section(self, notebook_id, section_name):
        if section_name not in self.sections:
            self.sections[section_name] = {}
            self.created.append(section_name)
        return SectionRef(f"sec-{section_name}", section_name)

    def list_pages(self, section_id):
        name = section_id.replace("sec-", "", 1)
        return [(pid, title) for title, pid in self.sections.get(name, {}).items()]

    def create_page(self, section_id, page):
        name = section_id.replace("sec-", "", 1)
        pid = f"page-{page.title}"
        self.created_pages.append((name, page.title))
        if not self.drop_pages:
            # 真通道建完页，页面就会出现在分区里 —— 回读才能看见
            self.sections.setdefault(name, {})[page.title] = pid
        return pid

    def update_page_content(self, page_id, page, strategy="replace_div"):
        self.updated.append(page_id)

    def delete_page(self, page_id):
        return None


def check_inspect_targets() -> list[str]:
    """已有分区/同名页面的识别：这才是「接在后面写」的关键。

    每次都用全新的临时状态库，保证连跑两次结果一致。
    """
    failed = []

    def fresh_cfg(policy: str) -> Config:
        cfg = Config()
        cfg.section_mode = "fixed"
        cfg.section_fixed = "2026夏"
        cfg.existing_page_policy = policy
        cfg.title_template = "{M}月{D}日"
        cfg.vault_path = _SELFTMP_VAULT
        cfg.state_path = str(tempfile.mkdtemp(prefix="importer-state-") + "/state.sqlite3")
        return cfg

    def scan_with(cfg: Config, existing_pages: dict[str, dict[str, str]]):
        pipe = Pipeline(cfg, log=lambda level, msg: None)
        items = pipe.scan()
        pipe.writer = FakeWriter(existing_pages)
        pipe.inspect_targets(items)
        return pipe, items

    # ---- 场景：OneNote 里已有「2026夏」，里面一篇本地日记的同名页都没有 ----
    cfg = fresh_cfg("update")
    _, items = scan_with(cfg, {"2026夏": {"6月21日": "p-old-1", "6月20日": "p-old-2"}})
    print("  场景①  分区已存在、里面没有同名页面：")
    for it in items:
        print(f"    {it.action:6s} | {it.section} | {it.title} | 已有分区={it.section_exists} "
              f"| 命中旧页={it.matched_page_id or '-'}")
        if not it.section_exists:
            failed.append(f"「{it.title}」没认出已有分区，会被当成新建分区")
        if it.action != "create":
            failed.append(f"「{it.title}」分区里没有同名页面，应为 create，实际 {it.action}")

    if not items:
        return failed or ["没扫描到任何笔记"]

    # ---- 场景：分区里已经有跟本地同名的那一页 ----
    dup_title = items[0].title
    cfg2 = fresh_cfg("update")
    pipe2, items2 = scan_with(cfg2, {"2026夏": {dup_title: "p-existing"}})
    hit = next(it for it in items2 if it.title == dup_title)
    print(f"  场景②  分区里已有同名页 {dup_title!r}：动作={hit.action} 命中={hit.matched_page_id}")
    if hit.matched_page_id != "p-existing":
        failed.append(f"已有同名页面没被识别，matched_page_id={hit.matched_page_id!r}")
    if hit.action != "update":
        failed.append(f"已有同名页面应为 update，实际 {hit.action}")

    # 真正写入一遍：不该再建分区，也不该再建那一页
    stats2 = pipe2.run(items2, dry_run=False)
    print(f"    实际写入新建={pipe2.writer.created_pages} 更新={pipe2.writer.updated} "
          f"新建分区={pipe2.writer.created}")
    if pipe2.writer.created:
        failed.append(f"往已有分区写时不该再建分区，实际建了：{pipe2.writer.created}")
    if (dup_title in [t for _, t in pipe2.writer.created_pages]):
        failed.append(f"「{dup_title}」已有同名页面，不该又新建一页")

    # 写完必须能回读核对 —— 这是「提示已导入、OneNote 里却找不到」的对策
    report = stats2.get("report") or []
    if not report:
        failed.append("非预览模式跑完没有产出「写入位置核对」报告")
    else:
        r = report[0]
        print(f"  场景⑤  回读核对：笔记本={r['notebook']!r} 分区={r['section']!r} "
              f"{r['before']} → {r['after']} 页，新增 {r['created']} / 更新 {r['updated']}，"
              f"未对上={r['missing']}")
        if r["missing"]:
            failed.append(f"回读时页面没对上：{r['missing']}")
        # 更新不会让页数变多，所以增量只应该等于「新建」的那几页
        if r["before"] is not None and r["after"] != r["before"] + r["created"]:
            failed.append(f"回读页数增量（{r['before']}→{r['after']}）与新建数（{r['created']}）不符")
        # 「在分区里排第几页」必须报出来，用户就是靠这个找的
        pos = r["positions"]
        print(f"          位置：{ {t: f'第 {p} 页' for t, p in pos.items()} }")
        if not pos or any(p <= 0 for p in pos.values()):
            failed.append(f"回读没给出页面在分区里的位置：{pos}")
        elif max(pos.values()) != r["after"]:
            failed.append(f"位置最大值（{max(pos.values())}）超过了分区页数（{r['after']}）")

    # 对照组：通道「吞掉」页面（说成功、其实没写进去），回读必须报出来
    cfg5 = fresh_cfg("update")
    pipe5, items5 = scan_with(cfg5, {"2026夏": {}})
    pipe5.writer.drop_pages = True
    stats5 = pipe5.run(items5, dry_run=False)
    report5 = stats5.get("report") or []
    caught = bool(report5) and any(r["missing"] for r in report5)
    print(f"  场景⑥  写入静默丢失时，回读能发现：{'OK' if caught else 'NG'}"
          f"（未对上 {sum(len(r['missing']) for r in report5)} 页）")
    if not caught:
        failed.append("页面实际没写进去时，回读核对没能发现问题（等于「已导入」是假的）")

    # ---- 场景：策略改成 skip，同名页要被跳过 ----
    cfg3 = fresh_cfg("skip")
    _, items3 = scan_with(cfg3, {"2026夏": {dup_title: "p-existing"}})
    hit3 = next(it for it in items3 if it.title == dup_title)
    print(f"  场景③  skip 策略下 {dup_title!r}：动作={hit3.action}")
    if hit3.action != "skip":
        failed.append(f"skip 策略下重名页面应跳过，实际 {hit3.action}")

    # ---- 场景：策略改成 create，同名页照建 ----
    cfg4 = fresh_cfg("create")
    _, items4 = scan_with(cfg4, {"2026夏": {dup_title: "p-existing"}})
    hit4 = next(it for it in items4 if it.title == dup_title)
    print(f"  场景④  create 策略下 {dup_title!r}：动作={hit4.action}")
    if hit4.action != "create":
        failed.append(f"create 策略下应照常新建，实际 {hit4.action}")

    return failed


def check_plan_signature() -> list[str]:
    """计划指纹：改了关键设置，之前那份计划就必须作废。

    不做这一步就会「预览按 A 分区、导入却写进 B」。这里只验算法，
    不碰界面；界面上的衔接在 tools/smoke_ui.py 里走查。
    """
    from app import plan_signature

    failed = []
    same_kind = Config()
    also_same = Config()
    if plan_signature(same_kind) != plan_signature(also_same):
        failed.append("两份相同配置的指纹不一致，会导致每次同步都重扫")
    print(f"  {'OK ' if not failed else 'NG '} 相同配置 → 指纹相同")

    variants = {
        "换分区方式": lambda c: setattr(c, "section_mode", "fixed"),
        "换固定分区名": lambda c: (setattr(c, "section_mode", "fixed"),
                                   setattr(c, "section_fixed", "2026夏")),
        "换分区模板": lambda c: setattr(c, "section_template", "{YYYY}{season}"),
        "换标题模板": lambda c: setattr(c, "title_template", "{M}月{D}日"),
        "换笔记本": lambda c: setattr(c, "notebook_name", "另一个"),
        "换重名策略": lambda c: setattr(c, "existing_page_policy", "skip"),
        "换日记目录": lambda c: setattr(c, "vault_path", "D:/other"),
    }
    base = plan_signature(Config())
    for name, mutate in variants.items():
        c = Config()
        mutate(c)
        if plan_signature(c) == base:
            failed.append(f"{name}之后指纹没变，旧计划会被继续沿用")
            print(f"  NG  {name} → 指纹没变")
        else:
            print(f"  OK  {name} → 指纹变化，旧计划作废")

    # 非关键设置不该让计划白重扫
    c = Config()
    c.throttle_ms = 999
    c.dry_run = not c.dry_run
    c.embed_images = not c.embed_images
    print(f"  {'OK ' if plan_signature(c) == base else 'NG '} 只改节流/预览/图片开关 → 指纹不变")
    if plan_signature(c) != base:
        failed.append("非关键设置也触发重扫，等于每次点导入都白扫一遍")
    return failed


def check_quiet_subprocess() -> list[str]:
    """每个子进程调用都必须带 silent_kwargs()。

    漏掉一个，用户就会看到蓝色控制台窗口「一闪而过」——他会以为后台在乱跑东西。
    这条规则靠人眼守不住，所以直接静态扫。
    """
    failed: list[str] = []
    targets = ["app.py", "cli.py"]
    targets += [str(p.relative_to(ROOT)) for p in sorted((ROOT / "writers").glob("*.py"))]
    wanted = {"subprocess.run", "subprocess.Popen", "subprocess.call",
              "subprocess.check_output", "subprocess.check_call"}
    for rel in targets:
        path = ROOT / rel
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _dotted(node.func) not in wanted:
                continue
            ok = any(
                (isinstance(kw.value, ast.Call) and _dotted(kw.value.func) == "silent_kwargs")
                or (isinstance(kw.value, ast.Name) and kw.value.id == "silent_kwargs")
                for kw in node.keywords
            )
            if not ok:
                failed.append(f"{rel}:{node.lineno} {_dotted(node.func)} 没带 silent_kwargs()，会闪黑窗口")
    if os.name == "nt":
        kw = silent_kwargs()
        if not kw.get("creationflags"):
            failed.append("silent_kwargs() 在 Windows 上没给出 CREATE_NO_WINDOW")
    print(f"  子进程调用点：{len(targets)} 个文件已扫")
    return failed


def _dotted(node) -> str:
    """把 ast.Attribute 还原成 'subprocess.run' 这种点号名字。"""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def check_log_adapter() -> list[str]:
    """Pipeline 内部用 (msg, level)，writer 用 (level, msg)，中间的适配不能反。

    反了不会报错，只会让日志变成一行行「· INFO」——很难发现，但一眼就能看出不对劲。
    """
    failed: list[str] = []
    tmp = Path(tempfile.gettempdir()) / "onenote-importer-selftest" / "log-adapter"
    cfg = Config()
    cfg.vault_path = _SELFTMP_VAULT or str(tmp / "vault")
    cfg.state_path = str(tmp / "state.sqlite3")
    seen: list[tuple[str, str]] = []
    pipe = Pipeline(cfg, log=lambda msg, level="INFO": seen.append((msg, level)))
    pipe._logfn()("WARN", "writer 说的一句警告")
    ok = seen == [("writer 说的一句警告", "WARN")]
    print(f"  {'OK ' if ok else 'NG '} writer→Pipeline 日志适配（实际 {seen}）")
    if not ok:
        failed.append("日志适配反了，日志会显示成「消息=级别」")
    pipe.state.close()
    return failed


TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000004000000040806000000a9f"
    "15a7e0000001749444154789c63646060f8cf4004601c55485f858c0c0c0c0c003a1d03ff0000000"
    "049454e44ae426082"
)

# 一份「OneNote 自己吐出来的」页面底稿，用来验证合并逻辑。
# 关键细节都按真实回读的样子写：outline 带 objectID（复用它是「就地替换」的关键），
# 还有 Position / Size 兄弟节点（不能因为换正文就把它们弄丢）；
# 两条 outline 是故意留的 —— 早期版本写坏过的页面就是这个样子，收成一条才算修好。
FAKE_BASE_PAGE = (
    '<?xml version="1.0"?>'
    '<one:Page xmlns:one="http://schemas.microsoft.com/office/onenote/2013/onenote" '
    'ID="OLD-ID" pageLevel="1" lastModifiedTime="2026-09-21T05:00:00.000Z">'
    '<one:PageSettings RTL="false" color="white" />'
    '<one:Title lang="zh-CN" style="font-size:20.0pt"><one:OE><one:T><![CDATA[]]></one:T></one:OE></one:Title>'
    '<one:Outline objectID="{AAA-111}">'
    '<one:Position x="36.0" y="86.0" z="0" />'
    '<one:OEChildren><one:OE><one:T><![CDATA[]]></one:T></one:OE></one:OEChildren>'
    "</one:Outline>"
    '<one:Outline objectID="{BBB-222}"><one:OEChildren><one:OE><one:T><![CDATA[该被替换掉]]></one:T></one:OE>'
    "</one:OEChildren></one:Outline>"
    "</one:Page>"
)


def check_page_xml() -> list[str]:
    """COM 通道的页面 XML 结构自检（纯离线，不碰 OneNote）。

    2026-09-21 那次「提示导入成功、OneNote 里一个字也没有」，根因全在这一节：
    OneNote 的 schema 严格得离谱，错一个标签就整页拒收，而且它一次只报第一个错。
    这几条是拿真实 OneNote 一条条试出来的，钉在这儿防止改渲染时又踩回去。
    """
    import tempfile

    from core.onenote_html import MediaPart
    from writers.com import _ComConverter, build_page_xml

    failed: list[str] = []
    tmp = Path(tempfile.gettempdir()) / "onenote-importer-selftest" / "pagexml"
    tmp.mkdir(parents=True, exist_ok=True)
    img = tmp / "dot.png"
    img.write_bytes(TINY_PNG)
    media = {"imageBlock0": MediaPart("imageBlock0", img, "image/png", "image", len(TINY_PNG))}
    conv = _ComConverter(media, width_limit=640)

    def frag(snippet: str) -> str:
        html = f"<html><body><div data-id='content-root'>{snippet}</div></body></html>"
        return conv.fragment("6月22日", html)[1]

    bullets = frag("<ul><li>甲</li></ul>")
    nested = frag("<ul><li>甲<ul><li>乙</li></ul></li></ul>")
    ordered = frag("<ol><li>甲</li><li>乙</li></ol>")
    table = frag("<table><tr><th>列</th></tr><tr><td>值</td></tr></table>")
    image = frag('<p><img src="name:imageBlock0" width="64" /></p>')

    checks = {
        # ① Title 的内容模型只认 OE。写成 OEChildren 会整页被拒:
        #    「根据父元素 Title 的内容模型，元素 OEChildren 为意外元素。要求: OE」
        "Title 里是 <one:OE> 而不是 <one:OEChildren>":
            "<one:Title><one:OE>" in build_page_xml("PID", "标题", bullets)
            and "<one:Title><one:OEChildren" not in build_page_xml("PID", "标题", bullets),
        # ② 项目符号用原生 bullet（实测 OneNote 接受这个写法）
        "项目符号用原生 <one:Bullet>": '<one:Bullet bullet="2" />' in bullets,
        # ③ <one:Number> 怎么填都过不了校验，有序列表改文本编号
        "有序列表不用 <one:Number>": "<one:Number" not in ordered and "1. 甲" in ordered,
        # ④ 子列表用嵌套 OEChildren 表达层级，不用 indentation 属性
        "子列表用嵌套 OEChildren": "<one:OEChildren>" in nested,
        "不出现 indentation 属性": "indentation=" not in nested,
        # ⑤ 表格必须包在 OE 里：OEChildren 只认 OE / HTMLBlock
        "表格包在 <one:OE> 里": "<one:OE><one:Table>" in table,
        # ⑥ 图片里 Size 必须在 Data 之前
        "图片 Size 排在 Data 之前":
            "<one:Image" in image and image.index("<one:Size") < image.index("<one:Data>"),
    }

    # ⑦ 有底稿时必须「就地替换」而不是「追加」，否则每页开头多一条空 outline
    merged = build_page_xml("PID", "6月22日", bullets, FAKE_BASE_PAGE)
    checks.update({
        "合并底稿后只剩一条 outline": merged.count("<one:Outline") == 1,
        # 这条是 2026-09-21「更新后内容重复」的根因：新 outline 没有 objectID
        # 就只会被追加，旧的那条原地不动。必须复用第一条原大纲的 ID。
        "复用了原 outline 的 objectID": 'objectID="{AAA-111}"' in merged,
        "重复的旧 outline 已收掉": 'objectID="{BBB-222}"' not in merged,
        "原大纲的位置节点保留": '<one:Position x="36.0"' in merged,
        "底稿里原来的正文被清掉": "该被替换掉" not in merged,
        "标题被替换成新的": "6月22日" in merged,
        "页面 ID 被校正为目标页": 'ID="PID"' in merged and "OLD-ID" not in merged,
        "页面原有属性（pageLevel）保留": 'pageLevel="1"' in merged,
        "XML 仍然合法": True,
    })
    try:
        ET.fromstring(merged)
    except ET.ParseError as e:
        checks["XML 仍然合法"] = False
        failed.append(f"合并后的 XML 解析失败：{e}")
    try:
        ET.fromstring(build_page_xml("PID", "标题", table))
    except ET.ParseError as e:
        checks["XML 仍然合法"] = False
        failed.append(f"表格页面 XML 解析失败：{e}")

    for name, ok in checks.items():
        print(f"  {'OK ' if ok else 'NG '} {name}")
        if not ok:
            failed.append(name)
    return failed


def main() -> int:
    global _SELFTMP_VAULT

    print("--- 前置自检（静态 + 跨线程 + 子进程） ---")
    here = Path(__file__).parent
    for script, title in (("attrcheck.py", "静态属性自检"),
                          ("test_threads.py", "跨线程自检（状态库 + Pipeline）")):
        print(f"--- {title} ---")
        rc, out, err = run_script(here / script)
        print(out.strip())
        if rc != 0:
            print(err.strip())
            print(f"未通过：{script}")
            return rc

    tmp = Path(tempfile.gettempdir()) / "onenote-importer-selftest"
    vault = build_sample_vault(tmp / "vault")
    out = tmp / "out"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    _SELFTMP_VAULT = str(vault)

    cfg = Config()
    cfg.vault_path = str(vault)
    cfg.state_path = str(tmp / "state.sqlite3")
    # 清掉上一轮留下的状态库，保证「连跑两次结果一致」
    (tmp / "state.sqlite3").unlink(missing_ok=True)
    cfg.notebook_name = "日记"
    cfg.dry_run = True

    pipe = Pipeline(cfg, log=lambda level, msg: print(f"[{level}] {msg}"))
    plan = pipe.scan()
    print(f"\n扫描到 {len(plan)} 篇笔记")
    for item in plan:
        print(f"  {item.action:6s} | {item.section:10s} | {item.title}")
        assert item.action == CREATE, "自检库全新，理应全部是新建"

    assert section_name_for("{YYYY}-{MM}", __import__("datetime").datetime(2026, 9, 7), "x") == "2026-09"
    assert section_name_for("{YYYY}年{MM}月", __import__("datetime").datetime(2026, 9, 7), "x") == "2026年09月"

    for nf in pipe.index.notes:
        page, title = pipe.render(nf)
        target = out / (nf.rel.replace("/", "__").replace("\\", "__") + ".html")
        target.write_text(page.html, encoding="utf-8")
        print(f"\n=== {nf.rel} → {target.name}")
        print(f"    标题={page.title!r} 媒体={[m.part_name for m in page.media]} "
              f"大小={page.total_bytes()}B 警告={page.warnings}")
        ET.fromstring(page.html.replace("<!DOCTYPE html>", "").strip())
        assert "<title>" in page.html
        assert 'data-id="content-root"' in page.html

    # OneNote HTML 的关键约束自检
    first = next(out.glob("*2026-09-07*.html")) if list(out.glob("*2026-09-07*.html")) else None
    html = first.read_text(encoding="utf-8") if first else ""
    checks = {
        "callout 着色": "#FAEEDA" in html,
        "代码围栏": "<pre" in html,
        "任务清单": "☑" in html or "☐" in html,
        "表格": "<table" in html,
        "图片走 multipart 部件": 'src="name:imgblock0"' in html,
        "wikilink 降级为文本": "[[2026-09-06]]" in html,
        "created 时间戳": '<meta name="created"' in html,
    }
    print("\n--- HTML 约束自检 ---")
    for name, ok in checks.items():
        print(f"  {'OK ' if ok else 'NG '} {name}")
    failed = [k for k, v in checks.items() if not v]

    print("\n--- 分区策略自检 ---")
    failed += check_section_strategy()

    print("\n--- 已有分区识别自检 ---")
    failed += check_inspect_targets()

    print("\n--- 计划指纹自检 ---")
    failed += check_plan_signature()

    print("\n--- 子进程静默自检 ---")
    failed += check_quiet_subprocess()

    print("\n--- 日志适配自检 ---")
    failed += check_log_adapter()

    print("\n--- COM 页面 XML 结构自检 ---")
    failed += check_page_xml()

    print(f"\n渲染结果输出目录：{out}")
    if failed:
        print("未通过项：" + "、".join(failed))
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
