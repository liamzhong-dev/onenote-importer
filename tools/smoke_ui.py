# -*- coding: utf-8 -*-
"""界面冒烟测试：真的把窗口建出来跑几帧，确认控件不会炸，并留一张截图。

跑完自动关窗，不需要人工介入：
    python tools/smoke_ui.py [输出目录]

会检查：
  · 三个页面都能切过去
  · 计划树能填进数据
  · 「分区方式」「重名页面」两个分段控件能改值
  · 日志区能写入
"""
from __future__ import annotations

import ctypes
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.pipeline import PlanItem  # noqa: E402
from tools.screenshot import capture  # noqa: E402
from ui.theme import setup_dpi_awareness  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "sample-output"
OUT.mkdir(parents=True, exist_ok=True)

user32 = ctypes.windll.user32


def top_level_hwnd(tk_win) -> int:
    """tkinter 的 winfo_id() 拿到的是客户区句柄，上层还要再取一次父窗口。"""
    hwnd = tk_win.winfo_id()
    parent = user32.GetParent(hwnd)
    return parent or hwnd


def main() -> int:
    setup_dpi_awareness()
    from app import SyncApp

    class Smoke(SyncApp):
        problems: list[str] = []

        def __init__(self):
            super().__init__()
            self.repro_thread: threading.Thread | None = None
            self._settle = 0
            self.after(400, self._step_widgets)
            self.after(1600, self._step_pages)
            self.after(3200, self._step_sync_repro)

        # 步骤 1：动一遍新加的控件
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
            except Exception as e:  # noqa: BLE001 — 冒烟测试要把所有炸点摊开
                self.problems.append(f"操作分区控件时异常：{type(e).__name__}: {e}")

        # 步骤 2：切页 + 填计划树 + 写日志 + 截图
        def _step_pages(self):
            try:
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
                capture(top_level_hwnd(self), OUT / "01-导入页.png")
            except Exception as e:  # noqa: BLE001
                self.problems.append(f"填充计划树时异常：{type(e).__name__}: {e}")

            # 顺序跟侧边栏一致，文件名也就跟着排下来
            for key, name in (("auth", "02-账户与通道页"), ("adv", "03-高级设置页")):
                try:
                    self._show_page(key)
                    self.update()
                    capture(top_level_hwnd(self), OUT / f"{name}.png")
                except Exception as e:  # noqa: BLE001
                    self.problems.append(f"切换到{name}时异常：{type(e).__name__}: {e}")

        # 步骤 3：复现并守住「计划线程建库、同步线程复用」那条崩溃路径。
        # 预览模式不碰 OneNote，但会走到最后一步 record_run 记账 —— 正是原来炸的地方。
        def _step_sync_repro(self):
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
            #    不settle 就直接判，会偶发地误报「没写出未写入提示」——
            #    之前就因为这个偶发飘红，白白怀疑了半天业务代码。
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
            # 预览模式跑完必须说清「没写入」——上次含糊的「完成：新建 9」
            # 让人以为已经导进去了，白找了半天。
            if "预览完成（未写入 OneNote）" not in text:
                self.problems.append(
                    f"预览跑完没有明确写出「未写入 OneNote」（日志尾部：{text[-160:]!r}）")
            elif "新建 2" not in text:
                self.problems.append(f"同步链路没有跑完（日志里看不到「新建 2」；尾部：{text[-160:]!r}）")
            else:
                print("      GUI 同步链路复现通过（跨线程记账不再报错）")
                self._check_open_button()
            # 最后再截一张「任务进行中」的图（进度条只在这时候才出现）
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

        # 步骤 4：进度区。这一节是「点了扫描/刷新之后不知道后台在干什么」的回归测试：
        # 任务期间状态栏必须出现进度条 + 步骤文字 + 已用时长，任务结束必须自动收起。
        def _step_progress(self):
            try:
                self._show_page("sync")
                self.update()
                self._set_busy(True)                       # 主线程直调，等价于任务开始
                self._set_stage("核对 OneNote 里的分区与页面")
                self.update()
                # ⚠️ 判「显示出来没有」要用 winfo_manager()，不能用 winfo_ismapped()：
                #    pack() 之后 ismapped 还要等窗口管理器真正完成映射，同一份代码
                #    会时过时不过（实测飘过一次）。winfo_manager() 是「有没有被布局管理器
                #    接管」，pack → "pack"、pack_forget → ""，确定性的。
                shown = (bool(self.pbar.winfo_manager()), bool(self.lbl_progress.winfo_manager()))
                if not all(shown):
                    self.problems.append(
                        f"任务进行中，进度条 / 步骤文字没有显示（manager={shown}，"
                        f"_progress_visible={self._progress_visible}）")
                # ⚠️ ttk 控件的 cget() 返回 _tkinter.Tcl_Obj，`== "字面量"` **永远是 False**，
                #    必须 str() 包一层。第一次就是漏了这个，明明行为是对的却报了两个 NG。
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
                # 让「已用时长」跑几秒再截图：截图里能看到秒数在走，才是真的活着的进度
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
                capture(top_level_hwnd(self), OUT / "04-进度条.png")
                self._set_busy(False)
                self.update()
                if self.pbar.winfo_manager() or self.lbl_progress.winfo_manager():
                    self.problems.append(
                        f"任务结束后进度区没有自动收起"
                        f"（manager={self.pbar.winfo_manager()!r}, "
                        f"{self.lbl_progress.winfo_manager()!r}）")
                else:
                    print(f"      进度区：任务中显示、任务后收起（截图 04-进度条.png，"
                          f"进度文字 {label!r}）")
            except Exception as e:  # noqa: BLE001
                self.problems.append(f"截图进度区时异常：{type(e).__name__}: {e}")
            self._finish()

        def _finish(self):
            if self.problems:
                for p in self.problems:
                    print(f"[NG] {p}")
            else:
                print("[OK] 界面冒烟通过，截图已更新")
            self.destroy()

    app = Smoke()
    app.mainloop()
    return 1 if Smoke.problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
