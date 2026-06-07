# -*- coding: utf-8 -*-
"""Microsoft Graph 写入通道（主通道）。

- 创建页面：POST /me/onenote/sections/{id}/pages，multipart 里带 Presentation(HTML) + 图片/附件二进制部件
- 更新页面：PATCH /me/onenote/pages/{id}/content，优先整块替换 data-id="content-root" 的 div，
  失败则删除重建（body 目标不支持 replace，这是 Graph 的限制）
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from core.onenote_html import CONTENT_DIV_ID, RenderedPage
from .auth import AuthError, DeviceCodeAuth, TokenStore
from .base import (
    AuthRequired,
    NotebookRef,
    OneNoteWriter,
    ProbeResult,
    SectionRef,
    TransientError,
    WriterError,
)

API = "https://graph.microsoft.com/v1.0"


class GraphError(WriterError):
    def __init__(self, status: int, message: str, payload: str = ""):
        super().__init__(message)
        self.status = status
        self.payload = payload


def build_multipart(parts: list[tuple[str, bytes, str]]) -> tuple[bytes, str]:
    """构造 multipart/form-data。parts = [(部件名, 字节, Content-Type)]"""
    boundary = f"OneNoteBoundary{uuid.uuid4().hex}"
    buf = bytearray()
    for name, data, ctype in parts:
        buf += f"--{boundary}\r\n".encode()
        buf += f'Content-Disposition: form-data; name="{name}"\r\n'.encode()
        if ctype:
            buf += f"Content-Type: {ctype}\r\n".encode()
        buf += b"\r\n"
        buf += data
        buf += b"\r\n"
    buf += f"--{boundary}--\r\n".encode()
    return bytes(buf), f"multipart/form-data; boundary={boundary}"


class GraphWriter(OneNoteWriter):
    name = "graph"

    def __init__(self, cfg, log=None, token_store: TokenStore | None = None, step=None):
        super().__init__(cfg, log, step)
        self.store = token_store or TokenStore()
        self.auth = DeviceCodeAuth(cfg.graph_client_id, cfg.graph_authority)
        self._nb_cache: list[NotebookRef] | None = None
        self._sec_cache: dict[str, list[SectionRef]] = {}

    # ----------------------------------------------------------- 探测
    def probe(self) -> ProbeResult:
        if not (self.cfg.graph_client_id or "").strip():
            return ProbeResult(False, "未填写 Azure 应用客户端 ID")
        if self.store.is_expired() and not self.store.refresh_token:
            return ProbeResult(False, "尚未登录 Microsoft 账户")
        try:
            me = self._graph("GET", f"{API}/me?$select=displayName,userPrincipalName,mail")
        except AuthRequired as e:
            return ProbeResult(False, f"登录已失效：{e}")
        except WriterError as e:
            return ProbeResult(False, f"Graph 不可达：{e}")
        acct = me.get("userPrincipalName") or me.get("mail") or me.get("displayName") or "已登录"
        self.store.data["account"] = acct
        self.store.save()
        return ProbeResult(True, "Graph 通道可用", acct)

    def ensure_valid_token(self) -> str:
        if self.store.is_expired() and self.store.refresh_token:
            try:
                self.log("access token 过期，尝试静默刷新", "DEBUG")
                tok = self.auth.refresh(self.store.refresh_token)
                self.store.update_from_token(tok, self.cfg.graph_client_id)
            except AuthError as e:
                self.log(f"刷新失败：{e}", "WARN")
                raise AuthRequired(str(e))
        if not self.store.access_token:
            raise AuthRequired("尚未登录，请先完成设备码登录")
        return self.store.access_token

    # ----------------------------------------------------------- HTTP
    def _graph(self, method: str, url: str, body: bytes | None = None,
               content_type: str | None = None, expect: tuple[int, ...] = (200, 201, 204),
               tries: int = 3) -> Any:
        token = self.ensure_valid_token()
        self.step(self._step_label(method, url))
        headers = {"Authorization": f"Bearer {token}"}
        if content_type:
            headers["Content-Type"] = content_type
        elif method in ("POST", "PATCH"):
            headers["Content-Type"] = "application/json"

        last_err: Exception | None = None
        for attempt in range(tries):
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            if not (content_type or method in ("POST", "PATCH")):
                for k, v in headers.items():
                    req.add_header(k, v) if k != "Content-Type" else None
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    raw = resp.read()
                    if resp.status not in expect:
                        raise GraphError(resp.status, f"意外的状态码 {resp.status}", raw.decode("utf-8", "replace"))
                    if not raw:
                        return None
                    ctype = resp.headers.get("Content-Type", "")
                    if "application/json" in ctype:
                        try:
                            return json.loads(raw.decode("utf-8"))
                        except json.JSONDecodeError:
                            return raw.decode("utf-8", "replace")
                    return raw.decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                payload = e.read().decode("utf-8", "replace")
                if e.code in (401, 403):
                    if e.code == 401 and self.store.refresh_token and attempt == 0:
                        self.store.data["expires_at"] = 0
                        last_err = AuthRequired("access token 被拒绝")
                        continue
                    raise AuthRequired(self._fmt_payload(payload) or f"HTTP {e.code}")
                if e.code == 429 or e.code >= 500:
                    retry_after = int(e.headers.get("Retry-After", "0") or 0)
                    wait = retry_after or (2 ** attempt)
                    time.sleep(min(wait, 60))
                    last_err = TransientError(self._fmt_payload(payload) or f"HTTP {e.code}")
                    continue
                raise GraphError(e.code, self._fmt_payload(payload) or f"HTTP {e.code}", payload)
            except urllib.error.URLError as e:
                last_err = TransientError(f"网络错误：{e}")
                time.sleep(2 ** attempt)
                continue
        raise last_err or WriterError("请求失败")

    @staticmethod
    def _step_label(method: str, url: str) -> str:
        """把 Graph 请求翻译成一句人话，给界面上的进度提示用。"""
        if "/onenote/notebooks" in url and method == "GET":
            return "读取笔记本列表"
        if "/sections" in url and method == "GET":
            return "读取分区列表"
        if "/pages" in url and method == "GET":
            return "读取页面清单"
        if "/pages" in url and method == "POST":
            return "上传页面（含图片）"
        if "/content" in url:
            return "更新页面内容"
        if method == "DELETE":
            return "删除页面"
        return f"调用 Microsoft Graph（{method}）"

    @staticmethod
    def _fmt_payload(payload: str) -> str:
        try:
            j = json.loads(payload)
            inner = j.get("error", {})
            if isinstance(inner, dict):
                return f'{inner.get("code", "")}: {inner.get("message", "")}'.strip(": ")
            return str(inner)[:300]
        except Exception:
            return payload[:300]

    # ----------------------------------------------------------- 结构
    def list_notebooks(self, refresh: bool = False) -> list[NotebookRef]:
        if self._nb_cache is not None and not refresh:
            return self._nb_cache
        data = self._graph("GET", f"{API}/me/onenote/notebooks?$top=200")
        items = data.get("value", []) if isinstance(data, dict) else []
        self._nb_cache = [NotebookRef(i.get("id", ""), i.get("displayName", "")) for i in items]
        return self._nb_cache

    def ensure_notebook(self, display_name: str) -> NotebookRef:
        if (self.cfg.notebook_id or "").strip():
            for nb in self.list_notebooks():
                if nb.id == self.cfg.notebook_id.strip():
                    return nb
        for nb in self.list_notebooks():
            if nb.name.strip().lower() == (display_name or "").strip().lower():
                return nb
        self.log(f"笔记本「{display_name}」不存在，自动创建", "INFO")
        created = self._graph("POST", f"{API}/me/onenote/notebooks",
                              json.dumps({"displayName": display_name}).encode("utf-8"))
        ref = NotebookRef(created.get("id", ""), created.get("displayName", display_name))
        self._nb_cache = None
        return ref

    def list_sections(self, notebook_id: str, refresh: bool = False) -> list[SectionRef]:
        if not refresh and notebook_id in self._sec_cache:
            return self._sec_cache[notebook_id]
        url = f"{API}/me/onenote/notebooks/{urllib.parse.quote(notebook_id)}/sections?$top=300"
        data = self._graph("GET", url)
        items = data.get("value", []) if isinstance(data, dict) else []
        refs = [SectionRef(i.get("id", ""), i.get("displayName", "")) for i in items]
        self._sec_cache[notebook_id] = refs
        return refs

    def ensure_section(self, notebook_id: str, section_name: str) -> SectionRef:
        existing = self.find_section(notebook_id, section_name)
        if existing is not None:
            return existing
        self.log(f"分区「{section_name}」不存在，自动创建", "INFO")
        url = f"{API}/me/onenote/notebooks/{urllib.parse.quote(notebook_id)}/sections"
        body = json.dumps({"displayName": section_name}).encode("utf-8")
        created = self._graph("POST", url, body)
        self._sec_cache.pop(notebook_id, None)
        return SectionRef(created.get("id", ""), created.get("displayName", section_name))

    def list_pages_full(self, section_id: str) -> list[dict]:
        out: list[dict] = []
        url = (f"{API}/me/onenote/sections/{urllib.parse.quote(section_id)}/pages"
               f"?$select=id,title,createdDateTime&$top=100")
        seen = 0
        while url and seen < 20:
            data = self._graph("GET", url)
            items = data.get("value", []) if isinstance(data, dict) else []
            for i in items:
                out.append({"id": i.get("id", ""), "title": i.get("title", "") or "",
                            "created": i.get("createdDateTime", "") or "",
                            "name": i.get("title", "") or ""})
            url = data.get("@odata.nextLink") if isinstance(data, dict) else None
            seen += 1
        return out

    def list_pages(self, section_id: str) -> list[tuple[str, str]]:
        return [(i.get("id", ""), i.get("title", "")) for i in self.list_pages_full(section_id)]

    # ----------------------------------------------------------- 页面
    def create_page(self, section_id: str, page: RenderedPage) -> str:
        parts: list[tuple[str, bytes, str]] = [
            ("Presentation", page.html.encode("utf-8"), "text/html")
        ]
        for m in page.media:
            parts.append((m.part_name, m.read_bytes(), m.content_type))
        body, ctype = build_multipart(parts)
        url = f"{API}/me/onenote/sections/{urllib.parse.quote(section_id)}/pages"
        res = self._raw_post(url, body, ctype)
        page_id = self._extract_page_id(res)
        if not page_id:
            page_id = self._find_page_by_title(section_id, page.title)
        if not page_id:
            raise WriterError(f"页面「{page.title}」创建成功但拿不到 page id")
        return page_id

    def _raw_post(self, url: str, body: bytes, ctype: str) -> dict:
        token = self.ensure_valid_token()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                raw = resp.read()
                location = resp.headers.get("Location", "")
                try:
                    j = json.loads(raw.decode("utf-8")) if raw else {}
                except Exception:
                    j = {}
                if isinstance(j, dict):
                    j["__location"] = location
                    j["__status"] = resp.status
                return j
        except urllib.error.HTTPError as e:
            payload = e.read().decode("utf-8", "replace")
            if e.code in (401, 403):
                raise AuthRequired(self._fmt_payload(payload))
            if e.code == 507:
                raise WriterError("分区页面数已达上限（HTTP 507），请新建分区或清理旧页面")
            raise GraphError(e.code, self._fmt_payload(payload) or f"HTTP {e.code}", payload)

    @staticmethod
    def _extract_page_id(res: dict) -> str:
        for key in ("id", "pageId"):
            v = res.get(key)
            if isinstance(v, str) and v:
                return v
        loc = res.get("__location", "") or ""
        m = re.search(r"pages/(1-[0-9a-fA-F!\-]+)", loc)
        if m:
            return urllib.parse.unquote(m.group(1))
        if loc:
            tail = loc.rstrip("/").split("/")[-1]
            if tail and "http" not in tail:
                return urllib.parse.unquote(tail)
        return ""

    def _find_page_by_title(self, section_id: str, title: str) -> str:
        url = (f"{API}/me/onenote/sections/{urllib.parse.quote(section_id)}/pages"
               f"?$top=25&$orderby=createdDateTime desc&$select=id,title")
        try:
            data = self._graph("GET", url)
        except WriterError:
            return ""
        for p in (data.get("value", []) if isinstance(data, dict) else []):
            if (p.get("title") or "").strip() == title.strip():
                return p.get("id", "")
        return ""

    # ----------------------------------------------------------- 更新
    def update_page_content(self, page_id: str, page: RenderedPage, strategy: str = "replace_div") -> None:
        if strategy != "replace_div":
            raise WriterError("Graph 通道只支持 replace_div 更新；recreate 请交由 pipeline 处理")
        inner = page.inner_html or page.html
        if not inner:
            raise WriterError("页面内容为空")
        content = f'<div data-id="{CONTENT_DIV_ID}">{inner}</div>'
        commands = [{"target": f"#{CONTENT_DIV_ID}", "action": "replace", "content": content}]
        url = f"{API}/me/onenote/pages/{urllib.parse.quote(page_id)}/content"
        if page.media:
            parts: list[tuple[str, bytes, str]] = [("Commands", json.dumps(commands).encode("utf-8"), "application/json")]
            for m in page.media:
                parts.append((m.part_name, m.read_bytes(), m.content_type))
            body, ctype = build_multipart(parts)
            self._raw_patch(url, body, ctype)
        else:
            self._graph("PATCH", url, json.dumps(commands).encode("utf-8"))
        self._update_title(page_id, page.title)

    def _raw_patch(self, url: str, body: bytes, ctype: str) -> None:
        token = self.ensure_valid_token()
        req = urllib.request.Request(url, data=body, method="PATCH")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            payload = e.read().decode("utf-8", "replace")
            if e.code in (401, 403):
                raise AuthRequired(self._fmt_payload(payload))
            raise GraphError(e.code, self._fmt_payload(payload) or f"HTTP {e.code}", payload)

    def _update_title(self, page_id: str, title: str) -> None:
        """改标题属于锦上添花，失败不影响正文已更新这一事实。"""
        url = f"{API}/me/onenote/pages/{urllib.parse.quote(page_id)}/content"
        try:
            self._graph("PATCH", url, json.dumps([{"target": "title", "action": "replace", "content": title}]).encode("utf-8"))
        except WriterError as e:
            self.log(f"标题更新跳过（{e}）", "DEBUG")

    def replace_or_recreate(self, page_id: str, section_id: str, page: RenderedPage, strategy: str) -> tuple[str, str]:
        if strategy == "recreate":
            self.delete_page(page_id)
            new_id = self.create_page(section_id, page)
            return new_id, "recreate"
        try:
            self.update_page_content(page_id, page, "replace_div")
            return page_id, "replace_div"
        except AuthRequired:
            raise
        except WriterError as e:
            self.log(f"整块替换失败（{e}），降级为删除重建", "WARN")
            try:
                self.delete_page(page_id)
            except WriterError:
                pass
            new_id = self.create_page(section_id, page)
            return new_id, "recreate(fallback)"

    def delete_page(self, page_id: str) -> None:
        self._graph("DELETE", f"{API}/me/onenote/pages/{urllib.parse.quote(page_id)}")
