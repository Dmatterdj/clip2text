"""LLM 文案改写：走 OpenAI 兼容协议，支持流式。

设计意图
    不绑定任何厂商。base_url + api_key + model 三件套走天下：
      · Ollama        http://localhost:11434/v1
      · vLLM          http://localhost:8000/v1
      · DeepSeek      https://api.deepseek.com/v1
      · OpenAI        https://api.openai.com/v1

    没配就整个功能隐藏，不会出现"点了说不支持"的尴尬。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


# ---------------------------------------------------------------- 用量埋点
# 为什么用模块级 sink 而不是直接调用 db：
#   改写的入口在 api/batch_routes.py（不在本次改动范围），
#   而 stream_rewrite 本身又拿不到 Database。所以在 llm 里留一个
#   「谁想记账谁注册」的钩子，由 AppState 在构造时接上。
#   钩子抛异常一律吞掉：记账失败绝不能打断正在输出的改写流。
_usage_sink: Callable[[dict[str, Any]], None] | None = None


def set_usage_sink(sink: Callable[[dict[str, Any]], None] | None) -> None:
    """注册改写用量回调（AppState 构造时调用，shutdown 时置 None）。"""
    global _usage_sink
    _usage_sink = sink


def _emit_usage(payload: dict[str, Any]) -> None:
    sink = _usage_sink
    if sink is None:
        return
    try:
        sink(payload)
    except Exception:  # noqa: BLE001 - 埋点不能影响主流程
        log.warning("改写用量埋点失败（已忽略）：model=%s", payload.get("model"))


def _merge_usage(total: dict[str, int], usage: object) -> None:
    """把上游的 usage 累计进 total。

    不同兼容实现的字段名不完全一样（prompt_tokens / input_tokens），
    都认一遍；拿不到的维度保持 0，不猜。
    """
    if not isinstance(usage, Mapping):
        return
    total["prompt_tokens"] += _as_token(usage.get("prompt_tokens", usage.get("input_tokens")))
    total["completion_tokens"] += _as_token(
        usage.get("completion_tokens", usage.get("output_tokens"))
    )


def _as_token(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and value == value:
        return max(0, int(value))
    return 0


@dataclass
class RewriteStyle:
    key: str
    label: str
    description: str
    prompt: str


# 预设风格。prompt 直接写给模型，语言跟随原文。
STYLES: dict[str, RewriteStyle] = {
    "summary": RewriteStyle(
        key="summary",
        label="提炼要点",
        description="把长文案压缩成结构化要点",
        prompt=(
            "把下面这段内容提炼成 3-7 条要点。"
            "每条一行，前面用「- 」开头。保留关键数字和结论，删掉重复与语气词。"
            "不要添加原文没有的信息。"
        ),
    ),
    "xiaohongshu": RewriteStyle(
        key="xiaohongshu",
        label="小红书风格",
        description="口语化、有情绪、带 emoji 的短段落",
        prompt=(
            "把下面这段内容改写成小红书笔记风格："
            "开头一句抓人的钩子，正文短句分段，适当用 emoji，"
            "结尾加 3-5 个相关话题标签（#开头）。"
            "保持原文事实不变，不要编造经历。控制在 400 字以内。"
        ),
    ),
    "douyin": RewriteStyle(
        key="douyin",
        label="口播脚本",
        description="适合直接念出来的短视频脚本",
        prompt=(
            "把下面这段内容改写成 60 秒短视频口播脚本："
            "开头 3 秒必须有钩子，中间用「第一/第二/第三」分点，"
            "句子短、口语化、有停顿感。不要加旁白说明，只写要念出来的词。"
        ),
    ),
    "formal": RewriteStyle(
        key="formal",
        label="书面化",
        description="去掉口语和重复，适合文档归档",
        prompt=(
            "把下面这段内容改写成书面语。"
            "去掉口头禅、语气词和重复表达，补齐指代，保持原意。"
            "不要扩写，不要加原文没有的内容。"
        ),
    ),
    "polish": RewriteStyle(
        key="polish",
        label="润色订正",
        description="只改错别字和病句，不改结构",
        prompt=(
            "给下面这段内容做润色："
            "只修正错别字、标点和语病，去掉明显的语音识别错误。"
            "不要改变结构、长度和语气，不要扩写。"
        ),
    ),
}

DEFAULT_STYLE = "summary"
MAX_INPUT_CHARS = 60_000


def style_list() -> list[dict[str, str]]:
    return [
        {"key": s.key, "label": s.label, "description": s.description}
        for s in STYLES.values()
    ]


@dataclass
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.7
    timeout: int = 180
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        # 允许用户只填到域名，自动补 /v1 与 /chat/completions
        if not base.endswith("/v1"):
            base += "/v1"
        return base + "/chat/completions"


def build_llm_config(base_url: str, api_key: str, model: str) -> LLMConfig:
    return LLMConfig(base_url=base_url.strip(), api_key=api_key.strip(), model=model.strip())


def _build_messages(text: str, style_key: str, extra_instruction: str | None) -> list[dict]:
    style = STYLES.get(style_key)
    if style is None:
        raise LLMError(
            f"未知的改写风格: {style_key}。可选：{', '.join(STYLES)}"
        )
    if len(text) > MAX_INPUT_CHARS:
        raise LLMError(
            f"文本过长（{len(text)} 字），超过 {MAX_INPUT_CHARS} 字上限。"
            "请先分段，或改用更小的模型上下文。"
        )

    system = style.prompt
    if extra_instruction:
        system += f"\n\n补充要求：{extra_instruction}"

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": text},
    ]


async def stream_rewrite(
    text: str,
    config: LLMConfig,
    style_key: str = DEFAULT_STYLE,
    extra_instruction: str | None = None,
    *,
    task_id: str | None = None,
    usage_out: dict[str, int] | None = None,
) -> AsyncIterator[str]:
    """流式改写。逐段 yield 文本增量，适合直接塞进 SSE。

    用量（契约 §7 的 rewrite）：
      · 请求里带 `stream_options.include_usage`，OpenAI 兼容服务会在最后
        一帧（`choices` 为空）带上 usage；不支持的实现会直接忽略这个字段
      · 没有 usage 帧时 tokens 记 0，**不猜**（契约原文：「否则 tokens 记 0」）
      · 生成器被提前关闭（前端断开）也会在 finally 里上报：上游已经
        计费了，用户断开连接不等于没花钱
    """
    if not config.configured:
        raise LLMError("未配置 LLM_BASE_URL / LLM_MODEL，AI 改写不可用")
    if not text.strip():
        raise LLMError("没有可改写的内容")

    payload = {
        "model": config.model,
        "messages": _build_messages(text, style_key, extra_instruction),
        "stream": True,
        # 不给这个字段，流式响应里通常完全不返回 usage，rewrite 的
        # token 就永远记 0
        "stream_options": {"include_usage": True},
        "temperature": config.temperature,
    }
    headers = {"Content-Type": "application/json", **config.extra_headers}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
    # 只有真的和上游建立了 200 响应才记账：403/401 这种请求根本没产生
    # token，记一条全 0 的 rewrite 只会让用量面板的「调用次数」虚高。
    billable = False
    try:
        async with httpx.AsyncClient(timeout=config.timeout) as client:
            async with client.stream(
                "POST", config.endpoint(), json=payload, headers=headers
            ) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "ignore")[:400]
                    raise LLMError(f"LLM 返回 {resp.status_code}: {body}")
                billable = True
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    # usage 帧：OpenAI 兼容实现的最后一帧 choices 为空，
                    # 只有 usage。先收下来，正常文本增量照旧往下传。
                    _merge_usage(usage_total, obj.get("usage"))
                    delta = _extract_delta(obj)
                    if delta:
                        yield delta
    except httpx.HTTPError as exc:
        raise LLMError(f"LLM 请求失败：{exc}") from exc
    finally:
        if usage_out is not None:
            usage_out.update(usage_total)
        if billable:
            _emit_usage(
                {
                    "task_id": task_id,
                    "model": config.model,
                    "prompt_tokens": usage_total["prompt_tokens"],
                    "completion_tokens": usage_total["completion_tokens"],
                }
            )



def _extract_delta(obj: dict) -> str:
    choices = obj.get("choices") or []
    if not choices:
        return ""
    choice = choices[0]
    # 标准 OpenAI 格式
    delta = choice.get("delta") or {}
    content = delta.get("content")
    if content:
        return str(content)
    # 某些兼容实现直接给 message.content
    message = choice.get("message") or {}
    return str(message.get("content") or "")


async def rewrite_once(
    text: str,
    config: LLMConfig,
    style_key: str = DEFAULT_STYLE,
    extra_instruction: str | None = None,
    *,
    task_id: str | None = None,
    usage_out: dict[str, int] | None = None,
) -> str:
    """非流式封装，供 CLI 和测试使用。"""
    parts: list[str] = []
    async for chunk in stream_rewrite(
        text,
        config,
        style_key,
        extra_instruction,
        task_id=task_id,
        usage_out=usage_out,
    ):
        parts.append(chunk)
    return "".join(parts)
