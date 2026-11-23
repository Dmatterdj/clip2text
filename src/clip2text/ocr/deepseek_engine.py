"""DeepSeek 云 OCR：不想装本地模型时用，按页计费。

官方限制（必须遵守）：
  - 支持 PDF / DOC / DOCX / PPT / PPTX / PNG / JPEG / WebP / GIF / BMP / TIFF
  - 文件 ≤ 100 MiB，≤ 1000 页，总时长 ≤ 180 秒
  - purpose=text_recognition：Word/PPT 且无图时直接抽文本
  - purpose=rich_layout_recognition：一律逐页识别，带坐标

返回的文本里带 <｜box｜>[[x1,y1,x2,y2]]</｜box｜> 标记，坐标归一化到 0-1000。
本引擎会把标记剥掉，只保留纯文本（坐标解析不稳定，且在图片场景价值有限）。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import httpx

from .base import BaseOCREngine, OCRError, OCRLine, OCRResult

log = logging.getLogger(__name__)

MAX_BYTES = 100 * 1024 * 1024
MAX_TIMEOUT = 300

# <｜ref｜>文字<｜/ref｜><｜box｜>[[52, 40, 948, 96]]<｜/box｜>
_MARKUP_RE = re.compile(r"<｜[^｜]*｜>|<\|[^|]*\|>")


class DeepSeekOCREngine(BaseOCREngine):
    name = "deepseek"
    supports_bbox = False  # 我们剥掉了 box 标记，对外不承诺坐标

    def __init__(self, api_key: str = "", base_url: str = "https://api.deepseek.com") -> None:
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")

    def available(self) -> bool:
        return bool(self.api_key)

    def unavailable_reason(self) -> str | None:
        return None if self.api_key else "未配置 DEEPSEEK_API_KEY"

    def recognize(self, image: Path) -> OCRResult:
        if not self.available():
            raise OCRError(self.unavailable_reason() or "DeepSeek OCR 不可用")
        if not image.exists():
            raise OCRError(f"文件不存在: {image}")
        if image.stat().st_size > MAX_BYTES:
            raise OCRError("文件超过 DeepSeek 的 100MB 上限")

        url = f"{self.base_url}/v1/tools/ocr"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            with image.open("rb") as fh:
                files = {"file": (image.name, fh)}
                data = {"purpose": "text_recognition"}
                resp = httpx.post(
                    url, headers=headers, files=files, data=data, timeout=MAX_TIMEOUT
                )
        except httpx.HTTPError as exc:
            raise OCRError(f"DeepSeek OCR 请求失败：{exc}") from exc

        if resp.status_code != 200:
            raise OCRError(f"DeepSeek OCR 返回 {resp.status_code}: {resp.text[:300]}")

        body = resp.json()
        if "error" in body:
            raise OCRError(f"DeepSeek OCR 失败：{body['error'].get('message')}")

        pages = body.get("pages") or []
        if not pages:
            return OCRResult(text="", lines=[], engine=self.name)

        # 多页（PDF）时逐页拼起来，页间用空行分隔
        all_lines: list[OCRLine] = []
        chunks: list[str] = []
        for idx, page in enumerate(pages):
            cleaned = _strip_markup(page.get("text") or "")
            if not cleaned.strip():
                continue
            chunks.append(cleaned)
            for text_line in cleaned.splitlines():
                if text_line.strip():
                    all_lines.append(OCRLine(text=text_line.strip()))
            log.debug("OCR 第 %d 页：%d 字", idx, len(cleaned))

        return OCRResult(
            text="\n\n".join(chunks),
            lines=all_lines,
            page=len(pages),
            engine=self.name,
        )


def _strip_markup(text: str) -> str:
    """去掉 <｜ref｜> / <｜box｜> 之类的特殊标记，只留正文。"""
    return _MARKUP_RE.sub("", text)
