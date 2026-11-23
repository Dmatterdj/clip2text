"""内容解析（长文本结构化总结）：map-reduce + 宽松 JSON 解析。

职责与 llm.py 分开
    llm.py 负责「按风格改写」，输出是自然语言；
    这里负责「把长文本拆成结构」，输出必须能落到 Chapter / Quote 上，
    而且**必须带真实时间戳**。两者的提示词、容错、成本模型都不同，
    混在一个文件里会很快变成一团。

为什么必须两阶段
    3 万字一次性问 LLM，一是放不下，二是模型会漏掉中段、时间戳乱编。
    先按时间/字符分窗逐窗结构化（map），再把各窗摘要合并（reduce），
    既控制单次输入，也能让每个章节回填到真实窗口边界。

契约：docs/ANALYSIS-CONTRACT.md
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import httpx

from .llm import LLMConfig
from .models import Analysis, Chapter, Quote, Segment, Task, utcnow

if TYPE_CHECKING:
    from .config import Settings

log = logging.getLogger(__name__)

# ---- 分窗 ----
DEFAULT_WINDOW_SEC = 12 * 60  # ≈12 分钟：中文播客一窗约 3000-6000 字
DEFAULT_WINDOW_CHARS = 6000
MAX_WINDOWS = 40  # 上限，超过就加大单窗预算（避免 3 小时音频切出 100 窗烧钱）
WINDOW_WARN_THRESHOLD = 20  # 超过这个数说明用户会付更多钱，日志里要警告

# ---- 输出护栏 ----
TITLE_MAX = 40
SUMMARY_MAX = 200
ABSTRACT_MAX = 600  # 契约要求 120-300 字，留余量但别让模型输出撑爆 UI
KEY_POINTS_MAX = 10
QUOTES_MAX = 6
QUOTES_PER_WINDOW = 3
ENTITIES_MAX = 15

DEEPSEEK_FALLBACK_MODEL = "deepseek-chat"


class AnalysisError(RuntimeError):
    """分析前置条件不满足，或整体无法产出可信结果。"""


# ---------------------------------------------------------------- 模型配置
@dataclass(frozen=True)
class AnalysisLLM:
    """分析用哪套模型配置，以及为什么是它。

    source 让 capabilities 能如实告诉用户「现在用的是 LLM_* 还是
    DEEPSEEK_API_KEY 兜底」，不用去猜。
    """

    config: LLMConfig | None
    source: Literal["llm", "deepseek"] | None
    reason: str | None = None

    @property
    def configured(self) -> bool:
        return self.config is not None and self.config.configured


def resolve_analysis_llm(settings: Settings) -> AnalysisLLM:
    """决定分析走哪个模型。

    用户往往只配了 DEEPSEEK_API_KEY（ASR/OCR 已经在用），
    如果分析非要再配一套 LLM_*，等于把「一个 key 覆盖所有能力」的
    产品承诺打破了。所以这里做兜底：没有专用配置时复用 DeepSeek。
    """
    base_url = (settings.llm_base_url or "").strip()
    model = (settings.llm_model or "").strip()
    if base_url and model:
        return AnalysisLLM(
            config=LLMConfig(
                base_url=base_url,
                api_key=(settings.llm_api_key or "").strip(),
                model=model,
            ),
            source="llm",
            reason=None,
        )

    deepseek_key = (settings.deepseek_api_key or "").strip()
    deepseek_base = (settings.deepseek_base_url or "").strip()
    if deepseek_key and deepseek_base:
        return AnalysisLLM(
            config=LLMConfig(
                base_url=deepseek_base,
                api_key=deepseek_key,
                model=DEEPSEEK_FALLBACK_MODEL,
            ),
            source="deepseek",
            # 契约里 reason=null 表示「可用且无需解释」；这里确实需要解释，
            # 否则用户不知道分析为什么突然能用了、花的是哪个 key。
            reason=(
                "未配置 LLM_BASE_URL / LLM_MODEL，已自动复用 DEEPSEEK_API_KEY"
                f"（模型 {DEEPSEEK_FALLBACK_MODEL}）"
            ),
        )

    return AnalysisLLM(
        config=None,
        source=None,
        reason=(
            "未配置 LLM_BASE_URL / LLM_MODEL，也没有 DEEPSEEK_API_KEY。"
            "配置其中任意一个即可使用内容解析"
        ),
    )


# ---------------------------------------------------------------- 时间格式
def format_timestamp(seconds: float) -> str:
    """秒 → mm:ss 或 h:mm:ss。

    提示词、日志、前端时间戳都走这一个函数，避免两处格式化出现
    「1:02:03」和「62:03」这种不一致。
    """
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


# ---------------------------------------------------------------- 分窗
@dataclass
class Window:
    """一个待 map 的窗口。start/end 在分窗时就定下来，供 reduce 回填。"""

    index: int
    start: float
    end: float
    text: str
    timed_text: str
    has_timestamps: bool = True


def plan_windows(
    task: Task,
    *,
    window_sec: float = DEFAULT_WINDOW_SEC,
    window_chars: int = DEFAULT_WINDOW_CHARS,
    max_windows: int = MAX_WINDOWS,
) -> list[Window]:
    """按时间或字符预算切窗，取先到者。

    - 有 segments：用真实时间轴切，记下每个窗口的 start/end
    - 没有 segments（OCR/纯文本）：按字符切，时间戳全部留 0
    - 窗口数超过上限：加倍预算重切，避免长内容被切成上百窗
    """
    segments = [s for s in task.segments if s.text and s.text.strip()]
    if segments:
        budget_chars = max(1000, int(window_chars))
        budget_sec = max(60.0, float(window_sec))
        windows = _windows_from_segments(segments, budget_sec, budget_chars)
        guard = 0
        while len(windows) > max_windows and guard < 12:
            factor = max(2, math.ceil(len(windows) / max_windows))
            budget_chars *= factor
            budget_sec *= factor
            windows = _windows_from_segments(segments, budget_sec, budget_chars)
            guard += 1
        if len(windows) > max_windows:
            windows = _merge_windows(windows, max_windows)
        return _warn_if_many(windows)

    raw = (task.text_content or task.content or "").strip()
    if not raw:
        return []
    budget = max(1000, int(window_chars))
    windows = _windows_from_text(raw, budget)
    guard = 0
    while len(windows) > max_windows and guard < 12:
        factor = max(2, math.ceil(len(windows) / max_windows))
        budget *= factor
        windows = _windows_from_text(raw, budget)
        guard += 1
    if len(windows) > max_windows:
        windows = _merge_windows(windows, max_windows)
    return _warn_if_many(windows)


def _warn_if_many(windows: list[Window]) -> list[Window]:
    if len(windows) > WINDOW_WARN_THRESHOLD:
        log.warning(
            "内容解析将分 %d 个窗口（>%d）：每个窗口一次 LLM 调用，"
            "长内容会产生更高的 token 费用",
            len(windows),
            WINDOW_WARN_THRESHOLD,
        )
    return windows


def _windows_from_segments(
    segments: list[Segment], sec_budget: float, char_budget: int
) -> list[Window]:
    windows: list[Window] = []
    current: list[Segment] = []
    current_chars = 0

    def flush() -> None:
        nonlocal current, current_chars
        if not current:
            return
        start = current[0].start
        end = max(current[-1].end, start)
        text = "\n".join(s.text.strip() for s in current)
        timed = "\n".join(
            f"[{format_timestamp(s.start - start)}] {s.text.strip()}" for s in current
        )
        windows.append(
            Window(
                index=len(windows),
                start=start,
                end=end,
                text=text,
                timed_text=timed,
            )
        )
        current = []
        current_chars = 0

    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue

        # 单个 segment 本身就超预算：先把它前面的窗口收尾，再硬切这一段。
        if len(text) > char_budget:
            flush()
            for piece, piece_start, piece_end in _split_text(
                text, seg.start, seg.end, char_budget
            ):
                windows.append(
                    Window(
                        index=len(windows),
                        start=piece_start,
                        end=piece_end,
                        text=piece,
                        timed_text=f"[{format_timestamp(0)}] {piece}",
                    )
                )
            continue

        would_end = max(seg.end, seg.start)
        if current and (
            current_chars + len(text) > char_budget
            or would_end - current[0].start > sec_budget
        ):
            flush()
        current.append(seg)
        current_chars += len(text)

    flush()
    return windows


def _windows_from_text(text: str, char_budget: int) -> list[Window]:
    windows: list[Window] = []
    for offset in range(0, len(text), char_budget):
        piece = text[offset : offset + char_budget]
        windows.append(
            Window(
                index=len(windows),
                start=0.0,
                end=0.0,
                text=piece,
                timed_text=piece,
                has_timestamps=False,
            )
        )
    return windows


def _split_text(
    text: str, start: float, end: float, char_budget: int
) -> list[tuple[str, float, float]]:
    """把超长单段按字符硬切，并按字符占比分配时间区间。

    单段一般不会超预算；真超了（例如整段 OCR 文本被塞进一个 segment），
    按字符比例分配时间是唯一还能解释的做法，总比全给 0 或全给原区间诚实。
    """
    pieces = [text[i : i + char_budget] for i in range(0, len(text), char_budget)]
    total = len(pieces)
    span = max(0.0, end - start)
    out: list[tuple[str, float, float]] = []
    for i, piece in enumerate(pieces):
        piece_start = start + span * i / total
        piece_end = start + span * (i + 1) / total
        out.append((piece, piece_start, piece_end))
    return out


def _merge_windows(windows: list[Window], limit: int) -> list[Window]:
    """兜底合并：正常增长预算后不会走到这里。"""
    if len(windows) <= limit:
        return windows
    group_size = math.ceil(len(windows) / limit)
    merged: list[Window] = []
    for offset in range(0, len(windows), group_size):
        group = windows[offset : offset + group_size]
        if not group:
            continue
        merged.append(
            Window(
                index=len(merged),
                start=group[0].start,
                end=group[-1].end,
                text="\n".join(w.text for w in group),
                timed_text="\n".join(w.timed_text for w in group),
                has_timestamps=group[0].has_timestamps,
            )
        )
    return merged[:limit]


# ---------------------------------------------------------------- JSON 解析
def parse_json_object(raw: str) -> dict[str, Any] | None:
    """从模型输出里抠出一个 JSON 对象。

    顺序：直接 json.loads → 抓第一个平衡的 {...} 再试。
    不假设 response_format 可用：不同 OpenAI 兼容服务差异很大，
    模型也经常把 JSON 包进 ```json 代码块或加点客套话。
    """
    text = (raw or "").lstrip("\ufeff").strip()
    if not text:
        return None

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed

    for candidate in _iter_json_objects(text):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _iter_json_objects(text: str):
    """按出现顺序产出平衡的 {...} 片段（跳过字符串里的花括号）。"""
    for start, char in enumerate(text):
        if char != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for idx in range(start, len(text)):
            current = text[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    in_string = False
                continue
            if current == '"':
                in_string = True
            elif current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    yield text[start : idx + 1]
                    break


# ---------------------------------------------------------------- LLM 调用
ChatFn = Callable[
    [LLMConfig, list[dict[str, str]]],
    Awaitable[tuple[str, dict[str, int] | None]],
]
ProgressFn = Callable[[dict[str, Any]], Awaitable[None] | None]


async def chat_completion(
    config: LLMConfig,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.2,
) -> tuple[str, dict[str, int] | None]:
    """一次非流式 chat 调用，返回 (content, usage)。

    分析要的是完整 JSON，流式反而增加拼接成本；超时用 LLMConfig.timeout。
    温度调低：结构化抽取不需要创意，稳定比花哨重要。
    """
    if not config.configured:
        raise AnalysisError(
            "未配置分析模型：请设置 LLM_BASE_URL/LLM_MODEL 或 DEEPSEEK_API_KEY"
        )

    payload = {
        "model": config.model,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
    }
    headers = {"Content-Type": "application/json", **config.extra_headers}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    try:
        async with httpx.AsyncClient(timeout=config.timeout) as client:
            resp = await client.post(config.endpoint(), json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise AnalysisError(f"LLM 请求失败：{exc}") from exc

    if resp.status_code != 200:
        body = resp.text[:400]
        raise AnalysisError(f"LLM 返回 {resp.status_code}: {body}")

    try:
        obj = resp.json()
    except ValueError as exc:
        raise AnalysisError("LLM 返回的不是 JSON") from exc

    choices = obj.get("choices") or []
    content = ""
    if choices:
        message = choices[0].get("message") or {}
        content = str(message.get("content") or "")
    if not content.strip():
        raise AnalysisError("LLM 返回了空内容")

    return content, _parse_usage(obj.get("usage"))


def _parse_usage(raw: object) -> dict[str, int] | None:
    """OpenAI 标准是 prompt/completion_tokens；少数兼容实现用 input/output。"""
    if not isinstance(raw, dict):
        return None
    prompt = raw.get("prompt_tokens", raw.get("input_tokens"))
    completion = raw.get("completion_tokens", raw.get("output_tokens"))
    if prompt is None and completion is None:
        return None
    return {
        "prompt": _as_int(prompt) or 0,
        "completion": _as_int(completion) or 0,
    }


# ---------------------------------------------------------------- map 阶段
@dataclass
class MapResult:
    window: Window
    title: str
    summary: str
    key_points: list[str]
    quotes: list[dict[str, Any]]


def _map_messages(window: Window, total: int) -> list[dict[str, str]]:
    if window.has_timestamps:
        span = f"{format_timestamp(window.start)}–{format_timestamp(window.end)}"
        timing_hint = (
            f"本段对应 {span}（相对内容开头）。每行开头的 [mm:ss] "
            "是该行相对本窗起点的秒数。"
        )
    else:
        span = "无时间戳（例如图片 OCR 文本）"
        timing_hint = "本段没有时间戳，quotes.start 一律填 0。"

    system = (
        "你是长内容分析助手。用户给的是同一条内容的窗口 "
        f"{window.index}（共 {total} 个窗口），请只分析这一段。\n"
        f"{timing_hint}\n"
        "只输出一个 JSON 对象，不要 markdown 代码块、不要任何解释。格式：\n"
        '{"title": "...", "summary": "...", "key_points": ["..."], '
        '"quotes": [{"text": "...", "start": 12.3}]}\n'
        "要求：\n"
        "- title：8-20 字，概括这一窗讲了什么\n"
        "- summary：2-3 句，说清本段内容\n"
        "- key_points：0-5 条，每条一句话\n"
        "- quotes：0-3 条值得摘录的原话，必须逐字来自本段；"
        "start 用该行相对本窗起点的秒数\n"
        "不要输出 JSON 之外的任何文字。"
    )
    user = window.timed_text or window.text
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _parse_map(raw: str, window: Window) -> MapResult:
    data = parse_json_object(raw)
    if data is None:
        raise AnalysisError(f"窗口 {window.index} 的模型返回无法解析为 JSON")

    title = _truncate(_as_text(data.get("title")) or f"第 {window.index + 1} 部分", TITLE_MAX)
    summary = _truncate(_as_text(data.get("summary")), SUMMARY_MAX)
    key_points = _clean_str_list(data.get("key_points"), limit=KEY_POINTS_MAX)
    quotes = _clean_quotes(data.get("quotes"), window)
    return MapResult(
        window=window,
        title=title,
        summary=summary,
        key_points=key_points,
        quotes=quotes,
    )


def _clean_quotes(value: object, window: Window) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    span = max(0.0, window.end - window.start)
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = _as_text(item.get("text"))
        if not text:
            continue
        if window.has_timestamps:
            # 模型给的是「本窗内相对秒数」，回填成全局绝对时间；
            # 超出窗口的脏值夹到窗口边界内，宁可靠近也对，不跳错位置。
            relative = max(0.0, _as_float(item.get("start")))
            if span > 0:
                relative = min(relative, span)
            start = round(window.start + relative, 2)
        else:
            start = 0.0
        note = _as_text(item.get("note")) or None
        out.append({"start": start, "text": text, "note": note})
        if len(out) >= QUOTES_PER_WINDOW:
            break
    return out


# ---------------------------------------------------------------- reduce 阶段
def _reduce_messages(results: list[MapResult]) -> list[dict[str, str]]:
    system = (
        "你是长内容分析助手。下面会给出同一条内容按时间切成的各窗口的"
        "标题、摘要与要点。请合并成一份整体结构。\n"
        "只输出一个 JSON 对象，不要 markdown 代码块、不要任何解释。格式：\n"
        '{"abstract": "...", "chapters": [{"title": "...", "summary": "...", '
        '"window": 0}], "key_points": ["..."], "entities": ["..."]}\n'
        "要求：\n"
        "- abstract：120-300 字，概括整条内容，不要只复述某一窗\n"
        "- chapters：按时间顺序，每个章节引用一个窗口；"
        "window 必须使用下面列出的窗口序号\n"
        "- 每章 title 8-20 字，summary 1-3 句\n"
        "- key_points：3-8 条全局要点\n"
        "- entities：0-15 个重要人名、产品、概念\n"
        "不要输出 quotes 字段（原文引用由系统另行处理）。"
    )

    lines: list[str] = []
    for result in results:
        window = result.window
        if window.has_timestamps:
            span = f"{format_timestamp(window.start)}–{format_timestamp(window.end)}"
        else:
            span = "无时间戳"
        lines.append(f"窗口 {window.index}（{span}）")
        lines.append(f"标题：{result.title}")
        if result.summary:
            lines.append(f"摘要：{result.summary}")
        if result.key_points:
            lines.append("要点：")
            lines.extend(f"- {point}" for point in result.key_points)
        lines.append("")

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(lines).strip()},
    ]


def _assemble_reduce(
    data: dict[str, Any],
    results: list[MapResult],
    task: Task,
    config: LLMConfig,
    windows: int,
    token_usage: dict[str, int] | None,
) -> Analysis:
    by_index = {result.window.index: result for result in results}
    successful = sorted(by_index.values(), key=lambda item: item.window.start)

    abstract = _truncate(_as_text(data.get("abstract")), ABSTRACT_MAX)
    if not abstract:
        # reduce 漏了 abstract 时，用各窗摘要兜一个，至少不让摘要区空白。
        abstract = _truncate(
            " ".join(item.summary for item in successful if item.summary),
            ABSTRACT_MAX,
        )

    chapters: list[Chapter] = []
    seen_windows: set[int] = set()
    raw_chapters = data.get("chapters")
    if isinstance(raw_chapters, list):
        for item in raw_chapters:
            if not isinstance(item, dict):
                continue
            window_index = _as_int(item.get("window"))
            if window_index is None or window_index not in by_index:
                # 序号来自模型，可能越界/重号；跳过而不是猜一个位置
                continue
            if window_index in seen_windows:
                continue
            seen_windows.add(window_index)
            source = by_index[window_index]
            title = _truncate(
                _as_text(item.get("title")) or source.title, TITLE_MAX
            )
            summary = _truncate(_as_text(item.get("summary")), SUMMARY_MAX)
            chapters.append(
                Chapter(
                    start=source.window.start,
                    end=source.window.end,
                    title=title,
                    summary=summary,
                )
            )

    if not chapters:
        # reduce 没给出可用章节时，退化成「一窗一章」：
        # 至少保留可跳转的时间轴，比返回空章节对用户有用。
        chapters = [
            Chapter(
                start=item.window.start,
                end=item.window.end,
                title=item.title,
                summary=item.summary,
            )
            for item in successful
        ]
    chapters.sort(key=lambda chapter: chapter.start)

    key_points = _clean_str_list(data.get("key_points"), limit=KEY_POINTS_MAX)
    if not key_points:
        key_points = _clean_str_list(
            [point for item in successful for point in item.key_points],
            limit=KEY_POINTS_MAX,
        )
    entities = _clean_str_list(data.get("entities"), limit=ENTITIES_MAX)

    return Analysis(
        task_id=task.id,
        abstract=abstract,
        chapters=chapters,
        key_points=key_points,
        quotes=_merge_quotes(successful),
        entities=entities,
        language=task.language,
        model=config.model,
        windows=windows,
        token_usage=token_usage,
        state="SUCCESS",
        created_at=utcnow(),
    )


def _merge_quotes(results: list[MapResult]) -> list[Quote]:
    """quotes 直接用 map 结果：reduce 再生成一次会改写原文（契约 §3.3）。"""
    collected = [quote for result in results for quote in result.quotes]
    collected.sort(key=lambda item: item["start"])

    seen: set[str] = set()
    out: list[Quote] = []
    for item in collected:
        key = re.sub(r"\s+", "", item["text"]).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(
            Quote(start=float(item["start"]), text=item["text"], note=item.get("note"))
        )
        if len(out) >= QUOTES_MAX:
            break
    return out


# ---------------------------------------------------------------- 主流程
async def run_analysis(
    task: Task,
    config: LLMConfig,
    *,
    progress: ProgressFn | None = None,
    chat: ChatFn | None = None,
    window_sec: float = DEFAULT_WINDOW_SEC,
    window_chars: int = DEFAULT_WINDOW_CHARS,
    max_windows: int = MAX_WINDOWS,
) -> Analysis:
    """执行 map-reduce，返回 Analysis（可能 state=FAILED）。

    「模型返回垃圾」不算异常：逐窗跳过，失败过半才整体判 FAILED。
    只有前置条件不满足（没配模型 / 没文本）才抛 AnalysisError，
    让 HTTP 层返回 400。
    """
    if not config.configured:
        raise AnalysisError(
            "未配置分析模型：请设置 DEEPSEEK_API_KEY，"
            "或同时配置 LLM_BASE_URL 与 LLM_MODEL"
        )

    call = chat or chat_completion
    windows = plan_windows(
        task, window_sec=window_sec, window_chars=window_chars, max_windows=max_windows
    )
    if not windows:
        raise AnalysisError("任务没有可分析的文本内容")

    total = len(windows)
    results: list[MapResult] = []
    failures: list[str] = []
    usage_total = {"prompt": 0, "completion": 0}
    usage_seen = False

    def record_usage(usage: dict[str, int] | None) -> None:
        nonlocal usage_seen
        if usage is None:
            return
        usage_seen = True
        usage_total["prompt"] += int(usage.get("prompt") or 0)
        usage_total["completion"] += int(usage.get("completion") or 0)

    for index, window in enumerate(windows):
        await _emit(
            progress,
            {
                "phase": "mapping",
                "done": index,
                "total": total,
                "message": f"正在分析第 {index + 1}/{total} 段",
            },
        )
        try:
            raw, usage = await call(config, _map_messages(window, total))
            record_usage(usage)
            results.append(_parse_map(raw, window))
        except Exception as exc:  # noqa: BLE001
            # 单窗失败不中断：长内容里模型偶发输出坏 JSON 很常见，
            # 直接整单失败会让用户为已成功的窗口白付钱。
            reason = str(exc) if isinstance(exc, AnalysisError) else f"{type(exc).__name__}: {exc}"
            failures.append(reason)
            log.warning(
                "分析窗口 %d/%d 失败：%s", index + 1, total, reason
            )

    token_usage = dict(usage_total) if usage_seen else None
    await _emit(
        progress,
        {
            "phase": "mapping",
            "done": total,
            "total": total,
            "message": f"已完成 {total} 段",
        },
    )

    if failures and len(failures) * 2 > total:
        return Analysis(
            task_id=task.id,
            language=task.language,
            model=config.model,
            windows=total,
            token_usage=token_usage,
            state="FAILED",
            error=(
                f"{len(failures)}/{total} 个窗口的模型返回无法解析或请求失败，"
                f"已放弃本次分析。首个错误：{failures[0]}"
            ),
            created_at=utcnow(),
        )

    if not results:  # 理论上被上面的过半判断覆盖，保留兜底
        return Analysis(
            task_id=task.id,
            language=task.language,
            model=config.model,
            windows=total,
            token_usage=token_usage,
            state="FAILED",
            error="所有窗口都没有产出可用结果",
            created_at=utcnow(),
        )

    if total == 1:
        # 单窗短路：不需要 reduce，直接把这一窗当成唯一章节。
        only = results[0]
        return Analysis(
            task_id=task.id,
            abstract=_truncate(only.summary or only.title, ABSTRACT_MAX),
            chapters=[
                Chapter(
                    start=only.window.start,
                    end=only.window.end,
                    title=only.title,
                    summary=only.summary,
                )
            ],
            key_points=only.key_points[:KEY_POINTS_MAX],
            quotes=_merge_quotes([only]),
            entities=[],
            language=task.language,
            model=config.model,
            windows=1,
            token_usage=token_usage,
            state="SUCCESS",
            created_at=utcnow(),
        )

    await _emit(
        progress,
        {"phase": "reducing", "done": 0, "total": 1, "message": "正在汇总"},
    )

    try:
        raw, usage = await call(config, _reduce_messages(results))
        record_usage(usage)
        token_usage = dict(usage_total) if usage_seen else None
        data = parse_json_object(raw)
        if data is None:
            raise AnalysisError("汇总阶段模型返回无法解析为 JSON")
    except Exception as exc:  # noqa: BLE001
        reason = str(exc) if isinstance(exc, AnalysisError) else f"{type(exc).__name__}: {exc}"
        log.warning("分析汇总失败：%s", reason)
        return Analysis(
            task_id=task.id,
            language=task.language,
            model=config.model,
            windows=total,
            token_usage=token_usage,
            state="FAILED",
            error=f"汇总阶段失败：{reason}",
            created_at=utcnow(),
        )

    return _assemble_reduce(data, results, task, config, total, token_usage)


async def _emit(progress: ProgressFn | None, event: dict[str, Any]) -> None:
    if progress is None:
        return
    try:
        result = progress(event)
        if inspect.isawaitable(result):
            await result
    except Exception:  # noqa: BLE001
        # 进度回调失败（比如 SSE 连接断了）不应该拖垮分析本身
        log.exception("分析进度回调失败")


# ---------------------------------------------------------------- 进程内调度
class AnalysisRuns:
    """正在进行的分析任务注册表 + 进度订阅。

    为什么分析要脱离请求生命周期跑：长内容的 map 阶段可能十几分钟，
    用户关掉页面 / 切换 tab 都不该让已花掉的 token 白费。
    结果统一落库，重新连上来用 GET /analysis 也能看到。
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._listeners: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}

    def running(self, task_id: str) -> bool:
        handle = self._tasks.get(task_id)
        return handle is not None and not handle.done()

    def subscribe(self, task_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._listeners.setdefault(task_id, set()).add(queue)
        return queue

    def unsubscribe(self, task_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        listeners = self._listeners.get(task_id)
        if not listeners:
            return
        listeners.discard(queue)
        if not listeners:
            self._listeners.pop(task_id, None)

    async def publish(self, task_id: str, event: dict[str, Any]) -> None:
        for queue in list(self._listeners.get(task_id) or ()):
            queue.put_nowait(event)

    def ensure_started(
        self, task_id: str, factory: Callable[[], Awaitable[None]]
    ) -> bool:
        """已在跑就不重复开跑：重复点击不该让用户付两遍钱。"""
        if self.running(task_id):
            return False
        handle = asyncio.create_task(factory())
        self._tasks[task_id] = handle
        handle.add_done_callback(lambda _: self._tasks.pop(task_id, None))
        return True

    async def cancel(self, task_id: str) -> bool:
        handle = self._tasks.get(task_id)
        if handle is None or handle.done():
            return False
        handle.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await handle
        return True

    def cancel_all(self) -> None:
        """进程退出时尽力取消。

        shutdown 可能在不同线程/不同事件循环里被调用（TestClient 就是），
        这里绝不能因为取消失败而让关闭流程抛异常——结果已经落库，
        重启时还会把 RUNNING 标成 FAILED。
        """
        for handle in list(self._tasks.values()):
            with contextlib.suppress(Exception):
                handle.cancel()
        self._tasks.clear()


def build_job(
    *,
    task: Task,
    config: LLMConfig,
    db: Any,
    runs: AnalysisRuns,
    chat: ChatFn | None = None,
) -> Callable[[], Awaitable[None]]:
    """把「跑分析 + 落库 + 推事件」包成一个可调度任务。"""

    async def job() -> None:
        # 先落 RUNNING：前端能立刻看到「正在解析」，服务重启后也能被清理成 FAILED
        await asyncio.to_thread(
            db.upsert_analysis,
            Analysis(
                task_id=task.id,
                model=config.model,
                state="RUNNING",
                created_at=utcnow(),
            ),
        )

        async def progress(event: dict[str, Any]) -> None:
            await runs.publish(task.id, {"type": "progress", "data": event})

        try:
            analysis = await run_analysis(task, config, progress=progress, chat=chat)
        except asyncio.CancelledError:
            await _safe_store(
                db,
                Analysis(
                    task_id=task.id,
                    model=config.model,
                    state="FAILED",
                    error="分析被中断（重新发起解析或服务关闭），可再次点击解析",
                    created_at=utcnow(),
                ),
            )
            raise
        except AnalysisError as exc:
            await _safe_store(
                db,
                Analysis(
                    task_id=task.id,
                    model=config.model,
                    state="FAILED",
                    error=str(exc),
                    created_at=utcnow(),
                ),
            )
            await runs.publish(task.id, {"type": "error", "data": {"detail": str(exc)}})
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("分析任务异常")
            detail = f"分析失败：{type(exc).__name__}: {exc}"
            await _safe_store(
                db,
                Analysis(
                    task_id=task.id,
                    model=config.model,
                    state="FAILED",
                    error=detail,
                    created_at=utcnow(),
                ),
            )
            await runs.publish(task.id, {"type": "error", "data": {"detail": detail}})
            return

        await _safe_store(db, analysis)
        if analysis.state == "SUCCESS":
            await runs.publish(
                task.id,
                {"type": "result", "data": analysis.model_dump(mode="json")},
            )
        else:
            await runs.publish(
                task.id,
                {"type": "error", "data": {"detail": analysis.error or "分析失败"}},
            )

    return job


async def _safe_store(db: Any, analysis: Analysis) -> None:
    try:
        await asyncio.to_thread(db.upsert_analysis, analysis)
    except Exception:  # noqa: BLE001
        # 数据库已关闭（进程退出中）时不该再抛，否则任务日志全是噪音
        log.warning("分析结果落库失败：task_id=%s", analysis.task_id)


# ---------------------------------------------------------------- 小工具
def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1].rstrip() + "…"


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return default
    else:
        return default
    return number if math.isfinite(number) else default


def _clean_str_list(value: object, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _as_text(item)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out
