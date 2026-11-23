"""B 站适配器 —— 一个"干净"的示范实现。

为什么单独写 B 站而不是全靠 yt-dlp：
    B 站有公开的 web 接口（网页自己就在用），不需要破解签名。
    它同时也是最好的插件范例：结构清晰、只调用公开接口、
    不涉及任何绕过手段，社区照着就能写自己平台的适配器。

用到的接口（都是公开的 web 接口，无需登录）：
    GET api.bilibili.com/x/web-interface/view?bvid=…   作品元信息
    GET api.bilibili.com/x/player/playurl?…           音频流地址

⚠️ 只处理公开可访问的内容。需要登录/大会员的内容会返回明确的错误，
   本适配器不做任何绕过。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import httpx

from .base import ExtractContext, ExtractError, ExtractResult

log = logging.getLogger(__name__)

_VIEW_API = "https://api.bilibili.com/x/web-interface/view"
_PLAYURL_API = "https://api.bilibili.com/x/player/playurl"

_BV_RE = re.compile(r"/(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"/av(\d+)", re.IGNORECASE)

# 不带 Referer 会被 403，这是 B 站的 CDN 防盗链策略
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}


class BilibiliExtractor:
    hosts = ("bilibili.com", "b23.tv", "bili2233.cn")

    def matches(self, url: str) -> bool:
        from . import host_of

        host = host_of(url)
        return any(host == h or host.endswith("." + h) for h in self.hosts)

    def extract(self, url: str, ctx: ExtractContext) -> ExtractResult:
        real_url = self._resolve_short(url, ctx)
        bvid, aid = self._parse_id(real_url)
        if not bvid and not aid:
            raise ExtractError("没能从链接里认出 B 站作品号（BV 号或 av 号）")

        client = httpx.Client(
            headers=_HEADERS,
            timeout=ctx.timeout,
            follow_redirects=True,
            proxy=ctx.proxy or None,
        )
        try:
            info = self._fetch_view(client, bvid=bvid, aid=aid)
            audio_url = self._fetch_audio_url(client, info)
            local = self._download(client, audio_url, ctx.work_dir)
        finally:
            client.close()

        pages = info.get("pages") or []
        return ExtractResult(
            platform="bilibili",
            work_type="video",
            title=info.get("title"),
            content=info.get("desc"),
            cover=info.get("pic"),
            duration=float(info.get("duration") or 0) or None,
            video_urls=[audio_url],
            local_path=local,
            raw={
                "bvid": info.get("bvid"),
                "aid": info.get("aid"),
                "cid": pages[0].get("cid") if pages else info.get("cid"),
                "owner": (info.get("owner") or {}).get("name"),
            },
        )

    # ---------------- 内部步骤 ----------------
    def _resolve_short(self, url: str, ctx: ExtractContext) -> str:
        """b23.tv 短链要跟随跳转才能拿到作品号。"""
        from . import host_of

        if host_of(url) not in {"b23.tv", "bili2233.cn"}:
            return url
        try:
            with httpx.Client(
                headers=_HEADERS,
                timeout=ctx.timeout,
                follow_redirects=True,
                proxy=ctx.proxy or None,
            ) as c:
                return str(c.get(url).url)
        except httpx.HTTPError as exc:
            raise ExtractError(f"短链展开失败：{exc}") from exc

    @staticmethod
    def _parse_id(url: str) -> tuple[str | None, str | None]:
        bv = _BV_RE.search(url)
        av = _AV_RE.search(url)
        return (bv.group(1) if bv else None), (av.group(1) if av else None)

    def _fetch_view(
        self, client: httpx.Client, *, bvid: str | None, aid: str | None
    ) -> dict:
        params = {"bvid": bvid} if bvid else {"aid": aid}
        try:
            resp = client.get(_VIEW_API, params=params)
        except httpx.HTTPError as exc:
            raise ExtractError(f"请求 B 站接口失败：{exc}") from exc
        data = resp.json()
        if data.get("code") != 0:
            raise ExtractError(
                f"B 站接口返回错误：{data.get('message') or data.get('code')}"
                "（作品可能已删除或仅限登录可见）"
            )
        return data.get("data") or {}

    def _fetch_audio_url(self, client: httpx.Client, info: dict) -> str:
        pages = info.get("pages") or []
        cid = pages[0].get("cid") if pages else info.get("cid")
        if not cid:
            raise ExtractError("拿不到 cid，无法定位音频流")

        # fnval=16 请求 DASH 格式，里面 audio 数组是纯音频，体积远小于整段视频
        params = {
            "bvid": info.get("bvid") or "",
            "cid": cid,
            "fnval": 16,
            "fourk": 1,
        }
        try:
            resp = client.get(_PLAYURL_API, params=params)
        except httpx.HTTPError as exc:
            raise ExtractError(f"获取音频流失败：{exc}") from exc
        data = resp.json()
        if data.get("code") != 0:
            raise ExtractError(
                f"获取音频流失败：{data.get('message') or data.get('code')}"
                "（可能是大会员专享或地区限制）"
            )

        dash = (data.get("data") or {}).get("dash") or {}
        audios = dash.get("audio") or []
        if not audios:
            raise ExtractError("这个作品没有独立音轨（可能是纯音乐/无声内容）")
        # 取码率最高的音轨
        best = max(audios, key=lambda a: a.get("bandwidth") or 0)
        base = best.get("baseUrl") or best.get("base_url")
        if not base:
            raise ExtractError("音频流地址为空")
        return str(base)

    def _download(self, client: httpx.Client, url: str, work_dir: Path) -> Path:
        work_dir.mkdir(parents=True, exist_ok=True)
        dst = work_dir / "source.m4s"
        try:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with dst.open("wb") as fh:
                    for chunk in resp.iter_bytes(256 * 1024):
                        fh.write(chunk)
        except httpx.HTTPError as exc:
            raise ExtractError(f"下载音频失败：{exc}") from exc
        if not dst.exists() or dst.stat().st_size == 0:
            raise ExtractError("下载得到的音频为空")
        return dst
