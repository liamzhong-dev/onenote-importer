# -*- coding: utf-8 -*-
"""启动前自检之一：找出「self.x 被取用过，却从没赋值」的情况。

app.py 里有几个变量是靠 `_entry_row(..., attr="var_xxx")` 这种字符串间接创建的，
一旦忘了建对应的 UI 控件，`_load_cfg_to_ui` 一上来就 AttributeError，
而这类问题往往要点开某个页面才暴露。所以静态扫一遍最省事。

    python tests/attrcheck.py

判定规则：
    self.x 的取值 减去
      ① 类方法 / 模块函数名
      ② 本文件里出现的 `setattr(self, "x", ...)` 字面量
      ③ 祖先类（tk.Tk / Frame / Canvas / Text / Button / Label / BooleanVar …）自带的成员
    = 可疑项
"""
from __future__ import annotations

import ast
import sys
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.proc import force_utf8_console  # noqa: E402

# 中文 Windows 上 stdout 默认 GBK，输出里混进一个替换字符就 UnicodeEncodeError
force_utf8_console()

# 可能作为 self 祖先出现的类型 —— 够用就好，别为了零漏报把整个 tkinter 都塞进来
ANCESTORS = (tk.Tk, tk.Frame, tk.Canvas, tk.Text, tk.Button, tk.Label, tk.Toplevel, object)


def _params_of(func: ast.FunctionDef) -> list[str]:
    names: list[str] = []
    for arg in func.args.posonlyargs + func.args.args:
        names.append(arg.arg)
    return names


def _collect(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    defined: set[str] = set()
    setattr_names: set[str] = set()
    assigned: set[str] = set()
    used: dict[str, int] = {}
    # 函数名 → 形参名列表，用来把「位置参数」还原成参数名
    sigs: dict[str, list[str]] = {}

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
            sigs[node.name] = _params_of(node)
        # 类体里的类属性（如 Badge.VARIANTS），运行时通过 self 也能取到
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                for tgt in getattr(stmt, "targets", []):
                    if isinstance(tgt, ast.Name):
                        defined.add(tgt.id)

    def _maybe_var(want_args: list[str], value: ast.expr, index: int = -1):
        """形参名叫 attr/attr_name 且实参是字符串常量时，认为它间接创建了一个变量。"""
        if index >= 0 and index < len(want_args) and want_args[index] in ("attr", "attr_name"):
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                setattr_names.add(value.value)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "setattr":
            args = node.args
            if len(args) >= 2 and isinstance(args[1], ast.Constant) and isinstance(args[1].value, str):
                setattr_names.add(args[1].value)
        if not isinstance(node, ast.Call):
            continue
        fname = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        params = sigs.get(fname)
        if not params:
            continue
        # self.xxx(a, b, ...) —— 第一个实参对应 params[1]
        params_excl_self = params[1:]
        for i, arg in enumerate(node.args):
            _maybe_var(params_excl_self, arg, i)
        for kw in node.keywords:
            if kw.arg in params_excl_self and isinstance(kw.value, ast.Constant) \
                    and isinstance(kw.value.value, str):
                if kw.arg in ("attr", "attr_name"):
                    setattr_names.add(kw.value.value)

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                assigned.add(node.attr)
            else:
                used[node.attr] = used.get(node.attr, 0) + 1

    return defined, setattr_names, assigned, used


def check_messagebox_levels() -> list[str]:
    """拦一类特别阴的 bug：往队列里塞的是「级别」，tkinter 要的是「函数名」。

    `getattr(messagebox, "error")` 会抛 AttributeError，而且是在 Tkinter 回调里抛的，
    界面不崩、只在 stderr 里留一行 —— 表现就是「日志里有报错，但一个弹窗都没有」。
    """
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # MSGBOX_FN = {"error": "showerror", ...}
    allowed: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "MSGBOX_FN" for t in node.targets):
            if isinstance(node.value, ast.Dict):
                allowed = {k.value for k in node.value.keys
                           if isinstance(k, ast.Constant) and isinstance(k.value, str)}

    used: set[str] = set()
    for node in ast.walk(tree):
        # self.q.put(("msg", (level, ...)))
        if not isinstance(node, ast.Call):
            continue
        args = node.args
        if not args or not isinstance(args[0], ast.Tuple) or len(args[0].elts) < 2:
            continue
        head, payload = args[0].elts[0], args[0].elts[1]
        if not (isinstance(head, ast.Constant) and head.value == "msg"):
            continue
        if isinstance(payload, ast.Tuple) and payload.elts:
            lv = payload.elts[0]
            if isinstance(lv, ast.Constant) and isinstance(lv.value, str):
                used.add(lv.value)

    bad = sorted(used - allowed)
    print(f"[{'NG' if bad else 'OK'}]   app.py 弹窗级别 → MSGBOX_FN"
          f"（用了 {sorted(used) or '无'}；已注册 {sorted(allowed) or '无'}）")
    return [f'弹窗级别 "{b}" 不在 MSGBOX_FN 里，弹窗会静默失败' for b in bad] if bad else []


def main() -> int:
    inherited = {n for cls in ANCESTORS for n in dir(cls)}
    bad = False
    for target in sorted(ROOT.glob("*.py")) + sorted((ROOT / "ui").glob("*.py")):
        defined, setattr_names, assigned, used = _collect(target)
        known = defined | setattr_names | assigned | inherited
        miss = sorted(n for n in used if n not in known)
        rel = target.relative_to(ROOT)
        if miss:
            bad = True
            print(f"[可疑] {rel}: {', '.join(miss)}")
        else:
            print(f"[OK]   {rel}")

    problems = check_messagebox_levels()
    for p in problems:
        print(f"[可疑] {p}")
    if problems:
        bad = True
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
