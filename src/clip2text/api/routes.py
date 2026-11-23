"""HTTP API 路由。

约定：
  · 所有成功的响应都直接返回数据（不套 {code,msg,data}）
  · 错误统一由异常处理器转成 {"detail": "..."}，HTTP 状态码表达语义
  · 长任务一律异步：创建即返回 QUEUED，进度走 SSE
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse

from .. import __version__
from ..asr import engine_statuses
from ..config import Settings
from ..exporters import MEDIA_TYPES, SUPPORTED, export, safe_filename
from ..extractors import platform_catalog, platform_summary
from ..llm import style_list
from ..media import get_resolve_error, resolve_ffmpeg
from ..models import Task, TaskStatus
from ..ocr import ocr_statuses
from ..pipeline import AUDIO_EXT, IMAGE_EXT, VIDEO_EXT, detect_work_type
from ..state import get_state

if TYPE_CHECKING:
    from ..state import AppState

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


def _settings() -> Settings:
    return get_state().settings


def _analysis_capability(st: AppState) -> dict[str, object]:
    """内容解析能力上报。

    与 rewrite 的区别：分析允许复用 DEEPSEEK_API_KEY 兜底，
    所以「没有 LLM_* 也能 available=true」是正常的；
    reason 里必须说清当前用的是哪个来源，否则用户会以为配置没生效。
    """
    llm = st.analysis_config()
    return {
        "available": llm.configured,
        "reason": llm.reason,
        "source": llm.source,
        "model": llm.config.model if llm.config else None,
    }


# ---------------------------------------------------------------- capabilities
@router.get("/capabilities", summary="本机可用能力清单")
def capabilities() -> dict[str, object]:
    """前端据此决定渲染什么、禁用什么、提示什么。"""
    st = get_state()
    settings = st.settings
    ff = resolve_ffmpeg(settings.ffmpeg_mode)

    asr = engine_statuses(settings, st.pipeline.engines())
    ocr = ocr_statuses(settings)

    local_asr = [e for e in asr if e.available and e.name != "deepseek"]
    return {
        "version": __version__,
        "ffmpeg": {
            "available": ff is not None,
            "source": ff.source if ff else None,
            "path": ff.ffmpeg if ff else None,
            "reason": None if ff else get_resolve_error(),
        },
        "asr": [e.model_dump(mode="json") for e in asr],
        "asr_active": local_asr[0].name if local_asr else None,
        "has_timestamps": any(e.timestamps for e in local_asr),
        "ocr": [e.model_dump(mode="json") for e in ocr],
        "llm": {
            "available": bool(settings.llm_base_url and settings.llm_model),
            "reason": None
            if (settings.llm_base_url and settings.llm_model)
            else "未配置 LLM_BASE_URL / LLM_MODEL",
        },
        "extractors": {
            "enabled": settings.extractors_enabled,
            "platforms": platform_summary() if settings.extractors_enabled else [],
            # 产品视角的「用户能贴哪些链接」清单：每项带 name / label /
            # domains / available / via / kind。前端直接拿来渲染平台标签。
            # platform_summary() 是实现视角（哪个适配器在工作），两者不冲突：
            # 前者回答「用户能贴什么」，后者回答「解析器是否就绪」。
            "catalog": platform_catalog() if settings.extractors_enabled else [],
            "reason": None
            if settings.extractors_enabled
            else "已被显式关闭（EXTRACTORS_ENABLED=false），设回 true 即可恢复",
        },
        "rewrite": {
            "available": st.llm.configured,
            "styles": [s["key"] for s in style_list()],
        },
        "analysis": _analysis_capability(st),
        "exporters": list(SUPPORTED),
        "limits": {
            "max_upload_mb": settings.max_upload_mb,
            "max_batch_size": settings.max_batch_size,
            "max_concurrency": settings.max_concurrency,
            "chunk_max_sec": settings.chunk_max_sec,
            # 长内容护栏：0 表示不限制（前端可以据此决定要不要提示用户）
            "max_media_duration_min": settings.max_media_duration_min,
            "max_media_size_mb": settings.max_media_size_mb,
            "chunk_retry_max": settings.chunk_retry_max,
            "resume_enabled": settings.resume_enabled,
        },
    }


@router.get("/health", summary="健康检查")
def health() -> dict[str, object]:
    st = get_state()
    return {
        "status": "ok",
        "tasks": st.db.count_by_status(),
    }


# ---------------------------------------------------------------- uploads
ALLOWED_EXT = AUDIO_EXT | VIDEO_EXT | IMAGE_EXT


@router.post("/uploads", summary="上传文件")
async def upload(file: Annotated[UploadFile, File(description="音视频或图片文件")]) -> dict[str, object]:
    st = get_state()
    settings = st.settings

    if not file.filename:
        raise HTTPException(400, "缺少文件名")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(
            400,
            f"不支持的文件类型 {ext or '(无扩展名)'}。支持：{', '.join(sorted(ALLOWED_EXT))}",
        )

    upload_id = uuid.uuid4().hex
    dst_dir = settings.uploads_dir / upload_id
    dst_dir.mkdir(parents=True, exist_ok=True)

    # 文件名清洗，避免路径穿越
    safe_name = Path(file.filename).name.replace("/", "_").replace("\\", "_")
    dst = dst_dir / safe_name

    size = 0
    try:
        with dst.open("wb") as fh:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(
                        413, f"文件超过 {settings.max_upload_mb}MB 上限"
                    )
                fh.write(chunk)
    except HTTPException:
        shutil.rmtree(dst_dir, ignore_errors=True)
        raise
    finally:
        await file.close()

    if size == 0:
        shutil.rmtree(dst_dir, ignore_errors=True)
        raise HTTPException(400, "文件为空")

    return {
        "upload_id": upload_id,
        "filename": safe_name,
        "size": size,
        "path": str(dst),
        "work_type": detect_work_type(dst),
    }


# ---------------------------------------------------------------- tasks
def _task_or_404(task_id: str) -> Task:
    task = get_state().db.get(task_id)
    if task is None:
        raise HTTPException(404, f"任务不存在: {task_id}")
    return task


@router.post("/tasks", summary="创建任务", status_code=201)
async def create_task(payload: dict[str, object]) -> dict[str, object]:
    """两种来源：
    · url：   source_value 传作品链接（默认开启，可用 EXTRACTORS_ENABLED=false 关闭）
    · upload：source_value 传 /uploads 返回的 path

    必须是 async def：同步 def 会被 FastAPI 丢进线程池，
    而线程池里没有事件循环，无法调度后台任务。
    """
    st = get_state()
    settings = st.settings

    source_type = str(payload.get("source_type") or "url").lower()
    if source_type not in {"upload", "url"}:
        raise HTTPException(400, "source_type 只能是 upload 或 url")

    source_value = str(payload.get("source_value") or "").strip()
    if not source_value:
        raise HTTPException(400, "source_value 不能为空")

    if source_type == "url" and not settings.extractors_enabled:
        raise HTTPException(
            400,
            "链接解析已被显式关闭（EXTRACTORS_ENABLED=false）。"
            "设回 true 后重启服务即可恢复，并确认仅用于处理你拥有版权的内容。",
        )

    if source_type == "upload":
        p = Path(source_value)
        if not p.exists():
            raise HTTPException(400, f"找不到上传文件: {source_value}")
        # 只允许处理 uploads 目录下的文件，防止任意路径读取
        try:
            p.resolve().relative_to(settings.uploads_dir.resolve())
        except ValueError:
            raise HTTPException(400, "source_value 必须是 /uploads 返回的路径") from None

    work_type = payload.get("work_type")
    task = await asyncio.to_thread(
        st.pipeline.create_task,
        source_type=source_type,
        source_value=source_value,
        title=(str(payload["title"]) if payload.get("title") else None),
        work_type=str(work_type) if work_type else None,
    )

    # 先让创建请求返回，再在后台跑流水线
    asyncio.create_task(_deferred_start(task.id))

    return task.model_dump(mode="json")


async def _deferred_start(task_id: str) -> None:
    """让创建请求先返回，再开始跑流水线。"""
    await asyncio.sleep(0)
    get_state().pipeline.start(task_id)


@router.get("/tasks", summary="任务列表")
def list_tasks(
    status: str | None = Query(None, description="按状态过滤"),
    q: str | None = Query(None, description="搜索标题/文案/来源"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, object]:
    st = get_state()
    parsed: TaskStatus | None = None
    if status:
        try:
            parsed = TaskStatus(status.upper())
        except ValueError:
            raise HTTPException(400, f"未知状态: {status}") from None
    items, total = st.db.list(status=parsed, limit=limit, offset=offset, query=q)
    return {
        "items": [t.model_dump(mode="json") for t in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/tasks/{task_id}", summary="任务详情")
def get_task(task_id: str) -> dict[str, object]:
    return _task_or_404(task_id).model_dump(mode="json")


@router.delete("/tasks/{task_id}", summary="删除任务", status_code=204)
def delete_task(task_id: str) -> Response:
    st = get_state()
    if not st.db.delete(task_id):
        raise HTTPException(404, f"任务不存在: {task_id}")
    return Response(status_code=204)


@router.post("/tasks/{task_id}/cancel", summary="取消任务")
def cancel_task(task_id: str) -> dict[str, object]:
    st = get_state()
    task = _task_or_404(task_id)
    if task.status.terminal:
        raise HTTPException(409, f"任务已结束（{task.status.value}），无法取消")
    st.pipeline.cancel(task_id)
    return {"canceled": True, "task_id": task_id}


@router.get("/tasks/{task_id}/events", summary="SSE 进度流")
async def task_events(task_id: str, request: Request) -> StreamingResponse:
    """替代前端 3 秒轮询。任务进入终态后自动关闭连接。"""
    st = get_state()
    _task_or_404(task_id)

    async def gen():
        last_sig: tuple[str, int] | None = None
        idle = 0
        while True:
            if await request.is_disconnected():
                break
            task = st.db.get(task_id)
            if task is None:
                yield _sse("error", {"detail": "任务已被删除"})
                break
            sig = (task.status.value, task.progress)
            if sig != last_sig:
                last_sig = sig
                idle = 0
                yield _sse("task", task.model_dump(mode="json"))
            else:
                idle += 1
            if task.status.terminal:
                yield _sse("done", {"status": task.status.value})
                break
            if idle and idle % 20 == 0:
                # 心跳，防止中间代理断开空闲连接
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------- export
@router.post("/tasks/{task_id}/export", summary="导出任务结果")
def export_task(task_id: str, payload: dict[str, object] | None = None) -> Response:
    _ = get_state()  # 确保 AppState 已初始化
    task = _task_or_404(task_id)
    fmt = str((payload or {}).get("format") or "txt").lower()
    if fmt not in SUPPORTED:
        raise HTTPException(400, f"不支持的格式 {fmt}，可选：{', '.join(SUPPORTED)}")

    if not task.has_payload():
        raise HTTPException(409, "任务还没有可导出的内容")

    try:
        data = export(task, fmt)
    except Exception as exc:  # noqa: BLE001
        log.exception("导出失败")
        raise HTTPException(500, f"导出失败: {exc}") from exc

    filename = safe_filename(task, fmt)
    return Response(
        content=data,
        media_type=MEDIA_TYPES[fmt],
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{_quote(filename)}"
        },
    )


def _quote(s: str) -> str:
    from urllib.parse import quote

    return quote(s, safe="")
