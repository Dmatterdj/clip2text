"""RapidOCR 引擎：本地 CPU 推理，不联网、不花钱。

约束：rapidocr-onnxruntime 要求 Python <3.13，所以 pyproject 把
requires-python 锁在 <3.13。
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..models import utcnow  # noqa: F401  (保持与其它模块一致的导入风格)
from .base import BaseOCREngine, OCRError, OCRLine, OCRResult, normalize_box

log = logging.getLogger(__name__)

_ENGINE_CACHE: dict[str, object] = {}


class RapidOCREngine(BaseOCREngine):
    name = "rapidocr"
    supports_bbox = True

    def __init__(self) -> None:
        self._import_error: str | None = None
        try:
            import rapidocr_onnxruntime  # noqa: F401
        except ImportError as exc:
            self._import_error = (
                f"未安装 rapidocr-onnxruntime：{exc}。"
                "运行 `pip install 'clip2text[ocr]'`（需 Python <3.13）"
            )

    def available(self) -> bool:
        return self._import_error is None

    def unavailable_reason(self) -> str | None:
        return self._import_error

    def _engine(self):
        if "rapidocr" not in _ENGINE_CACHE:
            from rapidocr_onnxruntime import RapidOCR

            log.info("初始化 RapidOCR（首次会加载模型，约 1-2 秒）")
            _ENGINE_CACHE["rapidocr"] = RapidOCR()
        return _ENGINE_CACHE["rapidocr"]

    def recognize(self, image: Path) -> OCRResult:
        if not self.available():
            raise OCRError(self.unavailable_reason() or "RapidOCR 不可用")
        if not image.exists():
            raise OCRError(f"图片不存在: {image}")

        try:
            engine = self._engine()
            # RapidOCR 返回 (result, elapsed)；result 为 None 表示没检测到文字
            raw, _ = engine(str(image))
        except Exception as exc:  # noqa: BLE001 - onnxruntime 异常类型不稳定
            raise OCRError(f"RapidOCR 识别失败：{exc}") from exc

        if not raw:
            return OCRResult(text="", lines=[], engine=self.name)

        width, height = _image_size(image)
        lines: list[OCRLine] = []
        for item in raw:
            # RapidOCR 每条： [box(4 点), text, score]
            if len(item) < 3:
                continue
            box_pts, text, score = item[0], item[1], item[2]
            box = _box_from_points(box_pts, width, height)
            lines.append(
                OCRLine(text=str(text), box=box, confidence=float(score))
            )

        # 按阅读顺序排序：先上后下，同一行内先左后右
        lines.sort(key=lambda ln: (ln.box[1] if ln.box else 0, ln.box[0] if ln.box else 0))
        return OCRResult(text="", lines=lines, engine=self.name).dedupe_blank()


def _image_size(image: Path) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(image) as im:
            return im.width, im.height
    except Exception:  # noqa: BLE001 - 缺 Pillow 或图片损坏
        log.debug("读取图片尺寸失败，坐标将不做归一化")
        return 0, 0


def _box_from_points(points: object, width: int, height: int) -> list[int] | None:
    """RapidOCR 给 4 个角点，取外接矩形再归一化。"""
    try:
        pts = list(points)  # type: ignore[arg-type]
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
    except (TypeError, ValueError, IndexError):
        return None
    if not xs or not ys:
        return None
    return normalize_box(min(xs), min(ys), max(xs), max(ys), width, height)
