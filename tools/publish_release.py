#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布 / 更新一个 GitHub Release，并把附件传上去。只用标准库。

用法：

    set GITHUB_TOKEN=<fine-grained PAT，权限 Contents: Read and write>
    python tools/publish_release.py --tag v2.0.0 --name "v2.0.0" ^
        --body-file 发布说明-v2.0.0.md ^
        --asset onenote-importer-v2.0.0-win64.zip

说明：
- 仓库默认从 `git remote get-url origin` 推断，也可以用 --repo owner/name 指定。
- 同名 tag 的 Release 已经存在时走更新（不会重复创建，也不会连带删掉旧附件）。
- 附件名重复时默认覆盖：先删旧的同名附件，再传新的。
- token 只从环境变量读，不落盘、不打印。

为什么必须要 token：git 的 SSH 密钥只能推 commit 和 tag。
创建 Release、上传附件走的是 GitHub REST API，那是另一条认证通道。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.github.com"
UPLOAD_API = "https://uploads.github.com"
UA = "onenote-importer-publish-release/1.0"


class PublishError(RuntimeError):
    pass


def _request(method: str, url: str, token: str, *,
             payload: dict | None = None,
             raw: bytes | None = None,
             content_type: str = "application/json",
             timeout: int = 120) -> tuple[int, bytes]:
    """发一个请求，返回 (状态码, 响应体)。4xx/5xx 不抛异常，交给调用方判断。"""
    data = raw
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", UA)
    if data is not None:
        req.add_header("Content-Type", content_type)
        req.add_header("Content-Length", str(len(data)))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _json_or_err(status: int, body: bytes, what: str) -> dict:
    if status >= 400:
        try:
            msg = json.loads(body.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            msg = body.decode("utf-8", "replace")[:400]
        raise PublishError(f"{what} 失败：HTTP {status} {msg}")
    try:
        return json.loads(body.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise PublishError(f"{what} 返回的不是合法 JSON：{e}") from e


def guess_repo() -> str:
    """从 origin 的地址推断 owner/name，支持 https 与 ssh 两种写法。"""
    try:
        out = subprocess.run(["git", "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        raise PublishError(f"读不到 origin 地址：{e}") from e
    url = (out.stdout or "").strip()
    if not url:
        raise PublishError("origin 没有配置，请用 --repo owner/name 指定仓库")
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", url)
    if not m:
        raise PublishError(f"看不懂 origin 地址：{url!r}")
    return m.group(1)


def find_release(repo: str, tag: str, token: str) -> dict | None:
    st, body = _request("GET", f"{API}/repos/{repo}/releases/tags/{urllib.parse.quote(tag)}", token)
    if st == 404:
        return None
    return _json_or_err(st, body, f"查询 tag {tag} 的 Release")


def create_or_update(repo: str, token: str, *, tag: str, name: str,
                     body_text: str, draft: bool, prerelease: bool) -> dict:
    existing = find_release(repo, tag, token)
    payload = {
        "tag_name": tag,
        "name": name or tag,
        "body": body_text,
        "draft": draft,
        "prerelease": prerelease,
    }
    if existing:
        print(f"  tag {tag} 已有 Release（id={existing['id']}），改为更新")
        st, body = _request("PATCH", f"{API}/repos/{repo}/releases/{existing['id']}",
                            token, payload=payload)
        rel = _json_or_err(st, body, "更新 Release")
        print(f"  ✓ 已更新：{rel['html_url']}")
    else:
        st, body = _request("POST", f"{API}/repos/{repo}/releases", token, payload=payload)
        if st == 422:
            _json_or_err(st, body, "创建 Release（tag 可能还没推到远端）")
        rel = _json_or_err(st, body, "创建 Release")
        print(f"  ✓ 已创建：{rel['html_url']}")
    return rel


def upload_asset(repo: str, token: str, release: dict, path: Path, *,
                 replace: bool = True) -> None:
    name = path.name
    size = path.stat().st_size
    for a in release.get("assets", []):
        if a["name"] == name:
            if not replace:
                print(f"  - 附件 {name} 已存在，按参数跳过")
                return
            st, body = _request("DELETE", f"{API}/repos/{repo}/releases/assets/{a['id']}", token)
            if st not in (204, 404):
                _json_or_err(st, body, f"删除旧附件 {name}")
            print(f"  - 旧的同名附件 {name} 已删除")

    url = (f"{UPLOAD_API}/repos/{repo}/releases/{release['id']}/assets"
           f"?name={urllib.parse.quote(name)}")
    print(f"  上传 {name}（{size / 1024 / 1024:.1f} MB）…")
    ctype = "application/zip" if name.lower().endswith(".zip") else "application/octet-stream"
    st, body = _request("POST", url, token, raw=path.read_bytes(),
                        content_type=ctype, timeout=900)
    asset = _json_or_err(st, body, f"上传附件 {name}")
    print(f"  ✓ 已上传：{asset['browser_download_url']}（{asset['size']} 字节）")


def main() -> int:
    ap = argparse.ArgumentParser(description="发布或更新一个 GitHub Release")
    ap.add_argument("--tag", required=True, help="已推到远端的 tag，例如 v2.0.0")
    ap.add_argument("--name", default="", help="Release 标题，默认同 tag")
    ap.add_argument("--body-file", default="", help="发布说明 Markdown 文件")
    ap.add_argument("--body", default="", help="直接给发布说明（与 --body-file 二选一）")
    ap.add_argument("--asset", action="append", default=[], help="要上传的附件，可重复")
    ap.add_argument("--repo", default="", help="owner/name，默认从 origin 推断")
    ap.add_argument("--token-env", default="GITHUB_TOKEN", help="存放 token 的环境变量名")
    ap.add_argument("--draft", action="store_true", help="存成草稿")
    ap.add_argument("--prerelease", action="store_true", help="标为预发布")
    ap.add_argument("--keep-existing-assets", action="store_true",
                    help="附件重名时不覆盖，跳过")
    args = ap.parse_args()

    token = os.environ.get(args.token_env, "").strip()
    if not token:
        print(f"× 环境变量 {args.token_env} 是空的。先 set {args.token_env}=<token> 再跑。",
              file=sys.stderr)
        return 2

    repo = args.repo or guess_repo()
    print(f"仓库：{repo}")
    print(f"tag ：{args.tag}")

    if args.body_file:
        body_text = Path(args.body_file).read_text(encoding="utf-8")
        print(f"说明：{args.body_file}（{len(body_text)} 字符）")
    else:
        body_text = args.body

    assets = [Path(p) for p in args.asset]
    for a in assets:
        if not a.is_file():
            print(f"× 附件不存在：{a}", file=sys.stderr)
            return 2

    try:
        rel = create_or_update(repo, token, tag=args.tag, name=args.name,
                               body_text=body_text, draft=args.draft,
                               prerelease=args.prerelease)
        # 重新取一次，拿到最新的附件列表（更新路径下 rel 可能是旧的）
        fresh = find_release(repo, args.tag, token) or rel
        for a in assets:
            upload_asset(repo, token, fresh, a,
                         replace=not args.keep_existing_assets)
    except PublishError as e:
        print(f"× {e}", file=sys.stderr)
        return 1

    print("\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
