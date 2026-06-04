# -*- coding: utf-8 -*-
"""YAML frontmatter 的轻量解析。

只支持 Obsidian 日记实际会用到的子集：标量、流式列表 [a, b]、块级列表 - a。
不引入 PyYAML，保持零依赖。
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from pathlib import Path

_FENCE = re.compile(r"^---\s*$")
_DATE_PATTERNS = [
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y年%m月%d日",
    "%d/%m/%Y",
]
_ISO_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(:\d{2})?")
_FILENAME_DATE_RE = re.compile(r"(\d{4})[-_.年](\d{1,2})[-_.月](\d{1,2})")


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def split_frontmatter(text: str) -> tuple[dict, str]:
    """把 md 文本拆成 (frontmatter 字典, 正文)。"""
    lines = text.splitlines()
    if not lines or not _FENCE.match(lines[0].strip()):
        return {}, text
    for i in range(1, len(lines)):
        if _FENCE.match(lines[i].strip()) or lines[i].strip() in ("...", "---"):
            raw = lines[1:i]
            body = "\n".join(lines[i + 1:])
            return parse_simple_yaml(raw), body.lstrip("\n")
    return {}, text


def parse_simple_yaml(lines: list[str]) -> dict:
    meta: dict = {}
    current_key: str | None = None
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()

        # 块级列表项
        if line.startswith("- ") and current_key:
            val = _strip_quotes(line[2:])
            cur = meta.get(current_key)
            if isinstance(cur, list):
                cur.append(val)
            elif cur in (None, ""):
                meta[current_key] = [val]
            else:
                meta[current_key] = [cur, val]
            continue

        m = re.match(r"^([A-Za-z0-9_\-\u4e00-\u9fff]+)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if indent > 0 and current_key:
            # 一级嵌套：拼到父级的 dict 里
            parent = meta.get(current_key)
            if not isinstance(parent, dict):
                parent = {}
                meta[current_key] = parent
            parent[key] = _coerce(val)
            continue
        current_key = key
        meta[key] = _coerce(val)
    return meta


def _coerce(val: str):
    if val == "":
        return ""
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [_strip_quotes(x) for x in _split_csv(inner)]
    if val.lower() in ("true", "false"):
        return val.lower() == "true"
    if re.fullmatch(r"-?\d+", val):
        return int(val)
    return _strip_quotes(val)


def _split_csv(s: str) -> list[str]:
    out, buf, quote = [], "", None
    for ch in s:
        if quote:
            if ch == quote:
                quote = None
            buf += ch
        elif ch in "\"'":
            quote = ch
            buf += ch
        elif ch == ",":
            out.append(buf)
            buf = ""
        else:
            buf += ch
    out.append(buf)
    return [x.strip() for x in out if x.strip()]


def parse_date(value) -> _dt.datetime | None:
    """把各种写法的日期统一成 datetime（无时区，按本地语义理解）。"""
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, _dt.date):
        return _dt.datetime(value.year, value.month, value.day)
    if not isinstance(value, str):
        return None
    s = value.strip()
    m = _ISO_RE.match(s)
    if m:
        try:
            return _dt.datetime.fromisoformat(s.replace("Z", "").replace(" ", "T")[:19])
        except Exception:
            pass
    for pat in _DATE_PATTERNS:
        try:
            return _dt.datetime.strptime(s, pat)
        except ValueError:
            continue
    return None


@dataclass
class NoteMeta:
    frontmatter: dict
    title: str
    date: _dt.datetime | None
    tags: list[str]
    date_source: str  # frontmatter | filename | mtime | none


def collect_meta(
    md_path: Path,
    frontmatter: dict,
    body: str,
    date_keys: list[str],
    title_keys: list[str],
) -> NoteMeta:
    """按 frontmatter → 文件名 → 一级标题 → mtime 的优先级推断日期与标题。"""
    # --- 标题 ---
    title = ""
    for k in title_keys:
        v = frontmatter.get(k)
        if isinstance(v, str) and v.strip():
            title = v.strip()
            break
    if not title:
        for line in body.splitlines()[:30]:
            m = re.match(r"^#\s+(.+)$", line.strip())
            if m:
                title = m.group(1).strip()
                break
    if not title:
        title = md_path.stem

    # --- 日期 ---
    date, source = None, "none"
    for k in date_keys:
        for actual in (k, k.lower(), k.upper()):
            if actual in frontmatter:
                date = parse_date(frontmatter[actual])
                if date:
                    source = "frontmatter"
                    break
        if date:
            break
    if not date:
        m = _FILENAME_DATE_RE.search(md_path.stem)
        if m:
            try:
                date = _dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                source = "filename"
            except ValueError:
                date = None
    if not date:
        try:
            date = _dt.datetime.fromtimestamp(md_path.stat().st_mtime)
            source = "mtime"
        except OSError:
            pass

    # --- 标签 ---
    tags: list[str] = []
    for key in ("tags", "tag", "标签"):
        v = frontmatter.get(key)
        if isinstance(v, list):
            tags += [str(x).lstrip("#").strip() for x in v if str(x).strip()]
        elif isinstance(v, str) and v.strip():
            tags += [x.strip().lstrip("#") for x in _split_csv(v) if x.strip()]
    body_tags = [m.group(1) for m in re.finditer(r"(?:^|\s)#([\w\u4e00-\u9fff/-]+)", body)]
    seen, uniq = set(), []
    for t in tags + body_tags:
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    return NoteMeta(frontmatter=frontmatter, title=title, date=date, tags=uniq, date_source=source)
