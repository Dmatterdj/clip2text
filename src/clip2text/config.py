"""集中配置。所有字段都有默认值，因此不建 .env 也能启动。

配置三层（契约 docs/SETTINGS-CONTRACT.md §3）
    代码默认值  <  .env / 环境变量  <  `.data/settings.json`（界面写入）

`Settings()` 只吃前两层（pydantic-settings 本来就这么工作），
第三层在 `get_settings()` 里显式 apply —— 不去改 BaseSettings 的
source 顺序（那要 monkeypatch 内部实现，脆弱且难测）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .settings_store import apply_overrides, load_overrides, settings_path

# 用量估算单价（契约 §5）。用户可在网页「设置 → 用量 → 单价」里改，
# 改完写进 .data/settings.json 的 pricing 字段并立即生效。
# 单位：元/小时音频、元/张图、元/百万 token。
# 这是**估算**，不是账单：真实计费以各服务商的账单为准。
DEFAULT_PRICING: dict[str, float] = {
    "asr_per_hour": 0.78,
    "ocr_per_image": 0.0015,
    "llm_input_per_mtok": 1.0,
    "llm_output_per_mtok": 4.0,
}


def _default_pricing() -> dict[str, float]:
    return dict(DEFAULT_PRICING)


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

    # ---------- 用量估算单价 ----------
    # settings.json 的 pricing 字段会整体合并覆盖到这里（见 settings_store）。
    # 用 dict 而不是四个独立字段：单价是「用户可自由增减的一组数」，
    # 契约里也是 dict[str, float]，多个字段反而要在三处同步维护。
    pricing: dict[str, float] = Field(default_factory=_default_pricing)

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

    @field_validator("pricing")
    @classmethod
    def _check_pricing(cls, v: dict[str, float] | None) -> dict[str, float]:
        """保证 dict 里四个已知单价永远齐全。

        `Settings(pricing={"asr_per_hour": 2})` 这种半份配置不该让
        另外三个单价变成「缺失」——估算需要四个数才能算，缺一个就会
        静默少算一笔钱。
        """
        merged = _default_pricing()
        for name, value in (v or {}).items():
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
                continue
            if number < 0:
                continue
            merged[str(name)] = number
        return merged

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
def _env_settings() -> Settings:
    """只吃环境变量 + .env 的基线（不含 settings.json 覆盖）。

    单独缓存一份，是因为「这个 key 到底来自界面还是来自 .env」
    需要拿生效值和基线值对比才能回答（契约 §2.5 的 source 字段）。
    重复构造 Settings() 每次都要解析 .env，没必要。
    """
    return Settings()


def get_settings() -> Settings:
    """生效配置 = 环境变量/.env 基线 + settings.json 覆盖。

    settings.json 的路径取自基线里的 data_dir：JSON 文件本身就在
    .data 目录下，所以不能靠它自己来定位自己。
    """
    base = _env_settings()
    overrides = load_overrides(base.data_dir)
    if not overrides:
        return base
    return apply_overrides(base, overrides)


def env_settings() -> Settings:
    """基线配置（只读环境变量/.env）。给 settings 视图判断 source 用。"""
    return _env_settings()


def settings_file(data_dir: Path | str | None = None) -> Path:
    """settings.json 的绝对路径（给界面显示「存哪了」）。"""
    return settings_path(data_dir if data_dir is not None else _env_settings().data_dir)


def reset_settings_cache() -> None:
    """让下一次 get_settings() 重新读取环境变量与 settings.json。

    缓存只挂在 `_env_settings()` 上（settings.json 每次都要重读，
    否则外部编辑文件后进程看不到）。所以这里清的是基线缓存。
    仅供测试**和热更新**使用：界面写完配置后必须调用它（或
    AppState.reload_settings()），否则老对象会生效到进程重启（契约 §3）。
    """
    _env_settings.cache_clear()


def reload_settings() -> Settings:
    """丢弃缓存并返回重新解析后的配置。"""
    reset_settings_cache()
    return get_settings()


def effective_overrides(data_dir: Path | str | None = None) -> dict[str, Any]:
    """当前 settings.json 的内容（已清洗）。给设置接口和测试用。"""
    return load_overrides(data_dir if data_dir is not None else _env_settings().data_dir)

