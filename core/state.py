# -*- coding: utf-8 -*-
"""幂等状态库。

每条笔记按「相对 Vault 路径 + 内容 sha256」记账：
- 路径没见过 → create
- 路径见过且 hash 变了 → update
- hash 没变 → skip
这样反复运行不会产生重复页面，也不会把没改的日记重推一遍。

线程安全：README 里那个坑是靠这个类兜住的 —— GUI 里「扫描」和「同步」跑在
**不同的后台线程**上，如果各建一个连接，或者共用一个绑定到创建线程的连接，
sqlite3 默认会因为 `check_same_thread=True` 直接抛 ProgrammingError。
所以这里统一用 `check_same_thread=False` + 一把可重入锁，任何线程都能用同一个实例。
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    rel_path    TEXT PRIMARY KEY,
    sha256      TEXT NOT NULL,
    title       TEXT,
    section     TEXT,
    section_key TEXT,
    notebook    TEXT,
    page_id     TEXT NOT NULL,
    writer      TEXT,
    created_at  TEXT,
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_pages_section ON pages(section_key);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    finished_at TEXT,
    writer TEXT,
    created INTEGER,
    updated INTEGER,
    skipped INTEGER,
    failed INTEGER,
    dry_run INTEGER
);
"""


@dataclass
class PageRecord:
    rel_path: str
    sha256: str
    title: str
    section: str
    section_key: str
    notebook: str
    page_id: str
    writer: str
    created_at: str
    updated_at: str


class StateDB:
    """SQLite 状态库。所有方法都可以从任意线程调用。"""

    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 锁保护同一实例；timeout 让「计划线程」与「同步线程」各自的连接
        # 短时抢写时排队等一会儿，而不是立刻抛 database is locked。
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self):
        with self._lock:
            try:
                self.conn.close()
            except Exception:
                pass

    # ---------------- 查询 ----------------
    def get(self, rel_path: str) -> PageRecord | None:
        with self._lock:
            cur = self.conn.execute("SELECT * FROM pages WHERE rel_path = ?", (rel_path,))
            row = cur.fetchone()
        return PageRecord(**dict(row)) if row else None

    def all(self) -> dict[str, PageRecord]:
        with self._lock:
            cur = self.conn.execute("SELECT * FROM pages")
            rows = cur.fetchall()
        return {r["rel_path"]: PageRecord(**dict(r)) for r in rows}

    # ---------------- 写入 ----------------
    def upsert(self, rec: PageRecord) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO pages(rel_path, sha256, title, section, section_key, notebook, page_id, writer, created_at, updated_at)
                   VALUES(:rel_path, :sha256, :title, :section, :section_key, :notebook, :page_id, :writer, :created_at, :updated_at)
                   ON CONFLICT(rel_path) DO UPDATE SET
                       sha256=excluded.sha256, title=excluded.title, section=excluded.section,
                       section_key=excluded.section_key, notebook=excluded.notebook, page_id=excluded.page_id,
                       writer=excluded.writer, updated_at=excluded.updated_at""",
                rec.__dict__,
            )
            self.conn.commit()

    def delete(self, rel_path: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM pages WHERE rel_path = ?", (rel_path,))
            self.conn.commit()

    def record_run(self, stats: dict, writer: str, dry_run: bool) -> None:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self.conn.execute(
                "INSERT INTO runs(started_at, finished_at, writer, created, updated, skipped, failed, dry_run) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (now, now, writer, stats.get("created", 0), stats.get("updated", 0),
                 stats.get("skipped", 0), stats.get("failed", 0), int(bool(dry_run))),
            )
            self.conn.commit()

    def last_run(self) -> dict | None:
        with self._lock:
            cur = self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
        return dict(row) if row else None


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
