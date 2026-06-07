# -*- coding: utf-8 -*-
"""写入通道的统一抽象。Graph 与本地 COM 两个实现都遵循这套接口，
上层 pipeline 因此不需要知道底层走的是 REST 还是 Windows COM。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

from core.onenote_html import RenderedPage


class WriterError(Exception):
    """通道可用，但某次操作失败。"""


class AuthRequired(WriterError):
    """需要用户重新授权。"""


class TransientError(WriterError):
    """可能重试能成功（网络抖动、限流）。"""


@dataclass
class ProbeResult:
    available: bool
    reason: str = ""
    account: str = ""


@dataclass
class NotebookRef:
    id: str
    name: str


@dataclass
class SectionRef:
    id: str
    name: str


LogFn = Callable[[str, str], None]  # (level, message)
StepFn = Callable[[str], None]      # (正在做什么) —— 给界面上的进度提示用


class OneNoteWriter(ABC):
    name = "abstract"

    def __init__(self, cfg, log: LogFn | None = None, step: StepFn | None = None):
        self.cfg = cfg
        self._log = log or (lambda level, msg: None)
        self._step_fn = step or (lambda text: None)

    def log(self, msg: str, level: str = "INFO"):
        self._log(level, msg)

    def step(self, text: str):
        """上报「此刻正在做哪一步」。

        每次调 OneNote 要一两秒，界面上一片安静会让人以为程序死了。
        日志是给事后翻看的人用的，这个回调是给「正在进行中」的进度条用的。
        """
        try:
            self._step_fn(text)
        except Exception:
            pass

    # ---------------- 探测 ----------------
    @abstractmethod
    def probe(self) -> ProbeResult:
        """能否使用：Graph 看 token，COM 看本机有没有桌面版 OneNote。"""

    # ---------------- 结构 ----------------
    @abstractmethod
    def list_notebooks(self) -> list[NotebookRef]: ...

    @abstractmethod
    def ensure_notebook(self, display_name: str) -> NotebookRef: ...

    @abstractmethod
    def list_sections(self, notebook_id: str) -> list[SectionRef]: ...

    @abstractmethod
    def ensure_section(self, notebook_id: str, section_name: str) -> SectionRef: ...

    # ---------------- 页面 ----------------
    @abstractmethod
    def create_page(self, section_id: str, page: RenderedPage) -> str: ...

    @abstractmethod
    def update_page_content(self, page_id: str, page: RenderedPage, strategy: str = "replace_div") -> None: ...

    @abstractmethod
    def delete_page(self, page_id: str) -> None: ...

    @abstractmethod
    def list_pages(self, section_id: str) -> list[tuple[str, str]]:
        """返回分区内所有页面的 (page_id, title)，供「同名页面不重复建」判断。"""

    def list_pages_full(self, section_id: str) -> list[dict]:
        """带创建时间的页面清单（清理空页这类活儿需要）。

        默认实现退化到 list_pages —— 拿不到时间的通道返回空串即可。
        """
        return [{"id": pid, "title": title, "created": "", "name": title}
                for pid, title in self.list_pages(section_id)]

    def get_page_xml(self, page_id: str) -> str | None:
        """读回页面现有 XML（当改写底稿用）。

        COM 通道的 UpdatePageContent 对「没有 ID 的 Outline」是追加而不是替换，
        所以必须先拿到原页面结构才能把内容换干净。Graph 是纯 HTML，
        没有这个概念，返回 None 即代表「按整页重建」。
        """
        return None

    # ---------------- 只读查找（不会创建任何东西） ----------------
    def find_notebook(self, display_name: str) -> NotebookRef | None:
        want = (display_name or "").strip().lower()
        if not want:
            return None
        for nb in self.list_notebooks():
            if nb.name.strip().lower() == want:
                return nb
        return None

    def find_section(self, notebook_id: str, section_name: str) -> SectionRef | None:
        want = (section_name or "").strip().lower()
        if not want:
            return None
        for sec in self.list_sections(notebook_id):
            if sec.name.strip().lower() == want:
                return sec
        return None

    # ---------------- 默认实现 ----------------
    def goto_page(self, page_id: str) -> bool:
        """在 OneNote 里定位到某个页面。

        Graph 通道是纯 REST，没有本地窗口可以跳转，所以默认返回 False；
        COM 通道覆写它。返回 False 只代表「没能帮你打开」，不代表写入失败。
        """
        return False

    def replace_or_recreate(self, page_id: str, section_id: str, page: RenderedPage, strategy: str) -> tuple[str, str]:
        """返回 (新的 page_id, 实际采用的策略)。replace_div 失败时自动降级为重建。"""
        self.update_page_content(page_id, page, strategy)
        return page_id, strategy
