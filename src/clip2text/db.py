"""SQLite 存储层。零外部依赖，单文件数据库。

并发策略：全局一把 threading.Lock，所有写操作串行。
本地单用户场景（并发 ≤ 32）下这比连接池更简单也更安全。
所有公开方法都是同步的，异步调用方请用 asyncio.to_thread 包装。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Segment, Stage, Task, TaskStatus, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id             TEXT PRIMARY KEY,
    source_type    TEXT NOT NULL,
    source_value   TEXT NOT NULL,
    platform       TEXT,
    work_type      TEXT,
    status         TEXT NOT NULL,
    stage          TEXT NOT NULL,
    progress       INTEGER NOT NULL DEFAULT 0,
    title          TEXT,
    content        TEXT,
    text_content   TEXT,
    cover          TEXT,
    video_url      TEXT,
    video_urls     TEXT NOT NULL DEFAULT '[]',
    image_urls     TEXT NOT NULL DEFAULT '[]',
    audio_url      TEXT,
    duration       REAL,
    segments       TEXT NOT NULL DEFAULT '[]',
    language       TEXT,
    error_message  TEXT,
    engine         TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status     ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_source     ON tasks(source_value);
"""


def _dt_to_str(dt: datetime) -> str:
    return dt.isoformat()


def _str_to_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------- 行 <-> 模型 ----------------
    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            source_type=row["source_type"],
            source_value=row["source_value"],
            platform=row["platform"],
            work_type=row["work_type"],
            status=TaskStatus(row["status"]),
            stage=Stage(row["stage"]),
            progress=row["progress"],
            title=row["title"],
            content=row["content"],
            text_content=row["text_content"],
            cover=row["cover"],
            video_url=row["video_url"],
            video_urls=json.loads(row["video_urls"] or "[]"),
            image_urls=json.loads(row["image_urls"] or "[]"),
            audio_url=row["audio_url"],
            duration=row["duration"],
            segments=[Segment(**s) for s in json.loads(row["segments"] or "[]")],
            language=row["language"],
            error_message=row["error_message"],
            engine=row["engine"],
            created_at=_str_to_dt(row["created_at"]),
            updated_at=_str_to_dt(row["updated_at"]),
        )

    @staticmethod
    def _task_to_params(task: Task) -> dict[str, Any]:
        return {
            "id": task.id,
            "source_type": task.source_type,
            "source_value": task.source_value,
            "platform": task.platform,
            "work_type": task.work_type,
            "status": task.status.value,
            "stage": task.stage.value,
            "progress": task.progress,
            "title": task.title,
            "content": task.content,
            "text_content": task.text_content,
            "cover": task.cover,
            "video_url": task.video_url,
            "video_urls": json.dumps(task.video_urls, ensure_ascii=False),
            "image_urls": json.dumps(task.image_urls, ensure_ascii=False),
            "audio_url": task.audio_url,
            "duration": task.duration,
            "segments": json.dumps([s.model_dump() for s in task.segments], ensure_ascii=False),
            "language": task.language,
            "error_message": task.error_message,
            "engine": task.engine,
            "created_at": _dt_to_str(task.created_at),
            "updated_at": _dt_to_str(task.updated_at),
        }

    # ---------------- CRUD ----------------
    def create(self, task: Task) -> Task:
        params = self._task_to_params(task)
        cols = ", ".join(params)
        placeholders = ", ".join(f":{c}" for c in params)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO tasks ({cols}) VALUES ({placeholders})", params
            )
            self._conn.commit()
        return task

    def upsert(self, task: Task) -> Task:
        """不存在则插入，存在则整行更新。"""
        task.updated_at = utcnow()
        params = self._task_to_params(task)
        cols = ", ".join(params)
        placeholders = ", ".join(f":{c}" for c in params)
        updates = ", ".join(f"{c}=excluded.{c}" for c in params if c != "id")
        with self._lock:
            self._conn.execute(
                f"INSERT INTO tasks ({cols}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                params,
            )
            self._conn.commit()
        return task

    def update_fields(self, task_id: str, **fields: Any) -> None:
        """局部更新。segments / video_urls 等列表字段需已序列化。"""
        if not fields:
            return
        fields["updated_at"] = _dt_to_str(utcnow())
        assignments = ", ".join(f"{k}=:{k}" for k in fields)
        fields["_id"] = task_id
        with self._lock:
            self._conn.execute(
                f"UPDATE tasks SET {assignments} WHERE id=:_id", fields
            )
            self._conn.commit()

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    def delete(self, task_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def list(
        self,
        *,
        status: TaskStatus | None = None,
        limit: int = 50,
        offset: int = 0,
        query: str | None = None,
    ) -> tuple[list[Task], int]:
        where: list[str] = []
        params: list[Any] = []
        if status is not None:
            where.append("status=?")
            params.append(status.value)
        if query:
            where.append(
                "(title LIKE ? OR text_content LIKE ? OR content LIKE ? OR source_value LIKE ?)"
            )
            like = f"%{query}%"
            params.extend([like, like, like, like])
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) AS c FROM tasks {clause}", params
            ).fetchone()["c"]
            rows = self._conn.execute(
                f"SELECT * FROM tasks {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [self._row_to_task(r) for r in rows], total

    def all_ids_by_status(self, statuses: Iterable[TaskStatus]) -> list[str]:
        vals = [s.value for s in statuses]
        if not vals:
            return []
        placeholders = ",".join("?" * len(vals))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id FROM tasks WHERE status IN ({placeholders})", vals
            ).fetchall()
        return [r["id"] for r in rows]

    def count_by_status(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS c FROM tasks GROUP BY status"
            ).fetchall()
        return {r["status"]: r["c"] for r in rows}
