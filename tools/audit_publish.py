#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""发布前体检：AI 生成痕迹 + 隐私信息 + 待发布文件清单。

发版之前跑一遍，确认推上去的东西里既没有"这是 AI 写的"这类痕迹，
也没有作者本人的真实信息（姓名 / 邮箱 / 本机路径 / 真实笔记本名 / 日记内容）。

用法：
    python tools/audit_publish.py               # 全量扫描，打印报告
    python tools/audit_publish.py --show-clean  # 连"没有命中"的类别也列出来

退出码：0 = 没有命中；1 = 有命中（CI / 打包脚本可据此卡住发布）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---- 扫描范围：这些目录不进发布包，也就不扫 ----
SKIP_DIRS = {
    ".git", "__pycache__", "build", "dist", "sample-output",
    "photos", ".venv-pack", ".venv", ".pytest_cache", ".mypy_cache",
}
SKIP_DIR_PREFIX = (".stale-",)
SKIP_FILE_SUFFIX = (".pyc", ".pyo", ".ico", ".png", ".jpg", ".jpeg", ".gif",
                    ".zip", ".exe", ".sqlite3", ".log", ".xml")
SKIP_FILE_NAMES = {"audit_publish.py"}          # 本文件自己就含这些关键词

# ---- AI 生成痕迹 ----
# 说明：只在你**不想让人看出**用了 AI 时才需要清。工具遵循「不辩解」原则，
# 命中就列出来，是否处理由人判断。
AI_TRACES: list[tuple[str, str]] = [
    (r"co-authored-by",                    "提交/注释里的 AI 联合作者署名"),
    (r"generated\s+(?:with|by)",           "「Generated with ...」生成声明"),
    (r"\bclaude\b",                        "Claude"),
    (r"\banthropic\b",                     "Anthropic"),
    (r"\bchatgpt\b",                       "ChatGPT"),
    (r"\bopenai\b",                        "OpenAI"),
    (r"\bcopilot\b",                       "GitHub Copilot"),
    # 注意别写成裸 \bcursor\b —— tkinter 的 cursor="hand2" 会大面积误报
    (r"cursor\.sh|\bcursor\s+(?:ai|ide|editor)\b|by\s+cursor", "Cursor"),
    (r"\bwindsurf\b",                      "Windsurf"),
    (r"\bcodex\b",                         "Codex"),
    (r"\bllm\b",                           "LLM"),
    (r"\bgpt-?[0-9]",                      "GPT 模型名"),
    (r"人工智能|大模型|语言模型|机器学习生成", "中文 AI 措辞"),
    (r"由\s*AI\s*(?:生成|编写|撰写)",        "「由 AI 生成」"),
    (r"I\s+(?:cannot|can't)\s+(?:assist|help)", "模型拒答措辞残留"),
    (r"作为(?:一个)?\s*(?:AI|人工智能)",      "「作为一个 AI」"),
    (r"希望(?:这些|以上)\s*(?:内容|信息)",    "助手式收尾"),
    (r"🤖|✨|🚀|💡|🎉|📝|🔧",                "AI 常用 emoji"),
]

# ---- 隐私信息 ----
PRIVACY: list[tuple[str, str]] = [
    (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "邮箱地址（github noreply 除外）"),
    # 负向断言：`C:/Users/<用户名>` / `{user}` 这类占位写法是写给人看的，不算泄露
    (r"[Cc]:[\\/]{1,2}Users[\\/](?![<{（])", "Windows 本机绝对路径"),
    (r"/c/Users/(?![<{（])",               "Git Bash 本机绝对路径"),
    (r"/Users/(?![<{（])[A-Za-z]",          "macOS 本机绝对路径"),
    (r"\bhollysys\b",                      "公司/机器账户标识"),
    (r"\b12552\b|\b12555\b",               "本机用户名数字串"),
    (r"\bDiary\s*\d+\b",                   "真实 OneNote 笔记本名"),
    (r"\b(?:192\.168|10\.\d{1,3})\.\d{1,3}\.\d{1,3}\b", "内网 IP"),
    (r"\.corp\b|\.local\b|gitlab\.",       "内网域名 / 私有 GitLab"),
    (r"和利时|卡优倍",                       "公司名"),
]

# ---- 提示项：命中不等于有问题，只提醒人工确认一眼，**不进退出码** ----
NOTICES: list[tuple[str, str]] = [
    (r"20\d{2}\s*年?\s*[春夏秋冬]",
     "OneNote 分区名 —— 几乎都是模板示例（`{YYYY}{season}` 算出来的产物），"
     "但它确实和你自己的命名习惯重合，扫到就瞄一眼"),
]

# 命中这个邮箱不算隐私（它本来就是给公众看的）
EMAIL_ALLOW = re.compile(r"@users\.noreply\.github\.com$", re.I)


def iter_files():
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        parts = rel.parts
        if any(d in SKIP_DIRS or d.startswith(SKIP_DIR_PREFIX) for d in parts[:-1]):
            continue
        if p.name in SKIP_FILE_NAMES:
            continue
        if p.suffix.lower() in SKIP_FILE_SUFFIX:
            continue
        yield p, rel.as_posix()


def read_text(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None


def scan(rules, show_clean: bool):
    hits: dict[str, list[str]] = {}
    for path, rel in iter_files():
        text = read_text(path)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for pattern, label in rules:
                m = re.search(pattern, line, re.I)
                if not m:
                    continue
                frag = m.group(0)
                # 白名单：给公众看的邮箱
                if "邮箱" in label and EMAIL_ALLOW.search(frag):
                    continue
                hits.setdefault(label, []).append(
                    f"    {rel}:{lineno}  «{frag}»  {line.strip()[:88]}"
                )
    return hits


def scan_git_history() -> tuple[int, list[str]]:
    """Git 提交历史里的 AI 痕迹与隐私。

    这里是文件扫不到的盲区：**作者邮箱**和**提交说明**一旦推上去就是永久公开的，
    而且是别人最常拿来翻的地方。发版前必须单独过一遍。

    扫描范围刻意用 `--branches --tags --remotes`，不用 `--all`：
    后者会把 `refs/original/`（`git filter-branch` 改写历史时自动留的本地备份）
    一起算进来，于是「远端已经干净」的历史照样被判成有命中 —— 假报警。
    这里要回答的问题是「**将要推上去的**东西干不干净」，所以只扫这三类 ref；
    本地备份单独列出来提醒，不计入结论。
    """
    import subprocess

    lines: list[str] = []
    bad = 0
    try:
        raw = subprocess.run(
            ["git", "log", "--branches", "--tags", "--remotes",
             "--format=%h\x1f%an\x1f%ae\x1f%s\x1f%b\x1e"],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        ).stdout
        leftover = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", "refs/original"],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    except Exception as exc:                                    # noqa: BLE001
        return 0, [f"    （读不到 git 历史：{type(exc).__name__}: {exc}）"]

    authors: dict[str, int] = {}
    for chunk in raw.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        parts = chunk.split("\x1f")
        if len(parts) < 5:
            continue
        sha, name, email, subject, body = parts[0], parts[1], parts[2], parts[3], parts[4]
        authors[f"{name} <{email}>"] = authors.get(f"{name} <{email}>", 0) + 1

        if not EMAIL_ALLOW.search(email):
            lines.append(f"    {sha}  提交作者邮箱是真实邮箱：{email}")
            bad += 1

        for pattern, label in AI_TRACES + PRIVACY:
            for field, text in (("提交说明", subject + "\n" + body),):
                m = re.search(pattern, text, re.I)
                if m and not (("邮箱" in label) and EMAIL_ALLOW.search(m.group(0))):
                    lines.append(f"    {sha}  {field}命中 [{label}]：«{m.group(0)}»")
                    bad += 1

    lines.insert(0, f"    作者署名：{'; '.join(f'{k} ×{v}' for k, v in authors.items())}")
    lines.insert(1, "    扫描范围：本地分支 + tag + 远端追踪分支（能推上去的那些）")
    if leftover:
        lines.append("    本地另有一批改写历史的备份 ref —— 它们推不上去，不计入上面的结论，")
        lines.append("    但如果你是从这里对外发内容的，记得先清掉：")
        for ref in leftover.splitlines():
            lines.append(f"      {ref}")
    return bad, lines


def main() -> int:
    ap = argparse.ArgumentParser(description="发布前体检")
    ap.add_argument("--show-clean", action="store_true", help="把没有命中的类别也列出来")
    ap.add_argument("--readme-only", action="store_true", help="只扫文件，不查 git 历史")
    args = ap.parse_args()

    files = list(iter_files())
    total_bytes = sum(p.stat().st_size for p, _ in files)

    print("=" * 72)
    print(f"  发布前体检   {ROOT.name}")
    print("=" * 72)
    print(f"\n待发布文件 {len(files)} 个，合计 {total_bytes / 1024:.1f} KB\n")

    bad = 0
    for title, rules in (("一、AI 生成痕迹", AI_TRACES), ("二、隐私信息", PRIVACY)):
        hits = scan(rules, args.show_clean)
        n = sum(len(v) for v in hits.values())
        print(f"--- {title}：{'命中 %d 处' % n if n else '干净'} ---")
        if n:
            bad += n
            for label, lines in hits.items():
                print(f"  [{label}] {len(lines)} 处")
                for ln in lines[:14]:
                    print(ln)
                if len(lines) > 14:
                    print(f"    …… 另有 {len(lines) - 14} 处")
        elif args.show_clean:
            print("    （无命中）")
        print()

    if args.readme_only:
        return 0

    notices = scan(NOTICES, args.show_clean)
    n_notice = sum(len(v) for v in notices.values())
    print("--- 二 · 附：提示项（不阻断发布，扫到请瞄一眼）"
          f"{'：%d 处' % n_notice if n_notice else '：无'} ---")
    for label, lines in notices.items():
        print(f"  [{label}] {len(lines)} 处")
        for ln in lines[:5]:
            print(ln)
        if len(lines) > 5:
            print(f"    …… 另有 {len(lines) - 5} 处")
    print()

    gh_bad, gh_lines = scan_git_history()
    print(f"--- 三、Git 提交历史：{'命中 %d 处' % gh_bad if gh_bad else '干净'} ---")
    for ln in gh_lines:
        print(ln)
    print()
    bad += gh_bad

    print("=" * 72)
    print("  结论：" + ("有命中，处理后再发布" if bad else "干净，可以发布"))
    print("=" * 72)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
