# -*- coding: utf-8 -*-
"""Microsoft Graph 的设备码授权 + token 缓存。

不用 msal / requests：设备码流只有两个 REST 调用，标准库足够。
token 存放在 %APPDATA%\\ObsidianToOneNote\\token.json。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from core.config import GRAPH_SCOPES, appdata_dir


def authority_base(kind: str) -> str:
    kind = (kind or "consumers").strip()
    if kind.startswith("http"):
        return kind.rstrip("/")
    return f"https://login.microsoftonline.com/{kind or 'consumers'}/oauth2/v2.0"


@dataclass
class DeviceCodeSession:
    user_code: str
    device_code: str
    verification_uri: str
    expires_in: int
    interval: int
    message: str = ""
    deadline: float = 0.0

    @property
    def expired(self) -> bool:
        return time.time() > self.deadline


class AuthError(Exception):
    pass


class TokenStore:
    def __init__(self, path: Path | None = None):
        self.path = path or appdata_dir() / "token.json"
        self.data: dict = {}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def clear(self) -> None:
        self.data = {}
        try:
            self.path.unlink()
        except OSError:
            pass

    @property
    def access_token(self) -> str:
        return self.data.get("access_token", "")

    @property
    def refresh_token(self) -> str:
        return self.data.get("refresh_token", "")

    @property
    def expires_at(self) -> float:
        return float(self.data.get("expires_at", 0))

    @property
    def account(self) -> str:
        return self.data.get("account", "")

    def is_expired(self, skew: int = 120) -> bool:
        return (not self.access_token) or (time.time() > self.expires_at - skew)

    def update_from_token(self, token: dict, client_id: str = ""):
        self.data.update({
            "access_token": token.get("access_token", ""),
            "refresh_token": token.get("refresh_token", self.data.get("refresh_token", "")),
            "expires_in": token.get("expires_in"),
            "expires_at": time.time() + int(token.get("expires_in", 3600)),
            "scope": token.get("scope", ""),
            "token_type": token.get("token_type", "Bearer"),
            "client_id": client_id or self.data.get("client_id", ""),
        })
        self.save()


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8", "replace")
        try:
            j = json.loads(payload)
            raise AuthError(j.get("error_description") or j.get("error") or payload)
        except json.JSONDecodeError:
            raise AuthError(f"HTTP {e.code}: {payload}")


class DeviceCodeAuth:
    """Azure 公共客户端（允许公共客户端流）的设备码授权。"""

    def __init__(self, client_id: str, authority: str = "consumers", scopes: list[str] | None = None):
        self.client_id = (client_id or "").strip()
        self.authority = authority
        self.scopes = scopes or GRAPH_SCOPES
        self.base = authority_base(authority)

    # ---------------- 第一步：拿设备码 ----------------
    def start(self) -> DeviceCodeSession:
        if not self.client_id:
            raise AuthError("未填写 Azure 应用的客户端 ID")
        data = _post_form(f"{self.base}/devicecode", {
            "client_id": self.client_id,
            "scope": " ".join(self.scopes),
        })
        return DeviceCodeSession(
            user_code=data.get("user_code", ""),
            device_code=data.get("device_code", ""),
            verification_uri=data.get("verification_uri", "https://microsoft.com/devicelogin"),
            expires_in=int(data.get("expires_in", 900)),
            interval=int(data.get("interval", 5)),
            message=data.get("message", ""),
            deadline=time.time() + int(data.get("expires_in", 900)),
        )

    # ---------------- 第二步：轮询 ----------------
    def poll_once(self, session: DeviceCodeSession) -> dict | None:
        """返回 token dict；用户还没完成授权时返回 None；失败抛 AuthError。"""
        try:
            return _post_form(f"{self.base}/token", {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": self.client_id,
                "device_code": session.device_code,
            })
        except AuthError as e:
            msg = str(e)
            if "authorization_pending" in msg or "slow_down" in msg:
                return None
            raise

    def refresh(self, refresh_token: str) -> dict:
        return _post_form(f"{self.base}/token", {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "refresh_token": refresh_token,
            "scope": " ".join(self.scopes),
        })


# token 里是不记名凭证，权限收紧一些，别让同机其它用户读走
def tighten_permissions(path: Path) -> None:
    try:
        import os
        os.chmod(path, 0o600)
    except Exception:
        pass
