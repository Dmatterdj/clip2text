"""链接解析器的单元测试（不连网）。

小宇宙的页面样本取自真实响应（2026-11-23 抓取，已裁剪到只保留
被测逻辑用得到的字段）。**故意不用真实网络**：解析器失效时我们要能
从测试里立刻知道「是页面结构变了」，而不是「网络不稳」。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clip2text.config import Settings
from clip2text.extractors import (
    ExtractContext,
    ExtractError,
    pick_url,
    platform_catalog,
    platform_summary,
)
from clip2text.extractors.bilibili import BilibiliExtractor
from clip2text.extractors.podcast_rss import (
    PodcastFeedExtractor,
    _parse_duration,
    _strip_namespaces,
    split_episode_ref,
)
from clip2text.extractors.xiaoyuzhou import XiaoyuzhouExtractor, _plain_text


def make_ctx(tmp_path: Path, **kwargs) -> ExtractContext:
    return ExtractContext(work_dir=tmp_path / "work", **kwargs)


# ---------------------------------------------------------------- 小宇宙
EPISODE_ID = "6b0154496f7bbb70cd58cb2f"


def _next_data_page(episode: dict) -> str:
    payload = {"props": {"pageProps": {"episode": episode}}, "page": "/episode"}
    return (
        "<!DOCTYPE html><html><head><title>x</title></head><body>"
        f'<script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(payload, ensure_ascii=False)}</script>"
        "</body></html>"
    )


def _episode(**overrides) -> dict:
    base = {
        "type": "EPISODE",
        "eid": EPISODE_ID,
        "pid": "5f60d69183c34e85dd9889d3",
        "title": "世风日下，仁勋不古",
        "description": "本期嘉宾：彭林、森森",
        "shownotes": "<p><span>本期嘉宾：彭林</span></p><p>· GTA6 上市</p>",
        "duration": 7594,
        "image": {"picUrl": "https://image.xyzcdn.net/x.jpg"},
        "enclosure": {
            "url": "https://media.xyzcdn.net/5f60/media.m4a",
        },
        "media": {
            "source": {"mode": "PUBLIC", "url": "https://media.xyzcdn.net/a/media.m4a"}
        },
        "isPrivateMedia": False,
        "payType": "FREE",
        "podcast": {"title": "FView Friday", "author": "爱否科技"},
    }
    base.update(overrides)
    return base


def test_xiaoyuzhou_parse_eid():
    ex = XiaoyuzhouExtractor()
    assert ex.matches(f"https://www.xiaoyuzhoufm.com/episode/{EPISODE_ID}")
    assert ex.matches(f"https://xiaoyuzhoufm.com/episode/{EPISODE_ID}?utm_source=x")
    assert not ex.matches("https://www.bilibili.com/video/BV1xx411c7mD")

    assert ex._parse_eid(f"https://www.xiaoyuzhoufm.com/episode/{EPISODE_ID}") == EPISODE_ID
    with pytest.raises(ExtractError, match="单集"):
        ex._parse_eid("https://www.xiaoyuzhoufm.com/podcast/abc")


def test_xiaoyuzhou_parses_next_data(tmp_path):
    ex = XiaoyuzhouExtractor()
    meta = ex._parse_page(_next_data_page(_episode()))

    assert meta.title == "世风日下，仁勋不古"
    assert meta.podcast_title == "FView Friday"
    assert meta.author == "爱否科技"
    assert meta.duration == 7594.0
    assert meta.audio_url == "https://media.xyzcdn.net/5f60/media.m4a"
    assert meta.cover == "https://image.xyzcdn.net/x.jpg"
    assert meta.pay_type == "FREE"
    # shownotes 是 HTML，要转成纯文本
    assert "本期嘉宾：彭林" in meta.shownotes
    assert "<p>" not in meta.shownotes


def test_xiaoyuzhou_falls_back_to_media_url_when_enclosure_missing():
    ex = XiaoyuzhouExtractor()
    meta = ex._parse_page(_next_data_page(_episode(enclosure={})))
    assert meta.audio_url == "https://media.xyzcdn.net/a/media.m4a"


def test_xiaoyuzhou_refuses_non_public_media():
    """media.source.mode 不是 PUBLIC 时不能用这条直链 —— 这是合规红线。"""
    ex = XiaoyuzhouExtractor()
    meta = ex._parse_page(
        _next_data_page(
            _episode(
                enclosure={},
                media={"source": {"mode": "PRIVATE", "url": "https://x/y.m4a"}},
            )
        )
    )
    assert meta.audio_url is None


def test_xiaoyuzhou_missing_audio_error_carries_context():
    """拿不到音频时，错误信息必须把标题/简介/付费状态都带回来。"""
    ex = XiaoyuzhouExtractor()
    meta = ex._parse_page(
        _next_data_page(
            _episode(
                enclosure={},
                media={},
                payType="PAY",
                isPrivateMedia=True,
            )
        )
    )
    message = ex._explain_missing_audio(meta)
    assert "世风日下，仁勋不古" in message
    assert "私密" in message
    assert "GTA6" in message  # 简介节选也要给出来


def test_xiaoyuzhou_paywall_wording():
    ex = XiaoyuzhouExtractor()
    meta = ex._parse_page(
        _next_data_page(_episode(enclosure={}, media={}, payType="PAY"))
    )
    message = ex._explain_missing_audio(meta)
    assert "付费" in message
    assert "绕过" in message


def test_xiaoyuzhou_og_meta_fallback():
    """JSON 整块没了也不能直接崩，og: meta 至少能认出标题。"""
    page = (
        "<html><head>"
        '<meta property="og:title" content="只有 og 的页面" />'
        '<meta property="og:description" content="简介" />'
        "</head></html>"
    )
    meta = XiaoyuzhouExtractor()._parse_page(page)
    assert meta.title == "只有 og 的页面"
    assert meta.audio_url is None


def test_xiaoyuzhou_unrecognizable_page_raises():
    with pytest.raises(ExtractError, match="改版"):
        XiaoyuzhouExtractor()._parse_page("<html><body>nothing</body></html>")


def test_xiaoyuzhou_duration_guard_before_download():
    """时长超限要在下载前抛错，check_duration 是那个唯一的判口。"""
    ctx = make_ctx(Path("/tmp"), max_duration_sec=60 * 30)
    with pytest.raises(ExtractError, match="超过上限"):
        ctx.check_duration(7594, site="小宇宙")
    # 未超限 / 时长未知都要放行
    ctx.check_duration(600, site="小宇宙")
    ctx.check_duration(None, site="小宇宙")


def test_xiaoyuzhou_initial_state_fallback():
    state = {
        "episodeDetail": {
            "episode": {
                "title": "INITIAL_STATE 里的单集",
                "enclosure": {"url": "https://media.example.com/a.mp3"},
                "duration": "01:02:03",
            }
        }
    }
    page = (
        "<html><body><script>"
        f"window.__INITIAL_STATE__ = {json.dumps(state, ensure_ascii=False)};"
        "</script></body></html>"
    )
    meta = XiaoyuzhouExtractor()._parse_page(page)
    assert meta.title == "INITIAL_STATE 里的单集"
    assert meta.audio_url == "https://media.example.com/a.mp3"


def test_plain_text_strips_tags_and_unescapes():
    html = "<p>a &amp; b</p><p>c<br>d</p>"
    text = _plain_text(html)
    assert "a & b" in text
    assert "<" not in text
    assert "\n" in text


# ---------------------------------------------------------------- RSS
_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>测试播客</title>
    <description>频道简介</description>
    <itunes:author>某主播</itunes:author>
    <image><url>https://cdn.example.com/cover.jpg</url></image>
    <item>
      <title>第二集（最新）</title>
      <description>&lt;p&gt;第二集简介&lt;/p&gt;</description>
      <link>https://pod.example.com/ep/2</link>
      <enclosure url="https://cdn.example.com/ep2.mp3" type="audio/mpeg" length="123"/>
      <itunes:duration>01:02:03</itunes:duration>
      <pubDate>Mon, 20 Nov 2026 10:00:00 GMT</pubDate>
    </item>
    <item>
      <title>第一集</title>
      <description>第一集简介</description>
      <link>https://pod.example.com/ep/1</link>
      <enclosure url="https://cdn.example.com/ep1.mp3" type="audio/mpeg" length="456"/>
      <itunes:duration>1800</itunes:duration>
    </item>
  </channel>
</rss>
"""

_ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom 播客</title>
  <author><name>作者甲</name></author>
  <entry>
    <title>Atom 单集</title>
    <summary>简介文本</summary>
    <published>2026-01-02T03:04:05Z</published>
    <link rel="alternate" href="https://atom.example.com/ep/9"/>
    <link rel="enclosure" type="audio/mpeg" href="https://cdn.example.com/a9.mp3"/>
  </entry>
</feed>
"""


def test_rss_matches_feed_urls():
    ex = PodcastFeedExtractor()
    assert ex.matches("https://feed.xyzfm.space/abcd1234")
    assert ex.matches("https://example.com/podcast/feed.xml")
    assert ex.matches("https://example.com/rss")
    assert not ex.matches("https://www.bilibili.com/video/BV1xx411c7mD")


def test_rss_matcher_does_not_over_match_ordinary_pages():
    """按路径段匹配，不是子串匹配。

    回归护栏：第一版用 `hint in path`，把 /feedback、/rsshelp 这类普通页面
    也当成 feed，白白发请求并在最终错误里堆噪音。
    """
    ex = PodcastFeedExtractor()
    assert not ex.matches("https://example.com/feedback")
    assert not ex.matches("https://vimeo.com/rsshelp")
    # 真正的 feed 路径仍要命中
    assert ex.matches("https://example.com/feed")
    assert ex.matches("https://example.com/x/rss/")


def test_rss_parses_rss20():
    from xml.etree import ElementTree as ET

    ex = PodcastFeedExtractor()
    # 与生产路径一致：先抹掉命名空间，再解析
    root = _strip_namespaces(ET.fromstring(_RSS))
    channel = ex._parse_channel(root)
    items = ex._parse_items(root)

    assert channel["title"] == "测试播客"
    assert channel["author"] == "某主播"
    assert channel["image"] == "https://cdn.example.com/cover.jpg"
    assert len(items) == 2
    assert items[0].title == "第二集（最新）"
    assert items[0].audio_url == "https://cdn.example.com/ep2.mp3"
    assert items[0].duration == 3723.0  # 01:02:03
    assert items[1].duration == 1800.0
    # description 里的 HTML 要清掉
    assert items[0].description == "第二集简介"


def test_rss_parses_atom():
    from xml.etree import ElementTree as ET

    ex = PodcastFeedExtractor()
    # Atom 用默认命名空间：不抹掉的话 find("title") 一个都匹配不到，
    # 表现成「解析成功但标题全空」——这正是下面这条断言要防的回归。
    root = _strip_namespaces(ET.fromstring(_ATOM))
    channel = ex._parse_channel(root)
    items = ex._parse_items(root)

    assert channel["title"] == "Atom 播客"
    assert channel["author"] == "作者甲"
    assert len(items) == 1
    assert items[0].title == "Atom 单集"
    assert items[0].audio_url == "https://cdn.example.com/a9.mp3"
    assert items[0].link == "https://atom.example.com/ep/9"


def test_strip_namespaces_is_what_makes_atom_work():
    """回归护栏：不抹命名空间时 Atom 标题确实取不到（说明这步不可省）。"""
    from xml.etree import ElementTree as ET

    ex = PodcastFeedExtractor()
    raw = ET.fromstring(_ATOM)
    assert ex._parse_channel(raw)["title"] is None
    assert ex._parse_channel(_strip_namespaces(raw))["title"] == "Atom 播客"


def test_rss_picks_latest_by_default_and_by_link():
    from xml.etree import ElementTree as ET

    ex = PodcastFeedExtractor()
    items = ex._parse_items(_strip_namespaces(ET.fromstring(_RSS)))
    assert ex._pick_item(items, None).title == "第二集（最新）"
    assert ex._pick_item(items, "https://pod.example.com/ep/1").title == "第一集"
    # 追踪参数不影响匹配
    assert (
        ex._pick_item(items, "https://pod.example.com/ep/1?utm_source=x").title
        == "第一集"
    )
    assert ex._pick_item(items, "https://pod.example.com/ep/999") is None


def test_split_episode_ref():
    feed, ep = split_episode_ref("https://feed.xyzfm.space/abc#https://pod.example.com/ep/1")
    assert feed == "https://feed.xyzfm.space/abc"
    assert ep == "https://pod.example.com/ep/1"
    assert split_episode_ref("https://feed.xyzfm.space/abc") == (
        "https://feed.xyzfm.space/abc",
        None,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1800", 1800.0),
        ("01:02:03", 3723.0),
        ("02:03", 123.0),
        ("PT1H2M3S", 3723.0),
        ("", None),
        ("abc", None),
        (None, None),
    ],
)
def test_parse_duration_formats(raw, expected):
    assert _parse_duration(raw) == expected


def test_rss_episode_ref_matching():
    ex = PodcastFeedExtractor()
    assert ex.matches("https://feed.xyzfm.space/abc#https://pod.example.com/ep/1")
    assert not ex.matches("https://example.com/page#anchor")


def test_rss_no_items_raises(tmp_path, monkeypatch):
    """空 feed 要报错，而且要说清「这不是播客 feed」。"""
    from xml.etree import ElementTree as ET

    ex = PodcastFeedExtractor()
    empty = "<?xml version='1.0'?><rss><channel><title>空</title></channel></rss>"

    # 只替换抓取这一层：extract() 自己 new httpx.Client，
    # 在测试里塞 FakeClient 是替换不到的（第一次写这条测试时就踩了）。
    monkeypatch.setattr(
        ex, "_fetch_feed", lambda client, url: _strip_namespaces(ET.fromstring(empty))
    )
    with pytest.raises(ExtractError, match="没有单集"):
        ex.extract("https://feed.xyzfm.space/abc", make_ctx(tmp_path))


def test_rss_oversized_feed_rejected(tmp_path, monkeypatch):
    """超大响应要在 XML 解析前拦住（stdlib ElementTree 没有实体膨胀防护）。"""
    from clip2text.extractors import podcast_rss

    ex = PodcastFeedExtractor()

    class FakeResp:
        status_code = 200
        encoding = "utf-8"
        content = b"<rss/>" * 1

    class FakeClient:
        def get(self, url):
            return FakeResp()

    monkeypatch.setattr(podcast_rss, "_MAX_FEED_BYTES", 3)
    with pytest.raises(ExtractError, match="超过上限"):
        ex._fetch_feed(FakeClient(), "https://feed.xyzfm.space/abc")


# ---------------------------------------------------------------- B 站多 P
def _pages(n: int) -> list[dict]:
    return [
        {"page": i, "cid": 1000 + i, "part": f"第 {i} 讲", "duration": 600 + i}
        for i in range(1, n + 1)
    ]


def test_bilibili_parse_page_no():
    ex = BilibiliExtractor()
    assert ex._parse_page_no("https://www.bilibili.com/video/BV1xx411c7mD") is None
    assert ex._parse_page_no("https://www.bilibili.com/video/BV1xx411c7mD?p=3") == 3
    assert ex._parse_page_no("https://www.bilibili.com/video/BV1xx?p=0") is None
    assert ex._parse_page_no("https://www.bilibili.com/video/BV1xx?p=abc") is None


def test_bilibili_select_page():
    ex = BilibiliExtractor()
    pages = _pages(5)
    assert ex._select_page(pages, None)["page"] == 1
    assert ex._select_page(pages, 3)["page"] == 3
    # page 字段缺失时按列表下标兜底
    assert ex._select_page([{"cid": 1}, {"cid": 2}], 2)["cid"] == 2
    assert ex._select_page([], 1) is None


def test_bilibili_multi_page_title_marks_part():
    ex = BilibiliExtractor()
    pages = _pages(12)
    info = {"title": "一场长讲座"}
    title = ex._compose_title(info, pages[2], 3, len(pages))
    assert "一场长讲座" in title
    assert "P3" in title
    assert "第 3 讲" in title


def test_bilibili_single_page_title_unchanged():
    ex = BilibiliExtractor()
    info = {"title": "单 P 视频"}
    assert ex._compose_title(info, {"page": 1, "part": "正片"}, None, 1) == "单 P 视频"


def test_bilibili_multi_page_content_warns_clearly():
    """多 P 提示是这次的验收点：用户必须知道只处理了第一 P。"""
    ex = BilibiliExtractor()
    pages = _pages(3)
    content = ex._compose_content({"desc": "讲稿简介"}, pages, pages[0], None)

    assert "多 P" in content
    assert "共 3 P" in content
    assert "只处理了 P1" in content
    assert "?p=" in content  # 必须告诉用户怎么处理其他 P
    assert "P2 第 2 讲" in content
    assert "讲稿简介" in content


def test_bilibili_multi_page_hint_example_is_reachable():
    """提示里的 ?p=N 示例必须是真实存在的 P。

    回归护栏：第一版写死了 ?p=3，2 P 的视频上会显示成 ?p=3，
    照做会得到「超出范围」错误，用户会以为链接写坏了。
    """
    ex = BilibiliExtractor()
    pages = _pages(2)
    content = ex._compose_content({"desc": ""}, pages, pages[1], 2)  # 当前在 P2
    assert "?p=1" in content  # 指回另一个真实存在的 P
    assert "?p=3" not in content


def test_bilibili_download_targets_work_dir(tmp_path):
    """回归护栏：_download 必须收到目录而不是 ExtractContext 本身。

    这个 bug 真实发生过（重构时把 ctx 传了进去），而且被 yt-dlp 兜底掩盖：
    B 站适配器抛 AttributeError 后，兜底适配器也能下到同一个作品，
    表面上「能出结果」，只是悄悄走错了路径。所以要显式钉住。
    """
    ex = BilibiliExtractor()
    calls: list[object] = []

    class FakeResp:
        def __init__(self) -> None:
            self.headers = {}

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self, size: int):
            yield b"a" * 64

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class FakeClient:
        def stream(self, method, url):
            return FakeResp()

    def fake_download(client, url, target):
        calls.append(target)
        # 只有真正是 Path 时这个调用才不会炸
        target.mkdir(parents=True, exist_ok=True)
        dst = target / "source.m4s"
        dst.write_bytes(b"x" * 32)
        return dst

    ex._download = fake_download  # type: ignore[method-assign]
    ex._download(FakeClient(), "https://example.com/a.m4s", tmp_path)
    assert calls and isinstance(calls[0], Path)


def test_bilibili_extract_passes_work_dir_not_context(monkeypatch, tmp_path):
    """直接在 extract() 上钉住：传到 _download 的第三个参数必须是 Path。"""
    ex = BilibiliExtractor()
    seen: list[object] = []

    monkeypatch.setattr(
        ex,
        "_resolve_short",
        lambda url, ctx: "https://www.bilibili.com/video/BV1xx411c7mD",
    )
    monkeypatch.setattr(
        ex,
        "_fetch_view",
        lambda client, *, bvid, aid: {
            "bvid": "BV1xx411c7mD",
            "aid": 1,
            "title": "标题",
            "desc": "",
            "duration": 100,
            "pages": [{"page": 1, "cid": 111, "part": "正片", "duration": 100}],
        },
    )
    monkeypatch.setattr(ex, "_fetch_audio_url", lambda client, info, cid: "https://x/a.m4s")

    def fake_download(client, url, target):
        seen.append(target)
        dst = Path(target) / "source.m4s"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"x")
        return dst

    monkeypatch.setattr(ex, "_download", fake_download)
    ctx = make_ctx(tmp_path)
    result = ex.extract("https://www.bilibili.com/video/BV1xx411c7mD", ctx)

    assert seen == [ctx.work_dir]
    assert isinstance(seen[0], Path)
    assert result.local_path == ctx.work_dir / "source.m4s"


def test_bilibili_single_page_content_no_noise():
    ex = BilibiliExtractor()
    content = ex._compose_content({"desc": "简介"}, _pages(1), {"page": 1}, None)
    assert content == "简介"


def test_extract_context_content_length_guard():
    ctx = make_ctx(Path("/tmp"), max_bytes=10 * 1024 * 1024)
    with pytest.raises(ExtractError, match="超过上限"):
        ctx.check_content_length(str(50 * 1024 * 1024), site="某些平台")
    # 头缺失 / 非法值放行，由下载循环的字节计数兜底
    ctx.check_content_length(None, site="x")
    ctx.check_content_length("abc", site="x")


def test_extract_context_no_limits_means_no_guard():
    ctx = make_ctx(Path("/tmp"))  # max_duration_sec / max_bytes 都是 None
    ctx.check_duration(999999)
    ctx.check_content_length(str(10 * 1024 * 1024 * 1024))


# ---------------------------------------------------------------- 注册表
def test_pick_url_from_share_text():
    text = "3.87 复制打开抖音 https://v.douyin.com/abc123/ 看看"
    assert pick_url(text) == "https://v.douyin.com/abc123/"


def test_platform_summary_includes_new_adapters():
    summary = {item["name"]: item for item in platform_summary()}
    assert "xiaoyuzhou" in summary
    assert "podcastfeed" in summary
    assert "bilibili" in summary
    assert summary["ytdlp"]["fallback"] is True
    assert summary["bilibili"]["fallback"] is False


def test_platform_catalog_shape():
    catalog = platform_catalog()
    by_name = {item["name"]: item for item in catalog}
    assert by_name["bilibili"]["available"] is True
    assert by_name["bilibili"]["via"] == "builtin"
    assert by_name["xiaoyuzhou"]["via"] == "builtin"
    assert "youtube" in by_name
    for item in catalog:
        assert item["label"]
        assert item["domains"]
        # available 与 reason 必须自洽：不可用时一定要有原因
        if not item["available"]:
            assert item["reason"]


def test_platform_catalog_reports_missing_ytdlp_keys(monkeypatch):
    """yt-dlp 版本里缺某个 extractor 时，不能谎报支持。"""
    from clip2text import extractors as ex_mod

    monkeypatch.setattr(ex_mod, "_YTDLP_KEYS", set())
    by_name = {item["name"]: item for item in ex_mod.platform_catalog()}
    assert by_name["youtube"]["available"] is False
    assert "yt-dlp" in by_name["youtube"]["reason"]


def test_settings_defaults_are_link_first():
    s = Settings()
    assert s.extractors_enabled is True
    assert s.max_media_duration_min == 180
    assert s.max_media_size_mb == 2048
    assert s.chunk_retry_max == 2
    assert s.resume_enabled is True
    assert s.max_media_duration_sec == 180 * 60
    assert s.max_media_bytes == 2048 * 1024 * 1024


def test_settings_guards_can_be_disabled():
    s = Settings(max_media_duration_min=0, max_media_size_mb=0)
    assert s.max_media_duration_sec is None
    assert s.max_media_bytes is None
    ctx = ExtractContext(work_dir=Path("/tmp"), max_duration_sec=None, max_bytes=None)
    ctx.check_duration(999999)
