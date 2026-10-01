# -*- coding: utf-8 -*-
"""界面冒烟测试：真的把窗口建出来跑几帧，确认控件不会炸，并留一组截图。

跑完自动关窗，不需要人工介入：
    python tools/smoke_ui.py [输出目录]

会检查：
  · 四个页面（导入日记 / 导入照片 / 账户与通道 / 高级设置）都能切过去
  · 日记页：计划树能填、两个分段控件能改值、日志能写
  · 照片页：懒加载不会在启动时触发、表格能填、控件能改
  · 缩放：改档位后界面能重建，且**日志不丢、配置不丢**
  · 进度区：任务中显示、任务后收起
  · 跨线程记账那条历史崩溃路径（回归）
  · 截图：每张留 md5 指纹，收尾时比对 —— 两张一模一样就是抓到了同一个页面

⚠️ 跑之前会把「照片配置目录」和「缩放偏好文件」都指到临时目录 ——
   GUI 的「扫描/导入」会无条件 cfg.save()，不隔离就会把使用者真实的
   照片设置和界面缩放改掉（2026-09-25 踩过一次）。
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ★ 必须在导入 app / photo_app 之前设好隔离目录
_SMOKE_CFG = Path(tempfile.gettempdir()) / "onenote_photo_smoke"
shutil.rmtree(_SMOKE_CFG, ignore_errors=True)
_SMOKE_CFG.mkdir(parents=True, exist_ok=True)
os.environ["ONENOTE_PHOTO_CONFIG_DIR"] = str(_SMOKE_CFG)
# 日记侧的配置目录也跟着隔离。不隔离的话，冒烟会读到**使用者自己的** config.json，
# 于是本机路径和真实笔记本名就会印进截图 —— 截图是要进 README 的，绝不能带这些。
os.environ["APPDATA"] = str(_SMOKE_CFG)

import atexit  # noqa: E402

import core.uiprefs as uiprefs  # noqa: E402

uiprefs.prefs_path = lambda: _SMOKE_CFG / "ui.json"   # 缩放偏好也隔离

from core.pipeline import PlanItem  # noqa: E402
from tools.screenshot import capture, force_visible  # noqa: E402
from ui.theme import setup_dpi_awareness  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "sample-output"
OUT.mkdir(parents=True, exist_ok=True)

user32 = ctypes.windll.user32


def top_level_hwnd(tk_win) -> int:
    """tkinter 的 winfo_id() 拿到的是客户区句柄，上层还要再取一次父窗口。"""
    hwnd = tk_win.winfo_id()
    parent = user32.GetParent(hwnd)
    return parent or hwnd


def fake_group(day: str, n: int, page: str, action: str):
    """照片页表格用的假数据（只要属性齐，_fill_tree 就能渲染）。"""
    shots = [SimpleNamespace(size=1200 * 1024) for _ in range(n)]
    return SimpleNamespace(day=day, photos=shots, new_only=shots[:max(1, n - 1)],
                           page_title=page, action=action)


def main() -> int:
    setup_dpi_awareness()
    from app import SyncApp

    class Smoke(SyncApp):
        problems: list[str] = []

        def __init__(self):
            super().__init__()
            self.repro_thread: threading.Thread | None = None
            self._settle = 0
            self._finished = False
            self.shots: dict[str, str] = {}
            # ⚠️ 步骤之间必须**串成一条链**，不能各挂一个 after 定时器。
            #    各挂一个的话，前一步 _settle_page() 里的 self.update() 会把**已经到期**
            #    的后一步提前抽出来执行（update() 泵的是整个事件队列，不是只看当前回调）。
            #    实测踩过：_step_pages 还在等 auth 页重绘，_step_photos 就抢着把页面切走，
            #    结果 02b 和 03 两张截图的字节数一模一样、内容都是照片页。
            self.after(400, self._step_widgets)
            self.after(180_000, self._watchdog)   # 兜底：哪一步卡死也要把窗口关掉

        # 万一某一步抛了没接住的异常，链子会断在半路 —— 兜底收尾，别把窗口挂在屏幕上
        def _watchdog(self):
            if self._finished:
                return
            self.problems.append("冒烟脚本超时（某一步卡住了），窗口被强制关闭")
            self._finish()

        # 页面是几层 Frame 用 place 叠着、靠 lift() 切换的，切完必须留时间给窗口重绘 ——
        # 立刻 PrintWindow 会抓到**上一页**（实测踩过：切到高级设置，截出来还是导入日记页）。
        def _settle_page(self, seconds: float = 0.9):
            end = time.time() + seconds
            while time.time() < end:
                self.update()
                time.sleep(0.05)

        def _shot(self, name: str, settle: float = 0.9) -> Path:
            """等重绘 → 截图 → 记指纹。

            两处都不省：
            · 先把窗口拉回可见再 settle —— 这台机器的窗口会被反复压回最小化，
              还原之后 Tk 得重新画一遍，得留时间。
            · 截图时把 self.update 当消息泵传进去 —— capture 内部的等待如果只是
              time.sleep，Tk 完全没法处理重绘消息，PrintWindow 会拿到上一帧。
              指纹则是收尾时机械查「两张图内容一样」用的，比肉眼看靠谱。
            """
            force_visible(top_level_hwnd(self))
            self._settle_page(settle)
            path = OUT / f"{name}.png"
            capture(top_level_hwnd(self), path, pump=self.update)
            self.shots[name] = hashlib.md5(path.read_bytes()).hexdigest()
            return path

        # 步骤 1：动一遍日记页的新控件
        def _neutral_fields(self):
            """把界面上会显示「使用者自己的东西」的字段换成中性演示值。

            截图是要进 README 的，而真实值形如 `C:/Users/<用户名>/...`、
            或某个私有笔记本名 —— 一旦印进截图就等于公开。

            必须在**每次截图前**都做一遍：缩放重建那一步会回读 config.json，
            把测试造的临时目录又灌回控件里（踩过 —— 07 号截图上漏出过
            `C:/Users/<用户名>/AppData/Local/Temp/smoke-vault-xxxx`）。
            """
            self.var_vault.set("D:/我的日记库")
            self.var_notebook.set("日记")
            self.update()

        def _step_widgets(self):
            try:
                for mode in ("fixed", "template"):
                    self.var_section_mode.set(mode)
                    self.update()
                for pol in ("update", "skip", "create"):
                    self.var_page_policy.set(pol)
                    self.update()
                hint = self.lbl_policy_hint.cget("text")
                if not hint:
                    self.problems.append("重名页面策略的提示文案是空的")
                # 模板导出成中文季节
                self.var_section.set("{YYYY}{season}")
                self._ui_to_cfg()

                # 可导入格式：勾上/取消要能真的传进 config，提示文案要跟着变
                from core import convert as _cv
                if set(self.var_formats) != set(_cv.SOURCE_FORMATS):
                    self.problems.append(f"可导入格式的勾选项不全：{sorted(self.var_formats)}")
                for key in _cv.SOURCE_FORMATS:
                    self.var_formats[key].set(True)
                self.update()
                cfg_all = self._ui_to_cfg()
                if set(cfg_all.source_formats) != set(_cv.SOURCE_FORMATS):
                    self.problems.append(
                        f"全勾之后 source_formats 不对：{cfg_all.source_formats}")
                tip_all = self.var_format_hint.get()
                for key in _cv.SOURCE_FORMATS:
                    self.var_formats[key].set(key == "md")
                self.update()
                cfg_md = self._ui_to_cfg()
                if cfg_md.source_formats != ["md"]:
                    self.problems.append(
                        f"只勾 md 之后 source_formats 不对：{cfg_md.source_formats}")
                if not tip_all or not self.var_format_hint.get():
                    self.problems.append("可导入格式的提示文案是空的")
                if tip_all == self.var_format_hint.get():
                    self.problems.append("改了格式勾选，提示文案却没变")
                # 一个都不勾要兜底成 md，否则会一篇都扫不到
                for key in _cv.SOURCE_FORMATS:
                    self.var_formats[key].set(False)
                self.update()
                if self._ui_to_cfg().source_formats != ["md"]:
                    self.problems.append("一个格式都不勾时没兜底成 md")
                self.var_formats["md"].set(True)
                self.update()
                print("      可导入格式：勾选能进配置、提示文案跟着变、空选兜底成 md")
            except Exception as e:  # noqa: BLE001 — 冒烟要把所有炸点摊开
                self.problems.append(f"操作分区控件时异常：{type(e).__name__}: {e}")
            self.after(300, self._step_photos)

        # 步骤 3：切页 + 填计划树 + 写日志 + 截图
        # 现在由 _step_photos 接力调起来，所以进来第一件事是把页面切回导入日记页。
        def _step_pages(self):
            try:
                self._show_page("sync")
                self.update()
                self._neutral_fields()
                self.var_section.set("{YYYY}{season}")
                self.update()
                items = [
                    PlanItem("日记/2026-06-22.md", Path("x"), "update", "6月22日", "2026夏",
                             section_exists=True, matched_page_id="p-1"),
                    PlanItem("日记/2026-06-23.md", Path("x"), "create", "6月23日", "2026夏",
                             section_exists=True),
                    PlanItem("日记/2026-09-07.md", Path("x"), "create", "9月7日", "2026秋",
                             section_exists=False),
                    PlanItem("日记/2026-09-08.md", Path("x"), "skip", "9月8日", "2026秋",
                             section_exists=False),
                ]
                self._fill_tree(items)
                self.log("冒烟测试：计划树已填入 4 条", "OK")
                self.log("冒烟测试：分区「2026夏」已存在，直接写入", "INFO")
                self.log("冒烟测试：这篇在 OneNote 里已有同名页面，就地更新，不另建", "WARN")
                self.update()
                self._shot("01-导入日记页")
            except Exception as e:  # noqa: BLE001
                self.problems.append(f"填充计划树时异常：{type(e).__name__}: {e}")

            # 顺序跟侧边栏一致，文件名跟着排下来
            for key, name in (("auth", "03-账户与通道页"), ("adv", "04-高级设置页"),
                              ("about", "05-关于页")):
                try:
                    self._show_page(key)
                    self._shot(name)
                except Exception as e:  # noqa: BLE001
                    self.problems.append(f"切换到{name}时异常：{type(e).__name__}: {e}")
            self.after(400, self._step_sync_repro)

        # 步骤 2：照片页 —— 懒加载 + 表格 + 控件
        def _step_photos(self):
            try:
                page = self.photo_page
                if getattr(page, "_booted", False):
                    self.problems.append("还没切到照片页，它就已经去连 OneNote 了（懒加载失效）")
                self._show_page("photos")
                self.update()
                for w in ("lst_dirs", "cmb_notebook", "cmb_section", "tree", "txt_log",
                          "btn_plan", "btn_run"):
                    if not hasattr(page, w):
                        self.problems.append(f"照片页缺少控件 {w}")
                page._fill_tree([
                    fake_group("2026-09-25", 3, "9月25日", "append"),
                    fake_group("2026-09-26", 2, "", "create"),
                    fake_group("2026-09-24", 1, "9月24日", "skip"),
                ])
                page._log("冒烟测试：照片预览表已填 3 行", "OK")
                page.var_date_source.set("只看文件名")
                page.var_dry.set(True)

                # 压缩规格：换档要真的回填数值、锁住/放开手填
                from photos.config import PHOTO_PRESETS, preset_label
                for p in PHOTO_PRESETS:
                    page.var_preset.set(preset_label(p["key"]))
                    page._apply_preset()
                    self.update()
                    custom = p["key"] == "custom"
                    want_state = "normal" if custom else "readonly"
                    if str(page.spn_edge.cget("state")) != want_state:
                        self.problems.append(
                            f"压缩规格「{p['label']}」的输入框状态不对："
                            f"{page.spn_edge.cget('state')}")
                    if not custom:
                        if page.var_edge.get() != str(p["max_edge"]) or \
                                page.var_quality.get() != str(p["quality"]):
                            self.problems.append(
                                f"压缩规格「{p['label']}」没回填数值："
                                f"{page.var_edge.get()}/{page.var_quality.get()}")
                    if not page.var_preset_tip.get():
                        self.problems.append(f"压缩规格「{p['label']}」的说明是空的")
                    # 选档之后的取值必须原样进配置
                    page._collect_cfg()
                    if not custom and (page.cfg.max_edge != p["max_edge"]
                                       or page.cfg.jpeg_quality != p["quality"]):
                        self.problems.append(
                            f"压缩规格「{p['label']}」没进配置："
                            f"{page.cfg.max_edge}/{page.cfg.jpeg_quality}")
                # 停在「省空间」这一档展示，顺便验一下自定义档能手填
                page.var_preset.set(preset_label("space"))
                page._apply_preset()
                page.var_preset.set(preset_label("custom"))
                page._apply_preset()
                page.var_edge.set("1440")
                page.var_quality.set("80")
                page._collect_cfg()
                if page.cfg.max_edge != 1440 or page.cfg.jpeg_quality != 80:
                    self.problems.append(
                        f"自定义档的手填值没进配置：{page.cfg.max_edge}/{page.cfg.jpeg_quality}")
                page.var_preset.set(preset_label("space"))
                page._apply_preset()
                self.update()
                print("      照片压缩规格：6 档都能回填/锁框/进配置，自定义档能手填")

                self._settle_page(0.7)    # 等照片页自己的 _drain 冲日志、窗口完成重绘
                if not page.tree.get_children():
                    self.problems.append("照片页表格没填进去")
                rows = len(page.tree.get_children())
                print(f"      照片页：表格 {rows} 行、控件齐全、切页没触发启动连接")
                self._shot("02-导入照片页", 0.7)
                # 配置区是滚动的，压缩规格那张卡片在下面 —— 不滚到底截图里看不到它
                page.area.scroll_to_end()
                self._shot("02b-照片压缩规格", 0.6)
                page.area.scroll_to_top()
                self.update()
            except Exception as e:  # noqa: BLE001
                self.problems.append(f"检查照片页时异常：{type(e).__name__}: {e}")
            self.after(400, self._step_pages)

        # 步骤 4：复现并守住「计划线程建库、同步线程复用」那条崩溃路径。
        # 预览模式不碰 OneNote，但会走到最后一步 record_run 记账 —— 正是原来炸的地方。
        def _step_sync_repro(self):
            self._show_page("sync")
            try:
                vault = Path(tempfile.mkdtemp(prefix="smoke-vault-"))
                (vault / "2026-06-22.md").write_text(
                    "---\ndate: 2026-06-22\ntitle: 夏至\ntags: [测试]\n---\n\n"
                    "今天把拖了很久的一件事收尾了。\n", encoding="utf-8")
                (vault / "2026-06-23.md").write_text(
                    "---\ndate: 2026-06-23\ntitle: 次日\n---\n\n"
                    "- [ ] 写一份说明文档\n", encoding="utf-8")

                self.var_vault.set(str(vault))
                self.var_notebook.set("日记")
                self.var_section_mode.set("fixed")
                self.var_section_fixed.set("2026夏")
                self.var_dry.set(True)          # 预览：不写 OneNote，但会记账
                self.var_page_policy.set("update")
                cfg = self._ui_to_cfg()
                cfg.state_path = str(vault / "_state.sqlite3")

                def chain():
                    # 故意在同一个线程里先计划、再换到另一个线程同步，
                    # 并复用同一个 Pipeline —— 老代码就是在这里抛
                    # sqlite3.ProgrammingError 的。
                    self._job_plan(cfg)
                    t = threading.Thread(target=self._job_sync, args=(cfg,))
                    t.start()
                    t.join(timeout=120)
                    shutil.rmtree(vault, ignore_errors=True)

                self.repro_thread = threading.Thread(target=chain, daemon=True)
                self.repro_thread.start()
                self.after(500, self._poll_repro)
            except Exception as e:  # noqa: BLE001
                self.problems.append(f"准备同步复现时异常：{type(e).__name__}: {e}")
                self._finish()

        def _poll_repro(self):
            self.update()
            if self.repro_thread is not None and self.repro_thread.is_alive():
                self.after(400, self._poll_repro)
                return
            # ⚠️ 「线程结束」不等于「日志已经显示在 Text 上」：日志是投进队列、
            #    由 _drain 每 120ms 取一次，最后几行很可能还躺在队列里。
            #    不 settle 就直接判，会偶发地误报 —— 之前因为这个白白怀疑过业务代码。
            if self._settle < 2:
                self._settle += 1
                self.after(300, self._poll_repro)
                return
            text = self.txt_log.get("1.0", "end")
            bad = [k for k in ("ProgrammingError", "Traceback", "同步失败") if k in text]
            if bad:
                self.problems.append(f"同步链路报错：出现 {bad}")
                self._finish()
                return
            if "预览完成（未写入 OneNote）" not in text:
                self.problems.append(
                    f"预览跑完没有明确写出「未写入 OneNote」（日志尾部：{text[-160:]!r}）")
            elif "新建 2" not in text:
                self.problems.append(f"同步链路没有跑完（日志里看不到「新建 2」；尾部：{text[-160:]!r}）")
            else:
                print("      GUI 同步链路复现通过（跨线程记账不再报错）")
                self._check_open_button()
            self.after(300, self._step_progress)

        def _check_open_button(self):
            """没写入内容时「打开最后一页」必须是灰的；有页面 ID 才亮。"""
            if not hasattr(self, "btn_open"):
                self.problems.append("找不到「打开最后一页」按钮")
                return
            if str(self.btn_open["state"]) != "disabled":
                self.problems.append("预览跑完（没写入任何页面）时，「打开最后一页」不该是可点的")
            self.last_page_id = "fake-page-1"
            self._set_busy(False)
            if str(self.btn_open["state"]) == "disabled":
                self.problems.append("有页面可打开时，「打开最后一页」仍是灰的")
            else:
                print("      写入后「打开最后一页」会亮起")
            self.last_page_id = ""
            self._set_busy(False)

        # 步骤 5：进度区 —— 「点了扫描之后不知道后台在干什么」的回归测试
        def _step_progress(self):
            try:
                self._show_page("sync")
                self.update()
                self._set_busy(True)                       # 主线程直调，等价于任务开始
                self._set_stage("核对 OneNote 里的分区与页面")
                self.update()
                # ⚠️ 判「显示出来没有」要用 winfo_manager()，不能用 winfo_ismapped()：
                #    pack() 之后 ismapped 还要等窗口管理器真正完成映射，会时过时不过。
                shown = (bool(self.pbar.winfo_manager()), bool(self.lbl_progress.winfo_manager()))
                if not all(shown):
                    self.problems.append(
                        f"任务进行中，进度条 / 步骤文字没有显示（manager={shown}，"
                        f"_progress_visible={self._progress_visible}）")
                # ⚠️ ttk 控件的 cget() 返回 Tcl_Obj，`== "字面量"` 永远 False，必须 str() 包一层。
                if str(self.pbar.cget("mode")) != "indeterminate":
                    self.problems.append(
                        f"还没拿到篇数时应该转圈，实际 {self.pbar.cget('mode')!r}")
                self._set_progress(3, 9, "新建 9月7日")     # 拿到篇数 → 切走条
                self.update()
                label = str(self.lbl_progress.cget("text"))
                if "3/9" not in label:
                    self.problems.append(f"进度文字里没有「3/9」（实际 {label!r}）")
                if "新建 9月7日" not in label:
                    self.problems.append(f"进度文字里没有当前在写哪一篇（实际 {label!r}）")
                if str(self.pbar.cget("mode")) != "determinate":
                    self.problems.append(
                        f"拿到篇数后应该切回 determinate 走条，实际 {self.pbar.cget('mode')!r}")
                self.after(1200, self._shot_progress)
            except Exception as e:  # noqa: BLE001
                self.problems.append(f"检查进度区时异常：{type(e).__name__}: {e}")
                self._finish()

        def _shot_progress(self):
            try:
                label = str(self.lbl_progress.cget("text"))
                if not re.search(r"\d+s", label):
                    self.problems.append(f"进度文字里没有已用时长（实际 {label!r}）")
                self.update()
                self._neutral_fields()
                self._shot("06-进度条", 0.4)
                self._set_busy(False)
                self.update()
                if self.pbar.winfo_manager() or self.lbl_progress.winfo_manager():
                    self.problems.append(
                        f"任务结束后进度区没有自动收起"
                        f"（manager={self.pbar.winfo_manager()!r}, "
                        f"{self.lbl_progress.winfo_manager()!r}）")
                else:
                    print(f"      进度区：任务中显示、任务后收起（06-进度条.png，{label!r}）")
            except Exception as e:  # noqa: BLE001
                self.problems.append(f"截图进度区时异常：{type(e).__name__}: {e}")
            self.after(250, self._step_scale)

        # 步骤 6：界面缩放 —— 放最后，因为它会把整棵控件树推倒重建
        def _step_scale(self):
            try:
                self._show_page("adv")
                self.update()
                ui_before, font_before = self.ui.scale, self.ui.font_px
                self.var_ui_scale.set("1.15")
                self.var_font_scale.set("1.15")
                self.update()
                self.apply_scale()
                self.update()

                if not self.txt_log.winfo_exists():
                    self.problems.append("缩放重建后日志控件不见了")
                ui_after, font_after = self.ui.scale, self.ui.font_px
                if abs(ui_after - ui_before * 1.15) > 0.02:
                    self.problems.append(f"界面比例没生效：{ui_before} → {ui_after}")
                if abs(font_after - font_before * 1.15) > 0.02:
                    self.problems.append(f"字体比例没生效：{font_before} → {font_after}")
                # 重建不该丢业务状态
                text = self.txt_log.get("1.0", "end")
                if "冒烟测试" not in text:
                    self.problems.append("缩放重建把日志清空了（应该保留下来）")
                if not getattr(self, "photo_page", None) or not self.photo_page.winfo_exists():
                    self.problems.append("缩放重建后照片页没重建出来")
                self._show_page("sync")
                self.update()
                self._neutral_fields()
                self._shot("07-缩放115")
                print(f"      缩放重建：界面 {ui_before:.2f}→{ui_after:.2f}，"
                      f"字体 {font_before:.2f}→{font_after:.2f}，日志保留")

                # 切回标准档，别让下次启动带着测试的缩放
                self._show_page("adv")
                self.update()
                self.var_ui_scale.set("1.0")
                self.var_font_scale.set("1.0")
                self.apply_scale()
                self.update()
                # ⚠️ 别拿 ui.scale 跟 1.0 比 —— 它 = 系统 DPI 系数 × 用户系数，
                #    这台机器 DPI 150%，标准档下 ui.scale 本来就是 1.5。
                if abs(self.ui.ui_scale - 1.0) > 0.001 or abs(self.ui.font_scale - 1.0) > 0.001:
                    self.problems.append(
                        f"切回标准档失败：ui_scale={self.ui.ui_scale}, "
                        f"font_scale={self.ui.font_scale}")
                else:
                    print(f"      已切回标准档（系统 DPI 系数 {self.ui.dpi_scale}，"
                          f"实际 scale={self.ui.scale}）")
            except Exception as e:  # noqa: BLE001
                self.problems.append(f"检查缩放时异常：{type(e).__name__}: {e}")
            self._finish()

        def _finish(self):
            if self._finished:          # 兜底定时器和正常收尾可能都摸到这里
                return
            self._finished = True
            self._check_shots()
            if self.problems:
                for p in self.problems:
                    print(f"[NG] {p}")
            else:
                print("[OK] 界面冒烟通过，截图已更新")
            for name, h in self.shots.items():
                print(f"      {h[:8]}  {name}.png")
            try:
                self.destroy()
            except Exception:
                pass

        # 机械检查：两张截图逐字节相同 = 有一张肯定抓到了上一页
        def _check_shots(self):
            by_hash: dict[str, list[str]] = {}
            for name, h in self.shots.items():
                by_hash.setdefault(h, []).append(name)
            for names in by_hash.values():
                if len(names) > 1:
                    self.problems.append(
                        f"这几张截图内容一模一样，说明抓到了同一个页面：{'、'.join(sorted(names))}")

    app = Smoke()
    app.mainloop()
    shutil.rmtree(_SMOKE_CFG, ignore_errors=True)
    return 1 if Smoke.problems else 0


atexit.register(lambda: shutil.rmtree(_SMOKE_CFG, ignore_errors=True))

if __name__ == "__main__":
    raise SystemExit(main())
