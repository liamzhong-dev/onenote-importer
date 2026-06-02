# -*- coding: utf-8 -*-
"""配置模型与持久化。

配置文件默认放在 %APPDATA%\\OneNoteDiaryImporter\\config.json，
不在来源文件夹里留下任何产物。
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "OneNoteDiaryImporter"


def appdata_dir() -> Path:
    base = os.environ.get("APPDATA")
    if not base:
        base = str(Path.home())
    p = Path(base) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class Config:
    # ---- 来源：Markdown 日记文件夹 ----
    # 字段名保持 vault_path 以免破坏已保存的配置
    vault_path: str = ""
    include_globs: list[str] = field(default_factory=lambda: ["**/*.md"])
    exclude_globs: list[str] = field(
        default_factory=lambda: ["**/.obsidian/**", "**/.trash/**", "**/模板/**", "**/Templates/**"]
    )
    date_frontmatter_keys: list[str] = field(
        default_factory=lambda: ["date", "created", "day", "ctime", "creation date"]
    )
    title_frontmatter_keys: list[str] = field(default_factory=lambda: ["title", "标题", "name"])

    # ---- 目标：OneNote ----
    notebook_name: str = "日记"
    notebook_id: str = ""
    # 分区策略：
    #   template —— 按日期套模板命名（{YYYY}{season} 这类），已存在的同名分区直接复用
    #   fixed    —— 全部写入一个指定的已有分区，新页面接在原有页面后面
    section_mode: str = "template"
    section_template: str = "{YYYY}-{MM}"
    section_fixed: str = ""
    # 目标分区里已经存在同名页面时怎么办：
    #   update —— 视为同一篇，覆盖更新（默认，适合「我自己在 OneNote 里也写日记」和重复导入）
    #   skip   —— 保留 OneNote 里那份，跳过这一篇
    #   create —— 不管，照建新页面（会产生重名页面）
    existing_page_policy: str = "update"
    title_template: str = "{date} {title}"

    # ---- 渲染 ----
    embed_images: bool = True
    embed_attachments: bool = True
    max_media_bytes: int = 8 * 1024 * 1024
    image_width_limit: int = 640
    keep_frontmatter_block: bool = False

    # ---- 同步行为 ----
    update_existing: bool = True
    conflict_strategy: str = "replace_div"  # replace_div | recreate
    dry_run: bool = True
    throttle_ms: int = 400

    # ---- 通道 ----
    preferred_writer: str = "auto"  # auto | graph | com
    graph_client_id: str = ""
    graph_authority: str = "consumers"  # consumers | common | organizations | tenants/{id}
    com_enabled: bool = True

    # ---- 其它 ----
    state_path: str = ""
    log_level: str = "INFO"

    _KNOWN_KEYS: set[str] = field(default_factory=lambda: {
        "vault_path", "include_globs", "exclude_globs", "date_frontmatter_keys",
        "title_frontmatter_keys", "notebook_name", "notebook_id", "section_template",
        "section_mode", "section_fixed", "existing_page_policy",
        "title_template", "embed_images", "embed_attachments", "max_media_bytes",
        "image_width_limit", "keep_frontmatter_block", "update_existing", "conflict_strategy",
        "dry_run", "throttle_ms", "preferred_writer", "graph_client_id", "graph_authority",
        "com_enabled", "state_path", "log_level",
    })

    # ---------------- IO ----------------
    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or appdata_dir() / "config.json"
        cfg = cls()
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return cfg
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if k in cfg._KNOWN_KEYS:
                        setattr(cfg, k, v)
        return cfg

    def save(self, path: Path | None = None) -> Path:
        path = path or appdata_dir() / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    # ---------------- 派生 ----------------
    def state_db_path(self) -> Path:
        if self.state_path:
            return Path(self.state_path)
        return appdata_dir() / "state.sqlite3"

    def validate(self) -> list[str]:
        """返回人类可读的错误列表，空列表表示配置可用。"""
        errs: list[str] = []
        if not self.vault_path:
            errs.append("未选择日记文件夹")
        elif not Path(self.vault_path).is_dir():
            errs.append(f"日记文件夹不存在：{self.vault_path}")
        if not self.notebook_name and not self.notebook_id:
            errs.append("未指定目标笔记本")
        if self.conflict_strategy not in ("replace_div", "recreate"):
            errs.append("conflict_strategy 只能是 replace_div 或 recreate")
        return errs


# Graph 权限：读写豆记 + 创建页面 + 离线刷新
GRAPH_SCOPES = ["Notes.ReadWrite", "Notes.Create", "User.Read", "offline_access"]

DEFAULT_CLIENT_ID_HINT = (
    "在 Azure 门户 → 应用注册 → 新建注册 → 支持账户类型选「任何 Microsoft 账户」；\n"
    "重定向 URI 填 http://localhost，勾选「允许公共客户端流」。复制应用(客户端) ID 填到这里。"
)
