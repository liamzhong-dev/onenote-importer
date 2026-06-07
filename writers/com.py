# -*- coding: utf-8 -*-
"""OneNote 桌面版 COM 写入通道（备通道，Graph 不可用或用户没配 Azure 时启用）。

通过 PowerShell 驱动 OneNote.Application COM 接口，因此不需要安装 pywin32。
缺点：只能写本机桌面版 OneNote 能访问的笔记本，且需 Windows。
更新页面用 UpdatePageContent 整体替换内容，page id 保持不变。
"""
from __future__ import annotations

import base64
import html
import json
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from core.onenote_html import CONTENT_DIV_ID, RenderedPage
from core.proc import decode_output, silent_kwargs
from .base import NotebookRef, OneNoteWriter, ProbeResult, SectionRef, WriterError

NS = "http://schemas.microsoft.com/office/onenote/2013/onenote"
PS_EXE = "powershell.exe"

HEADING_PT = {1: 20, 2: 16, 3: 14, 4: 12, 5: 11, 6: 11}

# 每次调 PowerShell 都要花一秒左右，界面上一片安静会让人以为卡住了。
# 这里把每个动作翻译成一句人话写进日志——用日志代替那个一闪而过的黑窗口。
_CMD_LABEL = {
    "probe": "探测桌面版 OneNote",
    "notebooks": "读取笔记本列表",
    "sections": "读取分区列表",
    "pages": "读取分区里的页面清单",
    "ensure-notebook": "定位 / 新建笔记本",
    "ensure-section": "定位 / 新建分区",
    "create-page": "新建页面",
    "get-page": "读回页面结构",
    "update-page": "写入页面内容",
    "delete-page": "删除页面",
    "goto-page": "在 OneNote 中定位页面",
}
# 结构性动作写 INFO，逐页动作写 DEBUG（几十篇日记时不至于刷屏）
_LOUD_CMDS = {"probe", "notebooks", "sections", "pages", "ensure-notebook",
              "ensure-section", "goto-page"}


def _cmd_of(args: tuple[str, ...]) -> str:
    try:
        return args[args.index("-Command") + 1]
    except (ValueError, IndexError):
        return ""


def _decode(raw: bytes) -> str:
    """PowerShell 5.1 在中文 Windows 上默认输出 GBK，这里按优先级试探解码。"""
    return decode_output(raw)


class ComUnavailable(WriterError):
    pass


class StructureUnavailable(WriterError):
    """读不回页面的原有 XML，因此无法安全地改写这一页。

    为什么这里选择「报错」而不是「硬写」：UpdatePageContent 靠 objectID 认对象，
    新 XML 里的 outline 没有 ID 就会被**追加**成第二条大纲，旧内容原地留着 ——
    页面看着多了一段、内容还不对，比直接失败糟糕得多。
    而「先删掉旧 outline 再写」在 COM 上没有 API：DeleteHierarchy 只接受
    笔记本 / 分区组 / 分区 / 页面这类**结构对象**，删不掉页面里的 outline
    （2026-09-21 实测：页面没被清掉，反而多出一条大纲）。
    所以剩下的选择只有「明确失败」和「写坏页面」，这里选前者。
    """
    pass


# ------------------------------------------------------------------ 图片尺寸
def image_dimensions(path: Path) -> tuple[int, int]:
    """不依赖 Pillow 读取 PNG/JPEG/GIF/BMP 的宽高；失败返回 0,0。"""
    try:
        with path.open("rb") as f:
            head = f.read(4096)
    except OSError:
        return 0, 0
    try:
        if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
            w = int.from_bytes(head[16:20], "big")
            h = int.from_bytes(head[20:24], "big")
            return w, h
        if head[:2] == b"\xff\xd8":
            i = 2
            while i < len(head) - 9:
                if head[i] != 0xFF:
                    i += 1
                    continue
                marker = head[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h = int.from_bytes(head[i + 5:i + 7], "big")
                    w = int.from_bytes(head[i + 7:i + 9], "big")
                    return w, h
                if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seg = int.from_bytes(head[i + 2:i + 4], "big")
                i += 2 + seg
            return 0, 0
        if head[:6] in (b"GIF87a", b"GIF89a") and len(head) >= 10:
            return int.from_bytes(head[6:8], "little"), int.from_bytes(head[8:10], "little")
        if head[:2] == b"BM" and len(head) >= 26:
            return int.from_bytes(head[18:22], "little"), int.from_bytes(head[22:26], "little")
    except Exception:
        pass
    return 0, 0


# ------------------------------------------------------------------ HTML → ONE XML
def _strip_doctype(html: str) -> str:
    for marker in ("<html", "<HTML"):
        idx = html.find(marker)
        if idx > 0:
            return html[idx:]
    return html


VOID_TAGS = {"img", "br", "hr", "input"}


def _ser(el: ET.Element) -> str:
    """序列化单个元素。不用 ET.tostring，因为它会把 tail 也算进去导致文本重复。"""
    attrs = "".join(f' {k}="{html.escape(v, quote=True)}"' for k, v in el.attrib.items())
    inner = _inner(el)
    if not inner:
        return f"<{el.tag}{attrs} />" if el.tag.lower() in VOID_TAGS else f"<{el.tag}{attrs}></{el.tag}>"
    return f"<{el.tag}{attrs}>{inner}</{el.tag}>"


def _inner(el: ET.Element) -> str:
    """把节点的子内容重新序列化成 HTML 字符串（用于塞进 CDATA）。"""
    parts = [el.text or ""]
    for child in el:
        parts.append(_ser(child))
        parts.append(child.tail or "")
    return "".join(parts).strip()


def _cdata(s: str) -> str:
    return f"<![CDATA[{s}]]>" if s else ""


def _t(text: str) -> str:
    return f"<one:T>{_cdata(text)}</one:T>"


def _title_element(title: str) -> str:
    """标题节点。

    ⚠️ 这里必须是 <one:Title><one:OE>…，**不能**写成 <one:Title><one:OEChildren>。
    OneNote 的 schema 里 Title 的内容模型只允许 OE，写成 OEChildren 会被整页拒收：
        「根据父元素 Title 的内容模型，元素 OEChildren 为意外元素。要求: OE。」
    第一版就是踩在这里：页面建出来了，内容一个字都写不进去。
    """
    return f"<one:Title><one:OE>{_t(title)}</one:OE></one:Title>"


def _replace_title_text(xml: str, title: str) -> str:
    """把底稿里 Title 下那个空的 <one:T> 换成我们的标题。"""
    self_closing = re.search(r"<one:Title\b[^>]*?/>", xml)
    if self_closing:
        return xml[:self_closing.start()] + _title_element(title) + xml[self_closing.end():]
    m = re.search(r"<one:Title\b[^>]*>", xml)
    if not m:
        return xml.replace("</one:Page>", _title_element(title) + "</one:Page>")
    start, end = m.end(), xml.find("</one:Title>", m.end())
    if end < 0:
        return xml
    inner = xml[start:end]
    new_t = _t(title)
    replaced = re.sub(r"<one:T\b[^>]*/>", new_t, inner, count=1)
    if replaced == inner:
        replaced = re.sub(r"<one:T\b[^>]*>.*?</one:T>", new_t, inner, count=1, flags=re.S)
    if replaced == inner:
        # Title 里连 OE 都没有（理论上不会），直接补一个完整的
        replaced = f"<one:OE>{new_t}</one:OE>"
    return xml[:start] + replaced + xml[end:]


def _force_page_id(xml: str, page_id: str) -> str:
    m = re.search(r"<one:Page\b[^>]*>", xml)
    if not m:
        return xml
    tag = m.group(0)
    if re.search(r'\sID="[^"]*"', tag):
        tag = re.sub(r'\sID="[^"]*"', f' ID="{page_id}"', tag, count=1)
    else:
        tag = tag[:-1].rstrip() + f' ID="{page_id}">'
    return xml[:m.start()] + tag + xml[m.end():]


# 一条 outline。Outline 不允许嵌套，所以「非贪婪匹配到第一个 </one:Outline>」
# 是安全的，不会把内层的 OEChildren 截断。顺带兼容自闭合写法。
_OUTLINE_RE = re.compile(r"<one:Outline\b(?P<attrs>[^>]*?)(?:/>|>(?P<body>.*?)</one:Outline>)", re.S)


def _splice_outline(xml: str, children_xml: str) -> str:
    """把页面里的 outline 收成一条，内容换成这一版。

    为什么不能「删掉旧 outline、在页尾追加一条新的」：UpdatePageContent **不是**
    整页替换。文档里那句「不修改也不删除你没有指定的页级对象」是真的 —— 新 outline
    没有 objectID，OneNote 只当它是个新对象追加进去，旧的那条原地留着。
    表现就是：导入一次多一条大纲，旧内容永远不消失，越导越多。

    正确做法是复用第一条现成 outline 的 objectID（OneNote 认这个 ID，会就地替换
    它的内容），同时把早期版本攒下的重复 outline 一并收掉 —— 也就是说，
    重跑一次导入能把之前被写坏的页面顺带修回来。

    代价是：这一页的人工增补（如果加在我们那条 outline 之外）会被覆盖。
    对「页面内容 = 这个 md 文件」的导入工具来说，这正是想要语义。
    """
    ms = list(_OUTLINE_RE.finditer(xml))
    if not ms:
        return xml.replace("</one:Page>",
                           f"<one:Outline><one:OEChildren>{children_xml}</one:OEChildren></one:Outline>"
                           "</one:Page>")
    first = ms[0]
    attrs = first.group("attrs") or ""
    body = first.group("body") or ""
    new_children = f"<one:OEChildren>{children_xml}</one:OEChildren>"
    if "<one:OEChildren" in body:
        # 贪婪匹配到最外层那个 </one:OEChildren>：内层的嵌套子列表不会被误截。
        # 位置（Position）、尺寸（Size）这些兄弟节点原样留着，大纲不会跳位置。
        new_body = re.sub(r"<one:OEChildren\b.*</one:OEChildren>", new_children,
                          body, count=1, flags=re.S)
    else:
        new_body = body + new_children
    out = xml[:first.start()] + f"<one:Outline{attrs}>{new_body}</one:Outline>"
    pos = first.end()
    for m in ms[1:]:
        out += xml[pos:m.start()]
        pos = m.end()
    return out + xml[pos:]


def build_page_xml(page_id: str, title: str, outline_xml: str, base_xml: str | None = None) -> str:
    """拼出 UpdatePageContent 能接受的页面 XML。

    两条要求，缺一个都写不进去：
      1. <one:Title> 里放 <one:OE>（见 _title_element 的注释）；
      2. 带底稿，并且**复用原 outline 的 objectID**（见 _splice_outline 的注释）。

    `base_xml` 为空时这里会退回整页重建 —— 但那只是兜底，**写入通道不会走**：
    重建出来的 outline 没有 objectID，写进一个已有页面会被追加成第二条大纲，
    所以 ComWriter 在读不回结构时直接报 StructureUnavailable，宁可失败也不写坏。
    （自检里单独测这条分支，是因为要确认它产出的 XML 本身合法。）
    """
    if not base_xml or "<one:Page" not in base_xml:
        return ('<?xml version="1.0" encoding="utf-8"?>'
                f'<one:Page xmlns:one="{NS}" ID="{page_id}">'
                + _title_element(title)
                + f"<one:Outline><one:OEChildren>{outline_xml}</one:OEChildren></one:Outline>"
                + "</one:Page>")
    xml = base_xml.strip()
    xml = _replace_title_text(xml, title)
    xml = _splice_outline(xml, outline_xml)
    return _force_page_id(xml, page_id)


class _ComConverter:
    def __init__(self, media: dict[str, RenderedPage], width_limit: int = 640):
        self.media_map = media
        self.width_limit = width_limit
        self.images_missing: list[str] = []

    def fragment(self, title: str, body_html: str) -> tuple[str, str]:
        """把渲染好的 HTML 变成 (标题纯文本, outline 里的 OEChildren 内容)。

        只产出「内容」，页面外壳交给 build_page_xml —— 因为外壳得拿底稿改。
        """
        root_html = _strip_doctype(body_html)
        try:
            root = ET.fromstring(root_html)
        except ET.ParseError:
            # 兜底：整篇当纯文本
            safe = body_html.replace("]]>", "]]]]><![CDATA[>")
            root = ET.fromstring(f"<html><head><title>{title}</title></head><body><div data-id='{CONTENT_DIV_ID}'><p>{safe}</p></div></body></html>")
        body = root.find("body")
        if body is not None:
            container = None
            for el in body:
                if el.get("data-id") == CONTENT_DIV_ID:
                    container = el
                    break
            container = container if container is not None else body
        else:
            container = root

        oes: list[str] = []
        for el in list(container):
            oes.extend(self.block(el))
        if not oes:
            oes = [self._oe("　")]
        return title, "".join(oes)

    # ---------------------------------------------------------- 块级映射
    def block(self, el: ET.Element, indent: int = 0) -> list[str]:
        tag = el.tag.lower()
        if tag == "img":
            return self._image_el(el, indent)
        if tag == "object":
            name = el.get("data-attachment") or el.get("data", "附件")
            return [self._oe(f"[附件 {name}：COM 通道暂不支持嵌入文件，请手动插入]", indent)]
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            size = HEADING_PT[int(tag[1])]
            style = f"font-size:{size}pt;font-weight:bold"
            return [self._oe(f'<span style="{style}">{_inner(el)}</span>', indent)]
        if tag == "p":
            return self._para(el, indent)
        if tag in ("ul", "ol"):
            return self._list(el, indent, ordered=(tag == "ol"))
        if tag == "pre":
            txt = (el.text or "").replace("]]>", "]]]]><![CDATA[>")
            return [self._oe(f'<span style="font-family:Consolas;font-size:10pt;background-color:#F1EFE8">{txt}</span>', indent)]
        if tag == "table":
            return [self._table(el, indent)]
        if tag == "hr":
            return [self._oe("────────────", indent)]
        if tag == "div":
            out: list[str] = []
            for child in list(el):
                out.extend(self.block(child, indent))
            return out
        if tag == "blockquote":
            return [self._oe(f'<span style="color:#444441">{_inner(el)}</span>', indent)]
        return [self._oe(_inner(el), indent)]

    def _oe(self, content_html: str, indent: int = 0) -> str:
        """一个段落节点。

        缩进不再用 indentation 属性表达 —— 那不在 2013 schema 的 OE 属性表里，
        被严格校验时会连累整页。真正的层级用 <one:OEChildren> 嵌套（见 _list_item），
        所以这里的 indent 参数保留只为兼容调用点，不再产出属性。
        """
        return f"<one:OE><one:T>{_cdata(content_html)}</one:T></one:OE>"

    def _para(self, el: ET.Element, indent: int) -> list[str]:
        children = list(el)
        imgs = [c for c in children if c.tag.lower() == "img"]
        objs = [c for c in children if c.tag.lower() == "object"]
        if imgs or objs:
            out = []
            for node in children:
                out.extend(self.block(node, indent))
            text = (el.text or "").strip()
            if text:
                out.append(self._oe(text, indent))
            return out
        return [self._oe(_inner(el), indent)]

    def _list(self, el: ET.Element, indent: int, ordered: bool) -> list[str]:
        items = [c for c in el if c.tag.lower() == "li"]
        return [self._list_item(item, indent, ordered, idx)
                for idx, item in enumerate(items, start=1)]

    def _list_item(self, item: ET.Element, indent: int, ordered: bool, idx: int) -> str:
        """一条列表项。子列表用嵌套的 OEChildren 表达层级，不再用空格+属性硬凑。"""
        nested = [c for c in item if c.tag.lower() in ("ul", "ol")]
        for child in nested:
            item.remove(child)
        body_html = _inner(item).strip()
        for child in nested:
            item.append(child)          # 放回去，别把原树改坏
        if ordered:
            marker, body = "", f"{idx}. {body_html}"
        else:
            marker, body = self._marker_el(ordered, idx), body_html
        inner = f"{marker}<one:T>{_cdata(body)}</one:T>"
        kids = "".join("".join(self.block(c, indent + 1)) for c in nested)
        if kids:
            inner += f"<one:OEChildren>{kids}</one:OEChildren>"
        return f"<one:OE>{inner}</one:OE>"

    @staticmethod
    def _marker_el(ordered: bool, idx: int) -> str:
        """项目符号用 OneNote 原生的 bullet（实测这个写法 OneNote 接受）。

        有序列表退回纯文本编号「1. 」：<one:Number> 试遍了
        numberSequence / numberFormat("decimal" / "Decimal" / "Arabic") /
        number / language 的各种组合，OneNote 一律拒收（不是 schema 报错
        就是 HRESULT 0x80042001）。与其赌一个猜不出的 schema 写法，
        不如用它认得的文本编号——显示效果一样，而且绝不会连累整页被拒。
        子列表的层级已经由嵌套 OEChildren 表达，这里不需要伪行号。
        """
        if ordered:
            return ""
        return '<one:List><one:Bullet bullet="2" /></one:List>'

    def _table(self, el: ET.Element, indent: int) -> str:
        rows = [c for c in el if c.tag.lower() == "tr"]
        out = []
        for row in rows:
            cells = [c for c in row if c.tag.lower() in ("td", "th")]
            cell_xml = "".join(
                f"<one:Cell><one:OEChildren>{self._oe(_inner(c))}</one:OEChildren></one:Cell>"
                for c in cells
            )
            out.append(f"<one:Row>{cell_xml}</one:Row>")
        # 表格必须包在 OE 里：OEChildren 的内容模型只认 OE / HTMLBlock，
        # 直接把 <one:Table> 塞进 OEChildren 会被整页拒收。
        return f"<one:OE><one:Table>{''.join(out)}</one:Table></one:OE>"

    # ---------------------------------------------------------- 图片 / 附件
    def _image_el(self, el: ET.Element, indent: int) -> list[str]:
        src = el.get("src", "")
        w = el.get("width")
        part_name = src.split("name:", 1)[1] if src.startswith("name:") else None
        media = self.media_map.get(part_name) if part_name else None
        if media is None:
            if src.startswith("http"):
                return [self._oe(f'<img src="{src}" />', indent)]
            self.images_missing.append(src)
            return [self._oe(f"[缺失图片 {src}]", indent)]
        path = media.path
        if path is None or not path.exists():
            return [self._oe(f"[缺失图片 {media.part_name}]", indent)]
        fmt = path.suffix.lower().lstrip(".")
        fmt_attr = f' format="{fmt}"' if fmt else ""
        try:
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError:
            return [self._oe(f"[无法读取图片 {path.name}]", indent)]
        iw, ih = image_dimensions(path)
        if w:
            try:
                iw = int(float(w))
            except ValueError:
                pass
        if iw and ih:
            scale = min(1.0, self.width_limit / float(iw)) if iw > self.width_limit else 1.0
            dw, dh = int(iw * scale), int(ih * scale)
        else:
            dw, dh = self.width_limit, int(self.width_limit * 0.75)
        # ⚠️ Size 必须排在 Data 前面。反过来的话 OneNote 会拒收整页：
        #    「根据父元素 Image 的内容模型，元素 Size 为意外元素。要求: OCRData, Preview」
        #    也就是说 Data 之后只允许再出现 OCRData / Preview。
        size = f'<one:Size width="{dw}" height="{dh}" />'
        return [f"<one:OE><one:Image{fmt_attr}>{size}<one:Data>{b64}</one:Data></one:Image></one:OE>"]


# ------------------------------------------------------------------ Writer
class ComWriter(OneNoteWriter):
    name = "com"

    def __init__(self, cfg, log=None, ps_path: Path | None = None, step=None):
        super().__init__(cfg, log, step)
        self.ps = ps_path or (Path(__file__).resolve().parent.parent / "bridge" / "onenote_com.ps1")
        self._available: bool | None = None

    # ---------------------------------------------------------- PS 调用
    def _run(self, *args: str, timeout: int = 180) -> dict:
        cmd = [PS_EXE, "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(self.ps)]
        cmd += list(args)
        name = _cmd_of(tuple(args))
        label = _CMD_LABEL.get(name, name or "调用 OneNote")
        self.step(label)
        self.log(f"OneNote：{label}…", "INFO" if name in _LOUD_CMDS else "DEBUG")
        try:
            # 不加 silent_kwargs 的话，每调一次 PowerShell 就会闪一个蓝色控制台窗口
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout, **silent_kwargs())
        except FileNotFoundError:
            raise ComUnavailable("找不到 PowerShell")
        except subprocess.TimeoutExpired:
            self.log(f"OneNote：{label} 超时（{timeout}s）", "WARN")
            raise WriterError(f"OneNote COM 调用超时：{label}")
        out = _decode(proc.stdout or b"").strip()
        err = _decode(proc.stderr or b"").strip()
        if not out:
            self.log(f"OneNote：{label} 没有返回结果", "WARN")
            raise WriterError(err or "OneNote COM 无输出")
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            raise WriterError(f"无法解析 COM 输出：{out[:300]}")
        if isinstance(data, dict) and not data.get("ok", True):
            msg = data.get("message") or data.get("error") or "未知错误"
            if data.get("error") in ("no_onenote", "hierarchy_failed"):
                raise ComUnavailable(msg)
            self.log(f"OneNote：{label} 失败 —— {msg}", "WARN")
            raise WriterError(msg)
        return data

    # ---------------------------------------------------------- 探测
    def probe(self) -> ProbeResult:
        if not self.cfg.com_enabled:
            return ProbeResult(False, "COM 通道已在设置里关闭")
        try:
            self._run("-Command", "probe", timeout=60)
        except ComUnavailable as e:
            return ProbeResult(False, str(e))
        except WriterError as e:
            return ProbeResult(False, str(e))
        self._available = True
        return ProbeResult(True, "桌面版 OneNote 可用", "本机 OneNote")

    # ---------------------------------------------------------- 结构
    def list_notebooks(self) -> list[NotebookRef]:
        data = self._run("-Command", "notebooks", timeout=120)
        raw = data.get("notebooks") or []
        if isinstance(raw, dict):
            raw = [raw]
        return [NotebookRef(i.get("id", ""), i.get("name", "")) for i in raw if isinstance(i, dict)]

    def ensure_notebook(self, display_name: str) -> NotebookRef:
        data = self._run("-Command", "ensure-notebook", "-Name", display_name, timeout=120)
        nb = data.get("notebook") or {}
        if not nb.get("id"):
            raise WriterError(f"无法定位笔记本「{display_name}」")
        return NotebookRef(nb.get("id", ""), nb.get("name", display_name))

    def list_sections(self, notebook_id: str) -> list[SectionRef]:
        data = self._run("-Command", "sections", "-NotebookId", notebook_id, timeout=120)
        raw = data.get("sections") or []
        if isinstance(raw, dict):
            raw = [raw]
        return [SectionRef(i.get("id", ""), i.get("name", "")) for i in raw if isinstance(i, dict)]

    def ensure_section(self, notebook_id: str, section_name: str) -> SectionRef:
        data = self._run("-Command", "ensure-section", "-NotebookId", notebook_id, "-Name", section_name, timeout=120)
        sec = data.get("section") or {}
        if not sec.get("id"):
            raise WriterError(f"无法创建分区「{section_name}」")
        return SectionRef(sec.get("id", ""), sec.get("name", section_name))

    def list_pages_full(self, section_id: str) -> list[dict]:
        data = self._run("-Command", "pages", "-SectionId", section_id, timeout=180)
        raw = data.get("pages") or []
        if isinstance(raw, dict):
            raw = [raw]
        return [dict(i) for i in raw if isinstance(i, dict)]

    def list_pages(self, section_id: str) -> list[tuple[str, str]]:
        return [(i.get("id", ""), i.get("title", "")) for i in self.list_pages_full(section_id)]

    # ---------------------------------------------------------- 页面
    def get_page_xml(self, page_id: str, retries: int = 2) -> str | None:
        """读回页面现有 XML（不含二进制），当改写底稿。

        空结果会重试一次：新建页面偶尔要等一拍才读得到正文，而这里的「读不到」
        会让调用方直接放弃这次写入，不值得为一次抽风付出整页回滚的代价。
        """
        import tempfile

        for attempt in range(max(1, retries)):
            with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8") as f:
                tmp = f.name
            try:
                self._run("-Command", "get-page", "-PageId", page_id, "-XmlFile", tmp, timeout=120)
                text = Path(tmp).read_text(encoding="utf-8", errors="replace").strip()
            finally:
                try:
                    Path(tmp).unlink()
                except OSError:
                    pass
            if text:
                return text
            if attempt + 1 < retries:
                self.log("页面结构读回是空的，稍后重试一次", "DEBUG")
                time.sleep(0.4)
        return None

    def _page_xml(self, page_id: str, page: RenderedPage) -> str:
        """产出这一版页面 XML（带走原 outline 的 objectID 与 Position）。

        读不回原页面结构就直接报错，**不**退回整页重建 —— 见 StructureUnavailable。
        """
        media_map = {m.part_name: m for m in page.media}
        conv = _ComConverter(media_map, width_limit=self.cfg.image_width_limit)
        title, outline = conv.fragment(page.title, page.html)
        try:
            base = self.get_page_xml(page_id)
        except Exception as e:  # noqa: BLE001 — 读不回结构一律当成「不能安全写」
            raise StructureUnavailable(f"读取页面原有结构失败：{e}") from e
        if not base or "<one:Page" not in base:
            raise StructureUnavailable("页面原有结构读回是空的（OneNote 还没写完，或页面已被删掉）")
        return build_page_xml(page_id, title, outline, base)

    def create_page(self, section_id: str, page: RenderedPage) -> str:
        data = self._run("-Command", "create-page", "-SectionId", section_id, timeout=120)
        page_id = data.get("pageId") or ""
        if not page_id:
            raise WriterError("OneNote 未返回新建页面的 ID")
        try:
            self._apply_content(page_id, page)
        except Exception as e:
            # 内容没写进去就把刚建的空页删掉。否则分区里会攒一堆「未命名」空页，
            # 下次重跑还得再建一堆 —— 13:55 那次就是这么在 2026夏 里留下 9 个空页的。
            try:
                self.delete_page(page_id)
                self.log("内容写入失败，已回滚删除刚建的空页面", "WARN")
            except Exception as de:
                self.log(f"回滚空页面失败（{de}），分区里可能留下一个「未命名」空页", "WARN")
            raise WriterError(str(e)) from e
        if (self.cfg.throttle_ms or 0) > 0:
            time.sleep(self.cfg.throttle_ms / 1000.0)
        return page_id

    def _apply_content(self, page_id: str, page: RenderedPage):
        xml = self._page_xml(page_id, page)
        # OneNote 要求 <T> 内的 ]]> 被转义，CDATA 之外不能有多余空白修正
        ET.fromstring(xml)  # 自检：XML 必须合法，宁可早失败也不要写坏页面
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8") as f:
            f.write(xml)
            tmp = f.name
        try:
            self._run("-Command", "update-page", "-PageId", page_id, "-XmlFile", tmp, timeout=300)
        finally:
            try:
                Path(tmp).unlink()
            except OSError:
                pass

    def update_page_content(self, page_id: str, page: RenderedPage, strategy: str = "replace_div") -> None:
        self._apply_content(page_id, page)

    def goto_page(self, page_id: str) -> bool:
        """让桌面版 OneNote 跳转到这一页——「写完到底去哪了」最直观的回答。"""
        if not (page_id or "").strip():
            return False
        try:
            self._run("-Command", "goto-page", "-PageId", page_id.strip(), timeout=60)
        except (WriterError, ComUnavailable) as e:
            self.log(f"打开 OneNote 页面失败：{e}", "WARN")
            return False
        return True

    def delete_page(self, page_id: str) -> None:
        data = self._run("-Command", "delete-page", "-PageId", page_id, timeout=120)
        if not data.get("deleted"):
            self.log("删除页面未返回结果", "WARN")
