"""DeepSeek 云 ASR 引擎：不占本地算力，按音频时长计费。

官方限制（必须遵守，否则请求直接被拒）：
  - 音频 ≤ 8 MiB
  - 时长 ≤ 120 秒
  - 只接受 16-bit PCM WAV 或 Ogg Opus
  - 无语言参数，自动检测
  - 只返回整段 text，没有时间戳与分段
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from ..models import Transcript
from .base import ASRError, BaseEngine

log = logging.getLogger(__name__)

MAX_BYTES = 8 * 1024 * 1024
MAX_SECONDS = 120


class DeepSeekASREngine(BaseEngine):
    name = "deepseek"
    supports_timestamps = False  # 关键差异：云引擎拿不到 segments
    max_chunk_sec = MAX_SECONDS

    def __init__(self, api_key: str = "", base_url: str = "https://api.deepseek.com") -> None:
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")

    def available(self) -> bool:
        return bool(self.api_key)

    def unavailable_reason(self) -> str | None:
        if not self.api_key:
            return "未配置 DEEPSEEK_API_KEY"
        return None

    def transcribe(self, audio: Path, language: str | None = None) -> Transcript:
        if not self.available():
            raise ASRError(self.unavailable_reason() or "DeepSeek ASR 不可用")
        if not audio.exists():
            raise ASRError(f"音频不存在: {audio}")
        size = audio.stat().st_size
        if size > MAX_BYTES:
            raise ASRError(
                f"音频 {size / 1024 / 1024:.1f}MB 超过 DeepSeek 的 8MB 上限，请先分片或转 Ogg Opus"
            )

        url = f"{self.base_url}/v1/tools/asr"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            with audio.open("rb") as fh:
                files = {"file": (audio.name, fh, "application/octet-stream")}
                resp = httpx.post(url, headers=headers, files=files, timeout=600.0)
        except httpx.HTTPError as exc:
            raise ASRError(f"DeepSeek ASR 请求失败: {exc}") from exc

        if resp.status_code != 200:
            detail = resp.text[:400]
            raise ASRError(f"DeepSeek ASR 返回 {resp.status_code}: {detail}")

        data = resp.json()
        text = (data.get("text") or "").strip()
        if not text:
            log.warning("DeepSeek ASR 返回空文本: %s", data)
        # 云端没有 segments，也无法可靠推断语言
        return Transcript(text=text, segments=[], language=language)
