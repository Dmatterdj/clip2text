"""批量任务、改写与 OCR 相关的路由。

拆成独立文件是为了让 routes.py 保持精简；两者都挂在 /api/v1 下。
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from ..batch import gather_inputs, resolve_upload_paths
from ..llm import DEFAULT_STYLE, STYLES, LLMError, stream_rewrite, style_list
from ..state import get_state

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------- 批量
@router.post("/batches", summary="创建批量任务", status_code=201)
async def create_batch(payload: dict[str, object]) -> dict[str, object]:
    st = get_state()
    settings = st.settings

    source_type = str(payload.get("source_type") or "url").lower()
    if source_type not in {"url", "upload"}:
        raise HTTPException(400, "source_type 只能是 url 或 upload")

    if source_type == "url" and not settings.extractors_enabled:
        raise HTTPException(
            400,
            "链接解析默认关闭。设置 EXTRACTORS_ENABLED=true 后重启服务，"
            "并确认仅用于处理你拥有版权的内容。",
        )

    raw_items = payload.get("items") or []
    if not isinstance(raw_items, list):
        raise HTTPException(400, "items 必须是数组")
    text = payload.get("text")
    items = gather_inputs([str(i) for i in raw_items], str(text) if text else None)

    if not items:
        raise HTTPException(400, "没有提交任何条目")

    if source_type == "upload":
        try:
            items = resolve_upload_paths(items, settings.uploads_dir)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    try:
        batch = await st.batches.create(
            source_type=source_type,
            items=items,
            title=(str(payload["title"]) if payload.get("title") else None),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    asyncio.create_task(_deferred_start(batch.id))

    return batch.model_dump(mode="json")


async def _deferred_start(batch_id: str) -> None:
    await asyncio.sleep(0)
    get_state().batches.start(batch_id)


@router.get("/batches", summary="批量列表")
def list_batches(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, object]:
    items, total = get_state().db.list_batches(limit=limit, offset=offset)
    return {
        "items": [b.model_dump(mode="json") for b in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/batches/{batch_id}", summary="批量详情（含子任务）")
async def get_batch(batch_id: str) -> dict[str, object]:
    st = get_state()
    batch = await st.batches.sync(batch_id)
    if batch is None:
        raise HTTPException(404, f"批量不存在: {batch_id}")

    tasks = []
    for tid in batch.task_ids:
        task = st.db.get(tid)
        if task is not None:
            tasks.append(task.model_dump(mode="json"))

    return {
        "batch": batch.model_dump(mode="json"),
        "tasks": tasks,
        "progress": batch.progress,
    }


@router.post("/batches/{batch_id}/cancel", summary="取消批量")
def cancel_batch(batch_id: str) -> dict[str, object]:
    st = get_state()
    batch = st.db.get_batch(batch_id)
    if batch is None:
        raise HTTPException(404, f"批量不存在: {batch_id}")
    if batch.status.terminal:
        raise HTTPException(409, f"批量已结束（{batch.status.value}）")
    st.batches.cancel(batch_id)
    return {"canceled": True, "batch_id": batch_id}


@router.delete("/batches/{batch_id}", summary="删除批量及其子任务", status_code=204)
async def delete_batch(batch_id: str) -> Response:
    ok = await get_state().batches.delete(batch_id)
    if not ok:
        raise HTTPException(404, f"批量不存在: {batch_id}")
    return Response(status_code=204)


@router.get("/batches/{batch_id}/events", summary="批量 SSE 进度流")
async def batch_events(batch_id: str, request: Request) -> StreamingResponse:
    st = get_state()
    if st.db.get_batch(batch_id) is None:
        raise HTTPException(404, f"批量不存在: {batch_id}")

    async def gen():
        last: tuple[str, int] | None = None
        idle = 0
        while True:
            if await request.is_disconnected():
                break
            batch = await st.batches.sync(batch_id)
            if batch is None:
                yield _sse("error", {"detail": "批量已被删除"})
                break
            sig = (batch.status.value, batch.done)
            if sig != last:
                last = sig
                idle = 0
                yield _sse(
                    "batch",
                    {
                        "id": batch.id,
                        "status": batch.status.value,
                        "total": batch.total,
                        "succeeded": batch.succeeded,
                        "failed": batch.failed,
                        "canceled": batch.canceled,
                        "progress": batch.progress,
                    },
                )
            else:
                idle += 1
            if batch.status.terminal:
                yield _sse("done", {"status": batch.status.value})
                break
            if idle and idle % 20 == 0:
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------- 改写
@router.get("/rewrite/styles", summary="可用的改写风格")
def rewrite_styles() -> dict[str, object]:
    st = get_state()
    return {
        "available": st.llm.configured,
        "styles": style_list(),
        "default": DEFAULT_STYLE,
        "reason": None if st.llm.configured else "未配置 LLM_BASE_URL / LLM_MODEL",
    }


@router.post("/rewrite", summary="流式改写（SSE）")
async def rewrite(payload: dict[str, object]) -> StreamingResponse:
    st = get_state()
    if not st.llm.configured:
        raise HTTPException(
            400,
            "AI 改写未启用。请在 .env 配置 LLM_BASE_URL、LLM_API_KEY、LLM_MODEL，"
            "可指向 Ollama 等本地服务。",
        )

    style = str(payload.get("style") or DEFAULT_STYLE)
    if style not in STYLES:
        raise HTTPException(
            400, f"未知风格 {style}，可选：{', '.join(STYLES)}"
        )

    text = str(payload.get("text") or "").strip()
    task_id = payload.get("task_id")

    if not text and task_id:
        task = st.db.get(str(task_id))
        if task is None:
            raise HTTPException(404, f"任务不存在: {task_id}")
        text = (task.text_content or task.content or "").strip()

    if not text:
        raise HTTPException(400, "没有可改写的内容（text 与 task_id 至少给一个）")

    extra = payload.get("instruction")
    extra_str = str(extra) if extra else None

    async def gen():
        try:
            async for delta in stream_rewrite(text, st.llm, style, extra_str):
                yield _sse("delta", {"text": delta})
            yield _sse("done", {"style": style})
        except LLMError as exc:
            yield _sse("error", {"detail": str(exc)})
        except Exception as exc:  # noqa: BLE001
            log.exception("改写失败")
            yield _sse("error", {"detail": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
