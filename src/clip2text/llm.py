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
from pydantic import BaseModel, Field

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

    def api_base(self) -> str:
        """归一化到 `.../v1`。

        用户可能填 `https://api.deepseek.com`、`https://api.deepseek.com/v1`
        或 `https://api.deepseek.com/v1/` —— 三种都要得到同一个结果，
        否则拼出来的 URL 会变成 /v1/v1/... 而 404。
        """
        base = self.base_url.rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        return base

    def endpoint(self, path: str = "/chat/completions") -> str:
        return self.api_base() + path

    def models_endpoint(self) -> str:
        return self.endpoint("/models")


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


# ---------------------------------------------------------------- 模型探测
class ModelInfo(BaseModel):
    """`GET /v1/models` 里一条模型记录。

    只保留各家都可能给出的字段，其余原样放进 meta 供前端展示。
    """

    id: str
    name: str = ""
    context_window: int | None = None
    max_output_tokens: int | None = None
    input_modalities: list[str] = Field(default_factory=list)
    # 是不是对话模型。有些服务会把 embedding / rerank 混在同一个列表里，
    # 选错了会在调用时报 400，所以提前标出来。
    chat_capable: bool = True
    meta: dict = Field(default_factory=dict)


# 名字里带这些词的通常是向量/重排模型，不能用来聊天
_NON_CHAT_HINTS = ("embed", "embedding", "rerank", "moderation", "whisper", "tts")


def _looks_chat_capable(model_id: str, raw: dict) -> bool:
    lower = (model_id or "").lower()
    if any(h in lower for h in _NON_CHAT_HINTS):
        return False
    # DeepSeek 等会在 output_modalities 里标；没有就按名字判断
    outs = raw.get("output_modalities")
    if isinstance(outs, list) and outs and "text" not in outs:
        return False
    return True


def list_models(
    base_url: str, api_key: str, *, timeout: int = 30
) -> list[ModelInfo]:
    """列出这条 key 能用的模型。

    这是 OpenAI 兼容协议的标准端点（DeepSeek / Ollama / vLLM / one-api
    这类网关都支持）。它需要一个**有效**的 key —— 传错 key 会拿到 401，
    所以这个调用本身也兼作连通性与鉴权的验证。

    兼容三种常见返回形状：
      1. {"data": [{"id": ...}, ...]}      ← OpenAI 标准
      2. [{"id": ...}, ...]                ← 少数网关直接给数组
      3. {"models": [{"name": ...}, ...]}  ← Ollama 原生格式
    """
    cfg = LLMConfig(base_url=base_url.strip(), api_key=api_key.strip(), model="")
    if not cfg.base_url:
        raise LLMError("请先填写 Base URL")

    headers: dict[str, str] = {}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"

    try:
        resp = httpx.get(cfg.models_endpoint(), headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        raise LLMError(f"连不上 {cfg.models_endpoint()}：{exc}") from exc

    if resp.status_code == 401 or resp.status_code == 403:
        raise LLMError("鉴权失败，请检查 API Key 是否正确、是否有该服务的权限")
    if resp.status_code == 404:
        raise LLMError(
            f"{cfg.models_endpoint()} 不存在。这个服务可能不支持 /v1/models，"
            "请手工填写模型名。"
        )
    if resp.status_code != 200:
        body = resp.text[:300]
        raise LLMError(f"取模型列表失败（HTTP {resp.status_code}）：{_scrub(body, cfg.api_key)}")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise LLMError("模型列表不是合法 JSON") from exc

    entries = _extract_model_entries(payload)
    out: list[ModelInfo] = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        mid = str(raw.get("id") or raw.get("name") or raw.get("model") or "").strip()
        if not mid:
            continue
        ctx = raw.get("context_window") or raw.get("context_length")
        out.append(
            ModelInfo(
                id=mid,
                name=str(raw.get("name") or raw.get("display_name") or ""),
                context_window=int(ctx) if isinstance(ctx, int | float) else None,
                max_output_tokens=(
                    int(raw["max_output_tokens"])
                    if isinstance(raw.get("max_output_tokens"), int | float)
                    else None
                ),
                input_modalities=[
                    str(x) for x in (raw.get("input_modalities") or []) if isinstance(x, str)
                ],
                chat_capable=_looks_chat_capable(mid, raw),
                meta={
                    k: v
                    for k, v in raw.items()
                    if k not in {"id", "name", "display_name", "context_window", "context_length"}
                    and not isinstance(v, (dict, list))
                },
            )
        )

    if not out:
        raise LLMError("接口返回了空模型列表，请确认这条 key 是否已开通模型")
    # 对话模型排前面，其余按 id 排序，前端下拉默认就好用
    out.sort(key=lambda m: (not m.chat_capable, m.id.lower()))
    return out


def _extract_model_entries(payload: object) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "models", "result"):
            val = payload.get(key)
            if isinstance(val, list):
                return val
    return []


def _scrub(text: str, secret: str) -> str:
    """上游报错里可能回显 key（DeepSeek 会自己打码，但不能指望每家都这样）。"""
    if secret and secret in text:
        return text.replace(secret, "***")
    return text
