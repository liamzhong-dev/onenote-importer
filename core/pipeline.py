# -*- coding: utf-8 -*-
"""编排层：扫描 → 渲染 → 幂等判定 → 写入。

GUI 与 CLI 都只跟 Pipeline 打交道，不直接碰 writer。
"""
from __future__ import annotations

import datetime as _dt
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from . import scanner
from .config import Config
from .frontmatter import collect_meta, split_frontmatter
from .mdast import Parser
from .onenote_html import RenderedPage, render_note
from .scanner import VaultIndex
from .state import PageRecord, StateDB, sha256_text
from writers.base import OneNoteWriter, WriterError
from writers.factory import build_writer

CREATE, UPDATE, SKIP, FAIL = "create", "update", "skip", "fail"

# 进度区那一行是给人看的，别把 create / update 这种内部动作名直接摆上去
_ACT_CN = {CREATE: "新建", UPDATE: "更新", SKIP: "跳过", FAIL: "失败"}


@dataclass
class PlanItem:
    rel: str
    path: Path
    action: str
    title: str
    section: str
    reason: str = ""
    page_id: str = ""
    # 计划阶段拉过已有分区 / 已有页面才有值：
    #   section_exists —— 目标分区在 OneNote 里已存在（会复用，而不是新建）
    #   matched_page_id —— 分区里已有同名页面，直接接管它
    section_exists: bool | None = None
    matched_page_id: str = ""


SEASON_NAMES = ("冬", "春", "夏", "秋")  # 索引 = (month % 12) // 3 → 12月、1月、2月都是冬


def season_of(date: _dt.datetime) -> tuple[str, int]:
    """返回 (季节名, 归属年份)。

    冬季跨年，按「冬天开始的那一年」归属：2025-12 / 2026-01 / 2026-02 → 2025 年冬。
    """
    season = SEASON_NAMES[(date.month % 12) // 3]
    year = date.year - 1 if date.month in (1, 2) else date.year
    return season, year


def section_name_for(template: str, date: _dt.datetime | None, meta_title: str) -> str:
    if date is None:
        out = (template
               .replace("{YYYY-MM}", "未标注日期")
               .replace("{YYYY}", "未标注日期")
               .replace("{MM}", "").replace("{DD}", "")
               .replace("{M}", "").replace("{D}", "")
               .replace("{season}", "")
               .replace("{title}", meta_title))
        return " ".join(out.split()).strip(" -") or "未标注日期"
    season, season_year = season_of(date)
    out = (template
           .replace("{YYYY-MM}", f"{date.year:04d}-{date.month:02d}")
           .replace("{YYYY}", f"{date.year:04d}")
           .replace("{MM}", f"{date.month:02d}")
           .replace("{M}", str(date.month))
           .replace("{DD}", f"{date.day:02d}")
           .replace("{D}", str(date.day))
           .replace("{season_year}", f"{season_year:04d}")
           .replace("{season}", season)
           .replace("{title}", meta_title))
    return " ".join(out.split()) or f"{date.year:04d}-{date.month:02d}"


def resolve_section_name(cfg: Config, date: _dt.datetime | None, meta_title: str) -> str:
    """按配置决定这一篇该进哪个分区。

    fixed 模式永远返回同一个分区名 —— 这样导入就是往你既有分区里追加页面。
    """
    if (cfg.section_mode or "template") == "fixed":
        name = (cfg.section_fixed or "").strip()
        if name:
            return name
    return section_name_for(cfg.section_template, date, meta_title)


class StopSignal(Exception):
    pass


class Pipeline:
    def __init__(self, cfg: Config, log=None, step=None):
        self.cfg = cfg
        self.log = log or (lambda level, msg: None)
        # step(text)：只在「正在做某件事」时调一次，给界面进度提示用。
        # 与 log 的区别是它不进日志、不做历史记录，只表达此刻的状态。
        self.step = step or (lambda text: None)
        self.state = StateDB(cfg.state_db_path())
        self.writer: OneNoteWriter | None = None
        self._sections: dict[str, str] = {}
        self.index: VaultIndex | None = None
        self.notebook_name: str = cfg.notebook_name
        # 分区名（小写）→ 导入前该分区已有的页面数，用于报告「84 → 93」这种增量
        self.section_pages: dict[str, dict[str, str]] = {}
        self.last_page_id: str = ""

    # ------------------------------------------------------------ 连接
    def _logfn(self):
        """writer 用的回调顺序是 (level, msg)，而 Pipeline 内部用的是 (msg, level)。

        直接把 self.log 递下去会让两边错位——日志里会出现「消息=INFO」这种乱套的输出。
        """
        return lambda level, msg: self.log(msg, level)

    def connect(self) -> OneNoteWriter:
        if self.writer is None:
            probes = []
            self.step("探测写入通道")
            try:
                self.writer, probes = build_writer(self.cfg, self._logfn(), self.step)
            except RuntimeError as e:
                for line in str(e).split("；"):
                    self.log(f"通道探测：{line}", "WARN")
                raise
            for kind, res in probes:
                self.log(f"通道 {kind}: {'可用' if res.available else res.reason}", "INFO")
            self.log(f"已选用通道：{self.writer.name}", "INFO")
        return self.writer

    # ------------------------------------------------------------ 扫描
    def scan(self) -> list[PlanItem]:
        cfg = self.cfg
        root = Path(cfg.vault_path)
        self.step("扫描本地日记文件")
        self.index = scanner.scan_vault(root, cfg.include_globs, cfg.exclude_globs)
        items: list[PlanItem] = []
        for nf in self.index.notes:
            text = self._read(nf.path)
            fm, body = split_frontmatter(text)
            meta = collect_meta(nf.path, fm, body, cfg.date_frontmatter_keys, cfg.title_frontmatter_keys)
            nf.text = text
            section = resolve_section_name(cfg, meta.date, meta.title)
            from .onenote_html import render_title
            title = render_title(meta, cfg.title_template)
            rec = self.state.get(nf.rel)
            digest = sha256_text(text)
            if rec is None:
                items.append(PlanItem(nf.rel, nf.path, CREATE, title, section, "首次同步"))
            elif rec.sha256 != digest:
                action = UPDATE if cfg.update_existing else SKIP
                items.append(PlanItem(nf.rel, nf.path, action, title, section,
                                      "内容有变更" if cfg.update_existing else "内容有变更，但未开启更新",
                                      page_id=rec.page_id))
            else:
                items.append(PlanItem(nf.rel, nf.path, SKIP, title, section, "内容未变化",
                                      page_id=rec.page_id))
        self.plan = items
        return items

    # ------------------------------------------------------------ 目标摸底
    def inspect_targets(self, items: list[PlanItem]) -> bool:
        """只读核对 OneNote 侧现状：分区是否已存在、分区里有没有同名页面。

        不会创建任何笔记本 / 分区 / 页面。通道连不上就返回 False，
        计划照样能预览，只是少了「复用 / 新建」的标注。
        """
        cfg = self.cfg
        self.step("核对 OneNote 里的分区与页面")
        try:
            writer = self.connect()
        except Exception as e:
            self.log(f"未连接 OneNote，跳过目标核对：{e}", "WARN")
            return False

        try:
            nb = writer.find_notebook(cfg.notebook_name)
            if nb is None:
                self.log(f"OneNote 里还没有笔记本「{cfg.notebook_name}」，导入时会新建", "WARN")
                return False
            self.notebook_name = nb.name
            existing = {s.name.strip().lower(): s for s in writer.list_sections(nb.id)}
        except Exception as e:
            self.log(f"读取目标笔记本失败：{e}", "WARN")
            return False

        wanted = [it.section for it in items]
        # 比对时忽略大小写与首尾空格，但日志里要显示用户原本写的样子
        uniq_display: dict[str, str] = {}
        for name in wanted:
            uniq_display.setdefault(name.strip().lower(), name.strip())
        uniq = list(uniq_display)
        reused = [k for k in uniq if k in existing]
        missing = [k for k in uniq if k not in existing]
        self.log(f"目标笔记本「{nb.name}」现有 {len(existing)} 个分区；"
                 f"本次命中已有分区 {len(reused)} 个、需要新建 {len(missing)} 个", "INFO")
        for k in reused:
            self.log(f"分区「{uniq_display[k]}」已存在，日记会接在原有页面后面写入", "OK")
        for k in missing:
            self.log(f"分区「{uniq_display[k]}」不存在，导入时会新建（若你想复用既有分区，"
                     f"把分区名改成一致的，或在「分区方式」里选固定写入一个分区）", "WARN")

        page_map: dict[str, dict[str, str]] = {}
        for k in reused:
            sec = existing[k]
            try:
                # 即使策略是「另建新页」也要读一次：一来看得见分区里现在有多少页，
                # 二来「导出后到底接在哪」这件事只有回读才算数。
                page_map[k] = {t.strip().lower(): pid for pid, t in writer.list_pages(sec.id)}
                self.section_pages[k] = page_map[k]
                self.log(f"分区「{sec.name}」现有 {len(page_map[k])} 页", "INFO")
            except Exception as e:
                self.log(f"读取分区「{sec.name}」的页面清单失败：{e}", "WARN")

        for it in items:
            key = it.section.strip().lower()
            it.section_exists = key in existing
            if not it.section_exists:
                continue
            hit = page_map.get(key, {}).get(it.title.strip().lower())
            if not hit:
                continue
            it.matched_page_id = hit
            if cfg.existing_page_policy == "update":
                it.action = UPDATE
                it.reason = "分区里已有同名页面，就地更新"
            elif cfg.existing_page_policy == "skip":
                it.action = SKIP
                it.reason = "分区里已有同名页面，按设置跳过"
        return True

    @staticmethod
    def _read(path: Path) -> str:
        return path.read_text(encoding="utf-8", errors="replace")

    # ------------------------------------------------------------ 渲染
    def render(self, nf) -> tuple[RenderedPage, str]:
        cfg = self.cfg
        assert self.index is not None
        fm, body = split_frontmatter(nf.text)

        def resolver(ref: str, kind: str):
            return self.index.resolve_attachment(ref, nf.path)

        blocks = Parser(body, resolver=resolver).parse()
        meta = collect_meta(nf.path, fm, body, cfg.date_frontmatter_keys, cfg.title_frontmatter_keys)
        page = render_note(nf.path, nf.rel, meta, blocks, cfg)
        return page, page.title

    # ------------------------------------------------------------ 执行
    def run(self, items: list[PlanItem], dry_run: bool | None = None,
            stop: threading.Event | None = None,
            progress=None) -> dict:
        cfg = self.cfg
        dry = cfg.dry_run if dry_run is None else dry_run
        if self.index is None:
            self.scan()
        if dry:
            self.log("预览模式：只算计划、不碰 OneNote —— 这次运行不会产生任何页面", "INFO")

        writer = None if dry else self.connect()
        if writer is not None and all(it.section_exists is None for it in items):
            # 没先做「扫描预览」就直接开始，这里补一次目标核对，
            # 否则会认不出 OneNote 里已有的同名页面而重复建页
            self.inspect_targets(items)
        stats = {"created": 0, "updated": 0, "skipped": 0, "failed": 0, "total": len(items)}
        failures: list[tuple[str, str]] = []
        total = len(items)

        for i, item in enumerate(items, start=1):
            if stop is not None and stop.is_set():
                raise StopSignal("已取消")
            self.step(f"{i}/{total} {item.title}")
            if item.action == SKIP:
                stats["skipped"] += 1
                if progress:
                    progress(i, total, f"跳过 {item.rel}")
                continue

            try:
                note = next(n for n in self.index.notes if n.rel == item.rel)
                if not note.text:
                    note.text = self._read(note.path)
                page, _ = self.render(note)
                for w in page.warnings:
                    self.log(f"{item.rel}: {w}", "WARN")

                if dry:
                    msg = f"[{item.action}] {item.title} → {self.notebook_name}/{item.section}（{page.total_bytes() // 1024}KB）"
                    if item.action == CREATE:
                        stats["created"] += 1
                    else:
                        stats["updated"] += 1
                    self.log(msg, "INFO")
                else:
                    section_id = self._ensure_section(writer, item.section)
                    known_pid = (item.page_id or item.matched_page_id or "").strip()
                    where = (f"笔记本「{self.cfg.notebook_name}」/ 分区「{item.section}」"
                             + ("" if item.section_exists is not False else "（本次新建的分区）"))
                    if item.action == CREATE or not known_pid:
                        pid = writer.create_page(section_id, page)
                        stats["created"] += 1
                        item.page_id = pid
                        item.action = CREATE
                        self._save_record(item, note, writer)
                        self.log(f"新建页面「{item.title}」→ {where}", "OK")
                    else:
                        pid, used = writer.replace_or_recreate(known_pid, section_id, page, cfg.conflict_strategy)
                        item.page_id = pid
                        stats["updated"] += 1
                        self._save_record(item, note, writer)
                        self.log(f"更新页面「{item.title}」→ {where}（{used}）", "OK")
                    self.last_page_id = item.page_id
                    time.sleep(max(0, cfg.throttle_ms) / 1000.0)
                if progress:
                    progress(i, total, f"{_ACT_CN.get(item.action, item.action)} {item.title}")
            except StopSignal:
                raise
            except WriterError as e:
                stats["failed"] += 1
                failures.append((item.rel, str(e)))
                self.log(f"{item.rel} 失败：{e}", "ERROR")
                if progress:
                    progress(i, total, f"失败 {item.rel}")
            except Exception as e:
                stats["failed"] += 1
                failures.append((item.rel, f"{type(e).__name__}: {e}"))
                self.log(f"{item.rel} 异常：{e}", "ERROR")
                self.log(traceback.format_exc(), "DEBUG")
                if progress:
                    progress(i, total, f"失败 {item.rel}")

        self.state.record_run(stats, writer.name if writer else "dry-run", dry)
        stats["failures"] = failures
        stats["dry"] = dry
        stats["last_page_id"] = self.last_page_id
        if dry:
            self.log(f"预览结束：共 {total} 篇（新建 {stats['created']}、更新 {stats['updated']}、"
                     f"跳过 {stats['skipped']}）。OneNote 一点没动 —— 要真正写进去，"
                     f"请到「目标」卡里取消勾选「预览模式」，再点「开始导入」。", "WARN")
        else:
            stats["report"] = self.verify_written(items)
        return stats

    # ------------------------------------------------------------ 写入后核对
    def verify_written(self, items: list[PlanItem]) -> list[dict]:
        """写完后再读一遍 OneNote，确认页面确实落地，并报出可核对的具体位置。

        不信任「调用没报错 = 写成功」：直接把分区重新列一遍，
        逐条比对页面标题是否真的在里面。这是「提示已导入、OneNote 里却找不到」
        这类问题的正面回答——要么给出确切路径，要么明确指出哪几页没对上。
        """
        touched = [it for it in items if it.action in (CREATE, UPDATE) and it.page_id]
        if not touched:
            return []
        self.step("核对写入结果")
        try:
            writer = self.connect()
            nb = writer.find_notebook(self.cfg.notebook_name)
        except Exception as e:
            self.log(f"写入后核对跳过（读不到 OneNote）：{e}", "WARN")
            return []
        if nb is None:
            self.log(f"写入后核对跳过：笔记本「{self.cfg.notebook_name}」没找到", "WARN")
            return []

        groups: dict[str, list[PlanItem]] = {}
        for it in touched:
            groups.setdefault(it.section, []).append(it)

        report: list[dict] = []
        self.log(f"写入位置核对：笔记本「{nb.name}」", "INFO")
        for section, group in groups.items():
            key = section.strip().lower()
            try:
                sec = writer.find_section(nb.id, section)
                raw = writer.list_pages(sec.id) if sec else None
            except Exception as e:
                self.log(f"  分区「{section}」回读失败：{e}", "WARN")
                continue
            if raw is None:
                self.log(f"  分区「{section}」不见了 —— 可能被改名或移动，请到 OneNote 里确认", "ERROR")
                continue
            # 顺序有意义：用户就是靠「第几页」在 OneNote 里找的
            order = [t.strip().lower() for _, t in raw]
            titles = {t.strip().lower(): pid for pid, t in raw}
            before = len(self.section_pages.get(key, {})) or None
            hits: list[tuple[str, str, int]] = []
            for it in group:
                tkey = it.title.strip().lower()
                hits.append((it.title, titles.get(tkey, ""), order.index(tkey) + 1 if tkey in order else 0))
            missing = [t for t, pid, _ in hits if not pid]
            made = sum(1 for it in group if it.action == CREATE)
            new_titles = {it.title.strip().lower() for it in group if it.action == CREATE}
            made_pos = sorted(p for t, _pid, p in hits if p and t.strip().lower() in new_titles)
            head = f"  ▸ 分区「{section}」"
            if before is not None:
                head += f"：页面数 {before} → {len(raw)}"
            else:
                head += f"：现有 {len(raw)} 页"
            head += f"（本次新增 {made}、更新 {len(group) - made}）"
            self.log(head + ("" if not missing else f"，其中 {len(missing)} 页未对上"),
                     "OK" if not missing else "WARN")
            if made_pos:
                lo, hi = made_pos[0], made_pos[-1]
                where = f"第 {lo} 页" if lo == hi else f"第 {lo}–{hi} 页"
                self.log(f"      新页在分区里的位置：{where}（共 {len(raw)} 页，"
                         f"从上往下数是这个位置）", "INFO")
            # 页数太多时没必要把整段日志刷满，列前 20 条足够核对
            for title, pid, pos in hits[:20]:
                self.log(f"      {'✔' if pid else '✘'} {title}" + (f"（第 {pos} 页）" if pos else ""),
                         "OK" if pid else "ERROR")
            if len(hits) > 20:
                self.log(f"      …（还有 {len(hits) - 20} 页，均已写入）", "INFO")
            for title in missing:
                self.log(f"      未在分区里找到「{title}」，请手动确认是否被 OneNote 延迟同步", "WARN")
            report.append({"notebook": nb.name, "section": section, "before": before,
                           "after": len(raw), "missing": missing,
                           "created": made, "updated": len(group) - made,
                           "pages": [(t, p) for t, p, _ in hits],
                           "positions": {t: p for t, _pid, p in hits}})

        if report and any(r["missing"] for r in report):
            self.log("有页面回读不到，建议在 OneNote 里点一次「同步」后再看", "WARN")
        return report

    # ------------------------------------------------------------ 辅助
    def _ensure_section(self, writer: OneNoteWriter, name: str) -> str:
        if name in self._sections:
            return self._sections[name]
        nb = writer.ensure_notebook(self.cfg.notebook_name)
        sec = writer.ensure_section(nb.id, name)
        self._sections[name] = sec.id
        return sec.id

    def _save_record(self, item: PlanItem, note, writer: OneNoteWriter):
        self.state.upsert(PageRecord(
            rel_path=item.rel,
            sha256=sha256_text(note.text),
            title=item.title,
            section=item.section,
            section_key=item.section,
            notebook=self.cfg.notebook_name,
            page_id=item.page_id,
            writer=writer.name,
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            updated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        ))

    def forget(self, rel: str) -> None:
        """忘记一条同步记录（下次会重新建页面，原 OneNote 页面需要手动删）。"""
        self.state.delete(rel)
