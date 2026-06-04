# -*- coding: utf-8 -*-
"""AST → OneNote 可用的 HTML。

OneNote 只接受受限 HTML 子集：不能用 class / 外部 CSS / script，
图片与附件必须以 multipart 二进制部件（name:part）或 data URI 提供。
这里产出的子树统一包在 <div data-id="content-root"> 内，
这样更新页面时能用 PATCH target="#content-root" 做整块替换。
"""
from __future__ import annotations

import datetime as _dt
import html
from dataclasses import dataclass, field
from pathlib import Path

from . import scanner
from .frontmatter import NoteMeta

CONTENT_DIV_ID = "content-root"

HEADING_SIZE = {1: "20pt", 2: "16pt", 3: "14pt", 4: "12pt", 5: "11pt", 6: "11pt"}
DEFAULT_FONT = "'Microsoft YaHei','Segoe UI',sans-serif"
MONO_FONT = "'Cascadia Mono','Consolas',monospace"


@dataclass
class MediaPart:
    part_name: str          # multipart 里的部件名，HTML 用 name:xxx 引用
    path: Path | None
    content_type: str
    kind: str               # image | file
    size: int = 0

    def read_bytes(self) -> bytes:
        return self.path.read_bytes() if self.path else b""


@dataclass
class RenderedPage:
    title: str
    html: str
    media: list[MediaPart] = field(default_factory=list)
    created: _dt.datetime | None = None
    warnings: list[str] = field(default_factory=list)
    inner_html: str = ""  # <div data-id="content-root"> 里的内容，供 PATCH 整块替换使用

    def total_bytes(self) -> int:
        return len(self.html.encode("utf-8")) + sum(m.size for m in self.media)


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def attr(s: str) -> str:
    return html.escape(str(s), quote=True)


class Renderer:
    def __init__(self, cfg, note_path: Path):
        self.cfg = cfg
        self.note_path = note_path
        self.media: list[MediaPart] = []
        self.warnings: list[str] = []
        self._img_seq = 0
        self._att_seq = 0

    # ------------------------------------------------------ 媒体
    def _attach_image(self, node: dict) -> str:
        src = node["src"]
        if node.get("external"):
            return f'<img src="{attr(src)}" alt="{attr(node.get("alt", ""))}" />'
        path = Path(src)
        if not path.exists():
            self.warnings.append(f"图片不存在：{path.name}")
            return esc(f"[缺失图片: {path.name}]")
        size = path.stat().st_size
        if size > self.cfg.max_media_bytes:
            self.warnings.append(f"图片 {path.name} 超过 {self.cfg.max_media_bytes // 1024 // 1024}MB，已跳过嵌入")
            return esc(f"[图片过大未嵌入: {path.name}]")
        name = f"imgblock{self._img_seq}"
        self._img_seq += 1
        self.media.append(MediaPart(name, path, scanner.content_type_for(path), "image", size))
        width = node.get("width")
        if width is None:
            width = self.cfg.image_width_limit
        else:
            width = min(int(width), self.cfg.image_width_limit)
        alt = node.get("alt") or path.stem
        return f'<img src="name:{name}" alt="{attr(alt)}" width="{width}" />'

    def _attach_file(self, node: dict) -> str:
        path = Path(node["src"])
        if not path.exists():
            self.warnings.append(f"附件不存在：{node.get('name', path.name)}")
            return esc(f"[缺失附件: {node.get('name', path.name)}]")
        size = path.stat().st_size
        if size > self.cfg.max_media_bytes:
            self.warnings.append(f"附件 {path.name} 过大，已跳过")
            return esc(f"[附件过大未嵌入: {path.name}]")
        name = f"fileblock{self._att_seq}"
        self._att_seq += 1
        ctype = scanner.content_type_for(path)
        self.media.append(MediaPart(name, path, ctype, "file", size))
        fname = node.get("name") or path.name
        return f'<object data-attachment="{attr(fname)}" data="name:{name}" type="{attr(ctype)}" />'

    # ------------------------------------------------------ 行内
    def inline(self, nodes: list[dict]) -> str:
        out = []
        for node in nodes:
            t = node["t"]
            if t == "text":
                out.append(esc(node["v"]).replace("  \n", "<br/>").replace("\n", "<br/>"))
            elif t == "code":
                out.append(f'<span style="font-family:{MONO_FONT};background-color:#F1EFE8">{esc(node["v"])}</span>')
            elif t == "strong":
                out.append(f"<b>{self.inline(node['c'])}</b>")
            elif t == "em":
                out.append(f"<i>{self.inline(node['c'])}</i>")
            elif t == "strike":
                out.append(f'<span style="text-decoration:line-through">{self.inline(node["c"])}</span>')
            elif t == "mark":
                out.append(f'<span style="background-color:#FAC775">{self.inline(node["c"])}</span>')
            elif t == "link":
                out.append(f'<a href="{attr(node["href"])}">{self.inline(node["c"])}</a>')
            elif t == "wikilink":
                out.append(f'<span style="color:#185FA5">[[{esc(node["alias"])}]]</span>')
            elif t == "note_embed":
                out.append(f'<span style="color:#888780">[[{esc(node["alias"] or node["target"])}]]</span>')
            elif t == "image":
                out.append(self._attach_image(node))
            elif t == "attachment":
                out.append(self._attach_file(node))
            else:
                out.append(esc(str(node.get("v", ""))))
        return "".join(out)

    # ------------------------------------------------------ 块级
    def blocks(self, bs: list[dict]) -> str:
        return "".join(self.block(b) for b in bs)

    def block(self, b: dict) -> str:
        if not isinstance(b, dict):
            return ""
        kind = b.get("type", "paragraph")
        if kind == "heading":
            lvl = min(max(int(b.get("level", 1)), 1), 6)
            return f'<h{lvl} style="font-family:{DEFAULT_FONT};font-size:{HEADING_SIZE[lvl]}">{self.inline(b.get("c", []))}</h{lvl}>'
        if kind == "paragraph":
            body = self.inline(b.get("c", []))
            if not body.strip():
                return ""
            return f'<p style="font-family:{DEFAULT_FONT};font-size:11pt">{body}</p>'
        if kind == "hr":
            return "<hr/>"
        if kind == "code":
            lang = b.get("lang", "")
            head = f'<p style="font-family:{MONO_FONT};font-size:9pt;color:#5F5E5A">{esc(lang or "code")}</p>' if lang else ""
            return (head + f'<pre style="font-family:{MONO_FONT};font-size:10pt;background-color:#F1EFE8;'
                    f'padding:6pt;border:1px solid #D3D1C7">{esc(b.get("text", ""))}</pre>')
        if kind == "quote":
            return self._quote(b)
        if kind == "list":
            return self._list(b)
        if kind == "table":
            return self._table(b)
        if kind == "image_block":
            img = self._attach_image(b)
            cap = b.get("alt", "")
            return f'<p style="text-align:left">{img}</p>' + (f'<p style="font-size:9pt;color:#888780">{esc(cap)}</p>' if cap else "")
        if kind == "attachment_block":
            return f'<p>{self._attach_file(b)}</p>'
        return ""

    def _quote(self, b: dict) -> str:
        inner = self.blocks(b.get("c", []))
        call = b.get("callout")
        if call:
            label = f'<p style="font-family:{DEFAULT_FONT};font-weight:bold;color:{call["fg"]}">{esc(call["label"])}</p>'
            return (f'<div style="background-color:{call["bg"]};border-left:3px solid {call["fg"]};'
                    f'padding:6pt 10pt">{label}{inner}</div>')
        return f'<div style="border-left:3px solid #B4B2A9;padding-left:10pt;color:#444441">{inner}</div>'

    def _list(self, b: dict) -> str:
        tag = "ol" if b.get("ordered") else "ul"
        return f'<{tag} style="font-family:{DEFAULT_FONT};font-size:11pt">{self._items(b.get("items", []))}</{tag}>'

    def _items(self, items: list[dict]) -> str:
        out = []
        for it in items:
            checked = it.get("checked")
            body = self.inline(it.get("c", []))
            if checked is None:
                head = "<li>"
            else:
                head = '<li data-tag="to-do">'
                body = ("☑ " if checked else "☐ ") + body
            child = it.get("children")
            nested = self._list(child) if child else ""
            # 子列表嵌在 <li> 内部，避免 OneNote 收到非法的 li → ul 兄弟结构
            out.append(f"{head}{body}{nested}</li>")
        return "".join(out)

    def _table(self, b: dict) -> str:
        aligns = b.get("align") or []
        head_cells = []
        for idx, cell in enumerate(b.get("header", [])):
            a = aligns[idx] if idx < len(aligns) else ""
            style = f"text-align:{a};" if a else ""
            head_cells.append(
                f'<th style="{style}border:1px solid #B4B2A9;padding:4pt;background-color:#F1EFE8">{self.inline(cell)}</th>'
            )
        head = f'<tr>{"".join(head_cells)}</tr>'
        rows = []
        for row in b.get("rows", []):
            cells = []
            for idx, cell in enumerate(row):
                a = aligns[idx] if idx < len(aligns) else ""
                style = f"text-align:{a};" if a else ""
                cells.append(f'<td style="{style}border:1px solid #D3D1C7;padding:4pt">{self.inline(cell)}</td>')
            rows.append(f'<tr>{"".join(cells)}</tr>')
        return (f'<table style="border-collapse:collapse;font-family:{DEFAULT_FONT};font-size:10pt">'
                f"{head}{''.join(rows)}</table>")


# ---------------------------------------------------------------- 页面组装

def render_title(meta: NoteMeta, template: str) -> str:
    title = (meta.title or "").strip()
    if not meta.date:
        out = template.replace("{title}", title)
        for key in ("{date}", "{YYYY}", "{MM}", "{DD}", "{M}", "{D}"):
            out = out.replace(key, "")
        return " ".join(out.split()).strip(" -") or title or "未命名"

    d = meta.date
    date_full = d.strftime("%Y-%m-%d")
    # 文件名本身就是日期时（2026-09-07.md），把标题里重复的那截剥掉，
    # 免得「{date} {title}」写成「2026-09-07 2026-09-07」
    if "{title}" in template and (title == date_full or title.startswith(date_full + " ")):
        title = title[len(date_full):].strip()
    out = (template
           .replace("{date}", date_full)
           .replace("{YYYY}", f"{d.year:04d}")
           .replace("{MM}", f"{d.month:02d}")
           .replace("{DD}", f"{d.day:02d}")
           .replace("{M}", str(d.month))
           .replace("{D}", str(d.day))
           .replace("{title}", title))
    return " ".join(out.split()).strip(" -") or date_full


def render_frontmatter_block(fm: dict) -> str:
    if not fm:
        return ""
    rows = "".join(
        f'<tr><td style="border:1px solid #D3D1C7;padding:3pt;color:#5F5E5A">{esc(str(k))}</td>'
        f'<td style="border:1px solid #D3D1C7;padding:3pt">{esc(str(v))}</td></tr>'
        for k, v in fm.items() if not isinstance(v, (dict,))
    )
    if not rows:
        return ""
    return (f'<table style="border-collapse:collapse;font-size:9pt;font-family:{DEFAULT_FONT};'
            f'background-color:#F1EFE8">{rows}</table>')


def render_note(note_path: Path, rel: str, meta: NoteMeta, blocks: list[dict], cfg) -> RenderedPage:
    r = Renderer(cfg, note_path)
    body_parts: list[str] = []

    if cfg.keep_frontmatter_block:
        fm_html = render_frontmatter_block(meta.frontmatter)
        if fm_html:
            body_parts.append(fm_html)

    body_parts.append(r.blocks(blocks))

    tags_line = ""
    if meta.tags:
        tags_line = (f'<p style="font-size:9pt;color:#185FA5">'
                     + " ".join(f"#{esc(t)}" for t in meta.tags) + "</p>")
        body_parts.append(tags_line)

    stamp = _dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    where = meta.date.strftime("%Y-%m-%d") if meta.date else "未标注日期"
    body_parts.append(
        f'<p style="font-size:8pt;color:#888780">来源 {esc(rel)} · 原文日期 {esc(where)}'
        f'{"" if meta.date_source == "frontmatter" else esc("（由" + meta.date_source + "推断）")}'
        f' · 同步于 {esc(stamp)}</p>'
    )

    body = "".join(body_parts)
    title = render_title(meta, cfg.title_template)

    created_meta = ""
    if meta.date:
        try:
            created_meta = f'<meta name="created" content="{meta.date.strftime("%Y-%m-%dT%H:%M:%S")}+08:00" />'
        except Exception:
            created_meta = ""

    html_doc = (
        "<!DOCTYPE html>\n"
        f'<html><head><title>{esc(title)}</title>'
        f'<meta charset="utf-8" />{created_meta}</head>'
        f'<body style="font-family:{DEFAULT_FONT};font-size:11pt">'
        f'<div data-id="{CONTENT_DIV_ID}">{body}</div>'
        "</body></html>"
    )
    return RenderedPage(title=title, html=html_doc, media=r.media, created=meta.date,
                        warnings=r.warnings, inner_html=body)
