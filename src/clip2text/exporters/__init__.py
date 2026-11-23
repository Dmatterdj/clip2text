"""导出器：把任务结果写成 txt / markdown / docx / xlsx。

设计要点：
  - 每个格式一个函数，签名统一 (task) -> bytes
  - 生成前先做文件名清洗，避免路径穿越
  - docx/xlsx 走内存流（BytesIO），不落临时文件
"""

from __future__ import annotations

import io
import re
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from ..models import Task

Format = Literal["txt", "markdown", "docx", "xlsx"]

SUPPORTED: tuple[str, ...] = ("txt", "markdown", "docx", "xlsx")

EXTENSIONS: dict[str, str] = {
    "txt": "txt",
    "markdown": "md",
    "docx": "docx",
    "xlsx": "xlsx",
}

_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

META_LABELS = [
    ("任务 ID", "id"),
    ("标题", "title"),
    ("平台", "platform"),
    ("类型", "work_type"),
    ("语言", "language"),
    ("时长", "duration"),
    ("来源", "source_value"),
    ("引擎", "engine"),
]


def safe_filename(task: Task, fmt: str) -> str:
    stem = (task.title or task.source_value or task.id).strip()
    stem = _UNSAFE.sub("_", stem)[:60].strip(" ._") or task.id
    return f"{stem}.{EXTENSIONS.get(fmt, 'txt')}"


def _fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "-"
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _timestamp(seconds: float) -> str:
    total = int(seconds)
    m, s = divmod(total, 60)
    return f"{m:02d}:{s:02d}"


def _meta_lines(task: Task) -> list[str]:
    out: list[str] = []
    for label, field in META_LABELS:
        value = getattr(task, field, None)
        if field == "duration":
            value = _fmt_duration(task.duration)
        if value in (None, ""):
            continue
        out.append(f"{label}: {value}")
    out.append(f"导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return out


# ---------------- txt ----------------
def to_txt(task: Task) -> bytes:
    lines = _meta_lines(task)
    lines.append("")
    lines.append("=" * 40)
    lines.append("")
    if task.text_content:
        lines.append(task.text_content)
    elif task.content:
        lines.append(task.content)
    else:
        lines.append("（无转写内容）")
    return ("\n".join(lines) + "\n").encode("utf-8")


# ---------------- markdown ----------------
def to_markdown(task: Task) -> bytes:
    title = task.title or f"任务 {task.id}"
    lines = [f"# {title}", ""]
    for label, field in META_LABELS:
        value = getattr(task, field, None)
        if field == "duration":
            value = _fmt_duration(task.duration)
        if value in (None, ""):
            continue
        if field == "id":
            continue
        lines.append(f"- **{label}**：{value}")
    lines.append("")

    if task.text_content:
        lines.extend(["## 转写文本", "", task.text_content, ""])

    if task.content and task.content != task.text_content:
        lines.extend(["## 原始正文", "", task.content, ""])

    if task.segments:
        lines.extend(["## 分句（带时间戳）", "", "| 时间 | 内容 |", "| --- | --- |"])
        for seg in task.segments:
            text = seg.text.replace("|", r"\|").replace("\n", " ")
            lines.append(f"| {_timestamp(seg.start)} | {text} |")
        lines.append("")

    if task.image_urls:
        lines.extend(["## 图片", ""])
        lines.extend(f"![]({u})" for u in task.image_urls)
        lines.append("")

    if task.video_urls:
        lines.extend(["## 视频链接", ""])
        lines.extend(f"- {u}" for u in task.video_urls)
        lines.append("")

    return ("\n".join(lines) + "\n").encode("utf-8")


# ---------------- docx ----------------
def to_docx(task: Task) -> bytes:
    from docx import Document
    from docx.shared import Pt

    doc = Document()

    title = task.title or f"任务 {task.id}"
    doc.add_heading(title, level=1)

    for label, field in META_LABELS:
        value = getattr(task, field, None)
        if field == "duration":
            value = _fmt_duration(task.duration)
        if value in (None, ""):
            continue
        para = doc.add_paragraph()
        para.add_run(f"{label}：").bold = True
        para.add_run(str(value))

    if task.text_content:
        doc.add_heading("转写文本", level=2)
        for para_text in task.text_content.split("\n"):
            if para_text.strip():
                doc.add_paragraph(para_text)

    if task.content and task.content != task.text_content:
        doc.add_heading("原始正文", level=2)
        doc.add_paragraph(task.content)

    if task.segments:
        doc.add_heading("分句（带时间戳）", level=2)
        table = doc.add_table(rows=1, cols=2)
        table.style = "Light Grid Accent 1"
        header = table.rows[0].cells
        header[0].text = "时间"
        header[1].text = "内容"
        for seg in task.segments:
            cells = table.add_row().cells
            cells[0].text = _timestamp(seg.start)
            cells[1].text = seg.text

    if task.video_urls:
        doc.add_heading("视频链接", level=2)
        for url in task.video_urls:
            doc.add_paragraph(url, style="List Bullet")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------- xlsx ----------------
def to_xlsx(task: Task) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()

    ws = wb.active
    ws.title = "转写结果"
    ws.append(["序号", "开始", "结束", "内容"])
    if task.segments:
        for i, seg in enumerate(task.segments, start=1):
            ws.append([i, _timestamp(seg.start), _timestamp(seg.end), seg.text])
    elif task.text_content:
        ws.append([1, "", "", task.text_content])

    for col, width in zip("ABCD", (8, 12, 12, 100), strict=False):
        ws.column_dimensions[col].width = width

    meta = wb.create_sheet("任务信息")
    meta.append(["字段", "值"])
    for label, field in META_LABELS:
        value = getattr(task, field, None)
        if field == "duration":
            value = _fmt_duration(task.duration)
        meta.append([label, "" if value is None else str(value)])
    meta.append(["导出时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
    meta.column_dimensions["A"].width = 16
    meta.column_dimensions["B"].width = 80

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


_EXPORTERS: dict[str, Callable[[Task], bytes]] = {
    "txt": to_txt,
    "markdown": to_markdown,
    "docx": to_docx,
    "xlsx": to_xlsx,
}

MEDIA_TYPES: dict[str, str] = {
    "txt": "text/plain; charset=utf-8",
    "markdown": "text/markdown; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def export(task: Task, fmt: str) -> bytes:
    fn = _EXPORTERS.get(fmt)
    if fn is None:
        raise ValueError(f"不支持的导出格式: {fmt}（可选 {', '.join(SUPPORTED)}）")
    return fn(task)
