"""一个假的 OpenAI 兼容服务，用于本地验证 clip2text 的 AI 能力。

它不是产品的一部分，只是夹具：
  · 让「没配真实 key」的开发者和 CI 也能跑通改写与分析链路
  · 能分别模拟分析的 map / reduce 两个阶段，用于验证时间戳回填

跑法：
    python scripts/fake_llm.py --port 9911
    LLM_BASE_URL=http://127.0.0.1:9911/v1 LLM_MODEL=fake clip2text serve
"""

from __future__ import annotations

import argparse
import json
import re
import time

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI(title="fake-openai")

# ---------------------------------------------------------------- 改写风格
STYLE_MARKERS = {
    "要点": "【要点版】",
    "小红书": "【小红书版】",
    "口播": "【口播版】",
    "书面": "【书面版】",
    "润色": "【润色版】",
}

# ---------------------------------------------------------------- 分析识别
# 两种阶段的系统提示词特征不同，靠关键词区分。
# 契约要求 reduce 只能看到各窗的 title/summary/key_points，
# 所以 reduce 提示词里会出现「窗口」「window」这类词。
_MAP_HINTS = ("title", "summary", "key_points", "quotes")
_REDUCE_HINTS = ("abstract", "chapters")


def _is_analysis(system: str) -> bool:
    return "json" in system.lower() and (
        any(h in system for h in _MAP_HINTS) or any(h in system for h in _REDUCE_HINTS)
    )


def _is_reduce(system: str) -> bool:
    return all(h in system for h in _REDUCE_HINTS) and "abstract" in system


def _extract_window_index(system: str, user: str) -> int:
    """从提示词里找窗口序号，让假输出可预测、便于断言。

    契约要求 map 提示词里给出时间范围；reduce 提示词里给出窗口列表。
    """
    for text in (system, user):
        # 形如 "window 3"、"窗口 3"、"第 3 段"
        m = re.search(r"(?:window|窗口|第)\s*(\d+)", text, re.I)
        if m:
            return int(m.group(1))
    return 0


def _extract_time_range(system: str, user: str) -> tuple[float, float]:
    """从提示词里抠出时间范围（秒），用于验证后端是否正确回填。

    支持 "12:00–24:00"、"720.0-1440.0" 这类写法。
    """
    for text in (system, user):
        m = re.search(
            r"(\d+:\d{2}(?::\d{2})?)\s*[–\-~至到]\s*(\d+:\d{2}(?::\d{2})?)", text
        )
        if m:
            return _hms(m.group(1)), _hms(m.group(2))
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:秒|s)?\s*[–\-~至到]\s*(\d+(?:\.\d+)?)", text)
        if m:
            return float(m.group(1)), float(m.group(2))
    return 0.0, 0.0


def _hms(s: str) -> float:
    parts = [float(p) for p in s.split(":")]
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def _fake_map(system: str, user: str) -> dict:
    """模拟 map 阶段：只关心本窗，返回相对秒数。"""
    idx = _extract_window_index(system, user)
    start, end = _extract_time_range(system, user)
    span = max(1.0, end - start)

    head = user.strip().replace("\n", " ")[:30] or "本段内容"

    return {
        "title": f"第 {idx + 1} 部分：{head}",
        "summary": f"这一段围绕「{head}」展开，假服务生成，用于验证分窗与合并链路。",
        "key_points": [f"要点 A（窗 {idx}）", f"要点 B（窗 {idx}）"],
        # 故意返回相对秒数：后端必须自己加上窗口起点
        "quotes": [
            {"text": f"这是第 {idx} 窗里的一句原话。", "start": round(span * 0.25, 2)},
            {"text": f"这是第 {idx} 窗里的另一句原话。", "start": round(span * 0.75, 2)},
        ],
    }


def _fake_reduce(system: str, user: str) -> dict:
    """模拟 reduce 阶段：拿到各窗摘要，产出整体结构。

    chapters[].window 指向窗口序号，后端据此回填真实 start/end。
    """
    # 从 user 里数出有多少个窗口（形如 "窗口 0"、"窗口 1"…）
    windows = sorted({int(m) for m in re.findall(r"(?:window|窗口)\s*(\d+)", user, re.I)})
    if not windows:
        windows = [0]

    return {
        "abstract": (
            "这是假服务生成的全文摘要，用于验证 reduce 阶段与前端渲染。"
            f"共 {len(windows)} 个窗口参与合并。"
        ),
        "chapters": [
            {
                "window": w,
                "title": f"章节 {i + 1}",
                "summary": f"第 {w} 窗合并后的小结。",
            }
            for i, w in enumerate(windows)
        ],
        "key_points": [f"全局要点 {i + 1}" for i in range(min(3, len(windows) + 1))],
        # reduce 里故意给 quotes：契约要求后端忽略它，只用 map 的
        "quotes": [{"text": "这是 reduce 塞进来的假金句，不该出现在结果里。", "start": 0}],
        "entities": ["假实体A", "假实体B"],
    }


@app.post("/v1/chat/completions")
async def chat(payload: dict):
    messages = payload.get("messages") or []
    system = messages[0]["content"] if messages else ""
    user = messages[1]["content"] if len(messages) > 1 else ""

    # ---------- 分析 ----------
    if _is_analysis(system):
        data = _fake_reduce(system, user) if _is_reduce(system) else _fake_map(system, user)
        body = json.dumps(data, ensure_ascii=False)
        usage = {"prompt_tokens": len(user) // 2, "completion_tokens": len(body) // 2}

        if not payload.get("stream"):
            return {
                "id": "fake",
                "object": "chat.completion",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": body}}
                ],
                "usage": usage,
            }

        async def gen_json():
            yield f"data: {json.dumps({'choices': [{'delta': {'content': body}}]}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen_json(), media_type="text/event-stream")

    # ---------- 改写 ----------
    marker = next((v for k, v in STYLE_MARKERS.items() if k in system), "【改写】")
    text = f"{marker}{user[:60]}…（由假服务生成，共 {len(user)} 字原文本）"

    if not payload.get("stream"):
        return {
            "id": "fake",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
        }

    async def gen():
        for ch in text:
            chunk = {
                "id": "fake",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"content": ch}}],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            time.sleep(0.01)
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9911)
    args = ap.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
