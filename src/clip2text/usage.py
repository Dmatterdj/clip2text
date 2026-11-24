"""用量记录与聚合（契约 docs/SETTINGS-CONTRACT.md §5 §7）。

两件事
    1. `record()` —— 埋点入口。**失败只 warning，绝不抛**：
       埋点把任务搞失败是最不可接受的事故（用户花了 40 分钟转写，
       最后因为一条统计 SQL 写不进去而全废）。
    2. `build_summary()` —— 按天/类型/模型聚合，并算出估算费用。

费用永远是「估算」
    单价来自 config.DEFAULT_PRICING，可被 settings.json 的 pricing 覆盖。
    这里只做面积 × 单价，不假装知道阶梯价、缓存价、批处理折扣；
    前端展示时必须带「估算」字样（契约 §5）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import DEFAULT_PRICING, get_settings
from .db import Database
from .models import UsageBucket, UsageSummary

log = logging.getLogger(__name__)

# 契约里的四类。顺序就是前端要展示的顺序：先说语音识别（最贵），
# 再说解析/改写（便宜但次数多）。
USAGE_KINDS: tuple[str, ...] = ("asr", "ocr", "analysis", "rewrite")

KIND_LABELS = {
    "asr": "语音识别",
    "ocr": "图片识别",
    "analysis": "内容解析",
    "rewrite": "文案改写",
}

# 费用保留 6 位小数：OCR 单张 0.0015 元，四舍五入到 2 位会变成 0.00，
# 让用户以为图片识别免费。
COST_PRECISION = 6

DEFAULT_DAYS = 30
MAX_DAYS = 365


# ---------------------------------------------------------------- 埋点
def record(
    db: Database,
    kind: str,
    *,
    task_id: str | None = None,
    model: str = "",
    audio_seconds: float = 0.0,
    images: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> bool:
    """记一笔用量。返回是否写入成功（仅供测试观察，调用方不必关心）。

    四个埋点位置（pipeline ASR / pipeline OCR / analysis / llm 改写）
    都走这一个函数，所以「吞异常 + 记日志」的逻辑只有一份。
    """
    try:
        db.add_usage_event(
            kind=kind,
            task_id=task_id,
            model=model,
            audio_seconds=audio_seconds,
            images=images,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        return True
    except Exception:  # noqa: BLE001 - 埋点绝不能影响主流程
        # 日志里不带任何 key / 请求内容，只有维度信息
        log.warning(
            "用量埋点失败（已忽略，不影响任务）：kind=%s task_id=%s model=%s",
            kind,
            task_id,
            model,
            exc_info=True,
        )
        return False


async def record_async(db: Database, kind: str, **fields: Any) -> bool:
    """异步上下文里的埋点：扔到线程池，别在事件循环里做 sqlite 写。

    连 `to_thread` 本身失败（事件循环正在关闭）也要吞掉。
    """
    try:
        return await asyncio.to_thread(record, db, kind, **fields)
    except Exception:  # noqa: BLE001
        log.warning(
            "用量埋点失败（已忽略，不影响任务）：kind=%s task_id=%s",
            kind,
            fields.get("task_id"),
            exc_info=True,
        )
        return False


# ---------------------------------------------------------------- 计价
def normalize_pricing(pricing: dict[str, float] | None = None) -> dict[str, float]:
    """补齐四个已知单价。

    settings.json 里可能只有一半（用户只改了语音单价），估算需要四个数
    才能算全，缺的用默认值补上，而不是当 0（当 0 会静默少算一笔钱）。
    """
    out = dict(DEFAULT_PRICING)
    for name, value in (pricing or {}).items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number != number or number < 0:  # NaN / 负数
            continue
        out[str(name)] = number
    return out


def current_pricing() -> dict[str, float]:
    return normalize_pricing(get_settings().pricing)


def estimate_cost(
    *,
    audio_seconds: float = 0.0,
    images: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    pricing: dict[str, float] | None = None,
) -> float:
    """估算费用（人民币元）。契约 §5 的公式，一个字都没改。"""
    p = normalize_pricing(pricing)
    cost = (
        float(audio_seconds or 0.0) / 3600.0 * p["asr_per_hour"]
        + int(images or 0) * p["ocr_per_image"]
        + int(prompt_tokens or 0) / 1e6 * p["llm_input_per_mtok"]
        + int(completion_tokens or 0) / 1e6 * p["llm_output_per_mtok"]
    )
    return round(cost, COST_PRECISION)


def _bucket(
    key: str, row: dict[str, Any] | None, pricing: dict[str, float]
) -> UsageBucket:
    data = row or {}
    audio = float(data.get("audio_seconds") or 0.0)
    images = int(data.get("images") or 0)
    prompt = int(data.get("prompt_tokens") or 0)
    completion = int(data.get("completion_tokens") or 0)
    return UsageBucket(
        key=key,
        audio_seconds=round(audio, 3),
        images=images,
        prompt_tokens=prompt,
        completion_tokens=completion,
        estimated_cost=estimate_cost(
            audio_seconds=audio,
            images=images,
            prompt_tokens=prompt,
            completion_tokens=completion,
            pricing=pricing,
        ),
    )


# ---------------------------------------------------------------- 时区
def local_offset_minutes(now: datetime | None = None) -> int:
    """服务端本地时区相对 UTC 的偏移（分钟）。

    北京 +480，纽约 -240/-300（夏令时），印度 +330。
    SQLite 不知道进程时区，所以聚合要把这个偏移显式加进 `date()`：
    存的是 UTC，转本地要 **加** offset（`date(created_at, '+480 minutes')`）。
    注意符号方向 —— 加反了会把北京用户早上的用量推到前一天。
    """
    moment = now or datetime.now(UTC)
    offset = moment.astimezone().utcoffset()
    if offset is None:  # 极端情况（某些平台的 naive tz）
        return 0
    return int(offset.total_seconds() // 60)


def _day_cutoff(days: int, now: datetime | None = None) -> str:
    """最近 N 天（含今天）的起始**本地**日期，形如 2026-10-26。

    「今天」必须按本地时区取：东八区用户在 00:00–08:00 之间，
    UTC 的今天还是昨天，用 UTC 往前推会少算一天。
    """
    moment = (now or datetime.now(UTC)).astimezone()
    today = moment.date()
    return (today - timedelta(days=max(0, days - 1))).isoformat()


def build_summary(
    db: Database,
    *,
    days: int = DEFAULT_DAYS,
    pricing: dict[str, float] | None = None,
    now: datetime | None = None,
    offset_minutes: int | None = None,
) -> UsageSummary:
    """组装 GET /api/v1/usage 的响应。

    · total：**全量**（用户想看「一共花了多少」，不能被 30 天窗口截断）
    · by_day：最近 days 天，按本地日倒序，只含有数据的日期
    · by_kind：asr / ocr / analysis / rewrite，固定顺序
    · by_model：按估算费用倒序（最贵的排前面，用户先看到钱花在哪）
    """
    price = normalize_pricing(pricing if pricing is not None else get_settings().pricing)
    days = max(1, min(int(days), MAX_DAYS))
    offset = local_offset_minutes(now) if offset_minutes is None else int(offset_minutes)

    total_row = db.usage_totals()
    kind_rows = {str(r.get("key")): r for r in db.usage_by_kind()}
    model_rows = db.usage_by_model()
    day_rows = db.usage_by_day(since=_day_cutoff(days, now), offset_minutes=offset)

    by_kind = [
        _bucket(name, kind_rows.get(name), price) for name in USAGE_KINDS
    ]
    # 未来若新增 kind（契约外），也要出现在列表里，而不是被静默丢弃
    extra = sorted(set(kind_rows) - set(USAGE_KINDS))
    by_kind.extend(_bucket(name, kind_rows[name], price) for name in extra)

    by_model = [_bucket(str(row.get("key") or "unknown"), row, price) for row in model_rows]
    by_model.sort(key=lambda b: (-b.estimated_cost, b.key))

    by_day = [
        _bucket(str(row.get("key") or ""), row, price)
        for row in day_rows
        if row.get("key")
    ]

    return UsageSummary(
        total=_bucket("total", total_row, price),
        by_day=by_day,
        by_kind=by_kind,
        by_model=by_model,
        event_count=db.usage_event_count(),
        currency="CNY",
        pricing=price,
    )


__all__ = [
    "DEFAULT_DAYS",
    "KIND_LABELS",
    "MAX_DAYS",
    "USAGE_KINDS",
    "build_summary",
    "current_pricing",
    "estimate_cost",
    "local_offset_minutes",
    "normalize_pricing",
    "record",
    "record_async",
]
