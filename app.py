# -*- coding: utf-8 -*-
""" OneNote 日记导入工具（图形界面）。

把本地 Markdown 日记批量导入 OneNote。当前支持 md 格式，
Obsidian 导出的文档可直接导入；后续会扩展更多格式。

用系统自带的 tkinter 实现，标准库 + 零 pip 依赖。
    python app.py              # 打开界面
    python app.py --selftest   # 无凭据跑一遍渲染自检
"""
from __future__ import annotations

import ctypes
import queue
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from core.config import DEFAULT_CLIENT_ID_HINT, Config  # noqa: E402
from core.pipeline import Pipeline, StopSignal  # noqa: E402
from core.proc import run_script, silent_kwargs  # noqa: E402
from ui.theme import (  # noqa: E402
    LOG_COLORS, Badge, Card, Check, FlatButton, NavItem, ScrollArea, Segmented, Ui, hline,
    round_rect, setup_dpi_awareness,
)
from writers.auth import AuthError, DeviceCodeAuth, TokenStore  # noqa: E402
from writers.factory import probe_all  # noqa: E402

APP_NAME = "OneNote 日记导入工具"
APP_TITLE = "OneNote 日记导入工具"
APP_VERSION = "1.0"

ACTION_LABEL = {"create": "新建", "update": "更新", "skip": "跳过", "fail": "失败",
                # 同步过程中逐行回填的状态（见 _update_row）
                "done": "已完成", "preview": "仅预览", "running": "处理中", "wait": "等待"}
ACTION_VARIANT = {"create": "accent", "update": "warn", "skip": "neutral", "fail": "danger",
                  "done": "success", "preview": "neutral"}

# 分区命名预设：{season} 按中文季节（春 3-5 / 夏 6-8 / 秋 9-11 / 冬 12-2，冬季归到起始年）
SECTION_TEMPLATE_PRESETS = [
    "{YYYY}{season}",
    "{YYYY}年{season}",
    "{YYYY}-{MM}",
    "{YYYY}年{MM}月",
    "{YYYY}年{MM}月{DD}日",
]

TITLE_TEMPLATE_PRESETS = [
    "{M}月{D}日",
    "{YYYY}-{MM}-{DD}",
    "{date} {title}",
    "{YYYY}年{M}月{D}日",
    "{M}月{D}日 {title}",
]

PAGE_POLICY_LABEL = {"update": "更新它", "skip": "跳过它", "create": "另建新页"}
PAGE_POLICY_HINT = {
    "update": "把 OneNote 里那一页当成同一篇，直接覆盖内容（推荐）",
    "skip": "保留 OneNote 里那份，不动它，也不重复建",
    "create": "不管重名，照建新页面（会出现两个同名页）",
}

# 队列里说的是「级别」，tkinter 要的是函数名 —— 两者不一样，
# 直接 getattr(messagebox, "error") 会抛 AttributeError，弹窗永远出不来。
MSGBOX_FN = {
    "info": "showinfo",
    "warning": "showwarning",
    "warn": "showwarning",
    "error": "showerror",
}

# 这些字段一旦变了，之前算出来的计划就作废 —— 否则会出现
# 「预览时按 2026-06 分区、导入时却以为写进了 2026夏」这种错位。
PLAN_SIG_FIELDS = (
    "vault_path", "include_globs", "exclude_globs",
    "notebook_name", "notebook_id", "section_mode", "section_template",
    "section_fixed", "existing_page_policy", "title_template",
    "update_existing", "state_path",
)


def plan_signature(cfg: Config) -> tuple:
    return tuple(f"{f}={getattr(cfg, f, None)!r}" for f in PLAN_SIG_FIELDS)


def human_ts() -> str:
    return time.strftime("%H:%M:%S")


class SyncApp(tk.Tk):
    def __init__(self):
        setup_dpi_awareness()      # 必须在 Tk 初始化之前
        super().__init__()
        self.cfg = Config.load()
        self.q: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.pipe: Pipeline | None = None
        self.plan = []
        self.plan_sig: tuple | None = None
        self.worker: threading.Thread | None = None
        # 「打开最后一页」用：上一次真正写入的页面 ID 和当时那个通道
        self.last_page_id: str = ""
        self.open_writer = None
        self.token_store = TokenStore()
        # 进度区状态：当前步骤 / 已用时长 / 是否转圈 / 已完成几篇
        self._stage_text = ""
        self._counts: tuple[int, int] = (0, 0)
        self._busy_since = 0.0
        self._spin_on = False
        self._has_counts = False

        self.ui = Ui(self)
        self.title(APP_TITLE)
        w, h = self.ui.px(1320), self.ui.px(870)
        self.geometry(f"{w}x{h}")
        self.minsize(self.ui.px(1120), self.ui.px(720))
        self.configure(bg=self.ui.c["app_bg"])
        try:
            self.iconbitmap(default="")
        except Exception:
            pass

        self._build_shell()
        self._build_sync_page()
        self._build_auth_page()
        self._build_adv_page()
        self._show_page("sync")
        self._load_cfg_to_ui()

        self.after(120, self._drain)
        self.after(700, self.do_probe)      # 启动后自动跑一次通道探测
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._center()
        self._bring_to_front()

    # ================================================================ 窗口
    def _center(self):
        try:
            self.update_idletasks()
            w, h = self.winfo_width(), self.winfo_height()
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            if w > sw * 0.9:
                w = int(sw * 0.9)
            if h > sh * 0.9:
                h = int(sh * 0.9)
            self.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        except Exception:
            pass

    def _bring_to_front(self):
        try:
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            self.after(800, lambda: self.attributes("-topmost", False))
        except Exception:
            pass

    # ================================================================ 骨架
    def _build_shell(self):
        ui, c = self.ui, self.ui.c
        root = tk.Frame(self, bg=c["app_bg"])
        root.pack(fill="both", expand=True)

        # ---------------- 侧边栏 ----------------
        self.sidebar = tk.Frame(root, bg=c["sidebar"], width=ui.px(232))
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        tk.Frame(root, bg=c["border"], width=1).pack(side="left", fill="y")

        brand = tk.Frame(self.sidebar, bg=c["sidebar"])
        brand.pack(fill="x", padx=ui.px(18), pady=(ui.px(20), ui.px(16)))
        logo = tk.Canvas(brand, bg=c["sidebar"], width=ui.px(38), height=ui.px(38),
                         highlightthickness=0, bd=0)
        logo.pack(side="left")
        r = ui.px(9)
        round_rect(logo, 0.5, 0.5, ui.px(38) - 0.5, ui.px(38) - 0.5, r, fill=c["accent"], outline=c["accent"])
        # 图标：文档落入容器（导入）—— 用线条绘制，避免特殊字符在某些字体下变成豆腐块
        _lw = ui.px(2)
        logo.create_line(ui.px(11), ui.px(11.5), ui.px(27), ui.px(11.5),
                         fill="#FFFFFF", width=_lw, capstyle="round")
        logo.create_line(ui.px(19), ui.px(15), ui.px(19), ui.px(23),
                         fill="#FFFFFF", width=_lw, capstyle="round")
        logo.create_polygon(ui.px(15), ui.px(21.5), ui.px(23), ui.px(21.5), ui.px(19), ui.px(27.5),
                            fill="#FFFFFF", outline="#FFFFFF")
        logo.create_line(ui.px(11), ui.px(30), ui.px(27), ui.px(30),
                         fill="#FFFFFF", width=_lw, capstyle="round")
        txt = tk.Frame(brand, bg=c["sidebar"])
        txt.pack(side="left", padx=(ui.px(11), 0))
        tk.Label(txt, text="日记导入工具", bg=c["sidebar"], fg=c["text"],
                 font=ui.font("h1", "bold")).pack(anchor="w")
        tk.Label(txt, text="Markdown → OneNote", bg=c["sidebar"], fg=c["text_muted"],
                 font=ui.font("tiny")).pack(anchor="w")

        nav = tk.Frame(self.sidebar, bg=c["sidebar"])
        nav.pack(fill="x", pady=(ui.px(4), 0))
        self.nav_items: dict[str, NavItem] = {}
        for key, icon, label in (("sync", "⇅", "导入"), ("auth", "⚿", "账户与通道"), ("adv", "⚙", "高级设置")):
            item = NavItem(nav, ui, icon, label, command=lambda k=key: self._show_page(k))
            item.pack(fill="x", padx=ui.px(10), pady=ui.px(2))
            self.nav_items[key] = item

        # ---------------- 侧边栏底部：通道状态 ----------------
        footer = tk.Frame(self.sidebar, bg=c["sidebar"])
        footer.pack(side="bottom", fill="x", padx=ui.px(14), pady=ui.px(14))
        hline(footer, ui, bg=c["sidebar"])
        box = tk.Frame(footer, bg=c["sidebar"])
        box.pack(fill="x", pady=(ui.px(12), 0))
        tk.Label(box, text="写入通道", bg=c["sidebar"], fg=c["text_muted"],
                 font=ui.font("tiny")).pack(anchor="w")
        self.badge_channel = Badge(box, ui, "尚未探测", "neutral", bg=c["sidebar"])
        self.badge_channel.pack(anchor="w", pady=(ui.px(6), ui.px(8)))
        FlatButton(box, ui, "打开配置目录", command=self.open_appdata, variant="ghost",
                   bg=c["sidebar"]).pack(anchor="w")
        tk.Label(box, text=f"v{APP_VERSION}", bg=c["sidebar"], fg=c["text_muted"],
                 font=ui.font("tiny")).pack(anchor="w", pady=(ui.px(10), 0))

        # ---------------- 右侧主区 ----------------
        self.main = tk.Frame(root, bg=c["app_bg"])
        self.main.pack(side="left", fill="both", expand=True)

        # ---------------- 状态栏（先占位，页面区随后填满剩余空间） ----------------
        status_wrap = tk.Frame(self.main, bg=c["app_bg"])
        status_wrap.pack(side="bottom", fill="x")
        tk.Frame(status_wrap, bg=c["border"], height=1).pack(fill="x")
        bar = tk.Frame(status_wrap, bg=c["surface"], height=ui.px(38))
        bar.pack(fill="x")
        bar.pack_propagate(False)
        self.dot = tk.Canvas(bar, bg=c["surface"], width=ui.px(10), height=ui.px(10),
                             highlightthickness=0, bd=0)
        self.dot.pack(side="left", padx=(ui.px(16), ui.px(8)))
        self.dot.create_oval(1, 1, ui.px(9), ui.px(9), fill=c["success"], outline=c["success"])
        self.status = tk.StringVar(value="就绪")
        tk.Label(bar, textvariable=self.status, bg=c["surface"], fg=c["text_soft"],
                 font=ui.font("small")).pack(side="left")

        self.lbl_progress = tk.Label(bar, text="", bg=c["surface"], fg=c["text_muted"],
                                     font=ui.font("small"))
        self.var_progress = tk.DoubleVar(value=0)
        self.pbar = ttk.Progressbar(bar, variable=self.var_progress, maximum=100,
                                    style="Modern.Horizontal.TProgressbar", length=ui.px(170))
        # 进度条默认隐藏，任务开始时才出现
        self._progress_visible = False

        # 页面宿主：填满状态栏以外的全部空间
        self.host = tk.Frame(self.main, bg=c["app_bg"])
        self.host.pack(side="top", fill="both", expand=True)
        self.pages: dict[str, tk.Frame] = {}
        for key in ("sync", "auth", "adv"):
            f = tk.Frame(self.host, bg=c["app_bg"])
            f.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.pages[key] = f

    def _show_page(self, key: str):
        for k, item in self.nav_items.items():
            item.set_active(k == key)
        self.pages[key].lift()

    # ================================================================ 页面头
    def _page_header(self, parent, title: str, subtitle: str) -> tk.Frame:
        """页面标题区，返回右侧动作容器（按钮请以它为父容器创建）。"""
        ui, c = self.ui, self.ui.c
        head = tk.Frame(parent, bg=c["app_bg"])
        head.pack(fill="x", padx=ui.px(24), pady=(ui.px(22), ui.px(14)))
        left = tk.Frame(head, bg=c["app_bg"])
        left.pack(side="left")
        tk.Label(left, text=title, bg=c["app_bg"], fg=c["text"],
                 font=ui.font("display", "bold")).pack(anchor="w")
        tk.Label(left, text=subtitle, bg=c["app_bg"], fg=c["text_muted"],
                 font=ui.font("small")).pack(anchor="w", pady=(ui.px(3), 0))
        actions = tk.Frame(head, bg=c["app_bg"])
        actions.pack(side="right", anchor="ne")
        return actions

    def _form(self, parent, label_width: int = 88) -> tk.Frame:
        f = tk.Frame(parent, bg=self.ui.c["surface"])
        f.pack(fill="x")
        f.columnconfigure(1, weight=1)
        f._label_width = label_width  # type: ignore[attr-defined]
        return f

    def _flabel(self, parent, text: str, row: int):
        ui, c = self.ui, self.ui.c
        tk.Label(parent, text=text, bg=c["surface"], fg=c["text_soft"], font=ui.font("small"),
                 anchor="w", width=0).grid(row=row, column=0, sticky="w",
                                           padx=(0, ui.px(10)), pady=ui.px(6))

    def _pad(self, parent, pad=(24, 0)):
        ui = self.ui
        f = tk.Frame(parent, bg=ui.c["app_bg"])
        f.pack(fill="x", padx=ui.px(pad[0]), pady=ui.px(pad[1]))
        return f

    # ================================================================ 导入页
    def _build_sync_page(self):
        ui, c = self.ui, self.ui.c
        page = self.pages["sync"]

        actions = self._page_header(
            page, "导入日记",
            "把本地 Markdown 日记批量写进 OneNote，重复运行不会产生重复页面")
        self.btn_stop = FlatButton(actions, ui, "停止", command=self.do_stop, variant="danger")
        self.btn_stop.configure(state="disabled")
        self.btn_plan = FlatButton(actions, ui, "扫描预览", command=self.do_plan, variant="secondary")
        self.btn_sync = FlatButton(actions, ui, "开始导入", command=self.do_sync, variant="primary")
        # 导入完之后最想知道的就是「东西到底在哪」，所以给一个一键跳转
        self.btn_open = FlatButton(actions, ui, "打开最后一页", command=self.open_in_onenote,
                                   variant="secondary")
        self.btn_open.configure(state="disabled")
        for b in (self.btn_sync, self.btn_plan, self.btn_open, self.btn_stop):
            b.pack(side="right", padx=(ui.px(8), 0))

        area = ScrollArea(page, ui, bg=c["app_bg"])
        area.pack(fill="both", expand=True)
        inner = area.inner
        inner.configure(bg=c["app_bg"])

        # ---------------- 来源 ----------------
        card = Card(inner, ui, "来源", "Markdown 日记文件夹与匹配规则（Obsidian 库可直接作为来源）")
        card.pack(fill="x", padx=ui.px(24), pady=(0, ui.px(12)))
        form = self._form(card.body)
        self._flabel(form, "日记目录", 0)
        self.var_vault = tk.StringVar()
        ttk.Entry(form, textvariable=self.var_vault, width=1).grid(
            row=0, column=1, sticky="ew", pady=ui.px(5))
        FlatButton(form, ui, "浏览…", command=self.pick_vault, variant="secondary").grid(
            row=0, column=2, sticky="e", padx=(ui.px(8), 0), pady=ui.px(5))

        self._flabel(form, "包含规则", 1)
        globs = tk.Frame(form, bg=c["surface"])
        globs.grid(row=1, column=1, columnspan=2, sticky="ew", pady=ui.px(5))
        globs.columnconfigure(0, weight=1, uniform="glob")
        globs.columnconfigure(2, weight=1, uniform="glob")
        self.var_include = tk.StringVar(value="**/*.md")
        ttk.Entry(globs, textvariable=self.var_include, width=1).grid(row=0, column=0, sticky="ew")
        tk.Label(globs, text="排除", bg=c["surface"], fg=c["text_muted"],
                 font=ui.font("small")).grid(row=0, column=1, padx=ui.px(10))
        self.var_exclude = tk.StringVar(value="**/.obsidian/**, **/模板/**")
        ttk.Entry(globs, textvariable=self.var_exclude, width=1).grid(row=0, column=2, sticky="ew")
        tk.Label(card.body, text="默认匹配 md 文件，多个模式用逗号分隔，支持 ** 通配；已排除 .obsidian 与模板目录。",
                 bg=c["surface"], fg=c["text_muted"], font=ui.font("tiny")).pack(
            anchor="w", pady=(ui.px(2), 0))

        # ---------------- 目标 ----------------
        card2 = Card(inner, ui, "目标", "写入哪个笔记本，以及分区怎么命名")
        card2.pack(fill="x", padx=ui.px(24), pady=(0, ui.px(12)))
        form2 = self._form(card2.body)
        self._flabel(form2, "笔记本", 0)
        self.var_notebook = tk.StringVar()
        self.cmb_notebook = ttk.Combobox(form2, textvariable=self.var_notebook, width=1)
        self.cmb_notebook.grid(row=0, column=1, sticky="ew", pady=ui.px(5))
        FlatButton(form2, ui, "刷新列表", command=self.refresh_notebooks, variant="secondary").grid(
            row=0, column=2, sticky="e", padx=(ui.px(8), 0), pady=ui.px(5))

        self._flabel(form2, "分区方式", 1)
        mode_wrap = tk.Frame(form2, bg=c["surface"])
        mode_wrap.grid(row=1, column=1, columnspan=2, sticky="w", pady=ui.px(5))
        self.var_section_mode = tk.StringVar(value="template")
        Segmented(mode_wrap, ui, [("template", "按日期自动分区"), ("fixed", "固定写入一个分区")],
                  self.var_section_mode).pack(side="left")
        self.var_section_mode.trace_add("write", lambda *_: self._sync_section_panels())

        holder = tk.Frame(form2, bg=c["surface"])
        holder.grid(row=2, column=1, columnspan=2, sticky="ew")

        # ---- 模板面板：按日期算出分区名 ----
        self.panel_tpl = tk.Frame(holder, bg=c["surface"])
        self.var_section = tk.StringVar(value="{YYYY}-{MM}")
        self.cmb_section_tpl = ttk.Combobox(self.panel_tpl, textvariable=self.var_section,
                                            values=SECTION_TEMPLATE_PRESETS)
        self.cmb_section_tpl.pack(fill="x", pady=ui.px(5))
        tk.Label(self.panel_tpl,
                 text="占位符 {YYYY} {MM} {M} {DD} {D} {season} {title}　"
                      "例：{YYYY}{season} → 2026夏，{YYYY}-{MM} → 2026-06。"
                      "算出来的分区名如果已存在就直接写进去，不会另建。",
                 bg=c["surface"], fg=c["text_muted"], font=ui.font("tiny"), justify="left",
                 wraplength=ui.px(720)).pack(anchor="w")

        # ---- 固定分区面板：全部进同一个分区 ----
        self.panel_fixed = tk.Frame(holder, bg=c["surface"])
        fx = tk.Frame(self.panel_fixed, bg=c["surface"])
        fx.pack(fill="x", pady=ui.px(5))
        self.var_section_fixed = tk.StringVar()
        self.cmb_section_fixed = ttk.Combobox(fx, textvariable=self.var_section_fixed)
        self.cmb_section_fixed.pack(side="left", fill="x", expand=True)
        FlatButton(fx, ui, "刷新分区", command=self.refresh_sections, variant="secondary").pack(
            side="left", padx=(ui.px(8), 0))
        tk.Label(self.panel_fixed,
                 text="所有日记都写进这一个分区，新页面接在原有页面后面。"
                      "分区里已有同名页面时按「高级设置」里的策略处理。",
                 bg=c["surface"], fg=c["text_muted"], font=ui.font("tiny"), justify="left",
                 wraplength=ui.px(720)).pack(anchor="w")

        self._sync_section_panels()

        # ---- 分区里已有同名页面怎么办 ----
        self._flabel(form2, "重名页面", 3)
        pol = tk.Frame(form2, bg=c["surface"])
        pol.grid(row=3, column=1, columnspan=2, sticky="w", pady=ui.px(5))
        self.var_page_policy = tk.StringVar(value="update")
        Segmented(pol, ui, [(k, PAGE_POLICY_LABEL[k]) for k in ("update", "skip", "create")],
                  self.var_page_policy).pack(side="left")
        self.lbl_policy_hint = tk.Label(pol, text="", bg=c["surface"], fg=c["text_muted"],
                                        font=ui.font("tiny"))
        self.lbl_policy_hint.pack(side="left", padx=(ui.px(12), 0))
        self.var_page_policy.trace_add("write", lambda *_: self._sync_policy_hint())
        self._sync_policy_hint()

        hline(card2.body, ui)
        opts = tk.Frame(card2.body, bg=c["surface"])
        opts.pack(fill="x", pady=(ui.px(12), 0))
        self.var_dry = tk.BooleanVar(value=True)
        self.var_update = tk.BooleanVar(value=True)
        self.var_images = tk.BooleanVar(value=True)
        Check(opts, ui, "预览模式（只列计划，不写入）", self.var_dry).pack(side="left")
        Check(opts, ui, "更新内容已变化的页面", self.var_update).pack(side="left", padx=ui.px(26))
        Check(opts, ui, "把图片嵌入页面", self.var_images).pack(side="left")

        # ---------------- 计划表 ----------------
        card3 = Card(inner, ui, "执行计划", "扫描后这里显示每篇日记的去向")
        card3.pack(fill="x", padx=ui.px(24), pady=(0, ui.px(12)))
        self.lbl_plan_summary = tk.Label(card3.body, text="尚未扫描", bg=c["surface"],
                                         fg=c["text_muted"], font=ui.font("small"))
        self.lbl_plan_summary.pack(anchor="w", pady=(0, ui.px(8)))

        tree_wrap = tk.Frame(card3.body, bg=c["surface"])
        tree_wrap.pack(fill="x")
        self.tree = ttk.Treeview(tree_wrap, columns=("act", "sec", "secstate", "title", "rel"),
                                 show="headings", style="Modern.Treeview", height=9)
        for col, head, w in (("act", "动作", 76), ("sec", "分区", 108), ("secstate", "分区状态", 92),
                             ("title", "页面标题", 260), ("rel", "文件", 380)):
            self.tree.heading(col, text=head)
            self.tree.column(col, width=ui.px(w), anchor="w", stretch=(col in ("title", "rel")))
        vs = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview,
                           style="Inner.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="x", expand=True)
        vs.pack(side="right", fill="y")
        for act, var in ACTION_VARIANT.items():
            self.tree.tag_configure(act, foreground=c[var] if var != "neutral" else c["text_muted"])
        self.tree.tag_configure("skip", foreground=c["text_muted"])

        self.lbl_empty = tk.Label(card3.body, text="点上方「扫描预览」生成计划",
                                  bg=c["surface"], fg=c["text_muted"], font=ui.font("small"))

        # ---------------- 日志 ----------------
        card4 = Card(inner, ui, "运行日志")
        card4.pack(fill="both", expand=True, padx=ui.px(24), pady=(0, ui.px(24)))
        log_wrap = tk.Frame(card4.body, bg=c["surface"])
        log_wrap.pack(fill="both", expand=True)
        self.txt_log = tk.Text(log_wrap, height=9, wrap="word", relief="flat", bd=0,
                               bg=c["surface"], fg=c["text_soft"], font=ui.font("mono"),
                               insertbackground=c["text"], padx=ui.px(2), pady=ui.px(2),
                               highlightthickness=0, selectbackground=c["accent_soft"])
        sc = ttk.Scrollbar(log_wrap, command=self.txt_log.yview, style="Inner.Vertical.TScrollbar")
        self.txt_log.configure(yscrollcommand=sc.set, state="disabled")
        self.txt_log.pack(side="left", fill="both", expand=True)
        sc.pack(side="right", fill="y")
        for lvl, color in LOG_COLORS.items():
            self.txt_log.tag_configure(lvl, foreground=color)

    # ================================================================ 账户页
    def _build_auth_page(self):
        ui, c = self.ui, self.ui.c
        page = self.pages["auth"]
        self._page_header(page, "账户与通道", "两条写入通道，失败时自动降级")
        area = ScrollArea(page, ui, bg=c["app_bg"])
        area.pack(fill="both", expand=True)

        card = Card(area.inner, ui, "Microsoft Graph", "主通道 · 需要 Azure 应用客户端 ID + 一次设备码登录")
        card.pack(fill="x", padx=ui.px(24), pady=(0, ui.px(12)))
        form = self._form(card.body)
        self._flabel(form, "客户端 ID", 0)
        self.var_client = tk.StringVar()
        ttk.Entry(form, textvariable=self.var_client).grid(row=0, column=1, columnspan=2,
                                                           sticky="ew", pady=ui.px(5))
        self._flabel(form, "账户类型", 1)
        rowb = tk.Frame(form, bg=c["surface"])
        rowb.grid(row=1, column=1, columnspan=2, sticky="ew", pady=ui.px(5))
        self.var_authority = tk.StringVar(value="consumers")
        ttk.Combobox(rowb, textvariable=self.var_authority, width=14, state="readonly",
                     values=("consumers", "common", "organizations")).pack(side="left")
        FlatButton(rowb, ui, "设备码登录", command=self.do_login, variant="primary").pack(
            side="left", padx=ui.px(10))
        FlatButton(rowb, ui, "清除登录", command=self.do_logout, variant="secondary").pack(side="left")

        self.var_auth_state = tk.StringVar(value="未检测")
        tk.Label(card.body, textvariable=self.var_auth_state, bg=c["surface"], fg=c["text_soft"],
                 font=ui.font("small")).pack(anchor="w", pady=(ui.px(12), 0))
        tk.Label(card.body, text=DEFAULT_CLIENT_ID_HINT, bg=c["surface"], fg=c["text_muted"],
                 font=ui.font("tiny"), justify="left", wraplength=ui.px(760)).pack(
            anchor="w", pady=(ui.px(6), 0))

        card2 = Card(area.inner, ui, "通道探测", "启动时会自动跑一次，也可以手动重跑")
        card2.pack(fill="x", padx=ui.px(24), pady=(0, ui.px(24)))
        self.txt_probe = tk.Text(card2.body, height=4, wrap="word", relief="flat", bd=0,
                                 bg=c["surface_alt"], fg=c["text_soft"], font=ui.font("mono"),
                                 padx=ui.px(12), pady=ui.px(10), highlightthickness=0)
        self.txt_probe.pack(fill="x", pady=(0, ui.px(12)))
        self.txt_probe.insert("1.0", "正在探测写入通道…")
        self.txt_probe.configure(state="disabled")
        FlatButton(card2.body, ui, "重新探测通道", command=self.do_probe,
                   variant="secondary").pack(anchor="w")

    # ================================================================ 高级页
    def _build_adv_page(self):
        ui, c = self.ui, self.ui.c
        page = self.pages["adv"]
        self._page_header(page, "高级设置", "页面渲染、写入行为与状态库")
        area = ScrollArea(page, ui, bg=c["app_bg"])
        area.pack(fill="both", expand=True)

        card = Card(area.inner, ui, "页面与写入行为")
        card.pack(fill="x", padx=ui.px(24), pady=(0, ui.px(12)))
        form = self._form(card.body)
        self._entry_row(form, 0, "标题模板", "var_title_tpl", "{M}月{D}日", 34,
                        combo=tuple(TITLE_TEMPLATE_PRESETS), combo_editable=True,
                        hint="占位符 {date} {YYYY} {MM} {M} {DD} {D} {title}")
        self._entry_row(form, 1, "图片宽度上限", "var_img_w", "640", 10,
                        hint="像素。超过会等比缩小，避免页面被撑爆。")
        self._entry_row(form, 2, "单图上限 (MB)", "var_max_mb", "8", 10,
                        hint="超过就跳过该图并在日志里提示。")
        self._entry_row(form, 3, "请求间隔 (ms)", "var_throttle", "400", 10,
                        hint="Graph 通道的节流，太小容易被限流。")
        self._entry_row(form, 4, "冲突策略", "var_conflict", "replace_div", 22, combo=(
            "replace_div", "recreate"))

        form.columnconfigure(1, weight=0)
        label_col = tk.Frame(card.body, bg=c["surface"])
        label_col.pack(fill="x", pady=(ui.px(14), 0))
        tk.Label(label_col, text="优先通道", bg=c["surface"], fg=c["text_soft"],
                 font=ui.font("small")).pack(anchor="w", pady=(0, ui.px(6)))
        self.var_writer = tk.StringVar(value="auto")
        Segmented(label_col, ui, [
            ("auto", "自动降级"), ("graph", "只看 Graph"), ("com", "只用桌面 COM"),
        ], self.var_writer).pack(anchor="w")

        sep = hline(card.body, ui)
        sep.pack_configure(pady=(ui.px(14), 0))
        fm_row = tk.Frame(card.body, bg=c["surface"])
        fm_row.pack(fill="x", pady=(ui.px(12), ui.px(2)))
        self.var_fm_block = tk.BooleanVar(value=False)
        Check(fm_row, ui, "页面顶部附上 frontmatter 表格", self.var_fm_block).pack(side="left")

        card2 = Card(area.inner, ui, "维护")
        card2.pack(fill="x", padx=ui.px(24), pady=(0, ui.px(24)))
        btns = tk.Frame(card2.body, bg=c["surface"])
        btns.pack(fill="x")
        FlatButton(btns, ui, "打开配置 / 状态目录", command=self.open_appdata,
                   variant="secondary").pack(side="left")
        FlatButton(btns, ui, "运行导入自检", command=self.run_selftest,
                   variant="secondary").pack(side="left", padx=ui.px(10))
        FlatButton(btns, ui, "恢复默认值", command=self.reset_defaults,
                   variant="ghost").pack(side="left")
        tk.Label(card2.body,
                 text="状态库默认在 %APPDATA%\\OneNoteDiaryImporter\\state.sqlite3，"
                      "记录「文件路径 → OneNote 页面 ID」的映射，重复运行不会产生重复页面。",
                 bg=c["surface"], fg=c["text_muted"], font=ui.font("tiny"),
                 justify="left", wraplength=ui.px(560)).pack(anchor="w", pady=(ui.px(14), 0))

    def _entry_row(self, form, row: int, label: str, attr: str, default: str, width: int,
                   hint: str | None = None, combo: tuple[str, ...] | None = None,
                   combo_editable: bool = False):
        ui, c = self.ui, self.ui.c
        self._flabel(form, label, row)
        var = tk.StringVar(value=default)
        setattr(self, attr, var)
        holder = tk.Frame(form, bg=c["surface"])
        holder.grid(row=row, column=1, columnspan=2, sticky="w", pady=ui.px(4))
        if combo:
            ttk.Combobox(holder, textvariable=var, width=width,
                         state="normal" if combo_editable else "readonly",
                         values=combo).pack(side="left")
        else:
            ttk.Entry(holder, textvariable=var, width=width).pack(side="left")
        if hint:
            tk.Label(holder, text=hint, bg=c["surface"], fg=c["text_muted"],
                     font=ui.font("tiny")).pack(side="left", padx=(ui.px(10), 0))

    # ================================================================ 配置
    def _load_cfg_to_ui(self):
        c = self.cfg
        self.var_vault.set(c.vault_path)
        self.var_include.set(", ".join(c.include_globs))
        self.var_exclude.set(", ".join(c.exclude_globs))
        self.var_notebook.set(c.notebook_name)
        self.var_section.set(c.section_template)
        self.var_section_mode.set(c.section_mode or "template")
        self.var_section_fixed.set(c.section_fixed)
        self.var_page_policy.set(c.existing_page_policy or "update")
        self._sync_section_panels()
        self.var_dry.set(c.dry_run)
        self.var_update.set(c.update_existing)
        self.var_images.set(c.embed_images)
        self.var_client.set(c.graph_client_id)
        self.var_authority.set(c.graph_authority)
        self.var_title_tpl.set(c.title_template)
        self.var_img_w.set(str(c.image_width_limit))
        self.var_max_mb.set(str(c.max_media_bytes // 1024 // 1024))
        self.var_throttle.set(str(c.throttle_ms))
        self.var_conflict.set(c.conflict_strategy)
        self.var_fm_block.set(c.keep_frontmatter_block)
        self.var_writer.set(c.preferred_writer)

    def _ui_to_cfg(self) -> Config:
        def split_list(s: str, default: list[str]) -> list[str]:
            items = [x.strip() for x in s.replace("，", ",").split(",") if x.strip()]
            return items or default

        c = self.cfg
        c.vault_path = self.var_vault.get().strip()
        c.include_globs = split_list(self.var_include.get(), ["**/*.md"])
        c.exclude_globs = split_list(self.var_exclude.get(), [])
        c.notebook_name = self.var_notebook.get().strip() or "日记"
        c.section_template = self.var_section.get().strip() or "{YYYY}-{MM}"
        c.section_mode = self.var_section_mode.get() or "template"
        c.section_fixed = self.var_section_fixed.get().strip()
        c.existing_page_policy = self.var_page_policy.get() or "update"
        c.dry_run = bool(self.var_dry.get())
        c.update_existing = bool(self.var_update.get())
        c.embed_images = bool(self.var_images.get())
        c.graph_client_id = self.var_client.get().strip()
        c.graph_authority = self.var_authority.get().strip() or "consumers"
        c.title_template = self.var_title_tpl.get().strip() or "{date} {title}"
        c.image_width_limit = self._int(self.var_img_w.get(), 640)
        c.max_media_bytes = int(float(self.var_max_mb.get() or 8) * 1024 * 1024) if self._int(self.var_max_mb.get(), 8) else 8 * 1024 * 1024
        c.throttle_ms = self._int(self.var_throttle.get(), 400)
        c.conflict_strategy = self.var_conflict.get()
        c.keep_frontmatter_block = bool(self.var_fm_block.get())
        c.preferred_writer = self.var_writer.get()
        return c

    @staticmethod
    def _int(text: str, default: int) -> int:
        try:
            return int(float(text))
        except (TypeError, ValueError):
            return default

    # ================================================================ 日志 / 队列
    def log(self, msg: str, level: str = "INFO"):
        self.q.put(("log", (msg, level)))

    def _ensure_log(self, msg: str, level: str):
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", f"[{human_ts()}] {msg}\n", level if level in LOG_COLORS else "INFO")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._ensure_log(*payload)
                elif kind == "status":
                    self.status.set(payload)
                elif kind == "progress":
                    done, total, text = payload
                    self._set_progress(done, total, text)
                elif kind == "stage":
                    self._set_stage(payload)
                elif kind == "plan":
                    self._fill_tree(payload)
                elif kind == "row":
                    idx, action = payload
                    self._update_row(idx, action)
                elif kind == "busy":
                    self._set_busy(payload)
                elif kind == "notebooks":
                    self.cmb_notebook.configure(values=payload)
                elif kind == "sections":
                    self.cmb_section_fixed.configure(values=payload)
                elif kind == "probe":
                    self._fill_probe(payload)
                elif kind == "_dot":
                    self._set_dot(payload)
                elif kind == "msg":
                    level, title, text = payload
                    show = getattr(messagebox, MSGBOX_FN.get(level, "showinfo"), messagebox.showinfo)
                    show(title, text)
        except queue.Empty:
            pass
        self.after(120, self._drain)

    # ================================================================ 计划表
    def _fill_tree(self, items):
        ui, c = self.ui, self.ui.c
        self.tree.delete(*self.tree.get_children())
        for i, it in enumerate(items, start=1):
            if it.section_exists is None:
                state = "未核对"
            elif not it.section_exists:
                state = "新建分区"
            elif it.matched_page_id:
                state = "已有页面"
            else:
                state = "已有分区"
            self.tree.insert("", "end", iid=str(i),
                             values=(ACTION_LABEL.get(it.action, it.action), it.section, state,
                                     it.title, it.rel),
                             tags=(it.action,))
        if items:
            self.lbl_empty.place_forget()
            counts: dict[str, int] = {}
            for it in items:
                counts[it.action] = counts.get(it.action, 0) + 1
            summary = " · ".join(f"{ACTION_LABEL.get(k, k)} {v}" for k, v in counts.items())
            self.lbl_plan_summary.configure(text=f"共 {len(items)} 篇　　{summary}", fg=c["text_soft"])
        else:
            self.lbl_plan_summary.configure(text="没有匹配到任何文件", fg=c["warn"])
            self.lbl_empty.configure(text="没有匹配到 .md 文件，检查一下包含 / 排除规则")
            self.lbl_empty.place(relx=0.5, rely=0.62, anchor="center")

    def _update_row(self, idx: int, action: str):
        iid = str(idx)
        if not self.tree.exists(iid):
            return
        vals = list(self.tree.item(iid, "values"))
        vals[0] = ACTION_LABEL.get(action, action)
        self.tree.item(iid, values=vals, tags=(action,))

    def _set_busy(self, busy: bool):
        self.btn_plan.set_enabled(not busy)
        self.btn_sync.set_enabled(not busy)
        self.btn_stop.set_enabled(busy)
        self.btn_open.set_enabled(bool(self.last_page_id) and not busy)
        self._set_dot("busy" if busy else "ok")
        self._show_progress(busy)
        if busy:
            self.status.set("处理中…")
        elif self.status.get() == "处理中…":
            # 任务自己报的状态（例如「完成：9 新建」）别被这里覆盖掉
            self.status.set("就绪")

    # ---------------------------------------------------------- 进度显示
    def _show_progress(self, on: bool):
        """任务期间常驻的进度区：转圈 + 当前步骤 + 已用时长。

        扫描预览 / 刷新分区 / 探测通道也要显示 —— 以前只有「开始导入」才显示，
        点了「扫描预览」之后界面一片安静，完全看不出后台在忙什么。
        """
        if on == self._progress_visible:
            return
        self._progress_visible = on
        if on:
            self.lbl_progress.pack(side="right", padx=(self.ui.px(10), self.ui.px(8)))
            self.pbar.pack(side="right", pady=self.ui.px(6))
            self._busy_since = time.time()
            self._stage_text = "准备中"
            self._counts = (0, 0)
            self._has_counts = False
            self._start_spin()
            self._tick()
        else:
            self._stop_spin()
            self.pbar.pack_forget()
            self.lbl_progress.pack_forget()
            self.var_progress.set(0)
            self._stage_text = ""
            self._counts = (0, 0)
            self._busy_since = 0.0
            self.lbl_progress.configure(text="")

    def _start_spin(self):
        if self._spin_on:
            return
        self._spin_on = True
        self.pbar.configure(mode="indeterminate")
        self.pbar.start(55)

    def _stop_spin(self):
        if not self._spin_on:
            return
        self._spin_on = False
        try:
            self.pbar.stop()
        finally:
            self.pbar.configure(mode="determinate")

    def _set_stage(self, text: str):
        """更新「现在正在做什么」。还没拿到篇数进度时就转圈，让人一眼看出在跑。"""
        self._stage_text = (text or "").strip() or "处理中"
        if not self._has_counts:
            self._start_spin()
        self._refresh_progress_label()

    def _set_progress(self, done: int, total: int, text: str):
        if total:
            self._has_counts = True
            self._stop_spin()
            self._counts = (done, total)
            self.var_progress.set(done / total * 100)
        if text:
            self._stage_text = text
        self._refresh_progress_label()

    def _refresh_progress_label(self):
        parts = [self._stage_text or "处理中"]
        done, total = self._counts
        if total:
            parts.append(f"{done}/{total}")
        if self._busy_since:
            secs = time.time() - self._busy_since
            parts.append(f"{secs:.0f}s" if secs < 60 else f"{secs / 60:.1f}min")
        self.lbl_progress.configure(text=" · ".join(parts))

    def _tick(self):
        """每 250ms 刷新一次已用时长 —— 卡在某个慢步骤上时，秒数在动就不像死机。"""
        if not self._progress_visible:
            return
        self._refresh_progress_label()
        self.after(250, self._tick)

    def _set_dot(self, state: str):
        c = self.ui.c
        color = {"ok": c["success"], "busy": c["accent"], "warn": c["warn"], "err": c["danger"]}.get(state, c["success"])
        self.dot.delete("all")
        self.dot.create_oval(1, 1, self.ui.px(9), self.ui.px(9), fill=color, outline=color)

    # ================================================================ 动作
    def pick_vault(self):
        d = filedialog.askdirectory(title="选择存放 Markdown 日记的文件夹")
        if d:
            self.var_vault.set(d)

    def refresh_notebooks(self):
        cfg = self._ui_to_cfg()
        self.log("读取笔记本列表…", "INFO")

        def job():
            try:
                picked = self._pick_writer(cfg, self._logfn(), self._stage_async)
                if picked is None:
                    self.q.put(("msg", ("warning", "通道不可用",
                                        "两个通道都不可用，先到「账户与通道」页完成登录或检查桌面版 OneNote。")))
                    return
                names = [nb.name for nb in picked.list_notebooks()]
                self.q.put(("notebooks", names))
                self.log(f"读取到 {len(names)} 个笔记本", "OK")
                self._emit_sections(cfg, picked)
            except Exception as e:
                self.log(f"读取笔记本失败：{e}", "ERROR")

        self._bg(job)

    def refresh_sections(self):
        cfg = self._ui_to_cfg()
        self.log("读取分区列表…", "INFO")

        def job():
            try:
                picked = self._pick_writer(cfg, self._logfn(), self._stage_async)
                if picked is None:
                    self.q.put(("msg", ("warning", "通道不可用",
                                        "两个通道都不可用，先到「账户与通道」页完成登录或检查桌面版 OneNote。")))
                    return
                self._emit_sections(cfg, picked)
            except Exception as e:
                self.log(f"读取分区失败：{e}", "ERROR")

        self._bg(job)

    @staticmethod
    def _pick_writer(cfg: Config, log=None, step=None):
        """挑一个当前可用的通道；都不会用时返回 None。

        每一步都写进日志、也都上报给进度区 —— 之前这里是静默的，
        用户只能看到几个蓝色窗口闪过去，完全不知道后台在干什么。
        """
        from writers.factory import make_writer, probe_all
        log = log or (lambda level, msg: None)
        for kind, res in probe_all(cfg, log, step=step):
            label = {"graph": "Microsoft Graph", "com": "桌面版 OneNote (COM)"}.get(kind, kind)
            log("OK" if res.available else "INFO",
                f"通道 {label}：{'可用' if res.available else res.reason}")
            if res.available:
                return make_writer(kind, cfg, log, step=step)
        return None

    def _logfn(self):
        """writer / probe 的回调是 (level, msg)，这里适配成本类的 (msg, level)。"""
        return lambda level, msg: self.log(msg, level)

    def _emit_sections(self, cfg: Config, writer):
        nb = writer.find_notebook(cfg.notebook_name)
        if nb is None:
            self.q.put(("sections", []))
            self.log(f"OneNote 里还没有笔记本「{cfg.notebook_name}」，分区列表为空", "WARN")
            return
        names = [s.name for s in writer.list_sections(nb.id)]
        names = [n for n in names if n not in ("删除的页面", "Deleted Pages")]
        self.q.put(("sections", names))
        shown = "、".join(names[:8]) + ("…" if len(names) > 8 else "")
        self.log(f"笔记本「{nb.name}」下有 {len(names)} 个分区：{shown}", "OK")

    def _sync_section_panels(self):
        tpl = self.var_section_mode.get() == "template"
        self.panel_fixed.pack_forget()
        self.panel_tpl.pack_forget()
        (self.panel_tpl if tpl else self.panel_fixed).pack(fill="x")

    def _sync_policy_hint(self):
        key = self.var_page_policy.get() or "update"
        self.lbl_policy_hint.configure(text=PAGE_POLICY_HINT.get(key, ""))

    def do_plan(self):
        cfg = self._ui_to_cfg()
        errs = cfg.validate()
        if errs:
            messagebox.showwarning("配置不完整", "\n".join(errs))
            return
        self.stop_event.clear()
        self._bg(lambda: self._job_plan(cfg))

    def _job_plan(self, cfg: Config):
        self.q.put(("status", "扫描中…"))
        self._set_dot_async("busy")
        pipe = Pipeline(cfg, log=self.log, step=self._stage_async)
        self.pipe = pipe
        items = pipe.scan()
        self.q.put(("plan", items))
        self.q.put(("status", "核对 OneNote 里的分区与页面…"))
        # 只读核对：分区是否已存在、分区里有没有同名页面。不会写任何东西。
        pipe.inspect_targets(items)
        self.plan = items
        self.plan_sig = plan_signature(cfg)
        self.q.put(("plan", items))
        counts = {}
        for it in items:
            counts[it.action] = counts.get(it.action, 0) + 1
        summary = "、".join(f"{ACTION_LABEL.get(k, k)} {v}" for k, v in counts.items()) or "空"
        self.log(f"扫描完成：共 {len(items)} 篇（{summary}）", "OK")
        self.q.put(("status", f"已生成计划：{summary}"))
        self._set_dot_async("ok")

    def _set_dot_async(self, state: str):
        self.q.put(("_dot", state))

    def _stage_async(self, text: str):
        """写通道上报「正在做什么」时的回调（跨线程，只能往队列里投）。"""
        self.q.put(("stage", text))

    def do_sync(self):
        cfg = self._ui_to_cfg()
        errs = cfg.validate()
        if errs:
            messagebox.showwarning("配置不完整", "\n".join(errs))
            return
        if not self.plan:
            self.do_plan_if_needed(cfg)
        if not cfg.dry_run and not messagebox.askyesno(
                "确认写入", f"即将把 {len(self.plan)} 条计划写入 OneNote 笔记本「{cfg.notebook_name}」。继续？"):
            return
        self.stop_event.clear()
        self._bg(lambda: self._job_sync(cfg))

    def do_plan_if_needed(self, cfg: Config):
        pipe = Pipeline(cfg, log=self.log, step=self._stage_async)
        self.pipe = pipe
        self.plan = pipe.scan()
        self.plan_sig = plan_signature(cfg)
        self.q.put(("plan", self.plan))

    def _job_sync(self, cfg: Config):
        started = time.time()
        try:
            if self.plan and getattr(self, "plan_sig", None) != plan_signature(cfg):
                # 预览之后又改了分区方式 / 模板 / 笔记本之类的设置，旧计划必须作废，
                # 否则会照着旧分区名去写，跟界面上显示的完全对不上。
                self.log("设置与上次预览时不一致，按当前设置重新扫描计划…", "WARN")
                self.plan = []

            needs_plan = not self.plan
            if needs_plan or self.pipe is None:
                # 重新扫描时顺便换一个新 Pipeline：状态库路径也可能跟着改了
                pipe = Pipeline(cfg, log=self.log, step=self._stage_async)
                self.pipe = pipe
            else:
                pipe = self.pipe
                pipe.cfg = cfg

            if needs_plan:
                self.plan = pipe.scan()
                self.plan_sig = plan_signature(cfg)
                # 只读核对，认不出已有分区/同名页面就会重复建页
                pipe.inspect_targets(self.plan)
                self.q.put(("plan", self.plan))
            self.stop_event.clear()

            def progress(done, total, text):
                self.q.put(("progress", (done, total, text)))
                if cfg.dry_run:
                    row = "skip" if "跳过" in text else "preview"
                else:
                    row = "fail" if "失败" in text else ("skip" if "跳过" in text else "done")
                self.q.put(("row", (done, row)))

            stats = pipe.run(self.plan, dry_run=cfg.dry_run, stop=self.stop_event, progress=progress)
            secs = time.time() - started
            tail = (f"新建 {stats['created']}、更新 {stats['updated']}、"
                    f"跳过 {stats['skipped']}、失败 {stats['failed']}")
            if stats.get("dry"):
                # 预览跑完就报「完成」，正是上次让人以为「已经导入了」的原因，这里说清楚
                self.log(f"预览完成（未写入 OneNote）：{tail}，耗时 {secs:.1f}s", "INFO")
                self.log("OneNote 里不会有任何新页面。取消勾选「预览模式」后再点「开始导入」才会真的写入。", "WARN")
                status = f"预览完成（未写入）：{stats['created']} 新建 / {stats['updated']} 更新"
            else:
                self.log(f"完成：{tail}，耗时 {secs:.1f}s", "OK" if stats["failed"] == 0 else "WARN")
                self.last_page_id = stats.get("last_page_id", "")
                self.open_writer = pipe.writer
                if self.last_page_id:
                    # 按钮的启用交给 _set_busy(False)，那是在主线程跑的；
                    # 这里直接碰控件是跨线程改 Tk，容易崩。
                    self.log("想看看写进哪里了？点右上角「打开最后一页」，OneNote 会直接跳到那一页。", "INFO")
                status = f"完成：{stats['created']} 新建 / {stats['updated']} 更新 / {stats['failed']} 失败"
            for rel, err in stats.get("failures", []):
                self.log(f"  · {rel}: {err}", "ERROR")
            self.q.put(("status", status))
        except StopSignal:
            self.log("已手动停止", "WARN")
            self.q.put(("status", "已停止"))
        except Exception as e:
            self.log(f"同步失败：{e}", "ERROR")
            self.log(traceback.format_exc(), "DEBUG")
            self.q.put(("msg", ("error", "同步失败", str(e))))
        # busy 的收尾统一由 _bg 负责，这里不再重复发

    def do_stop(self):
        self.stop_event.set()
        self.status.set("正在停止…")

    def open_in_onenote(self):
        """让桌面版 OneNote 跳到刚写入的那一页。

        「提示导入成功、却在 OneNote 里找不到」这个问题，最好的回答就是直接把
        那一页摆在他眼前——顺便也证明了页面真的存在。
        """
        page_id = (self.last_page_id or "").strip()
        if not page_id:
            messagebox.showinfo("还没有可打开的页面",
                                "先跑一次导入（记得关掉「预览模式」），这里会定位到刚写入的最后一页。")
            return
        writer = self.open_writer
        if writer is None:
            messagebox.showinfo("通道已失效", "重新跑一次导入后可以再用这个按钮。")
            return

        def job():
            self._stage_async("在 OneNote 中定位页面")
            if writer.goto_page(page_id):
                self.log("已在 OneNote 中定位到刚写入的最后一页", "OK")
            else:
                self.log("没能自动打开 OneNote。手工找法：在左侧笔记本列表里展开目标笔记本，"
                         "点开对应的分区，页面按时间排在最下面。", "WARN")

        self._bg(job)

    def _bg(self, fn):
        """所有后台任务统一从这儿起：进出各发一次 busy。

        以前只有「开始导入」会置忙，扫描 / 刷新全在后台悄悄跑，
        用户只能干瞪眼。统一走这里，进度区就不会漏开漏关。
        """

        def run():
            self.q.put(("busy", True))
            try:
                fn()
            finally:
                self.q.put(("busy", False))

        t = threading.Thread(target=run, daemon=True)
        self.worker = t
        t.start()

    # ================================================================ 登录
    def do_login(self):
        cfg = self._ui_to_cfg()
        cfg.save()
        auth = DeviceCodeAuth(cfg.graph_client_id, cfg.graph_authority)
        try:
            session = auth.start()
        except AuthError as e:
            messagebox.showerror("无法开始登录", str(e))
            return

        ui, c = self.ui, self.ui.c
        win = tk.Toplevel(self)
        win.title("设备码登录")
        win.configure(bg=c["app_bg"])
        w, h = ui.px(560), ui.px(360)
        win.geometry(f"{w}x{h}+{self.winfo_x() + ui.px(160)}+{self.winfo_y() + ui.px(120)}")
        win.transient(self)
        win.grab_set()

        card = Card(win, ui, "用浏览器完成授权", "登录一次即可，之后自动静默续期", pad=(20, 18))
        card.pack(fill="both", expand=True, padx=ui.px(14), pady=ui.px(14))
        tk.Label(card.body, text="1  打开下面的网址", bg=c["surface"], fg=c["text"],
                 font=ui.font("body", "bold")).pack(anchor="w")
        link = tk.StringVar(value=session.verification_uri)
        ttk.Entry(card.body, textvariable=link, font=ui.font("mono")).pack(fill="x", pady=(ui.px(6), ui.px(14)))
        tk.Label(card.body, text="2  输入验证码", bg=c["surface"], fg=c["text"],
                 font=ui.font("body", "bold")).pack(anchor="w")
        code_wrap = tk.Frame(card.body, bg=c["accent_soft"])
        code_wrap.pack(fill="x", pady=(ui.px(6), ui.px(14)))
        tk.Label(code_wrap, text=session.user_code, bg=c["accent_soft"], fg=c["accent"],
                 font=ui.font("display", "bold")).pack(pady=ui.px(10))

        btns = tk.Frame(card.body, bg=c["surface"])
        btns.pack(fill="x")
        FlatButton(btns, ui, "打开浏览器", variant="primary",
                   command=lambda: webbrowser.open(session.verification_uri)).pack(side="left")
        FlatButton(btns, ui, "复制验证码", variant="secondary",
                   command=lambda: (self.clipboard_clear(), self.clipboard_append(session.user_code))).pack(
            side="left", padx=ui.px(8))

        state = tk.StringVar(value="等待授权…（完成后本窗口自动关闭）")
        tk.Label(card.body, textvariable=state, bg=c["surface"], fg=c["text_muted"],
                 font=ui.font("small")).pack(anchor="w", pady=(ui.px(14), 0))

        def poll():
            while not session.expired:
                time.sleep(max(3, session.interval))
                try:
                    token = auth.poll_once(session)
                except AuthError as e:
                    self.q.put(("msg", ("error", "登录失败", str(e))))
                    return
                if token:
                    self.token_store.update_from_token(token, cfg.graph_client_id)
                    self.log("设备码登录成功", "OK")
                    self.q.put(("msg", ("info", "登录完成", "已保存刷新令牌，之后会静默续期。")))
                    win.after(50, win.destroy)
                    self._ui_to_cfg().save()
                    return
            state.set("验证码已过期，请重试")
            self.q.put(("status", "登录超时"))

        threading.Thread(target=poll, daemon=True).start()

    def do_logout(self):
        self.token_store.clear()
        self.var_auth_state.set("已清除登录信息")
        self.log("已清除本地 token", "INFO")

    def do_probe(self):
        cfg = self._ui_to_cfg()
        self.log("探测写入通道…", "INFO")

        def job():
            try:
                res = probe_all(cfg, self._logfn(), step=self._stage_async)
                self.q.put(("probe", res))
            except Exception as e:
                self.log(f"探测失败：{e}", "ERROR")

        self._bg(job)

    def _fill_probe(self, results):
        c = self.ui.c
        self.txt_probe.configure(state="normal")
        self.txt_probe.tag_configure("ok", foreground=c["success"])
        self.txt_probe.tag_configure("bad", foreground=c["danger"])
        self.txt_probe.tag_configure("dim", foreground=c["text_muted"])
        self.txt_probe.delete("1.0", "end")
        for kind, res in results:
            name = {"graph": "Microsoft Graph", "com": "桌面版 OneNote (COM)"}.get(kind, kind)
            tag = "ok" if res.available else "bad"
            self.txt_probe.insert("end", "● ", tag)
            self.txt_probe.insert("end", f"{name}  ")
            self.txt_probe.insert("end", "可用\n" if res.available else "不可用\n", tag)
            self.txt_probe.insert("end", f"  {res.reason}\n\n", "dim")
        self.txt_probe.configure(state="disabled")

        avail = [k for k, r in results if r.available]
        label = {"graph": "Graph", "com": "桌面 COM"}
        if avail:
            prefer = self.var_writer.get()
            order = ["graph", "com"] if prefer != "com" else ["com", "graph"]
            main = next((a for a in order if a in avail), avail[0])
            self.badge_channel.set("使用中：" + label.get(main, main), "success")
            self.var_auth_state.set("可用通道：" + "、".join(label.get(a, a) for a in avail))
        else:
            self.badge_channel.set("无可用通道", "danger")
            self.var_auth_state.set("可用通道：无 — 请完成登录或安装桌面版 OneNote")

    # ================================================================ 杂项
    def open_appdata(self):
        from core.config import appdata_dir
        path = appdata_dir()
        try:
            import os
            os.startfile(str(path))  # noqa: S606 — 用户主动打开自己的配置目录
        except Exception:
            subprocess.Popen(["explorer", str(path)], **silent_kwargs())

    def run_selftest(self):
        script = HERE / "tests" / "selftest.py"
        self.log(f"运行自检：{script}", "INFO")

        def job():
            self._stage_async("跑自检（渲染 / 分区 / 跨线程）")
            rc, out, err = run_script(script)
            for line in out.splitlines():
                self.log(line, "INFO")
            tail = err.strip()
            if tail:
                for line in tail.splitlines()[:20]:
                    self.log(line, "ERROR")
            self.log(f"自检退出码 {rc}", "OK" if rc == 0 else "ERROR")

        self._bg(job)

    def reset_defaults(self):
        if not messagebox.askyesno("恢复默认", "清空当前设置并恢复默认值？"):
            return
        self.cfg = Config()
        self._load_cfg_to_ui()
        self.log("已恢复默认配置", "INFO")

    def on_close(self):
        try:
            cfg = self._ui_to_cfg()
            cfg.save()
            if self.pipe:
                self.pipe.state.close()
        except Exception:
            pass
        self.destroy()


def _enable_dpi_awareness():
    """Windows 高 DPI 缩放时开启进程级感知，避免界面发虚或尺寸偏小。"""
    setup_dpi_awareness()


def main():
    _enable_dpi_awareness()
    if "--selftest" in sys.argv:
        rc, out, err = run_script(HERE / "tests" / "selftest.py")
        text = (out + err).rstrip() + "\n"
        if sys.stdout is not None:
            sys.stdout.write(text)
        if getattr(sys, "frozen", False):
            # 打包版没有控制台，输出得落到文件再弹个框，否则用户什么都看不到
            from core.config import appdata_dir
            log = appdata_dir() / "selftest.log"
            log.write_text(text, encoding="utf-8")
            messagebox.showinfo("导入自检", f"退出码 {rc}\n\n完整输出已写到：\n{log}")
        raise SystemExit(rc)
    app = SyncApp()
    app.mainloop()


if __name__ == "__main__":
    main()
