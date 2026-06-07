# -*- coding: utf-8 -*-
"""命令行入口，方便挂定时任务或批处理。

    python cli.py plan                 # 只预览计划，不写 OneNote
    python cli.py sync --dry           # 同上（明确走预览）
    python cli.py sync                 # 真正写入
    python cli.py selftest             # 运行渲染自检
    python cli.py probe                # 检查两个通道哪个可用
    python cli.py login                # Graph 设备码登录
"""
from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from core.config import Config  # noqa: E402
from core.pipeline import Pipeline, StopSignal  # noqa: E402
from writers.auth import AuthError, DeviceCodeAuth, TokenStore  # noqa: E402
from writers.factory import probe_all  # noqa: E402


def color(level: str) -> str:
    return {"OK": "✓", "INFO": "·", "WARN": "!", "ERROR": "✗", "DEBUG": " "}.get(level, "·")


def make_logger(verbose: bool):
    def log(level: str, msg: str):
        if level == "DEBUG" and not verbose:
            return
        stream = sys.stderr if level in ("ERROR", "WARN") else sys.stdout
        print(f"  {color(level)} {msg}", file=stream)
    return log


def pipe_logger(verbose: bool):
    """Pipeline 内部用的是 (msg, level)，跟 writer 的 (level, msg) 正好相反。

    直接把 make_logger 递进去会让两边错位——日志变成一行行「· INFO」。
    """
    base = make_logger(verbose)
    return lambda msg, level="INFO": base(level, msg)


def cmd_plan(cfg: Config, args) -> int:
    pipe = Pipeline(cfg, log=pipe_logger(args.verbose))
    items = pipe.scan()
    counts: dict[str, int] = {}
    for it in items:
        counts[it.action] = counts.get(it.action, 0) + 1
        if args.limit is None or it.action in ("create", "update"):
            print(f"{it.action:7s} | {it.section:12s} | {it.title}")
    print(f"\n合计 {len(items)} 篇：" + "、".join(f"{k}={v}" for k, v in counts.items()))
    pipe.state.close()
    return 0


def cmd_sync(cfg: Config, args) -> int:
    dry = args.dry if hasattr(args, "dry") else True
    if args.apply:
        dry = False
    pipe = Pipeline(cfg, log=pipe_logger(args.verbose))
    items = pipe.scan()
    stop = threading.Event()
    try:
        stats = pipe.run(items, dry_run=dry, stop=stop)
    except StopSignal:
        print("已取消")
        return 130
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    print(f"\n新建 {stats['created']} / 更新 {stats['updated']} / 跳过 {stats['skipped']} / 失败 {stats['failed']}")
    if stats.get("dry"):
        print("（预览模式，OneNote 没有任何改动；要真正写入请加 --apply）")
    for rel, err in stats.get("failures", []):
        print(f"  ✗ {rel}: {err}", file=sys.stderr)
    pipe.state.close()
    return 1 if stats["failed"] else 0


def cmd_probe(cfg: Config, args) -> int:
    for kind, res in probe_all(cfg, log=make_logger(args.verbose)):
        flag = "可用" if res.available else "不可用"
        print(f"[{kind}] {flag} — {res.reason}")
    return 0


def cmd_login(cfg: Config, args) -> int:
    if args.client_id:
        cfg.graph_client_id = args.client_id
    cfg.save()
    auth = DeviceCodeAuth(cfg.graph_client_id, cfg.graph_authority)
    try:
        session = auth.start()
    except AuthError as e:
        print(f"无法开始登录：{e}", file=sys.stderr)
        return 2
    print(f"\n请用浏览器打开：{session.verification_uri}")
    print(f"输入验证码：{session.user_code}\n")
    store = TokenStore()
    while not session.expired:
        import time
        time.sleep(max(3, session.interval))
        try:
            token = auth.poll_once(session)
        except AuthError as e:
            print(f"登录失败：{e}", file=sys.stderr)
            return 1
        if token:
            store.update_from_token(token, cfg.graph_client_id)
            print("✓ 登录完成，token 已保存，之后会自动续期。")
            return 0
    print("验证码已过期，请重试。", file=sys.stderr)
    return 1


def cmd_selftest(cfg: Config, args) -> int:
    import subprocess
    from core.proc import child_env, silent_kwargs
    return subprocess.call([sys.executable, str(HERE / "tests" / "selftest.py")],
                           env=child_env(), **silent_kwargs())


def main() -> int:
    ap = argparse.ArgumentParser(description="OneNote 日记导入工具：把 Markdown 日记批量写入 OneNote")
    ap.add_argument("cmd", choices=("plan", "sync", "probe", "login", "selftest"))
    ap.add_argument("--apply", action="store_true", help="真正写入 OneNote（sync 默认只预览）")
    ap.add_argument("--dry", action="store_true", help="显式走预览模式")
    ap.add_argument("--vault", help="覆盖配置里的 Vault 目录")
    ap.add_argument("--notebook", help="覆盖配置里的目标笔记本")
    ap.add_argument("--client-id", help="Azure 应用客户端 ID")
    ap.add_argument("--limit", action="store_true", help="plan 时只列出需要写入的条目")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    cfg = Config.load()
    if args.vault:
        cfg.vault_path = args.vault
    if args.notebook:
        cfg.notebook_name = args.notebook
    if args.client_id:
        cfg.graph_client_id = args.client_id

    if args.cmd in ("plan", "sync", "probe"):
        errs = cfg.validate()
        if args.cmd != "probe" and errs:
            for e in errs:
                print(f"  ! {e}", file=sys.stderr)
            print("配置不完整：请先用 python app.py 打开图形界面设置，或传入 --vault 等参数。", file=sys.stderr)
            return 2
        if args.cmd == "sync":
            cfg.save()

    handler = {"plan": cmd_plan, "sync": cmd_sync, "probe": cmd_probe,
               "login": cmd_login, "selftest": cmd_selftest}[args.cmd]
    return handler(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
