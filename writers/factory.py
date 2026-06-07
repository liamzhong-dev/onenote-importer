# -*- coding: utf-8 -*-
"""通道选择与自动降级。"""
from __future__ import annotations

from core.config import Config
from .base import LogFn, OneNoteWriter, ProbeResult, StepFn

_ORDER = {"auto": ["graph", "com"], "graph": ["graph", "com"], "com": ["com", "graph"]}


def make_writer(kind: str, cfg: Config, log: LogFn | None = None,
                step: StepFn | None = None) -> OneNoteWriter:
    if kind == "graph":
        from .graph import GraphWriter
        return GraphWriter(cfg, log, step=step)
    if kind == "com":
        from .com import ComWriter
        return ComWriter(cfg, log, step=step)
    raise ValueError(f"未知通道：{kind}")


def probe_all(cfg: Config, log: LogFn | None = None,
              step: StepFn | None = None) -> list[tuple[str, ProbeResult]]:
    results = []
    for kind in _ORDER.get(cfg.preferred_writer, ["graph", "com"]):
        try:
            w = make_writer(kind, cfg, log, step=step)
            res = w.probe()
        except Exception as e:  # 任何异常都视为该通道不可用
            res = ProbeResult(False, f"{type(e).__name__}: {e}")
        results.append((kind, res))
    return results


def build_writer(cfg: Config, log: LogFn | None = None,
                 step: StepFn | None = None) -> tuple[OneNoteWriter, list[tuple[str, ProbeResult]]]:
    """按优先级探测，返回第一个可用的 writer；全部不可用则抛错并附带原因。"""
    probes = probe_all(cfg, log, step=step)
    for kind, res in probes:
        if res.available:
            return make_writer(kind, cfg, log, step=step), probes
    reasons = "；".join(f"{k}: {r.reason}" for k, r in probes)
    raise RuntimeError(f"没有可用的写入通道 — {reasons}")
