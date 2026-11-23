"""OCR 引擎注册（P3 才实现，这里先提供状态与占位）。

现状：只做能力探测，让 /capabilities 能如实上报，
前端据此禁用「图片提取」入口，而不是点了才报错。
"""

from __future__ import annotations

import logging

from ..config import Settings
from ..models import EngineStatus

log = logging.getLogger(__name__)

__all__ = ["ocr_statuses", "OCRError"]


class OCRError(RuntimeError):
    pass


def _rapidocr_available() -> tuple[bool, str | None]:
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:
        return False, "未安装 rapidocr-onnxruntime（注意它要求 Python <3.13）"
    return True, None


def ocr_statuses(settings: Settings) -> list[EngineStatus]:
    ok, reason = _rapidocr_available()
    return [
        EngineStatus(
            name="rapidocr",
            available=ok,
            reason=reason,
            detail={"implemented": False, "note": "引擎已可安装，识别流程计划在 P3 实现"},
        ),
        EngineStatus(
            name="deepseek",
            available=bool(settings.deepseek_api_key),
            reason=None if settings.deepseek_api_key else "未配置 DEEPSEEK_API_KEY",
            detail={"implemented": False},
        ),
    ]
