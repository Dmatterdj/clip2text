"""批量任务调度。

设计要点
    · 去重：同一批里重复的链接只跑一次（用户经常重复粘贴）
    · 排队：超过 MAX_CONCURRENCY 的任务进队列，不会一次全开
    · 汇总：批量状态由子任务实时汇总，不单独维护计数器，
            避免出现"批量说 100% 但还有任务在跑"的不一致
    · 隔离：单条失败不影响其它条目，批量最终可能落到 PARTIAL
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from .config import Settings
from .db import Database
from .extractors import ExtractError, pick_url
from .models import Batch, BatchStatus, Task, TaskStatus, utcnow
from .pipeline import Pipeline

log = logging.getLogger(__name__)

BatchHook = Callable[[Batch], Awaitable[None] | None]


class BatchRunner:
    def __init__(self, pipeline: Pipeline, settings: Settings, db: Database) -> None:
        self.pipeline = pipeline
        self.settings = settings
        self.db = db
        self._runs: dict[str, asyncio.Task[None]] = {}
        self._cancelled: set[str] = set()
        self._hooks: list[BatchHook] = []

    def on_progress(self, hook: BatchHook) -> None:
        self._hooks.append(hook)

    async def _emit(self, batch: Batch) -> None:
        for hook in list(self._hooks):
            try:
                result = hook(batch)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001
                log.exception("批量进度回调失败")

    # ---------------- 创建 ----------------
    def normalize(self, items: list[str], *, source_type: str) -> tuple[list[str], list[str], list[str]]:
        """清洗 + 去重。返回 (有效, 重复, 无效)。"""
        if len(items) > self.settings.max_batch_size:
            raise ValueError(
                f"单次最多 {self.settings.max_batch_size} 条，你提交了 {len(items)} 条"
            )

        valid: list[str] = []
        duplicates: list[str] = []
        invalid: list[str] = []
        seen: set[str] = set()

        for raw in items:
            item = (raw or "").strip()
            if not item:
                continue
            if source_type == "url":
                try:
                    item = pick_url(item)
                except ExtractError:
                    invalid.append(raw.strip()[:200])
                    continue

            key = item.lower() if source_type == "url" else item
            if key in seen:
                duplicates.append(item)
                continue
            seen.add(key)
            valid.append(item)

        return valid, duplicates, invalid

    async def create(
        self,
        *,
        source_type: str,
        items: list[str],
        title: str | None = None,
    ) -> Batch:
        valid, duplicates, invalid = self.normalize(items, source_type=source_type)
        if not valid:
            raise ValueError(
                "没有可处理的条目"
                + (f"（{len(duplicates)} 条重复、{len(invalid)} 条无效）" if duplicates or invalid else "")
            )

        batch = Batch(
            id=uuid.uuid4().hex,
            source_type=source_type,  # type: ignore[arg-type]
            title=title,
            total=len(valid),
            duplicates=duplicates,
            invalid=invalid,
            status=BatchStatus.PENDING,
        )

        # 先把任务建出来（QUEUED），再交给调度器跑
        task_ids: list[str] = []
        for item in valid:
            task = await asyncio.to_thread(
                self.pipeline.create_task,
                source_type=source_type,
                source_value=item,
                title=None,
            )
            task_ids.append(task.id)
        batch.task_ids = task_ids

        await asyncio.to_thread(self.db.create_batch, batch)
        log.info(
            "创建批量 %s：%d 条（重复 %d、无效 %d）",
            batch.id, batch.total, len(duplicates), len(invalid),
        )
        return batch

    # ---------------- 执行 ----------------
    def start(self, batch_id: str) -> None:
        if batch_id in self._runs:
            return
        task = asyncio.create_task(self._run_guarded(batch_id))
        self._runs[batch_id] = task
        task.add_done_callback(lambda _: self._runs.pop(batch_id, None))

    async def _run_guarded(self, batch_id: str) -> None:
        try:
            await self.run(batch_id)
        except Exception:  # noqa: BLE001
            log.exception("批量 %s 未捕获异常", batch_id)
            await asyncio.to_thread(
                self.db.update_batch, batch_id, status=BatchStatus.FAILED
            )

    async def run(self, batch_id: str) -> Batch:
        batch = await asyncio.to_thread(self.db.get_batch, batch_id)
        if batch is None:
            raise ValueError(f"批量不存在: {batch_id}")

        await self._update(batch, status=BatchStatus.RUNNING)

        async def one(task_id: str) -> None:
            if batch_id in self._cancelled:
                await asyncio.to_thread(
                    self.db.update_fields,
                    task_id,
                    status=TaskStatus.CANCELED.value,
                    error_message="批量已取消",
                )
                return
            # pipeline 内部已有全局信号量，这里直接复用，排队自然发生
            await self.pipeline.run(task_id)

        # gather 默认会把异常收集起来，单条失败不影响其它
        await asyncio.gather(*(one(tid) for tid in batch.task_ids), return_exceptions=True)

        # 汇总最终状态：以子任务的实际结果为准
        final = await self._refresh_counts(batch)
        if batch_id in self._cancelled:
            status = BatchStatus.CANCELED
        elif final.failed == 0 and final.canceled == 0:
            status = BatchStatus.DONE
        elif final.succeeded == 0:
            status = BatchStatus.FAILED
        else:
            status = BatchStatus.PARTIAL

        await self._update(final, status=status)
        log.info(
            "批量 %s 结束：%s（成功 %d / 失败 %d / 取消 %d）",
            batch_id, status.value, final.succeeded, final.failed, final.canceled,
        )
        return final

    def cancel(self, batch_id: str) -> bool:
        batch = self.db.get_batch(batch_id)
        if batch is None:
            return False
        self._cancelled.add(batch_id)
        for tid in batch.task_ids:
            self.pipeline.cancel(tid)
        handle = self._runs.get(batch_id)
        if handle is not None:
            handle.cancel()
        return True

    # ---------------- 状态汇总 ----------------
    async def sync(self, batch_id: str) -> Batch | None:
        """从子任务重算计数。用于轮询时保证数据新鲜。"""
        batch = await asyncio.to_thread(self.db.get_batch, batch_id)
        if batch is None:
            return None
        if batch.status.terminal:
            return batch
        refreshed = await self._refresh_counts(batch)
        # 计数变化也要落库，否则轮询到的永远是 0
        if (
            refreshed.status != batch.status
            or refreshed.succeeded != batch.succeeded
            or refreshed.failed != batch.failed
            or refreshed.canceled != batch.canceled
        ):
            await self._update(refreshed, status=refreshed.status)
        return refreshed

    async def _refresh_counts(self, batch: Batch) -> Batch:
        tasks = await asyncio.to_thread(self._load_tasks, batch.task_ids)
        succeeded = sum(1 for t in tasks if t is not None and t.status == TaskStatus.SUCCESS)
        failed = sum(1 for t in tasks if t is not None and t.status == TaskStatus.FAILED)
        canceled = sum(1 for t in tasks if t is not None and t.status == TaskStatus.CANCELED)
        return batch.model_copy(
            update={
                "succeeded": succeeded,
                "failed": failed,
                "canceled": canceled,
                "updated_at": utcnow(),
            }
        )

    def _load_tasks(self, task_ids: list[str]) -> list[Task | None]:
        return [self.db.get(tid) for tid in task_ids]

    async def _update(self, batch: Batch, **fields: object) -> None:
        """更新并落库。

        计数（succeeded/failed/canceled）始终一起写：它们由 _refresh_counts
        算出来，如果只写 status 就会出现「状态是 DONE 但计数还是 0/2」——
        这种不一致会让前端进度条永远显示 0%。
        """
        for key, value in fields.items():
            setattr(batch, key, value)
        batch.updated_at = utcnow()
        payload: dict[str, object] = {
            "status": batch.status,
            "succeeded": batch.succeeded,
            "failed": batch.failed,
            "canceled": batch.canceled,
            "total": batch.total,
        }
        await asyncio.to_thread(self.db.update_batch, batch.id, **payload)
        await self._emit(batch)

    # ---------------- 删除 ----------------
    async def delete(self, batch_id: str) -> bool:
        batch = await asyncio.to_thread(self.db.get_batch, batch_id)
        if batch is None:
            return False
        # 级联删掉子任务，避免孤儿
        for tid in batch.task_ids:
            await asyncio.to_thread(self.db.delete, tid)
        return await asyncio.to_thread(self.db.delete_batch, batch_id)


def gather_inputs(items: list[str], text: str | None) -> list[str]:
    """合并 items 数组与多行文本。前端两种输入方式都支持。"""
    out = [i for i in items if i and i.strip()]
    if text:
        for line in text.splitlines():
            line = line.strip()
            if line:
                out.append(line)
    return out


def resolve_upload_paths(paths: list[str], uploads_dir: Path) -> list[str]:
    """校验上传路径都在 uploads 目录内，防任意路径读取。"""
    resolved: list[str] = []
    base = uploads_dir.resolve()
    for p in paths:
        candidate = Path(p)
        try:
            candidate.resolve().relative_to(base)
        except ValueError as exc:
            raise ValueError(f"路径不在 uploads 目录内: {p}") from exc
        if not candidate.exists():
            raise ValueError(f"文件不存在: {p}")
        resolved.append(str(candidate))
    return resolved
