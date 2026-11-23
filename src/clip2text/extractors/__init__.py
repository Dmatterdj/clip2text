"""链接解析注册表与统一入口。

⚠️ 合规提示
    链接解析默认开启（EXTRACTORS_ENABLED=true）。这个工具的产品定位是
    「贴一条链接 → 出文字」：长视频播客、知识科普、读书、讲座这类内容
    几乎都以链接形式存在，把默认值设成关闭等于把主场景锁在门外。
    合规兜底仍然保留：显式设 EXTRACTORS_ENABLED=false 可整体关闭。
    开启后请仅用于处理你自己拥有版权的内容，或已获得授权的内容。
    仓库内不含任何平台的签名密钥、登录 Cookie 或其他绕过手段。

解析顺序
    1. 第三方插件（entry_points 组 clip2text.extractors）
    2. 内置适配器（小宇宙、B 站）
    3. yt-dlp 兜底（YouTube / 抖音 / 小红书 / 微博 / TikTok / 喜马拉雅 …）

    内置适配器排在 yt-dlp 前面是有意的：它们能拿到比 yt-dlp 更完整的
    中文元信息（shownotes、多 P 清单），而 yt-dlp 只给一条 stream。
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
from .podcast_rss import PodcastFeedExtractor
from .xiaoyuzhou import XiaoyuzhouExtractor
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
    "PLATFORM_CATALOG",
    "builtin_extractors",
    "extract",
    "host_of",
    "load_plugins",
    "pick_url",
    "platform_catalog",
    "platform_summary",
]

# ---------------------------------------------------------------------------
# 平台清单：给 /capabilities 用，让前端能直接把「支持哪些平台」列出来。
#
# 为什么手写而不从 yt-dlp 自动推导：yt-dlp 有 1754 个 extractor，多数是
# 国外小众站点，全列出来对中文用户没有意义；用户真正想知道的是
# 「B 站 / 小宇宙 / 抖音 能不能用」。所以这里维护一份有代表性的清单，
# available 仍然以真实探测结果为准（yt-dlp 缺失时全部标不可用）。
# ---------------------------------------------------------------------------
PLATFORM_CATALOG: tuple[dict[str, object], ...] = (
    {
        "name": "bilibili",
        "label": "B 站",
        "domains": ["bilibili.com", "b23.tv"],
        "extractor": "bilibili",
        "kind": "long_video",
    },
    {
        "name": "xiaoyuzhou",
        "label": "小宇宙",
        "domains": ["xiaoyuzhoufm.com"],
        "extractor": "xiaoyuzhou",
        "kind": "podcast",
    },
    {
        "name": "podcast_rss",
        "label": "播客 RSS",
        "domains": ["feed.xyzfm.space", "feeds.acast.com", "anchor.fm", "libsyn.com"],
        "extractor": "podcastfeed",
        "kind": "podcast",
    },
    {
        "name": "youtube",
        "label": "YouTube",
        "domains": ["youtube.com", "youtu.be"],
        "ytdlp_keys": ["Youtube"],
        "kind": "long_video",
    },
    {
        "name": "douyin",
        "label": "抖音",
        "domains": ["douyin.com", "iesdouyin.com"],
        "ytdlp_keys": ["Douyin"],
        "kind": "short_video",
    },
    {
        "name": "xiaohongshu",
        "label": "小红书",
        "domains": ["xiaohongshu.com", "xhslink.com"],
        "ytdlp_keys": ["XiaoHongShu"],
        "kind": "image_text",
    },
    {
        "name": "ximalaya",
        "label": "喜马拉雅",
        "domains": ["ximalaya.com"],
        "ytdlp_keys": ["Ximalaya", "XimalayaAlbum"],
        "kind": "podcast",
    },
    {
        "name": "netease_music",
        "label": "网易云播客",
        "domains": ["music.163.com"],
        "ytdlp_keys": ["NetEaseMusicProgram"],
        "kind": "podcast",
    },
    {
        "name": "weibo",
        "label": "微博",
        "domains": ["weibo.com", "weibo.cn"],
        "ytdlp_keys": ["Weibo"],
        "kind": "short_video",
    },
    {
        "name": "tiktok",
        "label": "TikTok",
        "domains": ["tiktok.com"],
        "ytdlp_keys": ["TikTok"],
        "kind": "short_video",
    },
    {
        "name": "ted",
        "label": "TED",
        "domains": ["ted.com"],
        "ytdlp_keys": ["TedTalk"],
        "kind": "lecture",
    },
    {
        "name": "vimeo",
        "label": "Vimeo",
        "domains": ["vimeo.com"],
        "ytdlp_keys": ["Vimeo"],
        "kind": "long_video",
    },
    {
        "name": "zhihu",
        "label": "知乎",
        "domains": ["zhihu.com"],
        "ytdlp_keys": ["Zhihu"],
        "kind": "short_video",
    },
)


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
    """内置适配器。yt-dlp 放最后当兜底。

    RSS 排在 yt-dlp 前面：feed 里的 <enclosure> 是平台主动公开给订阅端的
    直链，比抓单集页面稳定；而且 yt-dlp 只能给一条 stream，拿不到
    shownotes 这类长播客真正需要的信息。
    """
    return [  # type: ignore[list-item]
        PodcastFeedExtractor(),
        XiaoyuzhouExtractor(),
        BilibiliExtractor(),
        YtDlpExtractor(),
    ]


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
            "链接解析已被显式关闭（EXTRACTORS_ENABLED=false）。"
            "设回 true（或删掉这一行）后重启服务即可恢复；"
            "开启后请仅用于处理你拥有版权的内容。"
        )

    clean = pick_url(url)
    ctx = ExtractContext(
        work_dir=work_dir,
        proxy=settings.proxy_url or None,
        cookie_file=settings.cookie_file or None,
        max_duration_sec=settings.max_media_duration_sec,
        max_bytes=settings.max_media_bytes,
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
    """给 /capabilities 用：列出各解析器的可用状态，前端据此提示。

    字段名保持不变（name / hosts / fallback / available / reason），
    只新增 label 等展示用字段，避免打破已有前端契约。
    """
    out: list[dict[str, object]] = []
    for inst in builtin_extractors():
        name = _alias(inst)
        available = getattr(inst, "available", lambda: True)()
        reason = getattr(inst, "unavailable_reason", lambda: None)() if not available else None
        entry: dict[str, object] = {
            "name": name,
            "label": _label_of(name),
            "hosts": list(getattr(inst, "hosts", ())),
            "fallback": not getattr(inst, "hosts", ()),
            "available": available,
            "reason": reason,
        }
        kind = _kind_of(name)
        if kind:
            entry["kind"] = kind
        out.append(entry)
    for name, inst in load_plugins().items():
        out.append(
            {
                "name": name,
                "label": name,
                "hosts": list(getattr(inst, "hosts", ())),
                "fallback": False,
                "available": True,
                "reason": None,
                "plugin": True,
            }
        )
    return out


def platform_catalog() -> list[dict[str, object]]:
    """面向用户的「支持哪些平台」清单，每个平台带 available 实测结果。

    和 platform_summary() 的区别：summary 是「哪些适配器在工作」（实现视角），
    catalog 是「用户能贴哪些链接」（产品视角）。前端展示用后者。
    """
    adapters = {item["name"]: item for item in _adapter_states()}
    notes = _ytdlp_key_index()

    out: list[dict[str, object]] = []
    for spec in PLATFORM_CATALOG:
        entry = dict(spec)
        extractor = spec.get("extractor")
        if extractor:
            state = adapters.get(str(extractor))
            entry["available"] = bool(state and state["available"])
            entry["reason"] = state["reason"] if state else "适配器未注册"
            entry["via"] = "builtin"
        else:
            keys = spec.get("ytdlp_keys") or []
            ytdlp = adapters.get("ytdlp")
            ytdlp_ok = bool(ytdlp and ytdlp["available"])
            # 只有 yt-dlp 真的支持这些 IE key 时才说可用：
            # 版本升级可能移走某个 extractor，报「支持」却解析不了最糟。
            missing = [k for k in keys if str(k) not in notes]
            entry["available"] = ytdlp_ok and not missing
            entry["reason"] = (
                None
                if entry["available"]
                else (
                    (ytdlp["reason"] if ytdlp and not ytdlp_ok else None)
                    or f"当前 yt-dlp 版本不含 {', '.join(missing)} 解析器，请升级 yt-dlp"
                )
            )
            entry["via"] = "yt-dlp"
        out.append(entry)
    return out


def _adapter_states() -> list[dict[str, object]]:
    states: list[dict[str, object]] = []
    for inst in builtin_extractors():
        available = getattr(inst, "available", lambda: True)()
        reason = getattr(inst, "unavailable_reason", lambda: None)() if not available else None
        states.append(
            {"name": _alias(inst), "available": available, "reason": reason}
        )
    return states


def _ytdlp_key_index() -> set[str]:
    """yt-dlp 的 extractor 短名集合（如 Youtube / BiliBili / Ximalaya）。

    只在 capabilities 里调用，且做了缓存：gen_extractor_classes 实测约 0.01s，
    但没必要每次请求都构建一遍。
    """
    global _YTDLP_KEYS
    if _YTDLP_KEYS is not None:
        return _YTDLP_KEYS
    keys: set[str] = set()
    try:
        from yt_dlp.extractor import gen_extractor_classes

        for cls in gen_extractor_classes():
            try:
                key = cls.ie_key()
            except Exception:  # noqa: BLE001 - 个别 extractor 的 ie_key 会抛
                continue
            if key:
                keys.add(str(key))
    except Exception:  # noqa: BLE001 - 未安装或版本异常都不该让 /capabilities 挂
        log.debug("读取 yt-dlp extractor 列表失败", exc_info=True)
    _YTDLP_KEYS = keys
    return keys


_YTDLP_KEYS: set[str] | None = None


def reset_ytdlp_key_cache() -> None:
    """仅供测试使用。"""
    global _YTDLP_KEYS
    _YTDLP_KEYS = None


def _label_of(name: str) -> str:
    for spec in PLATFORM_CATALOG:
        if spec.get("extractor") == name or spec.get("name") == name:
            return str(spec.get("label") or name)
    return name


def _kind_of(name: str) -> str | None:
    for spec in PLATFORM_CATALOG:
        if spec.get("extractor") == name or spec.get("name") == name:
            return str(spec.get("kind")) if spec.get("kind") else None
    return None
