"""yt-dlp 适配器 —— 覆盖绝大多数平台的主力实现。

yt-dlp 已原生支持（截至 2025 年）：
    小红书 XiaoHongShu · 抖音 Douyin · B 站全系 · 微博 Weibo
    TikTok · YouTube · 今日头条 · 西瓜 Ixigua · AcFun

不支持（需自研插件）：快手、视频号、拼多多视频。

为什么不自己写这些平台的解析：
    它们的接口签名（X-s / a_bogus）几天一变，写进主仓库等于给自己
    挖维护坑，还会牵连项目的合规风险。yt-dlp 由社区持续跟进，
    让它去打这场消耗战更合理。
"""

from __future__ import annotations

import logging
from pathlib import Path

from .base import ExtractContext, ExtractError, ExtractResult

log = logging.getLogger(__name__)


class YtDlpExtractor:
    """兜底适配器：hosts 为空表示"谁都能接"，调用方放在最后。"""

    hosts: tuple[str, ...] = ()

    def __init__(self) -> None:
        self._available: bool | None = None

    def available(self) -> bool:
        if self._available is None:
            try:
                import yt_dlp  # noqa: F401

                self._available = True
            except ImportError:
                self._available = False
        return self._available

    def unavailable_reason(self) -> str | None:
        if self.available():
            return None
        return "未安装 yt-dlp。运行 `pip install 'clip2text[extract]'` 后再试。"

    def matches(self, url: str) -> bool:
        return self.available()

    def extract(self, url: str, ctx: ExtractContext) -> ExtractResult:
        if not self.available():
            raise ExtractError(self.unavailable_reason() or "yt-dlp 不可用")

        import yt_dlp

        ctx.work_dir.mkdir(parents=True, exist_ok=True)
        opts: dict[str, object] = {
            "outtmpl": str(ctx.work_dir / "source.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            # 只要单文件流：合并需要 ffmpeg，且音频足够做转写
            "format": "bestaudio/best",
            "socket_timeout": ctx.timeout,
        }
        if ctx.proxy:
            opts["proxy"] = ctx.proxy
        if ctx.cookie_file:
            opts["cookiefile"] = ctx.cookie_file

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                prepared = ydl.prepare_filename(info)
        except Exception as exc:  # noqa: BLE001 - yt-dlp 异常类型非常多
            raise ExtractError(self._humanize(exc)) from exc

        local = Path(prepared)
        if not local.exists():
            candidates = sorted(ctx.work_dir.glob("source.*"))
            if not candidates:
                raise ExtractError("下载完成但找不到本地文件，请检查磁盘空间")
            local = candidates[0]

        return ExtractResult(
            platform=str(info.get("extractor_key") or "unknown").lower(),
            work_type="image" if _looks_like_image(local) else "video",
            title=info.get("title"),
            content=info.get("description"),
            cover=info.get("thumbnail"),
            duration=_as_float(info.get("duration")),
            video_urls=_pick_urls(info),
            local_path=local,
            raw={
                k: info.get(k)
                for k in ("id", "webpage_url", "ext", "uploader", "extractor")
                if k in info
            },
        )

    @staticmethod
    def _humanize(exc: Exception) -> str:
        """把 yt-dlp 的英文异常翻译成用户能照做的中文提示。"""
        text = str(exc)
        low = text.lower()
        if "unsupported url" in low:
            return (
                "yt-dlp 暂不支持这个链接。若是快手/视频号，"
                "需要装对应的插件包（见 README 的插件说明）。"
            )
        if "sign in" in low or "login" in low or "cookie" in low:
            return "该内容需要登录才能访问。请用 COOKIE_FILE 配置登录态后重试。"
        if "private" in low or "unavailable" in low:
            return "内容不可访问（可能已删除、设为私密，或地区限制）。"
        if "timed out" in low or "timeout" in low:
            return "下载超时。可尝试配置 PROXY_URL，或稍后重试。"
        if "georestricted" in low or "geo" in low:
            return "该内容有地区限制，需要配置相应地区的代理。"
        return f"解析失败：{text.splitlines()[0][:300]}"


def _as_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _looks_like_image(path: Path) -> bool:
    return path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def _pick_urls(info: dict) -> list[str]:
    """收集可直接下载的媒体直链（给前端展示/二次下载用）。"""
    urls: list[str] = []
    if info.get("url"):
        urls.append(str(info["url"]))
    for fmt in info.get("formats") or []:
        u = fmt.get("url")
        if u and u not in urls:
            urls.append(str(u))
        if len(urls) >= 3:
            break
    return urls
