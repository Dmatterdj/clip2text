"""集中配置。所有字段都有默认值，因此不建 .env 也能启动。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 服务 ----------
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "info"
    data_dir: Path = Path("./.data")

    # ---------- 并发 ----------
    max_concurrency: int = Field(default=2, ge=1, le=32)
    max_batch_size: int = Field(default=100, ge=1, le=1000)

    # ---------- 媒体 ----------
    ffmpeg_mode: str = "auto"  # auto | system | static
    max_upload_mb: int = Field(default=300, ge=1, le=4096)
    chunk_max_sec: int = Field(default=110, ge=10, le=3600)

    # ---------- ASR ----------
    asr_engine: str = "auto"
    asr_model: str = "large-v3-turbo"
    asr_language: str = ""

    # ---------- OCR ----------
    ocr_engine: str = "auto"

    # ---------- LLM ----------
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

    # ---------- 云服务 ----------
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"

    # ---------- 链接解析 ----------
    # 默认开启：这个工具的产品定位是「贴链接 → 出文字」（播客、知识科普、
    # 读书、讲座），上传文件只是并行能力，不该是唯一入口。
    # 合规兜底仍然保留：显式设 EXTRACTORS_ENABLED=false 可整体关闭。
    extractors_enabled: bool = True
    extractors_allow: str = ""
    proxy_url: str = ""
    cookie_file: str = ""

    # ---------- 长内容护栏 ----------
    # 3 小时播客会切成 ~100 段，任何一段卡住都会白烧几十分钟；
    # 超过阈值直接拒绝，比「跑了 40 分钟后 OOM/被限流」对用户友好。
    # 设为 0 表示不限制（自建机器上想跑超长内容时用）。
    max_media_duration_min: int = Field(default=180, ge=0, le=24 * 60)
    max_media_size_mb: int = Field(default=2048, ge=0)

    # ---------- 分片级重试 ----------
    # 2 小时播客切成 70 段，中间任一段失败就整任务作废太浪费：
    # 每段最多重试 chunk_retry_max 次，退避按 1x / 2x / 4x 指数增长。
    chunk_retry_max: int = Field(default=2, ge=0, le=10)
    chunk_retry_backoff_sec: float = Field(default=1.0, ge=0.0, le=60.0)

    # ---------- 断点续跑 ----------
    # 每段转写结果落盘到 work/<task_id>/checkpoint.json，
    # 进程重启后跳过已完成的分片，而不是从头再跑一遍。
    resume_enabled: bool = True

    @field_validator("ffmpeg_mode")
    @classmethod
    def _check_ffmpeg_mode(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in {"auto", "system", "static"}:
            raise ValueError("ffmpeg_mode 只能是 auto / system / static")
        return v

    @field_validator("asr_engine")
    @classmethod
    def _check_asr_engine(cls, v: str) -> str:
        v = v.lower().strip()
        allowed = {"auto", "mlx", "faster-whisper", "faster", "funasr", "deepseek"}
        if v not in allowed:
            raise ValueError(f"asr_engine 只能是 {sorted(allowed)}")
        return v

    # ---------- 派生路径 ----------
    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "clip2text.db"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def asr_language_or_none(self) -> str | None:
        return self.asr_language.strip() or None

    @property
    def allowed_extractors(self) -> list[str]:
        return [x.strip() for x in self.extractors_allow.split(",") if x.strip()]

    @property
    def max_media_duration_sec(self) -> float | None:
        """长内容时长上限（秒）。返回 None 表示不限制。"""
        return float(self.max_media_duration_min * 60) if self.max_media_duration_min else None

    @property
    def max_media_bytes(self) -> int | None:
        """长内容体积上限（字节）。返回 None 表示不限制。"""
        return self.max_media_size_mb * 1024 * 1024 if self.max_media_size_mb else None

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.uploads_dir, self.work_dir, self.exports_dir):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """仅供测试使用：让下一次 get_settings() 重新读取环境变量。"""
    get_settings.cache_clear()
