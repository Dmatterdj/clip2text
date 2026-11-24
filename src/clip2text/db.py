"""SQLite 存储层。零外部依赖，单文件数据库。

并发策略：全局一把 threading.Lock，所有写操作串行。
本地单用户场景（并发 ≤ 32）下这比连接池更简单也更安全。
所有公开方法都是同步的，异步调用方请用 asyncio.to_thread 包装。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import (
    Analysis,
    Batch,
    BatchStatus,
    Segment,
    Stage,
    Task,
    TaskStatus,
    utcnow,
)

log = logging.getLogger(__name__)

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

CREATE TABLE IF NOT EXISTS batches (
    id          TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    title       TEXT,
    task_ids    TEXT NOT NULL DEFAULT '[]',
    status      TEXT NOT NULL,
    total       INTEGER NOT NULL DEFAULT 0,
    succeeded   INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    canceled    INTEGER NOT NULL DEFAULT 0,
    duplicates  TEXT NOT NULL DEFAULT '[]',
    invalid     TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batches_created_at ON batches(created_at DESC);

-- 内容解析结果：一个任务只保留最新一份，重新分析直接覆盖。
-- payload 是 Analysis 的完整 JSON，列上的 model/state 只是为了查询与巡检。
CREATE TABLE IF NOT EXISTS analyses (
    task_id     TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    model       TEXT,
    state       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_analyses_state ON analyses(state);

-- 用量账本（契约 docs/SETTINGS-CONTRACT.md §8）。
-- 只追加不修改：这是「花过多少钱」的历史记录，删任务**不删**这里的行，
-- 否则用户删掉一个任务就会觉得自己没花过那笔钱。
CREATE TABLE IF NOT EXISTS usage_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    kind              TEXT NOT NULL,
    task_id           TEXT,
    model             TEXT NOT NULL DEFAULT '',
    audio_seconds     REAL NOT NULL DEFAULT 0,
    images            INTEGER NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_kind ON usage_events(kind);
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
        # 同一事务里级联删掉分析结果：留下孤儿数据既占空间，
        # 也会让「删了任务但 /analysis 还能读到」这种情形出现。
        with self._lock:
            self._conn.execute("DELETE FROM analyses WHERE task_id=?", (task_id,))
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

    # ---------------- 批量 ----------------
    @staticmethod
    def _row_to_batch(row: sqlite3.Row) -> Batch:
        return Batch(
            id=row["id"],
            source_type=row["source_type"],
            title=row["title"],
            task_ids=json.loads(row["task_ids"] or "[]"),
            status=BatchStatus(row["status"]),
            total=row["total"],
            succeeded=row["succeeded"],
            failed=row["failed"],
            canceled=row["canceled"],
            duplicates=json.loads(row["duplicates"] or "[]"),
            invalid=json.loads(row["invalid"] or "[]"),
            created_at=_str_to_dt(row["created_at"]),
            updated_at=_str_to_dt(row["updated_at"]),
        )

    def create_batch(self, batch: Batch) -> Batch:
        params = {
            "id": batch.id,
            "source_type": batch.source_type,
            "title": batch.title,
            "task_ids": json.dumps(batch.task_ids, ensure_ascii=False),
            "status": batch.status.value,
            "total": batch.total,
            "succeeded": batch.succeeded,
            "failed": batch.failed,
            "canceled": batch.canceled,
            "duplicates": json.dumps(batch.duplicates, ensure_ascii=False),
            "invalid": json.dumps(batch.invalid, ensure_ascii=False),
            "created_at": _dt_to_str(batch.created_at),
            "updated_at": _dt_to_str(batch.updated_at),
        }
        cols = ", ".join(params)
        placeholders = ", ".join(f":{c}" for c in params)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO batches ({cols}) VALUES ({placeholders})", params
            )
            self._conn.commit()
        return batch

    def update_batch(self, batch_id: str, **fields: Any) -> None:
        if not fields:
            return
        if "status" in fields and isinstance(fields["status"], BatchStatus):
            fields["status"] = fields["status"].value
        for key in ("task_ids", "duplicates", "invalid"):
            if key in fields:
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        fields["updated_at"] = _dt_to_str(utcnow())
        assignments = ", ".join(f"{k}=:{k}" for k in fields)
        fields["_id"] = batch_id
        with self._lock:
            self._conn.execute(
                f"UPDATE batches SET {assignments} WHERE id=:_id", fields
            )
            self._conn.commit()

    def get_batch(self, batch_id: str) -> Batch | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM batches WHERE id=?", (batch_id,)
            ).fetchone()
        return self._row_to_batch(row) if row else None

    def list_batches(self, *, limit: int = 50, offset: int = 0) -> tuple[list[Batch], int]:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) AS c FROM batches").fetchone()["c"]
            rows = self._conn.execute(
                "SELECT * FROM batches ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._row_to_batch(r) for r in rows], total

    def delete_batch(self, batch_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM batches WHERE id=?", (batch_id,))
            self._conn.commit()
        return cur.rowcount > 0

    # ---------------- 内容解析 ----------------
    def upsert_analysis(self, analysis: Analysis) -> Analysis:
        """一个任务只留一份结果，重复分析覆盖旧的（契约 §6）。"""
        params = {
            "task_id": analysis.task_id,
            "payload": analysis.model_dump_json(),
            "model": analysis.model or None,
            "state": analysis.state,
            "created_at": _dt_to_str(analysis.created_at),
            "updated_at": _dt_to_str(utcnow()),
        }
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO analyses (task_id, payload, model, state, created_at, updated_at)
                VALUES (:task_id, :payload, :model, :state, :created_at, :updated_at)
                ON CONFLICT(task_id) DO UPDATE SET
                    payload=excluded.payload,
                    model=excluded.model,
                    state=excluded.state,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at
                """,
                params,
            )
            self._conn.commit()
        return analysis

    def get_analysis(self, task_id: str) -> Analysis | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM analyses WHERE task_id=?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            return Analysis.model_validate_json(row["payload"])
        except ValueError:
            # 老版本/手工改坏的 payload 不该让整个接口 500：当作没有结果，
            # 用户重新点一次解析即可覆盖。
            log.warning("分析结果 payload 无法解析，已忽略：task_id=%s", task_id)
            return None

    def delete_analysis(self, task_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM analyses WHERE task_id=?", (task_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def fail_running_analyses(self, reason: str = "服务重启，分析被中断") -> int:
        """启动时把上次进程留下的 RUNNING 分析标记失败。

        与任务的处理一致：进程没了，SSE 连接也断了，不可能再有结果，
        留着 RUNNING 只会让前端永远转圈。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT task_id, payload FROM analyses WHERE state='RUNNING'"
            ).fetchall()

        count = 0
        for row in rows:
            try:
                analysis = Analysis.model_validate_json(row["payload"])
            except ValueError:
                analysis = Analysis(task_id=row["task_id"])
            analysis = analysis.model_copy(
                update={"state": "FAILED", "error": reason}
            )
            self.upsert_analysis(analysis)
            count += 1
        if count:
            log.info("启动清理：%d 个 RUNNING 分析标记为 FAILED", count)
        return count

    # ---------------- 用量账本 ----------------
    # 只追加、只聚合。注意 delete() 里**没有**删 usage_events ——
    # 删任务不该抹掉已经花掉的钱（契约 §8）。
    _USAGE_SUMS = (
        "SUM(audio_seconds) AS audio_seconds, "
        "SUM(images) AS images, "
        "SUM(prompt_tokens) AS prompt_tokens, "
        "SUM(completion_tokens) AS completion_tokens"
    )

    def add_usage_event(
        self,
        *,
        kind: str,
        task_id: str | None = None,
        model: str = "",
        audio_seconds: float = 0.0,
        images: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        created_at: datetime | None = None,
    ) -> int:
        """追加一条用量记录，返回自增 id。

        埋点方（usage.record）负责吞异常；这里让异常照常抛出，
        否则「写失败」会被静默吞掉两层，排查时无从下手。
        """
        stamp = _dt_to_str(created_at or utcnow())
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO usage_events (
                    kind, task_id, model, audio_seconds, images,
                    prompt_tokens, completion_tokens, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(kind),
                    str(task_id) if task_id else None,
                    str(model or ""),
                    float(audio_seconds or 0.0),
                    int(images or 0),
                    int(prompt_tokens or 0),
                    int(completion_tokens or 0),
                    stamp,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def usage_totals(self) -> dict[str, float]:
        """全量合计。空表返回全 0，而不是 None（前端不用判空）。"""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._USAGE_SUMS} FROM usage_events"
            ).fetchone()
        return _usage_row_to_dict(row)

    def usage_by_kind(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT kind AS key, {self._USAGE_SUMS} "
                "FROM usage_events GROUP BY kind"
            ).fetchall()
        return [_usage_row_to_dict(r, key_field="key") for r in rows]

    def usage_by_model(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT CASE WHEN model = '' THEN 'unknown' ELSE model END AS key, "
                f"{self._USAGE_SUMS} FROM usage_events GROUP BY key"
            ).fetchall()
        return [_usage_row_to_dict(r, key_field="key") for r in rows]

    def usage_by_day(self, since: str | None = None, offset_minutes: int = 0) -> list[dict[str, Any]]:
        """按「本地日」聚合，倒序。

        `created_at` 存的是 UTC 的 ISO 字符串（`utcnow()` → `+00:00`），
        而 SQLite 的 `date()` **直接截取日期部分、忽略时区偏移**：
        `date('2026-11-23T23:59:00+00:00')` → `2026-11-23`，
        对北京用户来说那条记录的「本地日」其实是 11-24。
        所以必须显式把偏移加上去：`date(created_at, '+480 minutes')`。

        偏移由调用方（usage.py）按服务端本地时区算好传进来 —— SQLite
        不知道进程的时区，硬编码 +8 会在别的时区部署时全错。
        用**分钟**而不是小时：印度 +05:30、尼泊尔 +05:45 这类半小时/
        刻钟时区用 `hours` 会算错（`-5.5 hours` 在 SQLite 里不生效）。
        """
        modifier = f"{int(offset_minutes):+d} minutes"
        where = f"WHERE date(created_at, '{modifier}') >= ?" if since else ""
        params: list[Any] = [since] if since else []
        with self._lock:
            rows = self._conn.execute(
                f"SELECT date(created_at, '{modifier}') AS key, {self._USAGE_SUMS} "
                f"FROM usage_events {where} GROUP BY key ORDER BY key DESC",
                params,
            ).fetchall()
        return [_usage_row_to_dict(r, key_field="key") for r in rows]

    def usage_event_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM usage_events").fetchone()
        return int(row["c"])

    def list_usage_events(self, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """原始记录，倒序。给「最近调用」列表和测试断言用。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM usage_events ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_all_usage(self) -> int:
        """清空用量账本。只由用户显式调用（DELETE /usage），不随删任务触发。"""
        with self._lock:
            cur = self._conn.execute("DELETE FROM usage_events")
            self._conn.commit()
        return int(cur.rowcount or 0)


def _usage_row_to_dict(row: sqlite3.Row, *, key_field: str | None = None) -> dict[str, Any]:
    keys = row.keys()
    out: dict[str, Any] = {}
    if key_field:
        out["key"] = row[key_field]
    for name in ("audio_seconds", "images", "prompt_tokens", "completion_tokens"):
        if name in keys:
            value = row[name]
            out[name] = 0 if value is None else value
    return out
