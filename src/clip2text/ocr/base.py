"""OCR 引擎契约。"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class OCRError(RuntimeError):
    pass


class OCRLine(BaseModel):
    text: str
    # 归一化到 0-1000 的坐标框 [x1, y1, x2, y2]，原点在左上角。
    # 不同引擎给原始像素，统一在这里换算，上层就不必关心分辨率。
    box: list[int] | None = None
    confidence: float | None = None


class OCRResult(BaseModel):
    text: str = ""
    lines: list[OCRLine] = Field(default_factory=list)
    page: int = 1
    engine: str = ""

    def dedupe_blank(self) -> OCRResult:
        self.lines = [ln for ln in self.lines if ln.text.strip()]
        self.text = "\n".join(ln.text.strip() for ln in self.lines)
        return self


@runtime_checkable
class OCREngine(Protocol):
    name: str
    supports_bbox: bool

    def available(self) -> bool: ...

    def unavailable_reason(self) -> str | None: ...

    def recognize(self, image: Path) -> OCRResult: ...


class BaseOCREngine:
    name: str = "base"
    supports_bbox: bool = True

    def available(self) -> bool:  # pragma: no cover
        raise NotImplementedError

    def unavailable_reason(self) -> str | None:
        return None

    def recognize(self, image: Path) -> OCRResult:  # pragma: no cover
        raise NotImplementedError


def normalize_box(
    x1: float, y1: float, x2: float, y2: float, width: int, height: int
) -> list[int]:
    """像素坐标 → 0-1000 归一化坐标。"""
    if width <= 0 or height <= 0:
        return []
    return [
        max(0, min(1000, round(x1 / width * 1000))),
        max(0, min(1000, round(y1 / height * 1000))),
        max(0, min(1000, round(x2 / width * 1000))),
        max(0, min(1000, round(y2 / height * 1000))),
    ]
