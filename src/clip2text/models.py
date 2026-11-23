"""数据契约。字段命名对齐 AnyToCopy 的开放 API，便于迁移与对照。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class TaskStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELED = "CANCELED"

    @property
    def terminal(self) -> bool:
        return self in {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELED}


class Stage(str, Enum):
    QUEUED = "queued"
    FETCHING = "fetching"
    EXTRACTING = "extracting"
    TRANSCODING = "transcoding"
    TRANSCRIBING = "transcribing"
    OCR = "ocr"
    PACKAGING = "packaging"
    DONE = "done"


class Segment(BaseModel):
    """带时间戳的分句。只有支持时间戳的 ASR 引擎才会填充。"""

    start: float
    end: float
    text: str


class Transcript(BaseModel):
    text: str = ""
    segments: list[Segment] = Field(default_factory=list)
    language: str | None = None


class Task(BaseModel):
    id: str
    source_type: Literal["url", "upload"]
    source_value: str
    platform: str | None = None
    work_type: Literal["video", "image", "audio"] | None = None

    status: TaskStatus = TaskStatus.QUEUED
    stage: Stage = Stage.QUEUED
    progress: int = 0

    # ---- 结果字段 ----
    title: str | None = None
    content: str | None = None
    text_content: str | None = None
    cover: str | None = None
    video_url: str | None = None
    video_urls: list[str] = Field(default_factory=list)
    image_urls: list[str] = Field(default_factory=list)
    audio_url: str | None = None
    duration: float | None = None

    # ---- 差异化 ----
    segments: list[Segment] = Field(default_factory=list)
    language: str | None = None

    # ---- 诊断 ----
    error_message: str | None = None
    engine: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def has_payload(self) -> bool:
        return bool(
            self.title
            or self.content
            or self.text_content
            or self.video_urls
            or self.image_urls
            or self.audio_url
        )


class TaskCreate(BaseModel):
    source_type: Literal["url", "upload"] = "upload"
    source_value: str
    title: str | None = None


class BatchStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    PARTIAL = "PARTIAL"  # 部分成功部分失败
    FAILED = "FAILED"
    CANCELED = "CANCELED"

    @property
    def terminal(self) -> bool:
        return self in {
            BatchStatus.DONE,
            BatchStatus.PARTIAL,
            BatchStatus.FAILED,
            BatchStatus.CANCELED,
        }


class Batch(BaseModel):
    """一次提交多条链接/文件。进度由子任务汇总，不单独维护。"""

    id: str
    source_type: Literal["url", "upload"] = "url"
    title: str | None = None
    task_ids: list[str] = Field(default_factory=list)
    status: BatchStatus = BatchStatus.PENDING

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    canceled: int = 0

    # 提交时被去重掉的条目（重复链接）
    duplicates: list[str] = Field(default_factory=list)
    # 提交时就被判为无效的条目（格式不对）
    invalid: list[str] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def done(self) -> int:
        return self.succeeded + self.failed + self.canceled

    @property
    def progress(self) -> int:
        if not self.total:
            return 0
        return round(self.done / self.total * 100)


class BatchCreate(BaseModel):
    """批量提交。两种输入形式：
    · urls：多行文本或字符串数组，逐条解析
    · paths：上传文件的 path 列表
    """

    source_type: Literal["url", "upload"] = "url"
    items: list[str] = Field(default_factory=list, description="链接或上传文件路径列表")
    text: str | None = Field(default=None, description="多行文本，每行一条链接")
    title: str | None = None


class EngineStatus(BaseModel):
    name: str
    available: bool
    reason: str | None = None
    timestamps: bool = False
    max_chunk_sec: int | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
