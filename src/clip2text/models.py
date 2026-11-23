"""数据契约。字段命名对齐 AnyToCopy 的开放 API，便于迁移与对照。"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


class TaskPublic(BaseModel):
    """对外暴露的任务视图（与内部 Task 一致，保留以便将来裁剪敏感字段）。"""

    @classmethod
    def from_task(cls, task: Task) -> dict[str, Any]:
        return task.model_dump(mode="json")


class EngineStatus(BaseModel):
    name: str
    available: bool
    reason: str | None = None
    timestamps: bool = False
    max_chunk_sec: int | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
