# -*- coding: utf-8 -*-
"""界面视觉系统：设计 token + 圆角控件 + ttk 主题。

只依赖标准库 tkinter。所有尺寸都走 `Ui.px()`，按屏幕 DPI 等比放大，
所以 150% 缩放的屏幕上也和 100% 一样"看起来一样大"。
"""
from __future__ import annotations

import ctypes
import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

# ----------------------------------------------------------------- 设计 token
COLORS = {
    "app_bg": "#F3F5F8",       # 应用底色
    "sidebar": "#FFFFFF",      # 侧边栏
    "surface": "#FFFFFF",      # 卡片
    "surface_alt": "#F8FAFC",  # 次级面板
    "border": "#E4E8EE",       # 1px 描边
    "border_strong": "#CFD6DF",
    "text": "#1A1F27",         # 主文字
    "text_soft": "#59616E",    # 次文字
    "text_muted": "#8B93A1",   # 弱文字
    "accent": "#2F6BFF",       # 主色
    "accent_hover": "#1F55DC",
    "accent_soft": "#ECF2FF",
    "success": "#0E9F6E",
    "success_soft": "#E6F6EF",
    "warn": "#B26A00",
    "warn_soft": "#FDF3E2",
    "danger": "#D8403F",
    "danger_soft": "#FDECEC",
    "info": "#2F6BFF",
    "shadow": "#EDF0F4",
}

LOG_COLORS = {
    "OK": COLORS["success"],
    "INFO": COLORS["text_soft"],
    "WARN": COLORS["warn"],
    "ERROR": COLORS["danger"],
    "DEBUG": COLORS["text_muted"],
}

UI_FONTS = ("Microsoft YaHei UI", "微软雅黑", "Segoe UI", "Arial")
MONO_FONTS = ("Cascadia Mono", "JetBrains Mono", "Consolas", "Courier New")
# 界面里的图标字符（⇅ ⚿ ⚙ 之类）在正文字体里可能缺字形，单独走符号字体
SYMBOL_FONTS = ("Segoe UI Symbol", "Segoe UI Emoji", "Arial Unicode MS")


def round_rect(cv: tk.Canvas, x1, y1, x2, y2, r, **kw):
    """在 Canvas 上画圆角矩形，返回 item id。"""
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return cv.create_polygon(pts, smooth=True, **kw)


AWARENESS = "未设置"


def setup_dpi_awareness() -> str:
    """必须在创建任何窗口之前调用。

    逐级降级：per-monitor v2 → per-monitor → system → 老 API。
    返回实际生效的档位，便于排障。
    """
    global AWARENESS
    if sys.platform != "win32":
        AWARENESS = "n/a"
        return AWARENESS
    u = ctypes.windll.user32
    try:
        fn = u.SetProcessDpiAwarenessContext
        fn.argtypes = [ctypes.c_void_p]
        fn.restype = ctypes.c_bool
        if fn(ctypes.c_void_p(-4)):            # PER_MONITOR_AWARE_V2
            AWARENESS = "per-monitor-v2"
            return AWARENESS
        if fn(ctypes.c_void_p(-2)):            # PER_MONITOR_AWARE
            AWARENESS = "per-monitor"
            return AWARENESS
    except Exception:
        pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:   # PROCESS_PER_MONITOR_DPI_AWARE
            AWARENESS = "per-monitor-shcore"
            return AWARENESS
        if ctypes.windll.shcore.SetProcessDpiAwareness(1) == 0:   # PROCESS_SYSTEM_DPI_AWARE
            AWARENESS = "system-shcore"
            return AWARENESS
    except Exception:
        pass
    try:
        if u.SetProcessDPIAware():
            AWARENESS = "system-legacy"
            return AWARENESS
    except Exception:
        pass
    AWARENESS = "none"
    return AWARENESS


def query_dpi() -> int:
    """当前生效的屏幕 DPI（96 = 100%）。"""
    if sys.platform != "win32":
        return 96
    u = ctypes.windll.user32
    try:
        dpi = int(u.GetDpiForSystem())
        if dpi >= 48:
            return dpi
    except Exception:
        pass
    try:
        hwnd = u.GetDesktopWindow()
        dpi = int(u.GetDpiForWindow(hwnd))
        if dpi >= 48:
            return dpi
    except Exception:
        pass
    try:
        hdc = u.GetDC(0)
        dpi = int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 88))   # LOGPIXELSX
        u.ReleaseDC(0, hdc)
        return dpi or 96
    except Exception:
        return 96


class Ui:
    """集中管理缩放、字体与 ttk 样式。"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.awareness = AWARENESS
        self.dpi = query_dpi()
        self.scale = max(1.0, round(self.dpi / 96.0, 2))
        self.fonts = self._resolve_fonts()
        self.c = COLORS
        # 字号（逻辑 pt）
        self.fs = {
            "display": 17, "h1": 14, "h2": 12, "body": 10.5,
            "small": 9.5, "tiny": 8.5, "mono": 10,
        }
        self._font_cache: dict[tuple, tkfont.Font] = {}
        self._install_theme()

    # ------------------------------------------------------------- 基础
    def _resolve_fonts(self) -> tuple[str, str, str]:
        try:
            fams = {f.lower() for f in tkfont.families(self.root)}
        except Exception:
            fams = set()
        ui = next((f for f in UI_FONTS if f.lower() in fams), "TkDefaultFont")
        mono = next((f for f in MONO_FONTS if f.lower() in fams), "Courier New")
        sym = next((f for f in SYMBOL_FONTS if f.lower() in fams), ui)
        return ui, mono, sym

    def px(self, n: float) -> int:
        """逻辑像素 → 物理像素。"""
        return int(round(n * self.scale))

    def font(self, key: str = "body", weight: str = "normal", mono: bool = False) -> tkfont.Font:
        cache_key = (key, weight, mono)
        if cache_key in self._font_cache:
            return self._font_cache[cache_key]
        size = self.fs.get(key, self.fs["body"])
        fam = self.fonts[1] if mono else self.fonts[0]
        f = tkfont.Font(root=self.root, family=fam, size=-int(round(size * self.scale)), weight=weight)
        self._font_cache[cache_key] = f
        return f

    def sym_font(self, key: str = "body", weight: str = "normal") -> tkfont.Font:
        """图标字体：确保 ⇅ ⚿ ⚙ 这类字符有字形，不会变成方框。"""
        cache_key = ("__sym__", key, weight)
        if cache_key in self._font_cache:
            return self._font_cache[cache_key]
        size = self.fs.get(key, self.fs["body"])
        f = tkfont.Font(root=self.root, family=self.fonts[2],
                        size=-int(round(size * self.scale)), weight=weight)
        self._font_cache[cache_key] = f
        return f

    # ------------------------------------------------------------- ttk 主题
    def _install_theme(self):
        c, px = self.c, self.px
        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass

        body = self.font("body")
        small = self.font("small")
        bold = self.font("body", "bold")
        h2 = self.font("h2", "bold")

        # ---- 按钮：次要（描边）
        st.configure(
            "Secondary.TButton", font=body, background=c["surface"], foreground=c["text"],
            bordercolor=c["border_strong"], lightcolor=c["surface"], darkcolor=c["surface"],
            focuscolor=c["surface"], relief="flat", borderwidth=1, padding=(px(14), px(7)),
        )
        st.map(
            "Secondary.TButton",
            background=[("pressed", c["surface_alt"]), ("active", c["surface_alt"]), ("disabled", c["surface_alt"])],
            foreground=[("disabled", c["text_muted"])],
            bordercolor=[("active", c["accent"]), ("disabled", c["border"])],
        )

        # ---- 输入框
        st.configure(
            "TEntry", fieldbackground=c["surface"], foreground=c["text"], insertcolor=c["text"],
            bordercolor=c["border_strong"], lightcolor=c["border_strong"], darkcolor=c["border_strong"],
            borderwidth=1, relief="flat", padding=(px(8), px(6)),
        )
        st.configure(
            "Focus.TEntry", fieldbackground=c["surface"], foreground=c["text"], insertcolor=c["text"],
            bordercolor=c["accent"], lightcolor=c["accent"], darkcolor=c["accent"],
            borderwidth=1, relief="flat", padding=(px(8), px(6)),
        )
        st.map("TEntry", bordercolor=[("focus", c["accent"])])
        st.configure(
            "TCombobox", fieldbackground=c["surface"], background=c["surface"], foreground=c["text"],
            arrowcolor=c["text_soft"], bordercolor=c["border_strong"],
            lightcolor=c["border_strong"], darkcolor=c["border_strong"],
            borderwidth=1, relief="flat", padding=(px(8), px(5)),
        )
        st.map(
            "TCombobox",
            fieldbackground=[("readonly", c["surface"])],
            bordercolor=[("focus", c["accent"]), ("hover", c["accent"])],
            arrowcolor=[("active", c["accent"])],
        )
        self.root.option_add("*TCombobox*Listbox.font", body)
        self.root.option_add("*TCombobox*Listbox.background", c["surface"])
        self.root.option_add("*TCombobox*Listbox.foreground", c["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", c["accent_soft"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", c["text"])
        self.root.option_add("*TCombobox*Listbox.borderWidth", 0)

        # ---- 复选框
        st.configure(
            "TCheckbutton", font=body, background=c["surface"], foreground=c["text"],
            focuscolor=c["surface"], indicatorcolor=c["surface"], indicatormargin=px(4),
            padding=(0, px(2)), relief="flat", borderwidth=0,
        )
        st.map(
            "TCheckbutton",
            indicatorcolor=[("selected", c["accent"]), ("active", c["accent_soft"]), ("!selected", c["surface"])],
            foreground=[("disabled", c["text_muted"])],
            background=[("active", c["surface"])],
        )
        st.configure(
            "Sidebar.TCheckbutton", font=body, background=c["surface_alt"], foreground=c["text"],
            focuscolor=c["surface_alt"], indicatorcolor=c["surface"], indicatormargin=px(4),
            padding=(0, px(2)), relief="flat", borderwidth=0,
        )
        st.map(
            "Sidebar.TCheckbutton",
            indicatorcolor=[("selected", c["accent"]), ("active", c["accent_soft"]), ("!selected", c["surface"])],
            background=[("active", c["surface_alt"])],
        )

        # ---- 单选框
        st.configure(
            "TRadiobutton", font=body, background=c["surface"], foreground=c["text"],
            focuscolor=c["surface"], indicatorcolor=c["surface"], indicatormargin=px(4),
            padding=(0, px(2)), relief="flat", borderwidth=0,
        )
        st.map(
            "TRadiobutton",
            indicatorcolor=[("selected", c["accent"]), ("active", c["accent_soft"]), ("!selected", c["surface"])],
            background=[("active", c["surface"])],
        )

        # ---- 进度条
        st.configure(
            "Modern.Horizontal.TProgressbar", troughcolor=c["shadow"], background=c["accent"],
            bordercolor=c["shadow"], lightcolor=c["accent"], darkcolor=c["accent"],
            borderwidth=0, thickness=px(7),
        )

        # ---- 表格
        st.configure(
            "Modern.Treeview", font=body, background=c["surface"], fieldbackground=c["surface"],
            foreground=c["text"], bordercolor=c["border"], borderwidth=0, relief="flat",
            rowheight=px(32),
        )
        st.configure(
            "Modern.Treeview.Heading", font=small, background=c["surface_alt"], foreground=c["text_soft"],
            relief="flat", borderwidth=0, padding=(px(10), px(8)),
        )
        st.map(
            "Modern.Treeview",
            background=[("selected", c["accent_soft"])],
            foreground=[("selected", c["text"])],
        )
        st.map("Modern.Treeview.Heading", background=[("active", c["surface_alt"])])
        st.layout("Modern.Treeview", [("Modern.Treeview.treearea", {"sticky": "nswe"})])

        # ---- 滚动条：细长无箭头
        for orient, name in (("vertical", "Vertical.TScrollbar"), ("horizontal", "Horizontal.TScrollbar")):
            st.configure(
                name, background=c["border_strong"], troughcolor=c["app_bg"],
                bordercolor=c["app_bg"], arrowcolor=c["app_bg"], relief="flat",
                borderwidth=0, arrowsize=0, width=px(8),
            )
            st.map(name, background=[("active", c["text_muted"]), ("pressed", c["text_soft"])])
        st.configure("Inner.Vertical.TScrollbar", background=c["border_strong"], troughcolor=c["surface"],
                     bordercolor=c["surface"], arrowcolor=c["surface"], relief="flat",
                     borderwidth=0, arrowsize=0, width=px(8))
        st.map("Inner.Vertical.TScrollbar", background=[("active", c["text_muted"])])


# ------------------------------------------------------------------ 卡片
class Card(tk.Frame):
    """圆角 + 1px 描边的白色卡片。内容放进 `card.body`（inner 为内层 Frame）。"""

    def __init__(self, parent, ui: Ui, title: str | None = None, subtitle: str | None = None,
                 pad: tuple[int, int] = (18, 16), bg: str | None = None, radius: int = 12):
        outer_bg = bg or ui.c["app_bg"]
        super().__init__(parent, bg=outer_bg, highlightthickness=0, bd=0)
        self.ui, self._outer_bg, self._radius = ui, outer_bg, ui.px(radius)
        self._padx, self._pady = ui.px(pad[0]), ui.px(pad[1])

        self.canvas = tk.Canvas(self, bg=outer_bg, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        self.inner = tk.Frame(self.canvas, bg=ui.c["surface"])
        self._win = self.canvas.create_window(self._padx, self._pady, window=self.inner, anchor="nw")

        if title:
            head = tk.Frame(self.inner, bg=ui.c["surface"])
            head.pack(fill="x", pady=(0, ui.px(pad[1] * 0.55)))
            tk.Label(head, text=title, bg=ui.c["surface"], fg=ui.c["text"],
                     font=ui.font("h2", "bold")).pack(side="left")
            if subtitle:
                tk.Label(head, text=subtitle, bg=ui.c["surface"], fg=ui.c["text_muted"],
                         font=ui.font("small")).pack(side="left", padx=(ui.px(10), 0), pady=(ui.px(2), 0))

        self.body = tk.Frame(self.inner, bg=ui.c["surface"])
        self.body.pack(fill="both", expand=True)

        self.canvas.bind("<Configure>", self._on_canvas)
        self.inner.bind("<Configure>", self._on_inner)

    def _on_inner(self, _ev=None):
        want = self.inner.winfo_reqheight() + self._pady * 2
        if abs(self.canvas.winfo_reqheight() - want) > 1:
            self.canvas.configure(height=want)

    def _on_canvas(self, _ev=None):
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        self.canvas.delete("cardbg")
        round_rect(self.canvas, 0.5, 0.5, w - 0.5, h - 0.5, self._radius,
                   fill=self.ui.c["surface"], outline=self.ui.c["border"], width=1, tags="cardbg")
        self.canvas.tag_lower("cardbg")
        self.canvas.itemconfigure(self._win, width=max(1, w - self._padx * 2))


class Badge(tk.Canvas):
    """小圆角标签，用于状态/通道提示。"""

    VARIANTS = {
        "neutral": ("surface_alt", "text_soft", "border"),
        "accent": ("accent_soft", "accent", "accent_soft"),
        "success": ("success_soft", "success", "success_soft"),
        "warn": ("warn_soft", "warn", "warn_soft"),
        "danger": ("danger_soft", "danger", "danger_soft"),
    }

    def __init__(self, parent, ui: Ui, text: str, variant: str = "neutral", bg: str | None = None):
        outer = bg or ui.c["surface"]
        self.ui = ui
        self._variant = variant
        self._text = text
        f = ui.font("tiny", "bold")
        # 注意：不能用 self._w / self._h —— tkinter 的 Misc 把 _w 用作 widget 路径名，
        # 会在 super().__init__() 里被覆盖成字符串。
        self._bw = f.measure(text) + ui.px(16)
        self._bh = ui.px(20)
        super().__init__(parent, bg=outer, highlightthickness=0, bd=0,
                         width=self._bw, height=self._bh)
        self._f = f
        self.bind("<Configure>", lambda e: self._draw())
        self._draw()

    def _draw(self):
        c = self.ui.c
        fill, fg, _ = self.VARIANTS.get(self._variant, self.VARIANTS["neutral"])
        w = max(self._bw, self.winfo_width())
        h = max(self._bh, self.winfo_height())
        self.delete("all")
        round_rect(self, 0.5, 0.5, w - 0.5, h - 0.5, h / 2, fill=c[fill], outline=c[fill])
        self.create_text(w / 2, h / 2 + 1, text=self._text, fill=c[fg], font=self._f)

    def set(self, text: str, variant: str | None = None):
        self._text = text
        if variant:
            self._variant = variant
        f = self.ui.font("tiny", "bold")
        self._bw = f.measure(text) + self.ui.px(16)
        self.configure(width=self._bw)
        self._draw()


class FlatButton(tk.Button):
    """扁平按钮：primary / secondary / ghost / danger 四种形态，自带 hover。"""

    def __init__(self, parent, ui: Ui, text: str, command=None, variant: str = "secondary",
                 bg: str | None = None, width: int | None = None):
        self.ui = ui
        self.variant = variant
        self._surface = bg or ui.c["surface"]
        style = self._palette(variant, self._surface)
        kw = dict(text=text, command=command, bd=0, relief="flat", highlightthickness=0,
                  cursor="hand2", font=ui.font("body", "bold" if variant == "primary" else "normal"),
                  padx=ui.px(16), pady=ui.px(7), **style)
        if width:
            kw["width"] = width
        super().__init__(parent, **kw)
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))

    def _palette(self, variant: str, surface: str):
        c = self.ui.c
        if variant == "primary":
            return dict(bg=c["accent"], fg="#FFFFFF", activebackground=c["accent_hover"],
                        activeforeground="#FFFFFF", disabledforeground="#C7D4F5")
        if variant == "danger":
            return dict(bg=c["danger_soft"], fg=c["danger"], activebackground="#FBDCDC",
                        activeforeground=c["danger"])
        if variant == "ghost":
            return dict(bg=surface, fg=c["text_soft"], activebackground=c["surface_alt"],
                        activeforeground=c["text"])
        return dict(bg=surface, fg=c["text"], activebackground=c["surface_alt"],
                    activeforeground=c["text"])

    def _hover(self, on: bool):
        if str(self["state"]) == "disabled":
            return
        c = self.ui.c
        table = {
            "primary": (c["accent_hover"], "#FFFFFF"),
            "secondary": (c["accent_soft"], c["accent"]),
            "ghost": (c["surface_alt"], c["text"]),
            "danger": ("#FBDCDC", c["danger"]),
        }
        bg, fg = table[self.variant]
        if on:
            self.configure(bg=bg, fg=fg)
        else:
            self.configure(**self._palette(self.variant, self._surface))

    def set_enabled(self, on: bool):
        c = self.ui.c
        self.configure(state="normal" if on else "disabled")
        if self.variant == "primary":
            self.configure(bg=c["accent"] if on else "#AFC3F7")


class NavItem(tk.Frame):
    """侧边栏导航项：选中时浅色底 + 左侧强调条。"""

    def __init__(self, parent, ui: Ui, icon: str, text: str, command=None, bg: str | None = None):
        surface = bg or ui.c["sidebar"]
        super().__init__(parent, bg=surface, cursor="hand2")
        self.ui, self._surface, self._command = ui, surface, command
        self.active = False

        self.bar = tk.Frame(self, bg=surface, width=ui.px(3))
        self.bar.pack(side="left", fill="y")
        self.icon = tk.Label(self, text=icon, bg=surface, fg=ui.c["text_soft"],
                             font=ui.sym_font("body"), width=2)
        self.icon.pack(side="left", padx=(ui.px(12), ui.px(2)), pady=ui.px(9))
        self.label = tk.Label(self, text=text, bg=surface, fg=ui.c["text"], font=ui.font("body"))
        self.label.pack(side="left", pady=ui.px(9))

        for w in (self, self.icon, self.label):
            w.bind("<Button-1>", self._click)
            w.bind("<Enter>", lambda e: self._hover(True))
            w.bind("<Leave>", lambda e: self._hover(False))

    def _click(self, _ev=None):
        if self._command:
            self._command()

    def _paint(self, bg: str, fg: str, bar: str):
        for w in (self, self.icon, self.label):
            w.configure(bg=bg)
        self.icon.configure(fg=fg)
        self.label.configure(fg=fg, font=self.ui.font("body", "bold" if self.active else "normal"))
        self.bar.configure(bg=bar)

    def _hover(self, on: bool):
        if self.active:
            return
        c = self.ui.c
        self._paint(c["surface_alt"] if on else self._surface,
                    c["text"] if on else c["text"], self._surface if on else self._surface)

    def set_active(self, on: bool):
        c = self.ui.c
        self.active = on
        if on:
            self.bar.configure(width=self.ui.px(3))
            self._paint(c["accent_soft"], c["accent"], c["accent"])
        else:
            self._paint(self._surface, c["text"], self._surface)


class Check(tk.Frame):
    """自绘复选框：圆角方框 + 勾，比 ttk 在 clam 下的指示符干净。"""

    def __init__(self, parent, ui: Ui, text: str, variable: tk.BooleanVar,
                 bg: str | None = None, width_hint: int | None = None):
        surface = bg or ui.c["surface"]
        super().__init__(parent, bg=surface, cursor="hand2")
        self.ui, self.var, self._bg = ui, variable, surface
        self._hover = False
        size = ui.px(17)
        self.box = tk.Canvas(self, bg=surface, width=size, height=size, highlightthickness=0, bd=0)
        self.box.pack(side="left")
        self.label = tk.Label(self, text=text, bg=surface, fg=ui.c["text"], font=ui.font("body"),
                              width=width_hint or 0, anchor="w")
        self.label.pack(side="left", padx=(ui.px(8), 0))
        for w in (self, self.box, self.label):
            w.bind("<Button-1>", self._toggle)
            w.bind("<Enter>", lambda e: self._hovering(True))
            w.bind("<Leave>", lambda e: self._hovering(False))
        self.var.trace_add("write", lambda *_: self._draw())
        self._draw()

    def _toggle(self, _ev=None):
        self.var.set(not bool(self.var.get()))

    def _hovering(self, on: bool):
        self._hover = on
        self._draw()

    def _draw(self):
        c, ui = self.ui.c, self.ui
        s = ui.px(17)
        on = bool(self.var.get())
        self.box.delete("all")
        fill = c["accent"] if on else (c["accent_soft"] if self._hover else c["surface"])
        outline = c["accent"] if (on or self._hover) else c["border_strong"]
        round_rect(self.box, 0.5, 0.5, s - 0.5, s - 0.5, ui.px(4.5), fill=fill, outline=outline)
        if on:
            self.box.create_line(s * 0.26, s * 0.52, s * 0.44, s * 0.70, s * 0.75, s * 0.31,
                                 fill="#FFFFFF", width=max(2, ui.px(2)),
                                 capstyle="round", joinstyle="round")


class Segmented(tk.Canvas):
    """分段选择器（替代一排单选按钮）。"""

    def __init__(self, parent, ui: Ui, options: list[tuple[str, str]], variable: tk.StringVar,
                 bg: str | None = None):
        self.ui, self.options, self.var = ui, options, variable
        self._f = ui.font("body")
        self._fb = ui.font("body", "bold")
        seg_w = max(ui.px(104), max(self._f.measure(lbl) for _, lbl in options) + ui.px(30))
        self._segw, self._sgh = seg_w, ui.px(34)
        super().__init__(parent, bg=bg or ui.c["surface"], highlightthickness=0, bd=0,
                         width=seg_w * len(options) + ui.px(10), height=self._sgh, cursor="hand2")
        self.bind("<Button-1>", self._click)
        self.bind("<Configure>", lambda e: self._draw())
        self.var.trace_add("write", lambda *_: self._draw())
        self._draw()

    def _draw(self):
        c, ui = self.ui.c, self.ui
        self.delete("all")
        w = self.winfo_width() or (self._segw * len(self.options) + ui.px(10))
        h = self._sgh
        if w < 10:
            w = self._segw * len(self.options) + ui.px(10)
        pad = ui.px(4)
        round_rect(self, 0.5, 0.5, w - 0.5, h - 0.5, ui.px(9),
                   fill=c["surface_alt"], outline=c["border"])
        seg_w = (w - pad * 2) / len(self.options)
        for i, (val, label) in enumerate(self.options):
            x0 = pad + i * seg_w
            sel = self.var.get() == val
            if sel:
                round_rect(self, x0 + 1, pad + 1, x0 + seg_w - 1, h - pad - 1, ui.px(7),
                           fill=c["surface"], outline=c["border"])
            self.create_text(x0 + seg_w / 2, h / 2, text=label,
                             fill=c["accent"] if sel else c["text_soft"],
                             font=self._fb if sel else self._f)

    def _click(self, ev):
        pad = self.ui.px(4)
        w = self.winfo_width() or self._segw * len(self.options)
        seg_w = (w - pad * 2) / len(self.options)
        idx = int(max(0, ev.x - pad) // seg_w) if seg_w > 0 else 0
        idx = max(0, min(idx, len(self.options) - 1))
        self.var.set(self.options[idx][0])


class ScrollArea(tk.Frame):
    """可滚动容器。把内容放进 `area.inner`。"""

    def __init__(self, parent, ui: Ui, bg: str | None = None):
        outer = bg or ui.c["app_bg"]
        super().__init__(parent, bg=outer, highlightthickness=0, bd=0)
        self.ui = ui
        self.canvas = tk.Canvas(self, bg=outer, highlightthickness=0, bd=0)
        self.vs = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vs.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vs.pack(side="right", fill="y")

        self.inner = tk.Frame(self.canvas, bg=outer)
        self._win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind_all(seq, self._on_wheel, add="+")

    def _on_inner(self, _ev=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, ev):
        self.canvas.itemconfigure(self._win, width=ev.width)

    def _on_wheel(self, ev):
        if not self.winfo_exists() or self.canvas.winfo_height() < 40:
            return
        try:
            widget = self.winfo_containing(ev.x_root, ev.y_root)
        except Exception:
            return
        w = widget
        while w is not None:
            if w is self:
                break
            w = getattr(w, "master", None)
        else:
            return
        delta = 0
        if getattr(ev, "num", None) == 4:
            delta = -1
        elif getattr(ev, "num", None) == 5:
            delta = 1
        elif getattr(ev, "delta", 0):
            delta = -1 if ev.delta > 0 else 1
        if delta:
            self.canvas.yview_scroll(delta, "units")


def hline(parent, ui: Ui, bg: str | None = None) -> tk.Frame:
    outer = bg or ui.c["surface"]
    f = tk.Frame(parent, bg=ui.c["border"], height=1)
    f.pack(fill="x")
    return f
