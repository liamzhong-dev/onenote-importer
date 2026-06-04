# -*- coding: utf-8 -*-
"""Markdown → 结构化块（AST）。

零依赖实现，覆盖 Obsidian 日记实际会用到的语法子集：
块级：ATX 标题、代码围栏、引用（含 callout）、嵌套列表与任务清单、
     GFM 表格、分割线、整行嵌入图片 / 附件、%% 注释。
行内：`代码`、**粗**、*斜*、~~删~~、==高亮==、[链接]()、[[wikilink]]、
     ![[嵌入]]、![]()、#标签、行尾双空格换行。
"""
from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------- 行内

_EMOJI_NAME = re.compile(r"^:[a-zA-Z0-9_+-]+:$")

_CODE = r"(?P<icode>`+)(?P<icode_body>.+?)(?P=icode)"
_IMG_MD = r"!\[(?P<md_alt>[^\]]*)\]\((?P<md_src>[^)\s]+)\s*(?:\"(?P<md_title>[^\"]*)\")?\)"
_EMBED = r"!\[\[(?P<embed>[^\]\n]+)\]\]"
_LINK_MD = r"\[(?P<lk_text>[^\]]*)\]\((?P<lk_href>[^)\s]+)\s*(?:\"(?P<lk_title>[^\"]*)\")?\)"
_WIKI = r"\[\[(?P<wiki>[^\]\n]+)\]\]"
_STRONG = r"(?P<s_del>\*\*|__)(?P<s_body>.+?)(?P=s_del)"
_STRIKE = r"(?P<k_del>~~)(?P<k_body>.+?)(?P=k_del)"
_MARK = r"(?P<m_del>==)(?P<m_body>.+?)(?P=m_del)"
_EM = r"(?P<e_del>[*_])(?P<e_body>[^*_\n]+?)(?P=e_del)"
_TAG = r"(?<![A-Za-z0-9_\\])#(?P<tag>[A-Za-z0-9_/\-\u4e00-\u9fff]+)"
_URL = r"(?<![(\w])(?P<url>https?://[^\s<>\)\]]+)"
_EMOJI = r"(?P<emoji>:[a-zA-Z0-9_+-]+:)"

_INLINE = re.compile(
    "|".join([_CODE, _IMG_MD, _EMBED, _LINK_MD, _WIKI, _STRONG, _STRIKE, _MARK, _EM, _TAG, _URL, _EMOJI]),
    re.S,
)

_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!~>])")


def text_inline(v: str) -> dict:
    return {"t": "text", "v": v}


def _clean(s: str) -> str:
    return _ESCAPE.sub(r"\1", s)


def parse_inline(text: str, resolver=None, embed_ctx: str = "inline") -> list[dict]:
    """把一行文本解析成行内节点列表。resolver(ref) -> Path|None 用于解析 [[...]]。"""
    out: list[dict] = []
    buf: list[str] = []

    def flush():
        merged = "".join(buf)
        if merged:
            out.append(text_inline(merged))
            buf.clear()

    pos, n = 0, len(text)
    while pos < n:
        m = _INLINE.search(text, pos)
        if not m:
            buf.append(text[pos:])
            break
        g = m.groupdict()
        start = m.start()

        # 行内强调不允许在单词内部（snake_case 保护）
        if g.get("e_body") is not None and g["e_del"] == "_" and start > 0:
            if text[start - 1].isalnum() or text[start - 1] == "_":
                buf.append(text[pos:start + 1])
                pos = start + 1
                continue

        buf.append(text[pos:start])
        pos = m.end()

        if g.get("icode_body") is not None:
            flush()
            out.append({"t": "code", "v": g["icode_body"].strip()})
            continue

        if g.get("md_src") is not None:
            flush()
            out.append(_make_media_node(g["md_src"], g["md_alt"] or "", resolver, external_ok=True))
            continue

        if g.get("embed") is not None:
            flush()
            raw = g["embed"].strip()
            base_raw = raw.split("#", 1)[0].strip()  # 去掉 #^block 之类的锚点
            node = _make_media_node(base_raw, "", resolver)  # |420 这种宽度参数在里面解析
            if node["t"] == "text":
                target, _, alias = base_raw.partition("|")
                out.append({"t": "note_embed", "target": target.strip(), "alias": alias.strip()})
            else:
                out.append(node)
            continue

        if g.get("lk_href") is not None:
            flush()
            out.append({"t": "link", "href": g["lk_href"], "c": parse_inline(g["lk_text"] or g["lk_href"], resolver)})
            continue

        if g.get("wiki") is not None:
            flush()
            raw = g["wiki"]
            target, _, alias = raw.partition("|")
            target = target.strip()
            alias = alias.strip()
            if resolver and resolver(target, "note") is not None:
                out.append({"t": "wikilink", "target": target, "alias": alias or Path(target).stem})
            else:
                out.append({"t": "wikilink", "target": target, "alias": alias or target})
            continue

        if g.get("s_body") is not None:
            flush()
            out.append({"t": "strong", "c": parse_inline(g["s_body"], resolver)})
            continue
        if g.get("k_body") is not None:
            flush()
            out.append({"t": "strike", "c": parse_inline(g["k_body"], resolver)})
            continue
        if g.get("m_body") is not None:
            flush()
            out.append({"t": "mark", "c": parse_inline(g["m_body"], resolver)})
            continue
        if g.get("e_body") is not None:
            flush()
            out.append({"t": "em", "c": parse_inline(g["e_body"], resolver)})
            continue
        if g.get("tag") is not None:
            buf.append(f"#{_clean(g['tag'])}")
            continue
        if g.get("url") is not None:
            flush()
            out.append({"t": "link", "href": g["url"], "c": [text_inline(g["url"])]})
            continue
        if g.get("emoji") is not None:
            buf.append(g["emoji"])
            continue
        buf.append(text[pos - 1:start + 1] or text[start])
    flush()
    return out


def _make_media_node(ref: str, alt: str, resolver, external_ok: bool = False) -> dict:
    ref = (ref or "").strip()
    if ref.startswith(("http://", "https://", "data:")):
        return {"t": "image", "src": ref, "external": True, "alt": alt, "width": None}
    width = None
    name = ref
    if "|" in ref:  # ![[a.png|300]] 中的宽度或别名
        name, tail = ref.split("|", 1)
        if re.fullmatch(r"\d{1,4}(x\d{1,4})?", tail.strip()):
            width = int(tail.split("x")[0])
        elif not alt:
            alt = tail.strip()
    ext = Path(name).suffix.lower()
    if ext in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".svg"}:
        path = resolver(name, "image") if resolver else None
        if path:
            return {"t": "image", "src": str(path), "external": False, "alt": alt, "width": width}
        return text_inline(f"[缺失图片: {name}]")
    # 其它文件 → 附件
    path = resolver(name, "attachment") if resolver else None
    if path:
        return {"t": "attachment", "src": str(path), "name": Path(name).name, "external": False}
    return text_inline(f"[缺失附件: {name}]")


# ---------------------------------------------------------------- 块级

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_HR = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([^\s`]*)")
_ITEM = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(\[[ xX]\]\s+)?(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_CALLOUT = re.compile(r"^\[!([a-zA-Z]+)\]([+-]?)\s*(.*)$", re.I)
_COMMENT_OPEN = "%%"
_COMMENT_CLOSE = "%%"
_IMAGE_ONLY = re.compile(r"^\s*(!\[\[[^\]]+\]\]|!\[[^\]]*\]\([^)]+\))\s*$")

CALLOUT_STYLE = {
    "note": ("#185FA5", "#E6F1FB"), "info": ("#185FA5", "#E6F1FB"), "summary": ("#185FA5", "#E6F1FB"),
    "abstract": ("#534AB7", "#EEEDFE"), "tldr": ("#534AB7", "#EEEDFE"), "todo": ("#185FA5", "#E6F1FB"),
    "tip": ("#0F6E56", "#E1F5EE"), "hint": ("#0F6E56", "#E1F5EE"), "success": ("#3B6D11", "#EAF3DE"),
    "check": ("#3B6D11", "#EAF3DE"), "done": ("#3B6D11", "#EAF3DE"),
    "question": ("#0F6E56", "#E1F5EE"), "help": ("#0F6E56", "#E1F5EE"), "faq": ("#0F6E56", "#E1F5EE"),
    "warning": ("#854F0B", "#FAEEDA"), "caution": ("#854F0B", "#FAEEDA"), "attention": ("#854F0B", "#FAEEDA"),
    "failure": ("#A32D2D", "#FCEBEB"), "danger": ("#A32D2D", "#FCEBEB"), "error": ("#A32D2D", "#FCEBEB"),
    "bug": ("#A32D2D", "#FCEBEB"),
    "example": ("#534AB7", "#EEEDFE"), "quote": ("#5F5E5A", "#F1EFE8"), "cite": ("#5F5E5A", "#F1EFE8"),
}
CALLOUT_LABEL = {
    "note": "备注", "info": "信息", "summary": "摘要", "abstract": "摘要", "tldr": "摘要", "todo": "待办",
    "tip": "提示", "hint": "提示", "success": "成功", "check": "完成", "done": "完成",
    "question": "疑问", "help": "帮助", "faq": "问答", "warning": "注意", "caution": "小心",
    "attention": "注意", "failure": "失败", "danger": "危险", "error": "错误", "bug": "缺陷",
    "example": "示例", "quote": "引用", "cite": "引用",
}


def _leading(s: str) -> int:
    n = 0
    for ch in s:
        if ch == " ":
            n += 1
        elif ch == "\t":
            n += 4
        else:
            break
    return n


def split_row(line: str) -> list[str]:
    cells, buf, i = [], "", 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line) and line[i + 1] == "|":
            buf += "|"
            i += 2
            continue
        if ch == "|":
            # 忽略首尾空 pipe
            cells.append(buf)
            buf = ""
            i += 1
            continue
        buf += ch
        i += 1
    cells.append(buf)
    if cells and cells[0].strip() == "":
        cells = cells[1:]
    if cells and cells[-1].strip() == "":
        cells = cells[:-1]
    return [c.strip() for c in cells]


class Parser:
    def __init__(self, text: str, resolver=None):
        self.raw = text.replace("\r\n", "\n").replace("\r", "\n")
        self.resolver = resolver

    # -------------------------------------------------- 入口
    def parse(self) -> list[dict]:
        lines = self._strip_comments(self.raw.split("\n"))
        return self.parse_blocks(lines)

    @staticmethod
    def _strip_comments(lines: list[str]) -> list[str]:
        out, skipping = [], False
        for ln in lines:
            if skipping:
                if _COMMENT_CLOSE in ln:
                    skipping = False
                    tail = ln.split(_COMMENT_CLOSE, 1)[1]
                    if tail:
                        out.append(tail)
                continue
            if ln.strip().startswith(_COMMENT_OPEN):
                rest = ln.split(_COMMENT_OPEN, 1)[1]
                if _COMMENT_CLOSE not in rest:
                    skipping = True
                    continue
                continue
            out.append(ln)
        return out

    # -------------------------------------------------- 块级主循环
    def parse_blocks(self, lines: list[str]) -> list[dict]:
        blocks: list[dict] = []
        i, n = 0, len(lines)
        while i < n:
            line = lines[i]

            if not line.strip():
                i += 1
                continue

            m = _FENCE.match(line)
            if m:
                fence = m.group(1)[0] * 3
                lang = (m.group(2) or "").strip()
                body, i = [], i + 1
                while i < n:
                    if lines[i].strip().startswith(fence):
                        i += 1
                        break
                    body.append(lines[i])
                    i += 1
                blocks.append({"type": "code", "lang": lang, "text": "\n".join(body)})
                continue

            m = _HEADING.match(line)
            if m:
                lvl = len(m.group(1))
                blocks.append({"type": "heading", "level": lvl, "c": parse_inline(m.group(2), self.resolver)})
                i += 1
                continue

            if _HR.match(line) and not line.strip().startswith("|"):
                blocks.append({"type": "hr"})
                i += 1
                continue

            if _QUOTE.match(line):
                quoted, i = [], i
                while i < n and (_QUOTE.match(lines[i]) or (lines[i].strip() == "" and i + 1 < n and _QUOTE.match(lines[i + 1]))):
                    mm = _QUOTE.match(lines[i])
                    quoted.append(mm.group(1) if mm else "")
                    i += 1
                blocks.extend(self._build_quote(quoted))
                continue

            # 整行图片 / 附件
            if _IMAGE_ONLY.match(line) and line.strip().startswith("!"):
                nodes = parse_inline(line.strip(), self.resolver)
                node = next((x for x in nodes if x["t"] != "text" or x["v"].strip()), nodes[0])
                if node["t"] == "image":
                    blocks.append({"type": "image_block", **{k: v for k, v in node.items() if k != "t"}})
                elif node["t"] == "attachment":
                    blocks.append({"type": "attachment_block", **{k: v for k, v in node.items() if k != "t"}})
                else:
                    blocks.append({"type": "paragraph", "c": [node]})
                i += 1
                continue

            if _ITEM.match(line):
                items, i = self._parse_list(lines, i)
                blocks.append(items)
                continue

            if "|" in line and i + 1 < n and _TABLE_SEP.match(lines[i + 1]) and "|" in lines[i + 1]:
                blocks.append(self._parse_table(lines, i))
                # 跳过 header + sep + body
                i += 2
                while i < n and "|" in lines[i] and lines[i].strip():
                    i += 1
                continue

            # 段落
            para, i = [], i
            while i < n:
                cur = lines[i]
                if (not cur.strip() or _HEADING.match(cur) or _HR.match(cur) or _FENCE.match(cur)
                        or _QUOTE.match(cur) or _ITEM.match(cur) or (_IMAGE_ONLY.match(cur) and cur.strip().startswith("!"))):
                    break
                para.append(cur.rstrip())
                i += 1
            if para:
                text = "  \n".join(para)  # 行尾双空格 → 软换行
                blocks.append({"type": "paragraph", "c": parse_inline(text, self.resolver)})
        return blocks

    # -------------------------------------------------- 引用 / callout
    def _build_quote(self, quoted: list[str]) -> list[dict]:
        callout = None
        first = quoted[0].strip() if quoted else ""
        m = _CALLOUT.match(first)
        if m:
            ctype = m.group(1).lower()
            fold = m.group(2) == "-"
            title = m.group(3).strip()
            callout = {
                "kind": ctype,
                "label": title or CALLOUT_LABEL.get(ctype, ctype),
                "folded": fold,
                "fg": CALLOUT_STYLE.get(ctype, ("#185FA5", "#E6F1FB"))[0],
                "bg": CALLOUT_STYLE.get(ctype, ("#185FA5", "#E6F1FB"))[1],
            }
            quoted = quoted[1:]
        inner = self.parse_blocks(quoted)
        return [{"type": "quote", "callout": callout, "c": inner}]

    # -------------------------------------------------- 列表
    def _parse_list(self, lines: list[str], i: int) -> tuple[dict, int]:
        """收集属于同一个列表的所有行，再交给 _build_items 还原层级。

        边界规则：遇到标题 / 代码围栏 / 分割线 / 引用 / 表格，或缩进回退到 base
        且不是列表项时立即结束；空行只有在后面仍跟着列表内容时才保留。
        """
        n = len(lines)
        base = _leading(lines[i])
        first_ordered = bool(re.match(r"^\s*\d{1,9}[.)]", lines[i].lstrip()))
        collected: list[str] = []
        j = i

        def is_item(s: str) -> bool:
            return bool(_ITEM.match(s.lstrip()))

        while j < n:
            cur = lines[j]
            if not cur.strip():
                k = j + 1
                while k < n and not lines[k].strip():
                    k += 1
                if k >= n:
                    break
                nxt = lines[k]
                lead_next = _leading(nxt)
                if lead_next > base:
                    collected.append("")
                    j = k
                    continue
                if lead_next == base and is_item(nxt) and bool(re.match(r"^\s*\d{1,9}[.)]", nxt.lstrip())) == first_ordered:
                    collected.append("")
                    j = k
                    continue
                break

            lead = _leading(cur)
            if _FENCE.match(cur) or _HEADING.match(cur) or _HR.match(cur) or _QUOTE.match(cur):
                break
            if lead <= base and "|" in cur:  # 表格起始
                break
            if is_item(cur) and (lead == base or lead > base):
                if lead == base and bool(re.match(r"^\s*\d{1,9}[.)]", cur.lstrip())) != first_ordered:
                    break
                collected.append(cur)
                j += 1
                continue
            if lead > base:  # 子内容 / 惰性延续
                collected.append(cur)
                j += 1
                continue
            # 缩进回退且不是列表项 → 列表结束
            break

        while collected and not collected[-1].strip():
            collected.pop()
        ordered = first_ordered
        items = self._build_items(collected, base)
        return {"type": "list", "ordered": ordered, "items": items}, j

    def _build_items(self, lines: list[str], indent: int) -> list[dict]:
        items: list[dict] = []
        i, n = 0, len(lines)

        while i < n:
            raw = lines[i]
            if not raw.strip():
                i += 1
                continue
            cur_indent = _leading(raw)
            if cur_indent < indent:
                break
            m = _ITEM.match(raw.lstrip())
            if cur_indent == indent and m:
                marker = m.group(2)
                checked = None
                if m.group(3) is not None:
                    checked = m.group(3).strip().lower() == "[x]"
                content = [m.group(4)]
                i += 1
                # 收集本项后续行（缩进更深或空行延续）
                while i < n:
                    nxt = lines[i]
                    if not nxt.strip():
                        i += 1
                        continue
                    nxt_indent = _leading(nxt)
                    if nxt_indent > indent or (nxt_indent >= indent and not _ITEM.match(nxt.lstrip())):
                        content.append(nxt)
                        i += 1
                        continue
                    break
                while content and not content[-1].strip():
                    content.pop()
                # 分离子列表
                child_block_lines: list[str] = []
                while content and _ITEM.match(content[-1].lstrip() or ""):
                    child_block_lines.insert(0, content.pop())
                children = None
                if child_block_lines:
                    sub_indent = min(_leading(x) for x in child_block_lines if x.strip())
                    children = {
                        "type": "list",
                        "ordered": bool(re.match(r"^\s*\d{1,9}[.)]", child_block_lines[0].lstrip())),
                        "items": self._build_items(child_block_lines, sub_indent),
                    }
                inline_src = "  \n".join(x.strip() for x in content) if content else ""
                items.append({
                    "checked": checked,
                    "marker": marker,
                    "c": parse_inline(inline_src, self.resolver) if inline_src else [],
                    "children": children,
                })
                continue
            if cur_indent > indent:
                i += 1
                continue
            i += 1
        return items

    # -------------------------------------------------- 表格
    def _parse_table(self, lines: list[str], i: int) -> dict:
        header = [parse_inline(c, self.resolver) for c in split_row(lines[i])]
        aligns = [("center" if c.startswith(":") and c.endswith(":") else "right" if c.endswith(":") else "left" if c.startswith(":") else "")
                  for c in split_row(lines[i + 1])]
        rows = []
        j = i + 2
        n = len(lines)
        while j < n and "|" in lines[j] and lines[j].strip():
            rows.append([parse_inline(c, self.resolver) for c in split_row(lines[j])])
            j += 1
        return {"type": "table", "header": header, "align": aligns, "rows": rows}
