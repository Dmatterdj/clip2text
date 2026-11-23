"""ASR 引擎抽象。

统一契约：
    engine.available()      -> bool          是否可用（已安装且可运行）
    engine.transcribe(path) -> Transcript    单段转写
    engine.max_chunk_sec    -> int | None    单段时长上限，None 表示无硬限制
    engine.supports_timestamps -> bool       是否返回带时间戳的 segments

上层（core.pipeline）只依赖这个 Protocol，不关心具体实现，
因此新增引擎不需要改业务代码。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from ..models import Transcript


class ASRError(RuntimeError):
    pass


@runtime_checkable
class ASREngine(Protocol):
    name: str
    supports_timestamps: bool
    max_chunk_sec: int | None

    def available(self) -> bool: ...

    def unavailable_reason(self) -> str | None: ...

    def transcribe(self, audio: Path, language: str | None = None) -> Transcript: ...


class BaseEngine:
    """提供默认实现，具体引擎只需覆盖 transcribe 与可用性判断。"""

    name: str = "base"
    supports_timestamps: bool = False
    max_chunk_sec: int | None = None

    def available(self) -> bool:  # pragma: no cover - 子类覆盖
        raise NotImplementedError

    def unavailable_reason(self) -> str | None:
        return None if not self.available() else None

    def transcribe(self, audio: Path, language: str | None = None) -> Transcript:
        raise NotImplementedError
