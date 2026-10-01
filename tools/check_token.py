#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自检一个 GitHub token 能不能用来发 Release。只用标准库。

为什么需要它：GitHub 在权限不够时只回一句含糊的
    "Resource not accessible by personal access token"（HTTP 403），
不告诉你缺哪个权限、也没法从 GET /repos 的 permissions 字段看出来
（那个字段给的是「你本人」对仓库的权限，跟 token 权限无关，很容易被误导）。

这里把探测拆成四步，逐步定位到底卡在哪一层：

    1. GET  /user                       → token 本身有没有效
    2. GET  /repos/{repo}               → 能不能读到这个仓库
                                          （注意：public 仓库不认证也能读，
                                          所以这一步通了不代表 token 覆盖了它）
    3. POST /repos/{repo}/git/blobs     → 有没有 Contents: Write
                                          （失败时响应头会明说是缺哪个权限）
    4. GET  /repos/{repo}/releases      → 顺手看看已有 Release

第 3 步会真的在仓库里留一个悬空 blob 对象。它不挂在任何 commit 上，
不会被 checkout、不影响仓库内容，GitHub 之后会自己回收。介意的话加
--no-write-probe 跳过，但那样就测不出写权限了。

用法：
    set GITHUB_TOKEN=<fine-grained PAT，权限 Contents: Read and write>
    python tools/check_token.py
    python tools/check_token.py --repo owner/name
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
UA = "onenote-importer-check-token/1.0"


def call(method: str, url: str, token: str, payload: dict | None = None,
         timeout: int = 30) -> tuple[int, dict, bytes]:
    """发请求，返回 (状态码, 响应头, 响应体)。4xx/5xx 不抛异常。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", UA)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except Exception as e:  # noqa: BLE001
        return 0, {}, str(e).encode("utf-8", "replace")


def guess_repo() -> str:
    """从 origin 推断 owner/name。"""
    try:
        out = subprocess.run(["git", "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    url = (out.stdout or "").strip()
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else ""


def _accepted(headers: dict) -> str:
    """GitHub 在权限不足时会用这个头告诉你「这个端点要什么权限」。"""
    for k, v in headers.items():
        if k.lower() == "x-accepted-github-permissions":
            return v
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="自检 GitHub token 的发布权限")
    ap.add_argument("--repo", default="", help="owner/name，默认从 origin 推断")
    ap.add_argument("--token-env", default="GITHUB_TOKEN", help="存放 token 的环境变量名")
    ap.add_argument("--no-write-probe", action="store_true",
                    help="跳过写权限探测（不想在仓库里留悬空 blob 时用）")
    args = ap.parse_args()

    token = os.environ.get(args.token_env, "").strip()
    if not token:
        print(f"× 环境变量 {args.token_env} 是空的。", file=sys.stderr)
        return 2

    repo = args.repo or guess_repo()
    if not repo:
        print("× 推断不出仓库，请用 --repo owner/name 指定。", file=sys.stderr)
        return 2

    print(f"token 长度 {len(token)}，前缀 {token[:18]}…，末 6 位 …{token[-6:]}")
    print(f"目标仓库 {repo}")
    print()

    verdict: list[str] = []

    # --- 1. token 本身 ---
    st, _, body = call("GET", f"{API}/user", token)
    if st == 200:
        login = json.loads(body).get("login", "?")
        print(f"[1/4] token 有效          ✓ 登录名 {login}")
    else:
        print(f"[1/4] token 有效          × HTTP {st}")
        verdict.append("token 本身无效或已过期/被撤销 —— 去重新建一个。")
        _print_verdict(verdict)
        return 1

    # --- 2. 仓库可读 ---
    st, _, body = call("GET", f"{API}/repos/{repo}", token)
    if st == 200:
        d = json.loads(body)
        print(f"[2/4] 读到仓库            ✓ visibility={d.get('visibility')} "
              f"default_branch={d.get('default_branch')}")
        if d.get("visibility") == "public":
            print("                          ⚠ public 仓库不认证也能读，"
                  "这一步通了不代表 token 覆盖了它")
    else:
        print(f"[2/4] 读到仓库            × HTTP {st}")
        verdict.append("读不到这个仓库 —— 检查 token 的 Repository access "
                       "是否包含它（或选了 All repositories）。")
        _print_verdict(verdict)
        return 1

    # --- 3. Contents: Write ---
    if args.no_write_probe:
        print("[3/4] Contents: Write      - 已跳过（--no-write-probe）")
    else:
        st, hdr, body = call("POST", f"{API}/repos/{repo}/git/blobs", token,
                             {"content": "check-token probe", "encoding": "utf-8"})
        if st == 201:
            print("[3/4] Contents: Write      ✓ 能写")
        else:
            acc = _accepted(hdr) or "(响应头没给)"
            print(f"[3/4] Contents: Write      × HTTP {st}，"
                  f"端点要求的权限：{acc}")
            verdict.append(
                "缺 Contents: Write。修法：\n"
                "    https://github.com/settings/personal-access-tokens\n"
                "    → 点进 token 的 Edit\n"
                "    → Repository access 确认包含该仓库\n"
                "    → Permissions → Repository permissions → Contents 改成 Read and write\n"
                "    → ★ 滚到页面底部点 Save changes（最容易漏的一步，改完不点 = 没改）")

    # --- 4. 已有 Release ---
    st, _, body = call("GET", f"{API}/repos/{repo}/releases", token)
    if st == 200:
        rels = json.loads(body)
        print(f"[4/4] 已有 Release        ✓ 共 {len(rels)} 个")
        for r in rels:
            assets = ", ".join(a["name"] for a in r.get("assets", [])) or "无附件"
            print(f"                          - {r['tag_name']} | {r['name']} | {assets}")
    else:
        print(f"[4/4] 已有 Release        × HTTP {st}")

    _print_verdict(verdict)
    return 0 if not verdict else 1


def _print_verdict(verdict: list[str]) -> None:
    print()
    if not verdict:
        print("==> 结论：这个 token 可以用来发 Release。")
    else:
        print("==> 结论：还不能发 Release。")
        for i, v in enumerate(verdict, 1):
            print(f"    {i}) {v}")


if __name__ == "__main__":
    raise SystemExit(main())
