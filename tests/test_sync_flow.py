# -*- coding: utf-8 -*-
"""不用 OneNote 也能跑的行为验证：幂等判定、预览执行、通道降级探测。

    python tests/test_sync_flow.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import Config  # noqa: E402
from core.pipeline import CREATE, SKIP, UPDATE, Pipeline  # noqa: E402
from core.proc import force_utf8_console  # noqa: E402
from core.state import PageRecord, sha256_text  # noqa: E402
from writers.factory import probe_all  # noqa: E402

force_utf8_console()
from tests.selftest import build_sample_vault  # noqa: E402


def make_cfg(tmp: Path) -> Config:
    cfg = Config()
    cfg.vault_path = str(tmp / "vault")
    cfg.state_path = str(tmp / "flow.sqlite3")
    cfg.notebook_name = "日记"
    cfg.update_existing = True
    cfg.dry_run = True
    return cfg


def main() -> int:
    tmp = Path(tempfile.gettempdir()) / "onenote-importer-flow"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    cfg = make_cfg(tmp)
    vault = build_sample_vault(tmp / "vault")

    logs: list[tuple[str, str]] = []
    # Pipeline 内部的日志约定是 (msg, level)，别写成 (level, msg)
    pipe = Pipeline(cfg, log=lambda msg, level="INFO": logs.append((msg, level)))

    # 1) 全新库 → 全部新建
    plan = pipe.scan()
    assert len(plan) == 2, plan
    assert all(p.action == CREATE for p in plan)
    print("1 首次扫描：2 篇全部新建  OK")

    # 2) 假装已经同步过（写入同 hash 的记录）→ 应全部跳过
    for nf in pipe.index.notes:
        pipe.state.upsert(PageRecord(
            rel_path=nf.rel, sha256=sha256_text(nf.text), title=nf.rel,
            section="2026-09", section_key="2026-09", notebook="日记",
            page_id="{fake-id}{1}", writer="graph",
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"), updated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        ))
    plan = pipe.scan()
    assert all(p.action == SKIP for p in plan), [(p.rel, p.action) for p in plan]
    assert all(p.page_id for p in plan)
    print("2 内容未变：全部跳过且保留 page_id  OK")

    # 3) 改一个文件的正文 → 那一篇变成 update，另一篇保持 skip
    target = vault / "日记" / "2026-09-07.md"
    target.write_text(target.read_text(encoding="utf-8") + "\n\n补一句：今天有点想家。\n", encoding="utf-8")
    plan = pipe.scan()
    actions = {p.rel: p.action for p in plan}
    assert actions["日记/2026-09-07.md"] == UPDATE, actions
    assert actions["日记/2026/2026-09-08.md"] == SKIP, actions
    print("3 单篇改动：仅该篇进入更新队列  OK")

    # 4) 预览模式执行：不建立任何 writer 连接，即使两个通道都不可用
    stats = pipe.run(plan, dry_run=True)
    assert stats["created"] + stats["updated"] == 1 and stats["skipped"] == 1, stats
    assert stats["failed"] == 0, stats
    assert pipe.writer is None, "预览模式不应建立连接"
    print(f"4 预览执行：新建 {stats['created']} / 更新 {stats['updated']} / 跳过 {stats['skipped']} / 失败 {stats['failed']}  OK")

    # 5) 通道探测：没有任何凭据时应当优雅降级（不抛异常）
    cfg.graph_client_id = ""
    results = probe_all(cfg, log=pipe._logfn())
    assert len(results) == 2, results
    for kind, res in results:
        print(f"   通道 {kind}: {'可用' if res.available else '不可用'} — {res.reason}")
    # graph 一定不可用（没配 client id）；com 取决于本机有没有装桌面版 OneNote，两种都算通过
    print("5 通道探测正常返回且不崩溃  OK")

    # 6) 失败记账：模拟作家抛错
    pipe2 = Pipeline(make_cfg(tmp), log=lambda msg, level="INFO": None)
    pipe2.cfg.com_enabled = False  # 关掉本机唯一可用的通道，模拟「都没得用」
    items2 = pipe2.scan()
    try:
        pipe2.run(items2, dry_run=False)
    except RuntimeError as e:
        assert "没有可用的写入通道" in str(e), str(e)
        print("6 无通道时同步立即失败并给出原因  OK")
    else:
        print("6 NG：无通道竟然没报错")
        return 1

    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
