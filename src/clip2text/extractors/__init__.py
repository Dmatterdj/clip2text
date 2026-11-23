"""链接解析注册表与统一入口。

⚠️ 合规提示
    默认关闭（EXTRACTORS_ENABLED=false）。开启后请仅用于处理你自己
    拥有版权的内容，或已获得授权的内容。仓库内不含任何平台的
    签名密钥、登录 Cookie 或其他绕过手段。

解析顺序
    1. 第三方插件（entry_points 组 clip2text.extractors）
    2. 内置适配器（B 站）
    3. yt-dlp 兜底（覆盖小红书、抖音、微博、TikTok、YouTube 等）
"""

from __future__ import annotations

import importlib.metadata
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

from ..config import Settings
from .base import ExtractContext, ExtractError, Extractor, ExtractResult
from .bilibili import BilibiliExtractor
from .ytdlp_engine import YtDlpExtractor

log = logging.getLogger(__name__)

ENTRY_GROUP = "clip2text.extractors"

# 用户经常直接粘贴整段分享文案（"3.87 复制打开抖音…"），先抽链接。
# 用 IGNORECASE：URL scheme 本身大小写不敏感，用户从别处复制来的
# "HTTPS://..." 也应该能识别。
_URL_RE = re.compile(r"https?://[^\s，,、；;\"'<>）)】\]]+", re.IGNORECASE)

__all__ = [
    "ExtractContext",
    "ExtractError",
    "ExtractResult",
    "Extractor",
    "builtin_extractors",
    "extract",
    "host_of",
    "load_plugins",
    "pick_url",
    "platform_summary",
]


def pick_url(text: str) -> str:
    """从一段分享文案里抽出第一个 http(s) 链接。"""
    m = _URL_RE.search(text or "")
    if not m:
        raise ExtractError(
            "没有找到有效链接。请粘贴以 http:// 或 https:// 开头的作品链接。"
        )
    return m.group(0).rstrip("。，,、；;）)】\"'")


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def builtin_extractors() -> list[Extractor]:
    """内置适配器。yt-dlp 放最后当兜底。"""
    return [BilibiliExtractor(), YtDlpExtractor()]  # type: ignore[list-item]


def load_plugins() -> dict[str, Extractor]:
    """加载通过 entry_points 注册的第三方适配器。"""
    found: dict[str, Extractor] = {}
    try:
        eps = importlib.metadata.entry_points(group=ENTRY_GROUP)
    except TypeError:  # Python < 3.10 旧签名
        eps = importlib.metadata.entry_points().get(ENTRY_GROUP, [])  # type: ignore[attr-defined]
    for ep in eps:
        try:
            factory = ep.load()
            inst = factory() if callable(factory) else factory
            if not hasattr(inst, "extract"):
                log.warning("插件 %s 没有 extract() 方法，已跳过", ep.name)
                continue
            found[ep.name] = inst
            log.debug("加载解析器插件: %s", ep.name)
        except Exception:  # noqa: BLE001 - 单个插件坏了不能拖垮整体
            log.exception("解析器插件加载失败: %s", ep.name)
    return found


def extract(url: str, work_dir: Path, settings: Settings) -> ExtractResult:
    """统一入口：抽链接 → 插件 → 内置 → yt-dlp。"""
    if not settings.extractors_enabled:
        raise ExtractError(
            "链接解析默认关闭。设置 EXTRACTORS_ENABLED=true 后重启服务。"
            "开启后请仅用于处理你拥有版权的内容。"
        )

    clean = pick_url(url)
    ctx = ExtractContext(
        work_dir=work_dir,
        proxy=settings.proxy_url or None,
        cookie_file=settings.cookie_file or None,
    )

    allow = set(settings.allowed_extractors)  # 为空表示全部允许

    candidates: list[tuple[str, Extractor]] = []
    for name, plugin in load_plugins().items():
        candidates.append((name, plugin))
    for inst in builtin_extractors():
        candidates.append((type(inst).__name__, inst))

    errors: list[str] = []
    for name, inst in candidates:
        try:
            if not inst.matches(clean):
                continue
            if allow and name not in allow and _alias(inst) not in allow:
                log.info("解析器 %s 被 EXTRACTORS_ALLOW 排除", name)
                continue
            log.info("使用解析器 %s 处理 %s", name, host_of(clean))
            return inst.extract(clean, ctx)
        except ExtractError as exc:
            errors.append(f"{name}: {exc}")
            log.warning("解析器 %s 失败：%s", name, exc)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            log.exception("解析器 %s 异常", name)

    detail = "；".join(errors[:3]) if errors else "没有匹配的解析器"
    raise ExtractError(f"无法解析这个链接。{detail}")


def _alias(inst: object) -> str:
    """把类名转成 allow 列表里更好写的短名，如 BilibiliExtractor -> bilibili。"""
    name = type(inst).__name__.lower()
    return name.replace("extractor", "").strip("_")


def platform_summary() -> list[dict[str, object]]:
    """给 /capabilities 用：列出各解析器的可用状态，前端据此提示。"""
    out: list[dict[str, object]] = []
    for inst in builtin_extractors():
        name = _alias(inst)
        available = getattr(inst, "available", lambda: True)()
        reason = getattr(inst, "unavailable_reason", lambda: None)() if not available else None
        out.append(
            {
                "name": name,
                "hosts": list(getattr(inst, "hosts", ())),
                "fallback": not getattr(inst, "hosts", ()),
                "available": available,
                "reason": reason,
            }
        )
    for name, inst in load_plugins().items():
        out.append(
            {
                "name": name,
                "hosts": list(getattr(inst, "hosts", ())),
                "fallback": False,
                "available": True,
                "reason": None,
                "plugin": True,
            }
        )
    return out
