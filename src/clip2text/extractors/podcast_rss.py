"""播客 RSS / Atom feed 适配器 —— 比抓任意平台页面都稳的长音频入口。

为什么值得单独做一个（而不是全靠各平台页面）：
    RSS 是播客行业的官方分发协议。几乎所有播客托管商都会生成 feed，
    小宇宙官方的「导出 feed」也是 `https://feed.xyzfm.space/<id>`。
    feed 里的 `<enclosure url=...>` 就是音频直链，是平台主动公开给
    订阅端（Apple Podcasts / Overcast / 所有泛用型客户端）拉取的，
    所以它比抓某个站点的单集网页更稳定，也更符合「只用公开数据」。

能处理两种输入：
    1. 直接的 feed 链接（.xml / /feed / /rss / podcast 托管商域名）
    2. 单集链接（feed 里某个 <item> 的 <link>），配合 feed_url 反查

    pyt orchestrator（pipeline）只传一条 URL 时走第 1 种；传
    `<feed_url>#<episode_url>` 时走第 2 种（见 split_episode_ref）。

不引入 feedparser 依赖：stdlib 的 xml.etree 足够解析 RSS 2.0 / Atom，
而项目原则是不加重型依赖。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx

from .base import ExtractContext, ExtractError, ExtractResult

log = logging.getLogger(__name__)

# 常见播客托管商：用户粘上来的多为单集页，但域名能看出它是播客 feed 生态
FEED_HOSTS = (
    "feed.xyzfm.space",
    "feeds.acast.com",
    "anchor.fm",
    "feeds.simplecast.com",
    "feeds.megaphone.fm",
    "megaphone.fm",
    "libsyn.com",
    "fireside.fm",
    "buzzsprout.com",
    "transistor.fm",
    "podbean.com",
    "simplecast.com",
    "omny.fm",
    "art19.com",
    "podtrac.com",
    "feeds.buzzsprout.com",
    "rss.com",
)

# 看起来像 feed 的路径特征（用于用户直接贴站点内 feed 地址时）。
# 语义：按「路径段」或「文件名后缀」匹配，不是子串匹配 —— 见 is_feed_url。
_FEED_PATH_HINTS = ("feed", "rss", "podcast", "feed.xml", "rss.xml", "podcast.xml")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}

# feed 大小上限：正常播客 feed（几百集）也就几 MB。
_MAX_FEED_BYTES = 32 * 1024 * 1024


@dataclass
class _Item:
    title: str | None = None
    description: str | None = None
    audio_url: str | None = None
    duration: float | None = None
    link: str | None = None
    published: str | None = None
    image: str | None = None


class PodcastFeedExtractor:
    """RSS / Atom 播客 feed。hosts 为空表示「按 URL 形态判断」，不按域名。

    之所以不按域名枚举：feed 可以挂在任何域名下（自建博客、公司站点、
    静态托管），按域名匹配会把绝大多数自建播客漏掉。
    """

    hosts: tuple[str, ...] = ()

    def matches(self, url: str) -> bool:
        return self.is_feed_url(url) or self.has_episode_ref(url)

    @staticmethod
    def is_feed_url(url: str) -> bool:
        host = _host(url)
        path = (_path(url) or "").lower()
        if any(host == h or host.endswith("." + h) for h in FEED_HOSTS):
            return True
        # 必须按路径「段」匹配，不能用 `"/feed" in path`：
        # 后者会把 https://x.com/feedback、/rsshelp 这类普通页面也当成 feed，
        # 白白发一次请求并污染错误信息（最终会回落到 yt-dlp，但没必浪费）。
        return any(_path_has_segment(path, hint) for hint in _FEED_PATH_HINTS) or (
            path.endswith((".xml", ".rss", ".atom"))
        )

    @staticmethod
    def has_episode_ref(url: str) -> bool:
        """`<feed_url>#<episode_url>` 形态：由 pipeline 在已知 feed 时拼接。"""
        return "#" in url and PodcastFeedExtractor.is_feed_url(url.split("#", 1)[0])

    # ---------------- 主流程 ----------------
    def extract(self, url: str, ctx: ExtractContext) -> ExtractResult:
        feed_url, episode_url = split_episode_ref(url)

        with httpx.Client(
            headers=_HEADERS,
            timeout=ctx.timeout,
            follow_redirects=True,
            proxy=ctx.proxy or None,
        ) as client:
            root = self._fetch_feed(client, feed_url)
            channel = self._parse_channel(root)
            items = self._parse_items(root)

            if not items:
                raise ExtractError(
                    "这个 feed 里没有单集（<item> 为空）。"
                    "确认它是播客 feed 而不是博客文章 feed。"
                )

            item = self._pick_item(items, episode_url)
            if item is None:
                raise ExtractError(
                    f"feed 里找不到对应的单集（共 {len(items)} 集）。"
                    "请确认单集链接和 feed 是同一个节目。"
                )
            if not item.audio_url:
                raise ExtractError(
                    f"这一集没有音频直链（<enclosure url> 缺失）：{item.title or '未命名'}。"
                    "可能是纯文字公告集，或托管商只对 App 开放。"
                )

            ctx.check_duration(item.duration, site="播客")
            local = self._download(client, item.audio_url, ctx)

        return ExtractResult(
            platform="podcast",
            work_type="video",  # 音频走同一条转写链路，只用于区分 OCR 分支
            title=item.title or channel.get("title"),
            content=item.description,
            cover=item.image or channel.get("image"),
            duration=item.duration,
            video_urls=[item.audio_url],
            local_path=local,
            raw={
                "feed_url": feed_url,
                "podcast": channel.get("title"),
                "author": channel.get("author"),
                "link": item.link,
                "published": item.published,
                "episode_count": len(items),
            },
        )

    # ---------------- 网络 ----------------
    def _fetch_feed(self, client: httpx.Client, feed_url: str) -> ET.Element:
        try:
            resp = client.get(feed_url)
        except httpx.HTTPError as exc:
            raise ExtractError(f"请求 feed 失败：{exc}") from exc
        if resp.status_code >= 400:
            raise ExtractError(f"feed 返回 HTTP {resp.status_code}：{feed_url}")

        body = resp.content
        # feed 是用户给的 URL，不能无限信任：几百集的节目 feed 也只有几 MB，
        # 超过上限的多半不是 feed（或是恶意构造）。stdlib 的 ElementTree
        # 对实体膨胀没有防护，所以必须在解析前拦一道。
        if len(body) > _MAX_FEED_BYTES:
            raise ExtractError(
                f"feed 体积 {len(body) // 1024}KB 超过上限 "
                f"{_MAX_FEED_BYTES // 1024}KB，多半不是正常的播客 feed。"
            )

        text = body.decode(resp.encoding or "utf-8", errors="replace").lstrip("\ufeff \r\n\t")
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise ExtractError(
                f"这不是可解析的 RSS/Atom：{exc}。"
                "如果链接指向的是播客单集网页，请改用节目真正的 feed 地址。"
            ) from exc
        return _strip_namespaces(root)

    # ---------------- 解析 ----------------
    def _parse_channel(self, root: ET.Element) -> dict[str, str | None]:
        """频道级元信息。RSS 2.0 与 Atom 的标签名不同，两套都试。"""
        channel = root.find("channel")
        node = channel if channel is not None else root
        return {
            "title": _text(node, "title"),
            "author": _channel_author(node),
            "image": _channel_image(node),
            "description": _text(node, "description") or _text(node, "subtitle"),
        }

    def _parse_items(self, root: ET.Element) -> list[_Item]:
        channel = root.find("channel")
        node = channel if channel is not None else root
        # RSS: <item>；Atom: <entry>
        raw_items = node.findall("item") or node.findall("entry")

        items: list[_Item] = []
        for raw in raw_items:
            item = _Item(
                title=_text(raw, "title"),
                description=_clean_html(
                    _text(raw, "description")
                    or _text(raw, "summary")
                    or _text(raw, "content")
                    or _itunes_text(raw, "summary")
                    or ""
                ),
                audio_url=_enclosure_url(raw),
                duration=_parse_duration(
                    _itunes_text(raw, "duration") or _text(raw, "duration")
                ),
                link=_episode_link(raw),
                published=_text(raw, "pubDate") or _text(raw, "published")
                or _text(raw, "updated"),
                image=_item_image(raw),
            )
            items.append(item)
        return items

    @staticmethod
    def _pick_item(items: list[_Item], episode_url: str | None) -> _Item | None:
        """有指定单集就按 link 匹配，否则取最新一集（feed 默认按时间倒序）。"""
        if not episode_url:
            return items[0]
        target = _normalize_url(episode_url)
        for item in items:
            if item.link and _normalize_url(item.link) == target:
                return item
        # 有些托管商的 <link> 带追踪参数，退一步做「包含」匹配
        for item in items:
            if item.link and target in _normalize_url(item.link):
                return item
        return None

    def _download(self, client: httpx.Client, url: str, ctx: ExtractContext) -> Path:
        ctx.work_dir.mkdir(parents=True, exist_ok=True)
        suffix = _suffix_of(url) or ".mp3"
        dst = ctx.work_dir / f"source{suffix}"
        try:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                ctx.check_content_length(resp.headers.get("content-length"), site="播客")
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
            raise ExtractError(f"下载播客音频失败：{exc}") from exc
        if not dst.exists() or dst.stat().st_size == 0:
            raise ExtractError("下载得到的播客音频为空")
        return dst


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
# 注意：feed 里 itunes:duration 才是可靠的时长，<enclosure length> 是字节数。
# 命名空间统一在 _strip_namespaces() 里抹掉，所以这里不保留 _NS 表 --
# 留着它反而会诱导后来的人写出 find("itunes:duration") 这种在 Atom 上必挂的代码。

def split_episode_ref(url: str) -> tuple[str, str | None]:
    """拆 `<feed>#<episode>`；没有 # 时整串都是 feed。"""
    if "#" in url:
        feed, _, episode = url.partition("#")
        if feed and episode:
            return feed, episode
    return url, None


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _path(url: str) -> str:
    try:
        return urlparse(url).path or ""
    except ValueError:
        return ""


def _path_has_segment(path: str, hint: str) -> bool:
    """路径里是否含某个「整段」（或整段文件名），不做子串匹配。

    为什么不用 `hint in path`：那样 `/feedback`、`/rsshelp` 都会命中，
    适配器会白白对普通页面发一次请求，还会在最终错误信息里留下一堆噪音。
    """
    if path.endswith("/" + hint):
        return True
    segments = [s for s in path.split("/") if s]
    return hint in segments


def _text(node: ET.Element | None, tag: str) -> str | None:
    """安全取子元素文本。node 为 None 也返回 None，避免层层判空。"""
    if node is None:
        return None
    child = node.find(tag)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def _itunes_text(node: ET.Element, tag: str) -> str | None:
    """取 iTunes 扩展字段。

    命名空间在 _strip_namespaces() 里已经抹掉了，所以这里直接按本地名找：
    `<itunes:duration>` 变成了 `<duration>`。RSS 2.0 没有同名核心标签，
    不会误取。
    """
    return _text(node, tag)


def _strip_namespaces(root: ET.Element) -> ET.Element:
    """把 `{namespace}tag` 全部还原成 `tag`。

    为什么要这一步：Atom feed 普遍用默认命名空间
    （`xmlns="http://www.w3.org/2005/Atom"`），此时 `find("title")` 一个都
    匹配不到 —— 表现为「feed 解析成功但标题全空」。抹掉命名空间是处理
    两种 feed 格式最省事也最不容易出错的做法。
    """
    for node in root.iter():
        if isinstance(node.tag, str) and "}" in node.tag:
            node.tag = node.tag.rsplit("}", 1)[1]
    return root


def _enclosure_url(item: ET.Element) -> str | None:
    """音频直链。RSS 用 <enclosure>，Atom 用 <link rel="enclosure">。"""
    enc = item.find("enclosure")
    if enc is not None:
        url = (enc.get("url") or "").strip()
        # type 可能是 video/mpeg 之类，只要 URL 是 http(s) 就可作为音频源
        if url.lower().startswith(("http://", "https://")):
            return url

    for link in item.findall("link"):
        if (link.get("rel") or "") == "enclosure":
            href = (link.get("href") or "").strip()
            if href.lower().startswith(("http://", "https://")):
                return href
    return None


def _episode_link(item: ET.Element) -> str | None:
    link = _text(item, "link")
    if link:
        return link
    for node in item.findall("link"):
        href = node.get("href")
        rel = node.get("rel") or "alternate"
        if href and rel == "alternate":
            return href.strip()
    guid = _text(item, "guid")
    if guid and guid.lower().startswith(("http://", "https://")):
        return guid
    return None


def _channel_author(node: ET.Element) -> str | None:
    """频道作者。三种写法都要认：
    · RSS 2.0：<itunes:author>（最常见）或 <author>（邮箱格式，通常是编辑而非主播）
    · Atom：<author><name>名字</name></author>
    """
    itunes = _text(node, "author")
    # RSS 的 <author> 常是 "email (Name)"，可读性差，优先用 itunes:author
    if itunes and "@" not in itunes:
        return itunes
    author_node = node.find("author")
    if author_node is not None:
        name = _text(author_node, "name")
        if name:
            return name
        if author_node.text and author_node.text.strip():
            return author_node.text.strip()
    return itunes


def _channel_image(node: ET.Element) -> str | None:
    image = node.find("image")
    if image is not None:
        url = _text(image, "url")
        if url:
            return url
    itunes = node.find("image")
    if itunes is not None:
        href = itunes.get("href")
        if href:
            return href.strip()
    return None


def _item_image(item: ET.Element) -> str | None:
    itunes = item.find("image")
    if itunes is not None:
        href = itunes.get("href")
        if href:
            return href.strip()
    return None


_DURATION_HMS = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)\s*$")


def _parse_duration(value: object) -> float | None:
    """itunes:duration 有三种写法：秒数、"MM:SS"、"HH:MM:SS"。

    ISO 8601（Atom 的 <duration>PT1H2M3S</duration>）也顺手支持。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    if text.upper().startswith("PT"):
        return _parse_iso_duration(text)

    m = _DURATION_HMS.match(text)
    if m:
        hours = int(m.group(1) or 0)
        minutes = int(m.group(2))
        seconds = float(m.group(3))
        return float(hours * 3600 + minutes * 60 + seconds) or None
    try:
        got = float(text)
    except ValueError:
        return None
    return got if got > 0 else None


def _parse_iso_duration(text: str) -> float | None:
    m = re.match(
        r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$", text.upper()
    )
    if not m:
        return None
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    seconds = float(m.group(3) or 0)
    total = hours * 3600 + minutes * 60 + seconds
    return total or None


_TAG_RE = re.compile(r"<[^>]+>")


def _clean_html(value: str) -> str | None:
    """feed 的 description 常带 HTML，去掉标签给用户看纯文本。"""
    if not value:
        return None
    import html as html_mod

    stripped = _TAG_RE.sub("", value)
    text = html_mod.unescape(stripped)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def _normalize_url(url: str) -> str:
    """比对 feed 里的 <link> 与用户贴的链接：忽略 scheme 大小写、末尾斜杠、追踪参数。"""
    candidate = (url or "").strip().lower()
    for sep in ("?", "#"):
        if sep in candidate:
            candidate = candidate.split(sep, 1)[0]
    return candidate.rstrip("/")


def _suffix_of(url: str) -> str:
    path = url.split("?")[0]
    dot = path.rfind(".")
    if dot == -1:
        return ""
    suffix = path[dot:]
    return suffix if 2 <= len(suffix) <= 6 else ""
