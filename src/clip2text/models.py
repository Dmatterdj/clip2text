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


class Chapter(BaseModel):
    """章节。start/end 是相对音频开头的秒数，前端靠它跳转。"""

    start: float
    end: float
    title: str
    summary: str = ""


class Quote(BaseModel):
    """值得摘录的原话。text 必须来自原文，不允许改写。"""

    start: float
    text: str
    note: str | None = None


class Analysis(BaseModel):
    """一次内容解析（长文本结构化总结）的完整结果。"""

    task_id: str
    abstract: str = ""
    chapters: list[Chapter] = Field(default_factory=list)
    key_points: list[str] = Field(default_factory=list)
    quotes: list[Quote] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)

    language: str | None = None
    model: str = ""
    windows: int = 0
    # 上游返回 usage 时填上，让用户知道一次分析花了多少 token；
    # 不返回就留 None，不要用估算值假装精确。
    token_usage: dict[str, int] | None = None
    state: Literal["RUNNING", "SUCCESS", "FAILED"] = "SUCCESS"
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------- 设置与用量
# 契约：docs/SETTINGS-CONTRACT.md §4
# 这些模型只用于「对外响应形状」，内部存盘格式在 settings_store.py 里。
class LLMSettings(BaseModel):
    """任意 OpenAI 兼容服务的配置视图。

    只有掩码，没有原文：`api_key` 永远不会出现在这个模型里，
    想加字段的用户请注意这一点（安全约束 §2.1）。
    """

    base_url: str = ""
    api_key_masked: str = ""  # 只读，GET 用
    api_key_set: bool = False  # 是否已配置（不暴露原文）
    model: str = ""
    source: Literal["settings", "env", "none"] = "none"


class DeepSeekSettings(BaseModel):
    api_key_masked: str = ""
    api_key_set: bool = False
    base_url: str = "https://api.deepseek.com"
    source: Literal["settings", "env", "none"] = "none"


class SettingsView(BaseModel):
    llm: LLMSettings
    deepseek: DeepSeekSettings
    # 有没有任一可用，等价于 analysis.available
    analysis_ready: bool = False
    # 配置文件路径（让用户知道东西存哪了）
    config_path: str = ""


UsageKind = Literal["asr", "ocr", "analysis", "rewrite"]


class UsageEvent(BaseModel):
    id: int
    kind: UsageKind
    task_id: str | None = None
    model: str = ""
    # 以下按 kind 填充，未使用为 0
    audio_seconds: float = 0
    images: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    created_at: datetime


class UsageBucket(BaseModel):
    """按维度聚合的一行。"""

    key: str  # 日期 2026-11-24 / 类型 asr / 模型 deepseek-chat
    audio_seconds: float = 0
    images: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost: float = 0  # 单位：人民币元


class UsageSummary(BaseModel):
    total: UsageBucket  # key 固定为 "total"
    by_day: list[UsageBucket]  # 最近 30 天，倒序
    by_kind: list[UsageBucket]  # asr/ocr/analysis/rewrite
    by_model: list[UsageBucket]
    event_count: int = 0
    currency: str = "CNY"
    # 计价依据，让用户知道估算怎么来的
    pricing: dict[str, float]

