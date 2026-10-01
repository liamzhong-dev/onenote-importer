# -*- coding: utf-8 -*-
"""界面偏好（缩放档位）的读写。

存在 `%APPDATA%\\OneNoteDiaryImporter\\ui.json`，**故意和 config.json 分开**：
它是「这台机器上看着舒服」的显示设置，跟导入业务无关 ——
在高级设置里点「恢复默认值」时不该把缩放一起抹掉。
"""
from __future__ import annotations

import json
from pathlib import Path

from core.config import appdata_dir

DEFAULT_UI_SCALE = 1.0
DEFAULT_FONT_SCALE = 1.0

# 界面给出的档位（值 → 显示名），两边共用，别在 GUI 里另写一份
UI_SCALE_CHOICES = [("0.85", "紧凑"), ("1.0", "标准"), ("1.15", "大"), ("1.3", "特大")]
FONT_SCALE_CHOICES = [("0.9", "小"), ("1.0", "标准"), ("1.15", "大"), ("1.3", "特大")]


def prefs_path() -> Path:
    return appdata_dir() / "ui.json"


def load_ui_prefs() -> tuple[float, float]:
    """返回 (界面比例, 字体比例)。文件缺失或损坏都退回 1.0，不抛异常。"""
    try:
        raw = json.loads(prefs_path().read_text(encoding="utf-8"))
        return float(raw.get("ui_scale", DEFAULT_UI_SCALE)), float(raw.get("font_scale", DEFAULT_FONT_SCALE))
    except Exception:
        return DEFAULT_UI_SCALE, DEFAULT_FONT_SCALE


def save_ui_prefs(ui_scale: float, font_scale: float) -> Path:
    p = prefs_path()
    p.write_text(json.dumps(
        {"ui_scale": round(float(ui_scale), 2), "font_scale": round(float(font_scale), 2)},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def nearest_choice(value: float, choices: list[tuple[str, str]]) -> str:
    """把存下来的数值对齐到最接近的档位（避免 Segmented 显示空白）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = 1.0
    return min(choices, key=lambda kv: abs(float(kv[0]) - v))[0]
