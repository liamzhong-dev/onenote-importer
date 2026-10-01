# -*- coding: utf-8 -*-
"""把「简单格式」的文件转成 Markdown，让它们走和 .md 完全相同的渲染链路。

支持（全部零 pip 依赖）：

    .md / .markdown   原样读出
    .txt              纯文本：探测编码 + 规范化空行
    .docx             Word：zipfile 解开 + xml.etree 直读 document.xml
    .html / .htm      网页：标准库 html.parser 取正文

**故意不做**：.doc（97-2003 的老二进制格式）、.pdf、.rtf。
这几个要么得装第三方库，要么得外调 Word/WPS 进程 —— 不属于"简单格式"，
硬做出来的稳定性还不如不做（会把用户的正文悄悄丢一半）。

转换产物只是内存里的中间 Markdown，不落盘。唯一的例外是 .docx 里抽出来的
图片：渲染层是"按路径读文件字节再内嵌"的，必须有真实文件可读，所以那些放在
`%APPDATA%\\OneNoteDiaryImporter\\media-cache\\<指纹>\\` 下面（只写这里，
绝不往用户的来源目录里放东西）。
"""
from __future__ import annotations

import hashlib
import posixpath
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

# ---------------------------------------------------------------- 格式登记

SOURCE_FORMATS: tuple[str, ...] = ("md", "txt", "docx", "html")

FORMAT_LABELS: dict[str, str] = {
    "md": "Markdown（.md）",
    "txt": "纯文本（.txt）",
    "docx": "Word 文档（.docx）",
    "html": "网页（.html / .htm）",
}

FORMAT_HINTS: dict[str, str] = {
    "md": "原生格式，Obsidian 库直接用这个",
    "txt": "记事本、备忘录导出的纯文本",
    "docx": "Word 2007 以后的 .docx；配图会一并抽出来嵌入",
    "html": "浏览器「另存为网页」，或任何本地 .html 文件",
}

FORMAT_EXTS: dict[str, tuple[str, ...]] = {
    "md": (".md", ".markdown"),
    "txt": (".txt",),
    "docx": (".docx",),
    "html": (".html", ".htm"),
}

SOURCE_EXTS: tuple[str, ...] = tuple(e for f in SOURCE_FORMATS for e in FORMAT_EXTS[f])

# 转换后正文的硬上限。一个几 MB 的网页能转出十几 MB 的 OneNote XML，
# 而 OneNote 对这种巨型 UpdatePageContent 很不友好，所以宁可截断并说清楚。
MAX_TEXT_CHARS = 400_000
TRUNCATED_NOTE = "> ⚠️ 转换后的内容太长，超出部分已截断。"


class ConvertError(Exception):
    """格式本身的毛病（文件坏了 / 不是它声称的那种格式）。"""


def format_of(path) -> str | None:
    """按后缀判断属于哪种来源格式；不认识返回 None。"""
    ext = Path(path).suffix.lower()
    for fmt, exts in FORMAT_EXTS.items():
        if ext in exts:
            return fmt
    return None


def exts_for(formats) -> set[str]:
    """界面上的格式勾选 → 后缀集合。空列表按 md 处理，免得一个文件都扫不到。"""
    out: set[str] = set()
    for fmt in (formats or ["md"]):
        out.update(FORMAT_EXTS.get(fmt, ()))
    return out


def normalize_formats(raw) -> list[str]:
    """把任意外来值收敛成合法的格式列表（保序、去重、非法项丢弃）。"""
    seen, out = set(), []
    for item in (raw or []):
        key = str(item).strip().lower()
        if key in FORMAT_EXTS and key not in seen:
            seen.add(key)
            out.append(key)
    return out or ["md"]


# ---------------------------------------------------------------- 主入口

def read_as_markdown(path) -> str:
    """读任意受支持的来源文件，返回可交给 Markdown 解析器的文本。"""
    path = Path(path)
    fmt = format_of(path)
    if fmt == "docx":
        text = docx_to_markdown(path)
    elif fmt == "html":
        text = html_to_markdown(_read_text(path))
    elif fmt == "txt":
        text = txt_to_markdown(_read_text(path))
    else:
        # .md 与「后缀不认识」都按纯文本读 —— 至少不会丢内容
        text = _read_text(path)

    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS].rstrip() + "\n\n" + TRUNCATED_NOTE + "\n"
    return text


def _read_text(path: Path) -> str:
    """按 UTF-8 → UTF-16(BOM) → GB18030 的顺序探测编码。

    顺序不能反：GB18030 几乎什么字节序列都能解出来，放前面会把 UTF-8
    的中文解成乱码而不报错，那就永远轮不到正确的那一个了。
    """
    raw = Path(path).read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for enc in ("gb18030", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- 纯文本

def txt_to_markdown(text: str) -> str:
    """纯文本 → Markdown。

    只做两件事：统一换行、把连续空行压成一个。**不加任何语法标记** ——
    猜标题、猜列表只会把用户原本的样子改掉；渲染层的段落本来就会保留换行。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    out: list[str] = []
    blank = 0
    for raw in text.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(line)
    return "\n".join(out).strip("\n")


# ---------------------------------------------------------------- Word .docx

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

_HEADING_PATTERNS = (
    re.compile(r"(?i)^heading\s*([1-9])$"),
    re.compile(r"^标题\s*([1-9])$"),
    re.compile(r"^([1-9])$"),
)


def _truthy(el) -> bool:
    """`<w:b/>` 为真，`<w:b w:val="0"/>` / `"false"` 为假。"""
    if el is None:
        return False
    val = el.get(f"{_W}val")
    if val is None:
        return True
    return str(val).strip().lower() not in ("0", "false", "off", "none")


def _style_id(p) -> str:
    ppr = p.find(f"{_W}pPr")
    if ppr is None:
        return ""
    st = ppr.find(f"{_W}pStyle")
    if st is None:
        return ""
    return str(st.get(f"{_W}val") or "")


def _heading_level(p) -> int | None:
    style = _style_id(p)
    for pat in _HEADING_PATTERNS:
        m = pat.match(style)
        if m:
            return int(m.group(1))
    # 有些文档不设样式，只给大纲级别。注意 9 在 Word 里表示「正文」而不是第 10 级
    ppr = p.find(f"{_W}pPr")
    if ppr is not None:
        lvl = ppr.find(f"{_W}outlineLvl")
        if lvl is not None:
            try:
                val = int(lvl.get(f"{_W}val"))
            except (TypeError, ValueError):
                return None
            if 0 <= val <= 8:
                return min(6, val + 1)
    return None


def _list_info(p, numbering: dict) -> tuple[int, bool] | None:
    """返回 (缩进层级, 是否有序)；不是列表项返回 None。"""
    ppr = p.find(f"{_W}pPr")
    if ppr is None:
        return None
    numpr = ppr.find(f"{_W}numPr")
    if numpr is None:
        return None
    ilvl = numpr.find(f"{_W}ilvl")
    numid = numpr.find(f"{_W}numId")
    level = 0
    if ilvl is not None:
        try:
            level = max(0, int(ilvl.get(f"{_W}val") or 0))
        except (TypeError, ValueError):
            level = 0
    nid = str(numid.get(f"{_W}val") or "") if numid is not None else ""
    if nid in ("", "0"):
        return None          # numId=0 是「取消编号」，不是列表
    fmt = (numbering.get(nid) or {}).get(level, "bullet")
    return level, fmt not in ("bullet", "none", "")


def _load_numbering(z: zipfile.ZipFile) -> dict[str, dict[int, str]]:
    """numId → {层级: numFmt}。读不到就返回空表，列表退化成无序。"""
    try:
        root = ET.fromstring(z.read("word/numbering.xml"))
    except (KeyError, ET.ParseError):
        return {}
    abstract: dict[str, dict[int, str]] = {}
    for an in root.findall(f"{_W}abstractNum"):
        aid = str(an.get(f"{_W}abstractNumId") or "")
        levels: dict[int, str] = {}
        for lvl in an.findall(f"{_W}lvl"):
            try:
                idx = int(lvl.get(f"{_W}ilvl") or 0)
            except (TypeError, ValueError):
                idx = 0
            fmt_el = lvl.find(f"{_W}numFmt")
            levels[idx] = str((fmt_el.get(f"{_W}val") if fmt_el is not None else "") or "bullet")
        abstract[aid] = levels
    out: dict[str, dict[int, str]] = {}
    for num in root.findall(f"{_W}num"):
        nid = str(num.get(f"{_W}numId") or "")
        ref = num.find(f"{_W}abstractNumId")
        aid = str((ref.get(f"{_W}val") if ref is not None else "") or "")
        out[nid] = abstract.get(aid, {})
    return out


def _load_rels(z: zipfile.ZipFile) -> dict[str, dict]:
    try:
        root = ET.fromstring(z.read("word/_rels/document.xml.rels"))
    except (KeyError, ET.ParseError):
        return {}
    out: dict[str, dict] = {}
    for rel in root:
        rid = rel.get("Id")
        if not rid:
            continue
        out[rid] = {
            "type": (rel.get("Type") or "").rsplit("/", 1)[-1],
            "target": rel.get("Target") or "",
            "external": (rel.get("TargetMode") or "") == "External",
        }
    return out


def _media_cache_dir(src: Path) -> Path:
    """按「路径 + 大小 + 修改时间」指纹建缓存目录，重复导入不重复解包。"""
    from .config import appdata_dir

    try:
        st = src.stat()
        key = hashlib.sha1(
            f"{src.resolve()}|{st.st_size}|{int(st.st_mtime)}".encode("utf-8")
        ).hexdigest()[:16]
    except OSError:
        key = hashlib.sha1(str(src).encode("utf-8")).hexdigest()[:16]
    d = appdata_dir() / "media-cache" / key
    d.mkdir(parents=True, exist_ok=True)
    return d


def _extract_media(z: zipfile.ZipFile, target: str, media_dir: Path) -> Path | None:
    entry = posixpath.normpath(target.lstrip("/"))
    if not entry.startswith("word/"):
        entry = posixpath.normpath("word/" + entry)
    dst = media_dir / Path(entry).name
    if dst.exists() and dst.stat().st_size > 0:
        return dst
    try:
        dst.write_bytes(z.read(entry))
    except (KeyError, OSError):
        return None
    return dst


def _md_path(p: Path) -> str:
    """Markdown 的图片地址：统一正斜杠，避免反斜杠在链接里被当转义符。"""
    return str(p).replace("\\", "/")


class _DocxWalker:
    """按顺序走 w:body，把段落 / 标题 / 列表 / 表格拼成 Markdown 块。"""

    def __init__(self, z: zipfile.ZipFile, rels: dict, numbering: dict, media_dir: Path):
        self.z = z
        self.rels = rels
        self.numbering = numbering
        self.media_dir = media_dir
        self.missing: list[str] = []
        # 有序列表的计数器：按层级存。遇到普通段落或无序项就清掉 ——
        # 否则两个分开的有序列表会接着上一条继续编号（1. 2. 然后 3. 4.）
        self._ordered_counters: dict[int, int] = {}

    # ---------------- 段落
    def paragraph(self, p) -> str:
        level = _heading_level(p)
        list_info = _list_info(p, self.numbering)
        text = self._inline(p)

        if level:
            self._ordered_counters.clear()
            return f'{"#" * level} {text.strip()}'.rstrip()
        if list_info is not None:
            depth, ordered = list_info
            if ordered:
                self._ordered_counters[depth] = self._ordered_counters.get(depth, 0) + 1
                marker = f"{self._ordered_counters[depth]}. "
            else:
                marker = "- "
                self._ordered_counters.pop(depth, None)
            return f'{"  " * depth}{marker}{text.strip()}'
        self._ordered_counters.clear()
        return text.strip()

    def _inline(self, node) -> str:
        """把一段（或一个超链接容器）里的 run 拼成带格式标记的行内文本。"""
        parts: list[str] = []
        for child in node:
            tag = child.tag
            if tag == f"{_W}r":
                parts.append(self._run(child))
            elif tag == f"{_W}hyperlink":
                rid = child.get(f"{_REL_NS}id") or ""
                inner = self._inline(child)
                rel = self.rels.get(rid) or {}
                href = rel.get("target") or ""
                if href.startswith(("http://", "https://")) and inner.strip():
                    parts.append(f"[{inner}]({href})")
                else:
                    parts.append(inner)
            elif tag in (f"{_W}smartTag", f"{_W}sdt", f"{_W}sdtContent",
                         f"{_W}fldSimple", f"{_W}ins", f"{_W}del"):
                # 内容控件 / 域 / 修订标记：里面装的还是 run，直接往下钻
                parts.append(self._inline(child))
        return "".join(parts)

    def _run(self, r) -> str:
        rpr = r.find(f"{_W}rPr")
        bold = italic = strike = False
        if rpr is not None:
            bold = _truthy(rpr.find(f"{_W}b"))
            italic = _truthy(rpr.find(f"{_W}i"))
            strike = _truthy(rpr.find(f"{_W}strike")) or _truthy(rpr.find(f"{_W}dstrike"))

        chunks: list[str] = []
        for ch in r:
            tag = ch.tag
            if tag == f"{_W}t":
                chunks.append(ch.text or "")
            elif tag == f"{_W}tab":
                chunks.append(" ")
            elif tag in (f"{_W}br", f"{_W}cr"):
                chunks.append("  \n")
            elif tag in (f"{_W}drawing", f"{_W}pict", f"{_W}object"):
                img = self._picture(ch)
                if img:
                    chunks.append(img)
        text = "".join(chunks)
        if not text:
            return ""
        # 行内标记只包住真正有内容的部分，空 run 加 ** 会在 OneNote 里变成一串星号
        core = text
        if core.strip():
            if bold:
                core = f"**{core}**"
            if italic:
                core = f"*{core}*"
            if strike:
                core = f"~~{core}~~"
        return core

    def _picture(self, node) -> str:
        """只取第一张图：一个 run 里塞多张图的情况在日记里基本不存在。"""
        blip = node.find(f".//{_A}blip")
        if blip is None:
            return ""
        rid = blip.get(f"{_REL_NS}embed") or blip.get(f"{_REL_NS}link") or ""
        rel = self.rels.get(rid) or {}
        target = rel.get("target") or ""
        if not target:
            return ""
        if rel.get("external"):
            return f"![]({target})" if target.startswith(("http://", "https://")) else ""
        path = _extract_media(self.z, target, self.media_dir)
        if path is None:
            self.missing.append(Path(target).name)
            return ""
        return f"![]({_md_path(path)})"

    # ---------------- 表格
    def table(self, tbl) -> str:
        rows: list[list[str]] = []
        for tr in tbl.findall(f"{_W}tr"):
            cells: list[str] = []
            for tc in tr.findall(f"{_W}tc"):
                cell_parts = []
                for p in tc.findall(f"{_W}p"):
                    cell_parts.append(self._inline(p).strip())
                # 单元格里的换行会毁掉表格结构，压成空格
                cell = " ".join(x for x in cell_parts if x).replace("|", "\\|")
                cells.append(" ".join(cell.split()))
            if cells:
                rows.append(cells)
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        head = "| " + " | ".join(rows[0]) + " |"
        sep = "| " + " | ".join(["---"] * width) + " |"
        body = ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n".join([head, sep] + body)


def _walk_docx(nodes, walker: _DocxWalker, blocks: list[str]) -> None:
    """按文档顺序摊平 w:body。

    只认 p 和 tbl；sdt（内容控件）往里钻一层 —— 不钻的话用了封面控件、
    日期控件的文档会整段整段地凭空消失。
    """
    for child in nodes:
        if child.tag == f"{_W}p":
            got = walker.paragraph(child)
            if got.strip():
                blocks.append(got)
        elif child.tag == f"{_W}tbl":
            got = walker.table(child)
            if got.strip():
                blocks.append(got)
        elif child.tag in (f"{_W}sdt", f"{_W}sdtContent"):
            content = child.find(f"{_W}sdtContent")
            _walk_docx(content if content is not None else child, walker, blocks)


def docx_to_markdown(path) -> str:
    path = Path(path)
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise ConvertError(f"打不开这个 .docx（{e}）") from e

    with z:
        try:
            root = ET.fromstring(z.read("word/document.xml"))
        except KeyError:
            # 有些软件把 .doc 直接改名成 .docx —— 里面根本没有 OOXML 结构
            raise ConvertError("这个文件里没有 word/document.xml，"
                               "可能只是把 .doc 改了后缀") from None
        except ET.ParseError as e:
            raise ConvertError(f"document.xml 解析失败（{e}）") from e

        rels = _load_rels(z)
        numbering = _load_numbering(z)
        media_dir = _media_cache_dir(path)
        walker = _DocxWalker(z, rels, numbering, media_dir)

        body = root.find(f"{_W}body")
        blocks: list[str] = []
        _walk_docx(body if body is not None else root, walker, blocks)

    if not blocks:
        # 兜底：至少把纯文字捞出来，别给用户一个空白页
        try:
            with zipfile.ZipFile(path) as z:
                root = ET.fromstring(z.read("word/document.xml"))
            text = "".join(t.text or "" for t in root.iter(f"{_W}t"))
            return txt_to_markdown(text)
        except Exception:  # noqa: BLE001
            return ""
    return "\n\n".join(blocks)


# ---------------------------------------------------------------- 网页 .html

class _HtmlToMarkdown(HTMLParser):
    """够用的正文提取：标题 / 段落 / 列表 / 引用 / 代码 / 表格 / 图片 / 链接。

    不做正文区域的智能识别（readability 那套）—— 代价太高、误判还难看。
    转而用白名单：script / style / nav / footer 这类噪音直接整块丢掉，
    剩下的按文档顺序摊平。浏览器「另存为网页」的结果用这套基本够看。
    """

    SKIP_TAGS = {
        "script", "style", "noscript", "svg", "iframe", "canvas", "template",
        "form", "nav", "footer", "aside", "button", "input", "select", "textarea",
    }
    BLOCK_TAGS = {
        "p", "div", "section", "article", "header", "main", "figure", "figcaption",
        "address", "fieldset", "details", "summary", "dl", "dt", "dd", "center",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.buf: list[str] = []
        self.skip_depth = 0
        self.in_title = False
        self.title = ""
        self.in_pre = False
        self.pre_buf: list[str] = []
        self.quote_depth = 0
        self.list_stack: list[dict] = []
        self.link_stack: list[tuple[str, int]] = []
        self.table: list[list[str]] | None = None
        self.row: list[str] | None = None
        self.cell_from = 0

    # ---------------- 输出
    def _flush(self) -> str:
        text = "".join(self.buf)
        self.buf = []
        text = text.strip()
        if text:
            if self.quote_depth:
                # 引用里可能是好几行（列表 + 段落），逐行都要挂上 >
                prefix = "> " * self.quote_depth
                text = "\n".join(
                    (prefix + ln) if ln.strip() else prefix.rstrip()
                    for ln in text.split("\n")
                )
            self.blocks.append(text)
        return text

    # ---------------- 标签
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "title":
            self.in_title = True
            return
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush()
            self.buf.append("#" * int(tag[1]) + " ")
        elif tag in ("strong", "b"):
            self.buf.append("**")
        elif tag in ("em", "i"):
            self.buf.append("*")
        elif tag in ("del", "s", "strike"):
            self.buf.append("~~")
        elif tag == "code" and not self.in_pre:
            self.buf.append("`")
        elif tag == "br":
            self.buf.append("  \n")
        elif tag == "hr":
            self._flush()
            self.blocks.append("---")
        elif tag == "pre":
            self._flush()
            self.in_pre = True
            self.pre_buf = []
        elif tag == "blockquote":
            self._flush()
            self.quote_depth += 1
        elif tag in ("ul", "ol"):
            self._flush()
            self.list_stack.append({"ordered": tag == "ol", "n": 0})
        elif tag == "li":
            self._flush()
            depth = max(0, len(self.list_stack) - 1)
            if self.list_stack and self.list_stack[-1]["ordered"]:
                self.list_stack[-1]["n"] += 1
                marker = f'{self.list_stack[-1]["n"]}. '
            else:
                marker = "- "
            self.buf.append("  " * depth + marker)
        elif tag == "img":
            self._image(dict(attrs))
        elif tag == "a":
            self.link_stack.append(((dict(attrs).get("href") or "").strip(), len(self.buf)))
        elif tag == "table":
            self._flush()
            self.table = []
        elif tag == "tr" and self.table is not None:
            self._flush()
            self.row = []
        elif tag in ("td", "th") and self.table is not None:
            self._flush()
            self.cell_from = len(self.buf)
        elif tag in self.BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self.in_title = False
            return
        if tag in self.SKIP_TAGS:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return

        if tag in ("strong", "b"):
            self.buf.append("**")
        elif tag in ("em", "i"):
            self.buf.append("*")
        elif tag in ("del", "s", "strike"):
            self.buf.append("~~")
        elif tag == "code" and not self.in_pre:
            self.buf.append("`")
        elif tag == "pre":
            code = "".join(self.pre_buf).strip("\n")
            self.pre_buf = []
            self.in_pre = False
            self.buf = []
            if code.strip():
                lang = ""
                self.blocks.append(f"```{lang}\n{code}\n```")
        elif tag == "blockquote":
            self._flush()
            self.quote_depth = max(0, self.quote_depth - 1)
        elif tag in ("ul", "ol"):
            self._flush()
            if self.list_stack:
                self.list_stack.pop()
        elif tag == "li":
            self._flush()
        elif tag == "a":
            self._close_link()
        elif tag in ("td", "th") and self.table is not None:
            cell = "".join(self.buf[self.cell_from:])
            del self.buf[self.cell_from:]
            cell = " ".join(cell.split()).replace("|", "\\|")
            if self.row is not None:
                self.row.append(cell)
        elif tag == "tr" and self.table is not None:
            if self.row:
                self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            self._flush()
            md = _rows_to_table(self.table)
            if md:
                self.blocks.append(md)
            self.table = None
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6") or tag in self.BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if self.in_title:
            self.title += data
            return
        if self.skip_depth or not data:
            return
        if self.in_pre:
            self.pre_buf.append(data)
            return
        # HTML 里空白是折叠的：换行、连续空格都压成一个
        collapsed = re.sub(r"[ \t\r\n\u3000]+", " ", data)
        if not collapsed:
            return
        if collapsed == " " and (not self.buf or self.buf[-1].endswith((" ", "\n"))):
            return
        self.buf.append(collapsed)

    # ---------------- 行内小工具
    def _image(self, attrs: dict):
        src = (attrs.get("src") or "").strip()
        alt = (attrs.get("alt") or "").strip().replace("]", " ")
        if not src or src.startswith("data:"):
            return                      # data: 多半是 1px 占位图或图标
        if (attrs.get("width") or "").strip() in ("0", "1"):
            return
        if any(k in src.lower() for k in ("spacer.gif", "blank.gif", "pixel.gif")):
            return
        self.buf.append(f"![{alt}]({src})")

    def _close_link(self):
        if not self.link_stack:
            return
        href, start = self.link_stack.pop()
        text = "".join(self.buf[start:])
        del self.buf[start:]
        if href.startswith(("http://", "https://")) and text.strip():
            self.buf.append(f"[{text}]({href})")
        else:
            # 站内相对链接在外面的文档里没有意义，只留文字
            self.buf.append(text)

    # ---------------- 收尾
    def finish(self) -> str:
        self._flush()
        blocks = [b for b in self.blocks if b.strip()]
        head = " ".join(self.title.split())
        # <title> 是这一页的名字，OneNote 的页面标题就靠它。只有当正文里
        # 已经有一个一模一样的 h1 时才不重复插 —— 光看"有没有 h1"是不够的，
        # 网页里的 h1 常常是站名或栏目名，不是文章标题。
        same = any(b.lstrip().startswith("# ") and b.strip("# ").strip() == head for b in blocks)
        if head and not same:
            blocks.insert(0, f"# {head}")
        return "\n\n".join(blocks)


def _rows_to_table(rows: list[list[str]]) -> str:
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    head = "| " + " | ".join(rows[0]) + " |"
    sep = "| " + " | ".join(["---"] * width) + " |"
    body = ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join([head, sep] + body)


def html_to_markdown(text: str) -> str:
    parser = _HtmlToMarkdown()
    try:
        parser.feed(text)
        parser.close()
    except Exception:  # noqa: BLE001  解析器再烂也不该让整趟导入挂掉
        pass
    return parser.finish()
