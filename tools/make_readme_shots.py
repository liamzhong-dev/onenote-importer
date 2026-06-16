# -*- coding: utf-8 -*-
"""生成 README 用的界面截图（全部是演示数据）。

用法（在项目根目录）：
    python tools/make_readme_shots.py [输出目录]

和 tools/smoke_ui.py 的分工：那个是冒烟测试，日记目录用的是本机临时路径，
图只留在 sample-output 里自用；这个脚本是给 README 公开用的，
界面上的路径、分区名、通道状态一律写死成演示值，
所以跑出来的图不会带出本机的用户名和目录结构。

不会读 %APPDATA% 下的真实配置，也不会碰 OneNote。
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.pipeline import PlanItem          # noqa: E402
from tools.screenshot import capture        # noqa: E402
from ui.theme import setup_dpi_awareness    # noqa: E402
from writers.base import ProbeResult        # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "doc" / "shots"
OUT.mkdir(parents=True, exist_ok=True)

DEMO_VAULT = r"D:\Notes\Diary"
DEMO_NOTEBOOK = "日记"

# 演示用的计划表：只放 3 条 —— 窗口高度被屏幕卡着（1320x870 再大就超出可视区），
# 计划树和日志各占 9 行，实际能露出来的只有表格的前三行多。
# 多放一条就会有半行挂在裁切边缘，看着像界面坏了，所以这里保持 3 条。
PLAN = [
    PlanItem("2026-06-22.md", Path("x"), "update", "6月22日", "2026-06",
             section_exists=True, matched_page_id="demo-p1"),
    PlanItem("2026-09-07.md", Path("x"), "create", "9月7日", "2026-09",
             section_exists=False),
    PlanItem("2026-09-09.md", Path("x"), "skip", "9月9日", "2026-09",
             section_exists=True, matched_page_id="demo-p2"),
]

LOGS = [
    ("扫描完成：3 篇待处理", "INFO"),
    ("分区「2026-06」已存在，直接写入", "INFO"),
    ("2026-06-22 在 OneNote 里已有同名页面，就地更新", "WARN"),
    ("预览模式：只生成计划，不写 OneNote", "INFO"),
]


def root_hwnd(win) -> int:
    """把 Tk 的客户区句柄提升到带标题栏的顶层窗口。"""
    hwnd = win.winfo_id()
    return ctypes.windll.user32.GetAncestor(wintypes.HWND(hwnd), 2) or hwnd


class _ShotApp:
    """外壳，避免在导入期就把 app 拉起来（app 需要 tkinter）。"""

    @staticmethod
    def build():
        from app import SyncApp

        class Shot(SyncApp):
            def __init__(self):
                super().__init__()
                self.after(2000, self._step_import)
                self.after(4200, self._step_sync)
                self.after(7000, self._step_adv)

            # ---- 演示态：每次截图前都重刷一遍，
            #      防止启动时的真实通道探测结果盖掉演示值 ----
            def _paint(self):
                self.var_vault.set(DEMO_VAULT)
                self.var_notebook.set(DEMO_NOTEBOOK)
                self.var_section_mode.set("template")
                self.var_section.set("{YYYY}-{MM}")
                self.var_page_policy.set("update")
                self.var_dry.set(True)
                self._fill_tree(PLAN)
                self._fill_probe([
                    ("graph", ProbeResult(False, "未填写 Azure 应用客户端 ID")),
                    ("com", ProbeResult(True, "桌面版 OneNote 已就绪")),
                ])
                self._set_dot("ok")
                self.update_idletasks()
                self.update()

            def _logs(self):
                for msg, level in LOGS:
                    self.log(msg, level)

            def _shot(self, name: str):
                self._paint()
                self.update()
                time.sleep(0.15)
                w, h = capture(root_hwnd(self), OUT / name)
                print(f"  {name}  {w}x{h}")

            # ---- 1. 导入页 ----
            def _step_import(self):
                try:
                    self._logs()
                    self._paint()
                    self.after(500, lambda: self._shot("01-import.png"))
                except Exception as e:  # noqa: BLE001
                    print(f"[NG] 导入页：{type(e).__name__}: {e}")
                    self.destroy()

            # ---- 2. 同步进行中（这条才看得到进度区）----
            def _step_sync(self):
                try:
                    self._show_page("sync")
                    self._paint()
                    self._set_busy(True)
                    self._set_stage("核对 OneNote 里的分区与页面")
                    self._set_progress(2, 3, "新建 9月7日")
                    self.update()
                    # 让「已用时长」走几秒，截图里的秒数才是活的
                    self.after(1300, lambda: self._shot("02-syncing.png"))
                    self.after(1600, lambda: self._set_busy(False))
                except Exception as e:  # noqa: BLE001
                    print(f"[NG] 同步中：{type(e).__name__}: {e}")
                    self.destroy()

            # ---- 3. 高级设置页 ----
            def _step_adv(self):
                try:
                    self._show_page("adv")
                    self._shot("03-advanced.png")
                except Exception as e:  # noqa: BLE001
                    print(f"[NG] 高级设置页：{type(e).__name__}: {e}")
                self.after(300, self._finish)

            def _finish(self):
                self.destroy()
                # ⚠️ 启动时那次通道探测跑在自己的线程上，它不结束的话
                #    解释器收尾会一直等，命令行看着就像卡死了。截图脚本没有
                #    需要优雅落盘的东西，直接退。
                os._exit(0)

        return Shot()


def main() -> int:
    setup_dpi_awareness()
    print(f"输出目录：{OUT}")
    app = _ShotApp.build()
    app.mainloop()
    print("完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
