"""批量调度的端到端测试（用假流水线，不跑真实 ASR）。"""

from __future__ import annotations

import pytest

from clip2text.batch import BatchRunner
from clip2text.config import Settings
from clip2text.db import Database
from clip2text.models import BatchStatus, Stage, TaskStatus


class FakePipeline:
    """假的流水线：按预设结果改动任务状态。"""

    def __init__(self, db: Database, results: list[str]) -> None:
        self.db = db
        self.results = results
        self.calls: list[str] = []
        self._i = 0

    def create_task(self, *, source_type, source_value, title=None, work_type=None):
        from clip2text.models import Task

        self._i += 1
        task = Task(
            id=f"task{self._i}",
            source_type=source_type,
            source_value=source_value,
            title=title,
            status=TaskStatus.QUEUED,
            stage=Stage.QUEUED,
        )
        self.db.create(task)
        return task

    async def run(self, task_id: str):
        self.calls.append(task_id)
        outcome = self.results[(len(self.calls) - 1) % len(self.results)]
        task = self.db.get(task_id)
        assert task is not None
        if outcome == "SUCCESS":
            self.db.update_fields(
                task_id,
                status=TaskStatus.SUCCESS.value,
                stage=Stage.DONE.value,
                progress=100,
                text_content="假转写结果",
            )
        elif outcome == "FAILED":
            self.db.update_fields(
                task_id,
                status=TaskStatus.FAILED.value,
                error_message="假失败",
            )
        else:
            self.db.update_fields(task_id, status=TaskStatus.CANCELED.value)

    def cancel(self, task_id: str) -> bool:
        return True


@pytest.fixture
def env(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()
    db = Database(settings.db_path)
    return settings, db


def make_runner(env, results: list[str]) -> BatchRunner:
    settings, db = env
    return BatchRunner(FakePipeline(db, results), settings, db)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_batch_all_success(env):
    runner = make_runner(env, ["SUCCESS"])
    batch = await runner.create(
        source_type="url",
        items=["https://a.com/1", "https://a.com/2", "https://a.com/3"],
    )
    assert batch.total == 3

    final = await runner.run(batch.id)
    assert final.status == BatchStatus.DONE
    assert final.succeeded == 3
    assert final.failed == 0
    assert final.progress == 100

    # 落库后也要一致（这是之前踩过的坑：状态 DONE 但计数 0）
    stored = env[1].get_batch(batch.id)
    assert stored is not None
    assert stored.succeeded == 3
    assert stored.status == BatchStatus.DONE


@pytest.mark.asyncio
async def test_batch_partial_failure(env):
    runner = make_runner(env, ["SUCCESS", "FAILED", "SUCCESS"])
    batch = await runner.create(
        source_type="url", items=[f"https://a.com/{i}" for i in range(3)]
    )
    final = await runner.run(batch.id)
    assert final.status == BatchStatus.PARTIAL
    assert final.succeeded == 2
    assert final.failed == 1
    assert final.done == 3


@pytest.mark.asyncio
async def test_batch_all_failed(env):
    runner = make_runner(env, ["FAILED"])
    batch = await runner.create(source_type="url", items=["https://a.com/1"])
    final = await runner.run(batch.id)
    assert final.status == BatchStatus.FAILED
    assert final.failed == 1


@pytest.mark.asyncio
async def test_batch_dedupes_before_running(env):
    runner = make_runner(env, ["SUCCESS"])
    batch = await runner.create(
        source_type="url",
        items=["https://a.com/1", "https://a.com/1", "https://a.com/2"],
    )
    assert batch.total == 2
    assert len(batch.duplicates) == 1
    final = await runner.run(batch.id)
    assert final.succeeded == 2


@pytest.mark.asyncio
async def test_batch_rejects_empty(env):
    runner = make_runner(env, ["SUCCESS"])
    with pytest.raises(ValueError, match="没有可处理的条目"):
        await runner.create(source_type="url", items=["", "   "])


@pytest.mark.asyncio
async def test_batch_cancel_marks_children(env):
    runner = make_runner(env, ["SUCCESS"])
    batch = await runner.create(
        source_type="url", items=["https://a.com/1", "https://a.com/2"]
    )
    runner.cancel(batch.id)
    final = await runner.run(batch.id)
    assert final.status == BatchStatus.CANCELED
    assert final.canceled == 2


@pytest.mark.asyncio
async def test_batch_sync_refreshes_counts(env):
    """轮询接口必须能看到最新计数，否则前端进度条不动。"""
    runner = make_runner(env, ["SUCCESS"])
    batch = await runner.create(
        source_type="url", items=["https://a.com/1", "https://a.com/2"]
    )
    # 只跑一个子任务，模拟"进行中"
    first = batch.task_ids[0]
    await runner.pipeline.run(first)  # type: ignore[attr-defined]

    refreshed = await runner.sync(batch.id)
    assert refreshed is not None
    assert refreshed.succeeded == 1
    stored = env[1].get_batch(batch.id)
    assert stored is not None
    assert stored.succeeded == 1


@pytest.mark.asyncio
async def test_batch_delete_cascades(env):
    runner = make_runner(env, ["SUCCESS"])
    batch = await runner.create(source_type="url", items=["https://a.com/1"])
    task_ids = list(batch.task_ids)

    assert await runner.delete(batch.id) is True
    assert env[1].get_batch(batch.id) is None
    for tid in task_ids:
        assert env[1].get(tid) is None


@pytest.mark.asyncio
async def test_batch_delete_unknown(env):
    runner = make_runner(env, ["SUCCESS"])
    assert await runner.delete("nope") is False


def test_batch_progress_zero_total():
    from clip2text.models import Batch

    b = Batch(id="x", source_type="url", total=0)
    assert b.progress == 0


def test_batch_status_terminal():
    assert BatchStatus.DONE.terminal
    assert BatchStatus.PARTIAL.terminal
    assert BatchStatus.FAILED.terminal
    assert BatchStatus.CANCELED.terminal
    assert not BatchStatus.RUNNING.terminal
    assert not BatchStatus.PENDING.terminal
