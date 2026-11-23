"""内容解析（长文本结构化总结）的 HTTP 接口。

与其它路由的差别
    分析要花真钱、跑很久，所以：
      · 已有 SUCCESS 结果且 force=false 时**不走 SSE**，直接返回 JSON
        （前端应先 GET 探一下，避免重复烧 token——契约 §5.1）
      · 分析脱离请求生命周期跑，前端断开不会丢掉已经付费的中间结果
      · 进度用 SSE 推，格式与任务进度流一致（event + data）

契约：docs/ANALYSIS-CONTRACT.md
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..analysis import build_job
from ..models import Task
from ..state import get_state

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


def _sse(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _has_text(task: Task) -> bool:
    if (task.text_content or "").strip() or (task.content or "").strip():
        return True
    return any(s.text and s.text.strip() for s in task.segments)


async def _load_task(task_id: str) -> Task:
    task = await asyncio.to_thread(get_state().db.get, task_id)
    if task is None:
        raise HTTPException(404, f"任务不存在: {task_id}")
    return task


# ---------------------------------------------------------------- 读取结果
@router.get("/tasks/{task_id}/analysis", summary="读取已有解析结果")
async def get_analysis(task_id: str) -> dict[str, object]:
    st = get_state()
    analysis = await asyncio.to_thread(st.db.get_analysis, task_id)
    if analysis is None:
        raise HTTPException(404, "还没有分析结果")
    return analysis.model_dump(mode="json")


# ---------------------------------------------------------------- 发起分析
@router.post(
    "/tasks/{task_id}/analyze",
    summary="发起内容解析（SSE）",
    # 返回体可能是 SSE 流，也可能是「已有结果」的普通 JSON；
    # 联合类型不能当 response_model，显式关掉自动生成。
    response_model=None,
)
async def analyze_task(
    task_id: str,
    request: Request,
    payload: dict[str, object] | None = None,
) -> StreamingResponse | dict[str, object]:
    st = get_state()
    llm = st.analysis_config()
    if not llm.configured or llm.config is None:
        # 没配模型时把「怎么配」说清楚，而不是丢一句「不可用」
        raise HTTPException(400, llm.reason or "未配置分析模型")

    task = await _load_task(task_id)
    if not _has_text(task):
        raise HTTPException(
            400, "任务没有可解析的文本内容（转写还没完成，或这个任务没有文字结果）"
        )

    force = bool((payload or {}).get("force"))

    existing = await asyncio.to_thread(st.db.get_analysis, task_id)
    if existing is not None and existing.state == "SUCCESS" and not force:
        # 已有结果就不重新生成：一次长内容解析可能花掉几万 token
        return existing.model_dump(mode="json")

    if force:
        # 重新解析时放弃旧的那次，避免两个分析同时写同一行
        await st.analyses.cancel(task_id)

    # 先订阅再启动：否则第一帧 progress 可能早于订阅到达而丢失
    queue = st.analyses.subscribe(task_id)
    started = st.analyses.ensure_started(
        task_id,
        build_job(task=task, config=llm.config, db=st.db, runs=st.analyses),
    )
    if not started:
        log.info("任务 %s 已有分析在进行，本次只订阅进度", task_id)

    async def gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    # 没事件时确认一下是不是已经结束了（订阅晚于完成的竞态）
                    if not st.analyses.running(task_id):
                        final = await asyncio.to_thread(st.db.get_analysis, task_id)
                        if final is not None:
                            if final.state == "SUCCESS":
                                yield _sse(
                                    "result", final.model_dump(mode="json")
                                )
                            else:
                                yield _sse(
                                    "error",
                                    {"detail": final.error or "分析失败"},
                                )
                        else:
                            yield _sse("error", {"detail": "分析已结束，但没有结果"})
                        break
                    yield ": keep-alive\n\n"
                    continue

                kind = str(event.get("type") or "")
                data = event.get("data")
                if kind == "progress":
                    yield _sse("progress", data)
                elif kind == "result":
                    yield _sse("result", data)
                    break
                elif kind == "error":
                    yield _sse("error", data)
                    break
        finally:
            st.analyses.unsubscribe(task_id, queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


__all__ = ["router"]
