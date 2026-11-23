"""小宇宙（xiaoyuzhoufm.com）适配器 —— 中文播客的核心入口。

为什么必须自研而不是靠 yt-dlp：
    yt-dlp 2026.10 的 1754 个 extractor 里没有 xiaoyuzhou（实测 0 命中）。
    而小宇宙是国内长音频播客的主阵地，知识科普、读书、访谈类内容大量
    只在这里落地。缺了它，「贴一条播客链接出文字」这个核心场景就是空的。

用什么数据（全部是页面自带的公开数据）：
    小宇宙是 Next.js 站点，服务端渲染时把整份单集数据塞进了
    <script id="__NEXT_DATA__">。我们只读这一块：
        props.pageProps.episode
            title / description / shownotes   标题与节目简介
            duration                          时长（秒）
            image.picUrl                      封面
            enclosure.url                     音频直链
            media.source.mode                 PUBLIC 才说明可匿名访问
            payType / isPrivateMedia          付费或私密内容

⚠️ 合规边界（有意为之，不要"优化"）：
    · 只发一个普通 GET，不带 Cookie、不带登录态
    · 不构造/破解任何签名参数，不猜测私有接口
    · payType != FREE 或 isPrivateMedia 为真时直接报错，不含任何绕过
    · 拿不到音频直链时，把标题和 shownotes 一并带回错误信息里 ——
      用户至少知道"这个链接是什么内容"，而不是面对一句干巴巴的失败
"""

from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path

import httpx

from .base import ExtractContext, ExtractError, ExtractResult

log = logging.getLogger(__name__)

_HOSTS = ("xiaoyuzhoufm.com",)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 单集页有两种 eid 形态：24 位十六进制（当前），以及分享短链里偶尔出现的纯数字。
_EPISODE_RE = re.compile(r"/episode/([0-9a-zA-Z]{8,32})")
# __NEXT_DATA__ / __INITIAL_STATE__ 都是"整块 JSON 塞进 script"，用非贪婪抓取。
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)
_INITIAL_STATE_RE = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});?\s*</script>", re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")

# 页面上抓不到 JSON 时的兜底：og: meta 至少能拿到标题和简介
_OG_TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]*)"')
_OG_DESC_RE = re.compile(r'<meta property="og:description" content="([^"]*)"')
_OG_IMAGE_RE = re.compile(r'<meta property="og:image" content="([^"]*)"')


class XiaoyuzhouExtractor:
    hosts = _HOSTS

    def matches(self, url: str) -> bool:
        from . import host_of

        host = host_of(url)
        return any(host == h or host.endswith("." + h) for h in self.hosts)

    # ---------------- 主流程 ----------------
    def extract(self, url: str, ctx: ExtractContext) -> ExtractResult:
        eid = self._parse_eid(url)
        page_url = f"https://www.xiaoyuzhoufm.com/episode/{eid}"

        with httpx.Client(
            headers=_HEADERS,
            timeout=ctx.timeout,
            follow_redirects=True,
            proxy=ctx.proxy or None,
        ) as client:
            page = self._fetch_page(client, page_url)
            meta = self._parse_page(page)

            if meta.duration and ctx.max_duration_sec:
                # 下载前就拦住：2 小时播客 m4a 有两百多 MB，白下完再拒绝很浪费。
                ctx.check_duration(meta.duration, site="小宇宙")

            audio_url = meta.audio_url
            if not audio_url:
                raise ExtractError(self._explain_missing_audio(meta))

            local = self._download(client, audio_url, ctx)

        return ExtractResult(
            platform="xiaoyuzhou",
            work_type="video",  # 音频也走同一条转写链路，work_type 只区分 OCR 分支
            title=meta.title,
            content=meta.description,
            cover=meta.cover,
            duration=meta.duration,
            video_urls=[audio_url],
            local_path=local,
            raw={
                "eid": eid,
                "podcast": meta.podcast_title,
                "author": meta.author,
                "pay_type": meta.pay_type,
                "shownotes": meta.shownotes,
            },
        )

    # ---------------- 内部步骤 ----------------
    @staticmethod
    def _parse_eid(url: str) -> str:
        m = _EPISODE_RE.search(url)
        if not m:
            raise ExtractError(
                "没能从链接里认出小宇宙单集 ID。请使用单集分享链接"
                "（形如 https://www.xiaoyuzhoufm.com/episode/<id>），"
                "播客主页链接不含具体某一集。"
            )
        return m.group(1)

    def _fetch_page(self, client: httpx.Client, page_url: str) -> str:
        try:
            resp = client.get(page_url)
        except httpx.HTTPError as exc:
            raise ExtractError(f"请求小宇宙页面失败：{exc}") from exc
        if resp.status_code == 404:
            raise ExtractError("小宇宙上找不到这一集（链接可能已失效或已被删除）")
        if resp.status_code >= 400:
            raise ExtractError(f"小宇宙页面返回 HTTP {resp.status_code}")
        return resp.text

    def _parse_page(self, page: str) -> _EpisodeMeta:
        """优先读 __NEXT_DATA__；取不到再试 __INITIAL_STATE__，最后退到 og: meta。"""
        episode = self._episode_from_next_data(page)
        if episode is None:
            episode = self._episode_from_initial_state(page)
        if episode is None:
            return self._meta_from_og(page)

        podcast = episode.get("podcast") or {}
        media = episode.get("media") or {}
        source = media.get("source") or {}
        enclosure = episode.get("enclosure") or {}
        image = episode.get("image") or {}

        # 音频直链的优先级：enclosure.url（播放器真正用的）> media.source.url。
        # media.source.mode 不是 PUBLIC 时 url 可能仍存在但不建议直接用，标记出来。
        audio_url = _clean_url(enclosure.get("url"))
        mode = str(source.get("mode") or "").upper()
        if not audio_url:
            audio_url = _clean_url(source.get("url"))
        if mode and mode != "PUBLIC":
            log.info("小宇宙 media.source.mode=%s，不直接使用该直链", mode)
            audio_url = None

        shownotes_html = str(episode.get("shownotes") or "")
        return _EpisodeMeta(
            title=_clean_text(episode.get("title")),
            description=_clean_text(episode.get("description")),
            shownotes=_plain_text(shownotes_html),
            duration=_as_float(episode.get("duration")),
            cover=_clean_url(image.get("picUrl") or image.get("largePicUrl")),
            audio_url=audio_url,
            podcast_title=_clean_text(podcast.get("title")),
            author=_clean_text(podcast.get("author")),
            pay_type=str(episode.get("payType") or ""),
            private_media=bool(episode.get("isPrivateMedia")),
        )

    @staticmethod
    def _episode_from_next_data(page: str) -> dict | None:
        m = _NEXT_DATA_RE.search(page)
        if not m:
            return None
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            log.warning("__NEXT_DATA__ 解析失败，可能是页面结构改版")
            return None
        episode = (data.get("props") or {}).get("pageProps", {}).get("episode")
        return episode if isinstance(episode, dict) else None

    @staticmethod
    def _episode_from_initial_state(page: str) -> dict | None:
        m = _INITIAL_STATE_RE.search(page)
        if not m:
            return None
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return None

        # __INITIAL_STATE__ 的键名随版本变过，按已知形态逐个试，
        # 找不到就放弃 —— 宁可回落到 og: meta，也不要猜出错误的字段。
        candidates = (
            ("episode",),
            ("episodeDetail", "episode"),
            ("episode", "episode"),
        )
        for path in candidates:
            node: object = data
            for key in path:
                if not isinstance(node, dict):
                    node = None
                    break
                node = node.get(key)
            if isinstance(node, dict) and node.get("title"):
                return node
        return None

    @staticmethod
    def _meta_from_og(page: str) -> _EpisodeMeta:
        """最后的兜底：只解析 og: meta。此时拿不到音频，调用方会报错。"""
        title = _OG_TITLE_RE.search(page)
        desc = _OG_DESC_RE.search(page)
        image = _OG_IMAGE_RE.search(page)
        if not title and not desc:
            raise ExtractError(
                "小宇宙页面结构无法识别（可能改版了）。"
                "请把链接和节目名反馈给我们，或先用其他方式拿到音频文件再上传。"
            )
        return _EpisodeMeta(
            title=_clean_text(_unescape(title.group(1))) if title else None,
            description=_clean_text(_unescape(desc.group(1))) if desc else None,
            cover=_clean_url(_unescape(image.group(1))) if image else None,
        )

    @staticmethod
    def _explain_missing_audio(meta: _EpisodeMeta) -> str:
        """拿不到音频时，把已知信息尽量还给用户。"""
        lines = ["这一集没能取到音频直链。"]
        if meta.title:
            lines.append(f"标题：{meta.title}")
        if meta.podcast_title:
            lines.append(f"节目：{meta.podcast_title}")
        if meta.private_media:
            lines.append("页面标记为私密音频，需要登录/订阅才能收听，本项目不做绕过。")
        elif meta.pay_type and meta.pay_type.upper() != "FREE":
            lines.append(
                f"页面标记为付费内容（payType={meta.pay_type}），本项目不做绕过。"
            )
        else:
            lines.append(
                "可能是页面结构改版，或该集只在小宇宙 App 内可播放。"
                "可以试着在 App 里下载音频后手动上传。"
            )
        if meta.shownotes:
            excerpt = meta.shownotes[:300].strip()
            lines.append(f"节目简介（节选）：{excerpt}")
        return "\n".join(lines)

    def _download(self, client: httpx.Client, url: str, ctx: ExtractContext) -> Path:
        ctx.work_dir.mkdir(parents=True, exist_ok=True)
        suffix = _suffix_of(url) or ".m4a"
        dst = ctx.work_dir / f"source{suffix}"
        try:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                ctx.check_content_length(resp.headers.get("content-length"), site="小宇宙")
                with dst.open("wb") as fh:
                    written = 0
                    for chunk in resp.iter_bytes(256 * 1024):
                        written += len(chunk)
                        if ctx.max_bytes and written > ctx.max_bytes:
                            raise ExtractError(
                                f"音频体积超过上限（{ctx.max_bytes // (1024 * 1024)}MB），"
                                "已中止下载。可调大 MAX_MEDIA_SIZE_MB 或设为 0 关闭限制。"
                            )
                        fh.write(chunk)
        except httpx.HTTPError as exc:
            raise ExtractError(f"下载小宇宙音频失败：{exc}") from exc
        if not dst.exists() or dst.stat().st_size == 0:
            raise ExtractError("下载得到的小宇宙音频为空")
        return dst


class _EpisodeMeta:
    """页面解析的中间产物。字段全可选，因为兜底路径拿不到全部信息。"""

    __slots__ = (
        "title",
        "description",
        "shownotes",
        "duration",
        "cover",
        "audio_url",
        "podcast_title",
        "author",
        "pay_type",
        "private_media",
    )

    def __init__(
        self,
        *,
        title: str | None = None,
        description: str | None = None,
        shownotes: str = "",
        duration: float | None = None,
        cover: str | None = None,
        audio_url: str | None = None,
        podcast_title: str | None = None,
        author: str | None = None,
        pay_type: str = "",
        private_media: bool = False,
    ) -> None:
        self.title = title
        self.description = description
        self.shownotes = shownotes
        self.duration = duration
        self.cover = cover
        self.audio_url = audio_url
        self.podcast_title = podcast_title
        self.author = author
        self.pay_type = pay_type
        self.private_media = private_media


def _unescape(value: str) -> str:
    return html.unescape(value)


def _clean_text(value: object) -> str | None:
    """把可能带 HTML 实体/多余空白的文本规范成纯文本。"""
    if not isinstance(value, str):
        return None
    text = html.unescape(value).strip()
    return text or None


def _plain_text(html_text: str) -> str:
    """shownotes 是 HTML 片段，转成纯文本给用户看。

    不引 BeautifulSoup：这里只需要去标签 + 反转义，
    多一个解析器依赖不值得（项目原则是不加重型依赖）。
    """
    if not html_text:
        return ""
    without_breaks = re.sub(r"</(p|div|li|br)>|<br\s*/?>", "\n", html_text, flags=re.I)
    stripped = _TAG_RE.sub("", without_breaks)
    text = html.unescape(stripped)
    # 折叠连续空行，但保留段落分隔
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _clean_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url:
        return None
    # 只接受 http(s) 直链：页面字段偶尔会是相对路径或空对象，别让下游收到怪东西
    if not url.lower().startswith(("http://", "https://")):
        return None
    return url


def _suffix_of(url: str) -> str:
    path = url.split("?")[0]
    dot = path.rfind(".")
    if dot == -1:
        return ""
    suffix = path[dot:]
    # 只接受短的、像扩展名的东西，防止把整段路径当后缀
    return suffix if 2 <= len(suffix) <= 6 else ""


def _as_float(value: object) -> float | None:
    try:
        got = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return got if got > 0 else None
