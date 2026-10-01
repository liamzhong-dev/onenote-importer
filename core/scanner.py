# -*- coding: utf-8 -*-
"""Vault 扫描与附件定位。

负责：递归发现候选来源文件（md / txt / docx / html）、套用 include/exclude
规则、为 wikilink 附件建立「短名 → 真实路径」索引（还原 Obsidian 的最短路径
解析规则）。

**glob 与格式的分工**：include_globs 只负责「目录层级」，
「哪些后缀算来源文件」由 source_formats 决定（见 core/convert.py）。
两者混在一起的话，用户打开 txt 开关却发现一条都扫不到 —— 因为默认那条
`**/*.md` 还挂在头上。所以匹配前会把 glob 末尾的来源后缀摘掉。
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import convert

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".svg"}
# OneNote 的 <object data-attachment> 对部分类型渲染更好，其余统一按附件处理
ATTACHMENT_EXTS = {".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".zip", ".mp3", ".m4a", ".mp4", ".md"}


@dataclass
class NoteFile:
    path: Path
    rel: str  # 相对 Vault 的路径（POSIX 分隔符）
    text: str = ""

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def fmt(self) -> str | None:
        """来源格式（md / txt / docx / html）；不认识的后缀返回 None。"""
        return convert.format_of(self.path)

    @property
    def is_markdown(self) -> bool:
        """只有 .md 走 frontmatter 解析 —— txt 开头那行 `---` 只是分割线，
        当成 frontmatter 会把中间的内容当 YAML 吃掉。"""
        return self.fmt == "md"


@dataclass
class VaultIndex:
    root: Path
    notes: list[NoteFile] = field(default_factory=list)
    _by_stem: dict[str, list[Path]] = field(default_factory=dict)
    _by_rel: dict[str, Path] = field(default_factory=dict)

    def resolve_attachment(self, ref: str, from_note: Path) -> Path | None:
        """按 Obsidian 的顺序解析 [[xxx]] / ![[xxx]] 里的目标。"""
        ref = ref.strip().strip("|").split("|")[0].strip()
        ref = ref.strip("<>").strip()
        if not ref:
            return None
        ref = ref.replace("\\", "/")
        # 1) 相对于当前 md
        cand = (from_note.parent / ref).resolve()
        if cand.exists() and cand.is_file():
            return cand
        # 2) 相对于 Vault 根（OS 长路径容错）
        cand = (self.root / ref).resolve()
        if cand.exists() and cand.is_file():
            return cand
        # 3) 按文件名匹配（Obsidian 的最短路径规则）
        base = Path(ref).name.lower()
        cands = self._by_stem.get(base)
        if cands:
            return cands[0]
        # 4) 去扩展名再试
        stem = Path(ref).stem.lower()
        cands = self._by_stem.get(stem)
        if cands:
            return cands[0]
        return None


def _match_any(rel: str, patterns: list[str]) -> bool:
    for pat in patterns:
        pat = _drop_source_ext(pat.replace("\\", "/").lstrip("./"))
        if fnmatch.fnmatch(rel, pat):
            return True
        if pat.startswith("**/") and fnmatch.fnmatch(rel, pat[3:]):
            return True
        # 目录级通配：dir/** 匹配其下所有
        if pat.endswith("/**") and (rel == pat[:-3] or rel.startswith(pat[:-2])):
            return True
    return False


def _drop_source_ext(pat: str) -> str:
    """把 glob 末尾的来源后缀摘掉，换成 `*`。

    `**/*.md` → `**/*`、`2026-*.md` → `2026-*`、`日记.md` → `日记*`。
    这样一来后缀筛选完全由 source_formats 负责，用户勾上 txt 之后
    原来那条 `**/*.md` 不会再把 `日记.txt` 挡在外面。
    """
    head, sep, base = pat.rpartition("/")
    stem, ext = os.path.splitext(base)
    if ext.lower() not in convert.SOURCE_EXTS:
        return pat
    if not stem:
        stem = "*"
    if not stem.endswith("*"):
        stem += "*"
    return f"{head}{sep}{stem}"


def scan_vault(root: Path, include_globs: list[str], exclude_globs: list[str],
               formats: list[str] | None = None) -> VaultIndex:
    root = Path(root).resolve()
    idx = VaultIndex(root=root)
    skip_dirs = {".obsidian", ".trash", ".git", "node_modules", ".smart-env"}
    exts = convert.exts_for(formats)

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fn in filenames:
            full = Path(dirpath) / fn
            try:
                rel = full.relative_to(root).as_posix()
            except ValueError:
                continue
            # 索引附件（供 wikilink 解析）—— 这里不筛格式，图片也要能解析到
            idx._by_stem.setdefault(fn.lower(), []).append(full)
            idx._by_rel[rel.lower()] = full
            if full.suffix.lower() not in exts:
                continue
            if exclude_globs and _match_any(rel, exclude_globs):
                continue
            if include_globs and not _match_any(rel, include_globs):
                continue
            idx.notes.append(NoteFile(path=full, rel=rel))

    idx.notes.sort(key=lambda n: n.rel)
    return idx


def is_media_ref(ref: str) -> str:
    """判定一个引用属于图片 / 附件 / 笔记。"""
    ext = Path(ref).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in ATTACHMENT_EXTS or ext:
        return "attachment"
    return "note"


def content_type_for(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".webp": "image/webp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".svg": "image/svg+xml",
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".zip": "application/zip",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".mp4": "video/mp4",
        ".txt": "text/plain",
        ".md": "text/markdown",
    }.get(ext, "application/octet-stream")
