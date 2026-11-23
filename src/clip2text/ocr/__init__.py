"""OCR 引擎注册与自动选择。

auto 优先级：本地 rapidocr > 云端 deepseek
理由与 ASR 一致：装了本地引擎就不该悄悄走付费云服务。
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import Settings
from ..models import EngineStatus
from .base import BaseOCREngine, OCREngine, OCRError, OCRLine, OCRResult
from .deepseek_engine import DeepSeekOCREngine
from .rapidocr_engine import RapidOCREngine

log = logging.getLogger(__name__)

__all__ = [
    "BaseOCREngine",
    "DeepSeekOCREngine",
    "OCREngine",
    "OCRError",
    "OCRLine",
    "OCRResult",
    "RapidOCREngine",
    "build_ocr_engines",
    "ocr_statuses",
    "recognize_images",
    "select_ocr_engine",
]

LOCAL_ORDER = ("rapidocr",)


def build_ocr_engines(settings: Settings) -> dict[str, OCREngine]:
    return {
        "rapidocr": RapidOCREngine(),
        "deepseek": DeepSeekOCREngine(
            api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url
        ),
    }


def select_ocr_engine(settings: Settings, engines: dict[str, OCREngine] | None = None) -> OCREngine:
    """按配置挑引擎。

    注意：「本地优先」的顺序只用于已知的内置引擎名，不能假设
    engines 字典里一定有 rapidocr——测试和第三方会传入自己的引擎。
    所以顺序是：显式指定 → 内置本地顺序 → 字典里其它可用引擎 → 云端。
    """
    engines = engines or build_ocr_engines(settings)
    wanted = settings.ocr_engine.lower()

    if wanted != "auto":
        eng = engines.get(wanted)
        if eng is None:
            raise OCRError(
                f"未知的 OCR 引擎: {settings.ocr_engine}（可用：{', '.join(engines)}）"
            )
        if not eng.available():
            raise OCRError(f"OCR 引擎 {wanted} 不可用：{eng.unavailable_reason()}")
        return eng

    # 1. 内置的本地引擎优先
    for key in LOCAL_ORDER:
        eng = engines.get(key)
        if eng is not None and eng.available():
            return eng

    # 2. 其它被注入的引擎（第三方插件、测试替身）
    for key, eng in engines.items():
        if key in LOCAL_ORDER or key == "deepseek":
            continue
        if eng.available():
            return eng

    # 3. 云端兜底
    cloud = engines.get("deepseek")
    if cloud is not None and cloud.available():
        log.info("本地 OCR 不可用，改用云 OCR")
        return cloud

    reasons = "; ".join(
        f"{k}: {e.unavailable_reason() or '不可用'}" for k, e in engines.items()
    )
    raise OCRError(
        "没有可用的 OCR 引擎。安装 `pip install 'clip2text[ocr]'`，"
        "或配置 DEEPSEEK_API_KEY。当前状态：" + reasons
    )


def ocr_statuses(settings: Settings, engines: dict[str, OCREngine] | None = None) -> list[EngineStatus]:
    engines = engines or build_ocr_engines(settings)
    out: list[EngineStatus] = []
    for key, eng in engines.items():
        ok = eng.available()
        out.append(
            EngineStatus(
                name=key,
                available=ok,
                reason=None if ok else (eng.unavailable_reason() or "不可用"),
                timestamps=False,
                detail={"supports_bbox": getattr(eng, "supports_bbox", False)},
            )
        )
    return out


def recognize_images(
    images: list[Path],
    settings: Settings,
    *,
    engines: dict[str, OCREngine] | None = None,
    on_progress: object = None,
) -> OCRResult:
    """批量识别多张图，按图片顺序合并成一个结果。

    小红书的图文笔记一次有 5-18 张图，所以这是最常见的用法。
    """
    if not images:
        raise OCRError("没有可识别的图片")

    engine = select_ocr_engine(settings, engines)
    all_lines: list[OCRLine] = []
    chunks: list[str] = []
    total = len(images)

    for i, img in enumerate(images):
        try:
            result = engine.recognize(img)
        except OCRError as exc:
            # 单张失败不该让整篇失败，记录后继续
            log.warning("第 %d 张图识别失败：%s", i + 1, exc)
            chunks.append(f"[第 {i + 1} 张图识别失败：{exc}]")
            continue
        if result.text.strip():
            chunks.append(result.text)
            all_lines.extend(result.lines)
        if callable(on_progress):
            on_progress((i + 1) / total)

    return OCRResult(
        text="\n\n".join(chunks),
        lines=all_lines,
        page=total,
        engine=engine.name,
    )
