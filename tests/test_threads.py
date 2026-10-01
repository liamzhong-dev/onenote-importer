# -*- coding: utf-8 -*-
"""跨线程回归测试：状态库必须能被「计划线程」和「同步线程」共用。

背景（就是线上崩的那次）：
    点「扫描预览」→ 后台线程 A 里构造 Pipeline（连带建了 SQLite 连接）
    点「开始导入」→ 复用同一个 Pipeline，但跑在后台线程 B 里
    → sqlite3 默认 check_same_thread=True，直接抛
      ProgrammingError: SQLite objects created in a thread can only be used in that same thread

跑法：
    python tests/test_threads.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import Config  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402
from core.proc import force_utf8_console  # noqa: E402
from core.state import PageRecord, StateDB, sha256_text  # noqa: E402

force_utf8_console()


def run_in_thread(fn):
    """把 fn 放到一个新线程里跑，把异常原样带回来。"""
    box: dict = {}

    def target():
        try:
            box["ok"] = fn()
        except BaseException as e:  # noqa: BLE001 — 测试要把任何异常都摊出来
            box["err"] = e
            box["tb"] = traceback.format_exc()

    t = threading.Thread(target=target)
    t.start()
    t.join(timeout=60)
    if "err" in box:
        raise box["err"]
    return box.get("ok")


def check(name: str, fn) -> bool:
    try:
        fn()
    except BaseException as e:  # noqa: BLE001
        print(f"  NG  {name}")
        print(f"      {type(e).__name__}: {e}")
        tb = traceback.format_exc().strip().splitlines()
        for line in tb[-6:]:
            print(f"      {line}")
        return False
    print(f"  OK  {name}")
    return True


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="importer-threads-"))
    failed = []

    # ---- 0. 对照组：默认连接确实不能跨线程用（证明这个测试有效） ----
    def control():
        path = tmp / "control.sqlite3"
        conn = sqlite3.connect(str(path))          # 故意不加 check_same_thread=False
        run_in_thread(lambda: conn.execute("SELECT 1"))

    control_raised = False
    try:
        control()
    except sqlite3.ProgrammingError:
        control_raised = True
    print(f"  {'OK ' if control_raised else 'NG '} 对照组：默认 sqlite 连接跨线程会抛错"
          f"（测试有效性）")
    if not control_raised:
        failed.append("对照组没能复现 ProgrammingError，这个测试就失去意义了")

    # ---- 1. StateDB：A 线程建，B 线程读写 ----
    db_path = tmp / "state.sqlite3"
    db_box: dict = {}

    def build_db():
        db_box["db"] = StateDB(db_path)

    run_in_thread(build_db)
    db: StateDB = db_box["db"]

    def use_in_other_thread():
        assert db.get("nope.md") is None
        db.upsert(PageRecord("a.md", sha256_text("x"), "标题", "2026夏", "2026夏", "日记",
                             "p-1", "com", "2026-09-21", "2026-09-21"))
        rec = db.get("a.md")
        assert rec is not None and rec.page_id == "p-1", rec
        db.record_run({"created": 1, "updated": 0, "skipped": 0, "failed": 0}, "fake", True)
        assert db.last_run() is not None
        db.all()
        db.delete("a.md")

    if not check("StateDB：换个线程照样能读能写", lambda: run_in_thread(use_in_other_thread)):
        failed.append("StateDB 跨线程失败")

    # ---- 2. 多线程并发写同一个 StateDB ----
    def concurrent():
        box: dict = {}

        def worker(n: int):
            def go():
                for i in range(20):
                    db.upsert(PageRecord(f"t{n}-{i}.md", sha256_text(f"{n}{i}"), "t", "s", "s",
                                         "nb", f"p-{n}-{i}", "fake", "x", "x"))
            run_in_thread(go)

        ts = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=60)
        assert len(db.all()) == 80, len(db.all())

    if not check("StateDB：4 个线程并发写不报 database is locked",
                 lambda: run_in_thread(concurrent)):
        failed.append("并发写失败")

    # ---- 3. Pipeline：计划线程里建，同步线程里跑（线上崩的就是这条） ----
    from tests.selftest import FakeWriter, build_sample_vault  # noqa: E402

    vault = build_sample_vault(tmp / "vault")
    cfg = Config()
    cfg.vault_path = str(vault)
    cfg.state_path = str(tmp / "pipeline-state.sqlite3")
    cfg.notebook_name = "日记"
    cfg.section_mode = "fixed"
    cfg.section_fixed = "2026夏"
    cfg.title_template = "{M}月{D}日"
    cfg.dry_run = False

    pipe_box: dict = {}

    def plan_thread():
        pipe = Pipeline(cfg, log=lambda msg, level="INFO": None)
        pipe.writer = FakeWriter({"2026夏": {}})
        pipe_box["pipe"] = pipe
        pipe_box["items"] = pipe.scan()
        pipe.inspect_targets(pipe_box["items"])

    run_in_thread(plan_thread)
    if not check("Pipeline：在计划线程里建好", lambda: None):
        failed.append("构造 Pipeline 失败")

    writer = pipe_box["pipe"].writer
    stats_box: dict = {}

    def sync_thread():
        # 不新建 Pipeline，直接用另一个线程里建好的那个 —— 复现线上场景
        stats_box["stats"] = pipe_box["pipe"].run(pipe_box["items"], dry_run=False)

    if not check("Pipeline：换到同步线程里跑完（含最后 record_run 记账）",
                 lambda: run_in_thread(sync_thread)):
        failed.append("Pipeline 跨线程 run 失败")

    stats = stats_box.get("stats", {})
    if stats:
        print(f"      同步结果：新建 {stats.get('created')} / 更新 {stats.get('updated')} / "
              f"失败 {stats.get('failed')}")
        if stats.get("failed"):
            failed.append(f"同步过程中有 {stats['failed']} 篇失败")
        if stats.get("created") != len(pipe_box["items"]):
            failed.append("新建数量与计划不符")

    # ---- 4. 预览模式也要能跨线程收尾（这次崩的就是 preview 路径） ----
    cfg2 = Config()
    cfg2.vault_path = str(vault)
    cfg2.state_path = str(tmp / "pipeline-state2.sqlite3")
    cfg2.notebook_name = "日记"
    cfg2.dry_run = True
    box2: dict = {}

    def plan2():
        p = Pipeline(cfg2, log=lambda msg, level="INFO": None)
        box2["p"] = p
        box2["items"] = p.scan()

    run_in_thread(plan2)
    if not check("Pipeline：预览模式下跨线程跑完（原崩溃路径）",
                 lambda: run_in_thread(lambda: box2["p"].run(box2["items"], dry_run=True))):
        failed.append("预览模式跨线程失败")

    print()
    if failed:
        print("未通过：")
        for f in failed:
            print(f"  · {f}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
