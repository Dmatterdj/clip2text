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
    extractors_enabled: bool = False
    extractors_allow: str = ""
    proxy_url: str = ""
    cookie_file: str = ""

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

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.uploads_dir, self.work_dir, self.exports_dir):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """仅供测试使用：让下一次 get_settings() 重新读取环境变量。"""
    get_settings.cache_clear()
