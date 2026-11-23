"""一个假的 OpenAI 兼容服务，仅用于本地验证 clip2text 的流式改写。

它不是产品的一部分，只是为了让"没配 key 也能测通改写链路"成为可能。
跑法：
    python scripts/fake_llm.py --port 9911
    LLM_BASE_URL=http://127.0.0.1:9911/v1 LLM_MODEL=fake clip2text serve
"""

from __future__ import annotations

import argparse
import json
import time

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI(title="fake-openai")

# 把系统提示词映射成可见的"改写结果"，方便肉眼确认风格参数生效了
STYLE_MARKERS = {
    "要点": "【要点版】",
    "小红书": "【小红书版】",
    "口播": "【口播版】",
    "书面": "【书面版】",
    "润色": "【润色版】",
}


@app.post("/v1/chat/completions")
async def chat(payload: dict):
    messages = payload.get("messages") or []
    system = messages[0]["content"] if messages else ""
    user = messages[1]["content"] if len(messages) > 1 else ""

    marker = next((v for k, v in STYLE_MARKERS.items() if k in system), "【改写】")
    body = f"{marker}{user[:60]}…（由假服务生成，共 {len(user)} 字原文本）"

    if not payload.get("stream"):
        return {
            "id": "fake",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": body}}],
        }

    async def gen():
        # 按字符切片，模拟真实的流式增量
        for ch in body:
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
