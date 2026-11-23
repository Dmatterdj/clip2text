"""链接解析（默认关闭）。

⚠️ 合规提示
    本模块仅用于处理你自己拥有版权的内容，或已获得授权的内容。
    使用前请确认符合目标平台的服务条款与《著作权法》。
    仓库内不得包含任何平台的签名密钥、登录 Cookie 或其他绕过手段。

⚠️ 默认关闭
    EXTRACTORS_ENABLED=false 时整条链路不可用，前端也不会显示粘贴链接入口。
    这是有意为之：绝大多数用户只需要「上传自己的文件」这一条主链路。

架构
    · 具体平台适配器由独立包通过 entry_points 注册（group: clip2text.extractors）
    · 内置的 yt-dlp 包装只覆盖它已原生支持的平台
    · 新增平台不必改主仓库：社区单独发一个 pip 包即可
"""

from __future__ import annotations

import importlib.metadata
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..config import Settings

log = logging.getLogger(__name__)

ENTRY_GROUP = "clip2text.extractors"

# 用户经常直接粘贴整段分享文案，先抽链接
_URL_RE = re.compile(r"https?://[^\s，,、；;\"'<>）)】]+")

SHORT_HOSTS = ("xhslink.com", "v.douyin.com", "b23.tv", "t.cn", "url.cn", "v.kuaishou.com")


class ExtractError(RuntimeError):
    pass


@dataclass
class ExtractResult:
    platform: str
    work_type: str = "video"
    title: str | None = None
    content: str | None = None
    cover: str | None = None
    duration: float | None = None
    video_urls: list[str] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    local_path: Path | None = None
    raw: dict | None = None


@runtime_checkable
class Extractor(Protocol):
    hosts: tuple[str, ...]

    def matches(self, url: str) -> bool: ...

    def extract(self, url: str, ctx: "ExtractContext") -> ExtractResult: ...


@dataclass
class ExtractContext:
    work_dir: Path
    settings: Settings
    proxy: str | None = None


def pick_url(text: str) -> str:
    """从一段分享文案里抽出第一个 http(s) 链接。"""
    m = _URL_RE.search(text or "")
    if not m:
        raise ExtractError("没有找到有效链接")
    url = m.group(0).rstrip("。，,、；;）)】\"'")
    return url


def load_plugins() -> dict[str, Extractor]:
    """加载所有通过 entry_points 注册的第三方适配器。"""
    found: dict[str, Extractor] = {}
    try:
        eps = importlib.metadata.entry_points(group=ENTRY_GROUP)
    except TypeError:  # Python < 3.10 的旧签名
        eps = importlib.metadata.entry_points().get(ENTRY_GROUP, [])  # type: ignore[attr-defined]
    for ep in eps:
        try:
            factory = ep.load()
            inst = factory() if callable(factory) else factory
            found[ep.name] = inst
            log.debug("加载解析器插件: %s", ep.name)
        except Exception:  # noqa: BLE001 - 单个插件坏了不能拖垮整体
            log.exception("解析器插件加载失败: %s", ep.name)
    return found


def host_of(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def extract(url: str, work_dir: Path, settings: Settings) -> ExtractResult:
    """统一入口：抽链接 → 尝试插件 → 回退 yt-dlp。"""
    if not settings.extractors_enabled:
        raise ExtractError(
            "链接解析默认关闭，请设置 EXTRACTORS_ENABLED=true 后重启。"
        )

    clean = pick_url(url)
    ctx = ExtractContext(work_dir=work_dir, settings=settings, proxy=settings.proxy_url or None)

    allow = settings.allowed_extractors
    plugins = load_plugins()
    if allow:
        plugins = {k: v for k, v in plugins.items() if k in allow}

    for name, plugin in plugins.items():
        try:
            if plugin.matches(clean):
                log.info("使用插件解析: %s", name)
                return plugin.extract(clean, ctx)
        except Exception:  # noqa: BLE001
            log.exception("插件 %s 解析失败，回退 yt-dlp", name)

    return _extract_with_ytdlp(clean, ctx)


def _extract_with_ytdlp(url: str, ctx: ExtractContext) -> ExtractResult:
    try:
        import yt_dlp
    except ImportError as exc:
        raise ExtractError(
            "未安装 yt-dlp。运行 `pip install 'clip2text[extract]'` 后再试。"
        ) from exc

    ctx.work_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(ctx.work_dir / "source.%(ext)s")
    opts: dict[str, object] = {
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
    }
    if ctx.proxy:
        opts["proxy"] = ctx.proxy
    if ctx.settings.cookie_file:
        opts["cookiefile"] = ctx.settings.cookie_file

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path_str = ydl.prepare_filename(info)
    except Exception as exc:  # noqa: BLE001 - yt-dlp 抛的异常类型非常多
        raise ExtractError(f"解析失败：{exc}") from exc

    local = Path(path_str)
    if not local.exists():
        # merge_output_format 可能改扩展名
        candidates = list(ctx.work_dir.glob("source.*"))
        if not candidates:
            raise ExtractError("下载完成但找不到本地文件")
        local = candidates[0]

    video_urls = []
    if info.get("url"):
        video_urls.append(str(info["url"]))
    for f in info.get("formats") or []:
        u = f.get("url")
        if u and u not in video_urls:
            video_urls.append(str(u))
            break

    return ExtractResult(
        platform=str(info.get("extractor_key") or host_of(url) or "unknown").lower(),
        work_type="image" if "image" in str(info.get("ext") or "") else "video",
        title=info.get("title"),
        content=info.get("description"),
        cover=info.get("thumbnail"),
        duration=info.get("duration"),
        video_urls=video_urls[:3],
        local_path=local,
        raw={k: info.get(k) for k in ("id", "webpage_url", "ext", "uploader") if k in info},
    )
