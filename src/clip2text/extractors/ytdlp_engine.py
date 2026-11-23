"""yt-dlp 适配器 —— 覆盖绝大多数平台的主力实现。

yt-dlp 已原生支持（实测 yt-dlp 2026.10，1754 个 extractor）：
    YouTube · B 站全系 · 抖音 Douyin · 小红书 XiaoHongShu · 微博 Weibo
    TikTok · 喜马拉雅 Ximalaya（含 Album）· 网易云音乐/播客 NetEaseMusicProgram
    网易云 DJ 电台 · QQ 音乐 · TED（TedTalk）· Vimeo · Twitch
    播客托管商：acast · Libsyn · 以及 21 个 *Podcast 类 extractor

不支持：快手、微信视频号（yt-dlp 里没有对应 extractor，实测 0 命中）。

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
        # yt-dlp 已经是核心依赖，正常安装不会有这个问题；
        # 提示里带上安装方式是因为有人会用 --no-deps 之类的方式装。
        return "未安装 yt-dlp。运行 `pip install yt-dlp`（或重装 clip2text）后再试。"

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
            # noplaylist=True：合集/多 P 只处理第一条。
            # 这不是"够用就行"，而是这个产品的定位——一条链接出一份文字。
            # 多 P 的情况由内置适配器（B 站）给出明确的清单提示，
            # 免得用户以为整场讲座都转完了。
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
                info = ydl.extract_info(url, download=False)
                # 元信息先到手再决定要不要真下：两小时的 4K 讲座直接开下
                # 可能白下几个 GB，超出上限还白等。
                ctx.check_duration(_as_float(info.get("duration")), site="该平台")
                ctx.check_content_length(
                    _approx_size(info, ctx.max_bytes), site="该平台"
                )
                info = ydl.extract_info(url, download=True)
                prepared = ydl.prepare_filename(info)
        except ExtractError:
            raise
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
            content=self._compose_content(info),
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
    def _compose_content(info: dict) -> str | None:
        """简介 + 合集提示。

        yt-dlp 配了 noplaylist=True，遇到合集只处理第一条。这件事必须
        写在结果里，否则用户会以为整个合集都转完了。
        """
        desc = (info.get("description") or "").strip() or None
        n_entries = info.get("n_entries")
        if isinstance(n_entries, int) and n_entries > 1:
            hint = f"⚠️ 这个链接是一个合集（共 {n_entries} 条），本次只处理了第 1 条。"
            return f"{hint}\n\n{desc}" if desc else hint
        return desc

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


def _approx_size(info: dict, max_bytes: int | None) -> str | None:
    """把 yt-dlp 元信息里的预估体积转成 Content-Length 的样子。

    filesize 常在下载前未知，filesize_approx 更常可用；两个都没有就返回
    None（check_content_length 拿到 None 会放行，由下载循环兜底）。
    """
    if not max_bytes:
        return None
    size = info.get("filesize") or info.get("filesize_approx")
    return str(size) if isinstance(size, int) and size > 0 else None


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
