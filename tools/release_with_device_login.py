#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""不用手工创建 PAT 也能发 Release —— 走 GitHub 的 OAuth 设备码授权。

为什么要有这个脚本：
    创建/更新 Release、上传附件走的是 GitHub REST API，需要写权限的凭据。
    手工建一个 fine-grained PAT 要摸进四层设置页、还得记得点 Save changes，
    容易漏步。OAuth 设备码流程把这件事压缩成「浏览器里点一下」。

    但设备码流程要用到 github.com（不是 api.github.com），
    在受限沙箱/代理环境里访问不到，所以这个脚本必须在**你自己机器的终端**里跑。

你会看到什么：
    1. 终端打印一个 8 位码（形如 A1B2-C3D4）和一个网址，并自动打开浏览器
    2. 你在浏览器里粘贴那个码、点授权
    3. 脚本自己拿到 access token，创建 Release、上传附件

关于凭据：
    - token 只活在这次进程的内存里，不落盘、不打印
    - 用完可以在 https://github.com/settings/applications 里撤销这次授权
    - 用的 client_id 是 GitHub CLI 的公开 client_id（社区常用做法），
      请求的 scope 只有 public_repo，够发 public 仓库的 Release

用法（在仓库目录下）：
    python tools/release_with_device_login.py --tag v2.0.0 --name v2.0.0

    # 说明文件和附件会自动去找：
    #   ../发布说明-v2.0.0.md
    #   ../onenote-importer-v2.0.0-win64.zip
    # 也可以显式指定：
    python tools/release_with_device_login.py --tag v2.0.0 \
        --body-file ../发布说明-v2.0.0.md \
        --asset ../onenote-importer-v2.0.0-win64.zip
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

# 同目录下的发布工具，复用它的 API 调用逻辑
sys.path.insert(0, str(Path(__file__).resolve().parent))
from publish_release import (  # noqa: E402
    PublishError,
    create_or_update,
    find_release,
    guess_repo,
    upload_asset,
)

# GitHub CLI 的公开 client_id。借用它是因为它已经启用了 device flow，
# 且你不需要为此注册自己的 OAuth App。
CLIENT_ID = "178c6fc778ccc68e1d6a"
# public 仓库发 Release 只需要 public_repo。
SCOPE = "public_repo"

DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
UA = "onenote-importer-device-login/1.0"


def _post_form(url: str, fields: dict, timeout: int = 30) -> tuple[int, dict]:
    """发一个 form 表单 POST，要 JSON 回来。"""
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("User-Agent", UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        return e.code, {"raw": raw[:400]}


def request_device_code() -> dict:
    st, d = _post_form(DEVICE_CODE_URL, {"client_id": CLIENT_ID, "scope": SCOPE})
    if st != 200 or "device_code" not in d:
        raise PublishError(
            f"拿设备码失败：HTTP {st} {d}\n"
            "（如果一直失败，说明网络到不了 github.com，"
            "可以改用网页手动发 Release。）"
        )
    return d


def poll_for_token(device_code: str, interval: int, expires_in: int) -> str:
    """按 GitHub 要求的节奏轮询，直到用户完成授权。"""
    deadline = time.time() + expires_in
    wait = max(interval, 5)
    attempt = 0
    while time.time() < deadline:
        time.sleep(wait)
        attempt += 1
        st, d = _post_form(TOKEN_URL, {
            "client_id": CLIENT_ID,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        })
        if "access_token" in d:
            return d["access_token"]
        err = d.get("error", "")
        if err == "authorization_pending":
            # 用户还没点完，继续等
            if attempt % 6 == 0:
                print(f"    还在等你授权…（已等 {attempt * wait} 秒）")
            continue
        if err == "slow_down":
            wait += 5
            continue
        if err == "expired_token":
            raise PublishError("设备码过期了，重新跑一次脚本。")
        if err == "access_denied":
            raise PublishError("你在浏览器里点了拒绝，没有授权。")
        raise PublishError(f"拿 token 失败：HTTP {st} {d}")
    raise PublishError("等待授权超时，重新跑一次脚本。")


def _discover(repo_dir: Path, tag: str) -> tuple[Path | None, list[Path]]:
    """没显式给就自动找发布说明和附件。"""
    for d in (repo_dir, repo_dir.parent):
        body = d / f"发布说明-{tag}.md"
        asset = d / f"onenote-importer-{tag}-win64.zip"
        hit_body = body if body.is_file() else None
        hits = [asset] if asset.is_file() else []
        if hit_body or hits:
            return hit_body, hits
    return None, []


def main() -> int:
    ap = argparse.ArgumentParser(description="设备码授权 + 发 Release（不需要 PAT）")
    ap.add_argument("--tag", required=True, help="已推到远端的 tag，例如 v2.0.0")
    ap.add_argument("--name", default="", help="Release 标题，默认同 tag")
    ap.add_argument("--body-file", default="", help="发布说明 Markdown")
    ap.add_argument("--body", default="", help="直接给发布说明文本")
    ap.add_argument("--asset", action="append", default=[], help="附件，可重复")
    ap.add_argument("--repo", default="", help="owner/name，默认从 origin 推断")
    ap.add_argument("--draft", action="store_true", help="存成草稿")
    ap.add_argument("--prerelease", action="store_true", help="标为预发布")
    ap.add_argument("--keep-existing-assets", action="store_true",
                    help="附件重名时跳过，不覆盖")
    args = ap.parse_args()

    repo_dir = Path(__file__).resolve().parent.parent
    repo = args.repo or guess_repo()
    if not repo:
        print("× 推断不出仓库，请用 --repo owner/name 指定。", file=sys.stderr)
        return 2

    # 发布说明
    if args.body_file:
        body_path = Path(args.body_file)
        if not body_path.is_file():
            print(f"× 发布说明不存在：{body_path}", file=sys.stderr)
            return 2
        body_text = body_path.read_text(encoding="utf-8")
    elif args.body:
        body_text = args.body
        body_path = None
    else:
        body_path, _ = _discover(repo_dir, args.tag)
        if body_path is None:
            print("× 没找到发布说明，用 --body-file 指定。", file=sys.stderr)
            return 2
        body_text = body_path.read_text(encoding="utf-8")

    # 附件
    assets = [Path(p) for p in args.asset]
    if not assets:
        _, assets = _discover(repo_dir, args.tag)
    for a in assets:
        if not a.is_file():
            print(f"× 附件不存在：{a}", file=sys.stderr)
            return 2

    print("=" * 62)
    print(f"  仓库：{repo}")
    print(f"  tag ：{args.tag}")
    if body_path:
        print(f"  说明：{body_path.name}（{len(body_text)} 字符）")
    for a in assets:
        print(f"  附件：{a.name}（{a.stat().st_size / 1024 / 1024:.1f} MB）")
    print("=" * 62)
    print()

    # ---- 第 1 步：拿设备码 ----
    print("[1/3] 向 GitHub 申请设备码…")
    try:
        dev = request_device_code()
    except PublishError as e:
        print(f"× {e}", file=sys.stderr)
        return 1

    user_code = dev["user_code"]
    uri = dev.get("verification_uri", "https://github.com/login/device")
    interval = int(dev.get("interval", 5))
    expires_in = int(dev.get("expires_in", 900))

    print()
    print("  " + "─" * 56)
    print(f"   ① 浏览器打开： {uri}")
    print(f"   ② 输入这个码： {user_code}")
    print(f"   ③ 点 Authorize")
    print("  " + "─" * 56)
    print(f"   码 {expires_in // 60} 分钟内有效，授权完脚本会自己往下走。")
    print()
    try:
        webbrowser.open(uri)
        print("   已尝试帮你打开浏览器；没弹出来的话手动复制上面的网址。")
    except Exception:  # noqa: BLE001
        print("   手动复制上面的网址到浏览器打开。")
    print()

    # ---- 第 2 步：等授权 ----
    print("[2/3] 等你在浏览器里完成授权…")
    try:
        token = poll_for_token(dev["device_code"], interval, expires_in)
    except PublishError as e:
        print(f"× {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n× 你中断了。", file=sys.stderr)
        return 1
    print("  ✓ 已授权，拿到临时凭据（只在内存里）")
    print()

    # ---- 第 3 步：发 Release ----
    print("[3/3] 创建 Release 并上传附件…")
    try:
        rel = create_or_update(repo, token, tag=args.tag, name=args.name or args.tag,
                               body_text=body_text, draft=args.draft,
                               prerelease=args.prerelease)
        fresh = find_release(repo, args.tag, token) or rel
        for a in assets:
            upload_asset(repo, token, fresh, a,
                         replace=not args.keep_existing_assets)
    except PublishError as e:
        print(f"× {e}", file=sys.stderr)
        return 1

    print()
    print("=" * 62)
    print(f"  完成 → {rel['html_url']}")
    print("=" * 62)
    print("  这次授权可以在 https://github.com/settings/applications 撤销。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
