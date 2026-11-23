"""B 站适配器 —— 一个"干净"的示范实现。

为什么单独写 B 站而不是全靠 yt-dlp：
    B 站有公开的 web 接口（网页自己就在用），不需要破解签名。
    它同时也是最好的插件范例：结构清晰、只调用公开接口、
    不涉及任何绕过手段，社区照着就能写自己平台的适配器。

用到的接口（都是公开的 web 接口，无需登录）：
    GET api.bilibili.com/x/web-interface/view?bvid=…   作品元信息
    GET api.bilibili.com/x/player/playurl?…           音频流地址

多 P / 合集
    长播客在 B 站经常是「一个 BV 号挂几十个 P」，或者一个合集里几十个视频。
    当前实现默认只处理第一 P（产品定位是「一条链接出一份文字」），但必须让
    用户知道这件事，否则他会以为整场讲座都转完了 —— 这是最糟的失败方式：
    结果看起来成功，内容却缺了 90%。

    所以：多 P 时返回的 raw 里带完整分 P 清单（标题 + 时长），
    并在任务 content 开头写一行提示。链接里带 ?p=N 时按 N 取对应 P。

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
# 分 P 参数：?p=3 / &p=3
_P_RE = re.compile(r"[?&]p=(\d+)")

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

        page_no = self._parse_page_no(url)  # 1-based，None 表示没指定
        client = httpx.Client(
            headers=_HEADERS,
            timeout=ctx.timeout,
            follow_redirects=True,
            proxy=ctx.proxy or None,
        )
        try:
            info = self._fetch_view(client, bvid=bvid, aid=aid)
            pages = info.get("pages") or []

            if page_no is not None and pages and not 1 <= page_no <= len(pages):
                raise ExtractError(
                    f"链接里的 p={page_no} 超出范围：这个作品只有 {len(pages)} 个分 P。"
                    "去掉 ?p= 参数会默认处理第 1 P。"
                )

            # 选中的分 P：有 ?p=N 用 N，否则第 1 P（B 站网页默认也是第 1 P）
            selected = self._select_page(pages, page_no)
            cid = selected.get("cid") if selected else info.get("cid")
            if not cid:
                raise ExtractError("拿不到 cid，无法定位音频流")

            # 长内容护栏要在下载前判断：多 P 的整场讲座可能是好几个小时
            part_duration = _as_float(selected.get("duration")) if selected else None
            ctx.check_duration(
                part_duration or _as_float(info.get("duration")), site="B 站"
            )

            audio_url = self._fetch_audio_url(client, info, cid)
            # 注意传 ctx.work_dir 而不是 ctx：_download 要的是目录。
            # 传错会被 yt-dlp 兜底掩盖过去（它也能下到同样的作品），
            # 表现为「能出结果但走错了适配器」，非常难发现。
            local = self._download(client, audio_url, ctx.work_dir)
        finally:
            client.close()

        title = self._compose_title(info, selected, page_no, len(pages))
        # 多 P 时优先用该 P 的封面帧，没有就退回作品封面
        cover = (selected.get("first_frame") if selected else None) or info.get("pic")
        return ExtractResult(
            platform="bilibili",
            work_type="video",
            title=title,
            content=self._compose_content(info, pages, selected, page_no),
            cover=cover,
            duration=part_duration or _as_float(info.get("duration")),
            video_urls=[audio_url],
            local_path=local,
            raw={
                "bvid": info.get("bvid"),
                "aid": info.get("aid"),
                "cid": cid,
                "owner": (info.get("owner") or {}).get("name"),
                "page_no": (selected.get("page") if selected else None),
                # 完整分 P 清单：前端要展示「这是 12 P 中的第 3 P」就靠它
                "pages": [
                    {
                        "page": p.get("page"),
                        "title": p.get("part") or p.get("title"),
                        "duration": _as_float(p.get("duration")),
                        "cid": p.get("cid"),
                    }
                    for p in pages
                ],
                "page_count": len(pages),
            },
        )

    # ---------------- 内部步骤 ----------------
    @staticmethod
    def _parse_page_no(url: str) -> int | None:
        """从 URL 里读 ?p=N。B 站的分 P 是 1-based。"""
        m = _P_RE.search(url)
        if not m:
            return None
        try:
            value = int(m.group(1))
        except ValueError:
            return None
        return value if value >= 1 else None

    @staticmethod
    def _select_page(pages: list[dict], page_no: int | None) -> dict | None:
        """按 ?p=N 取分 P；没指定就第 1 P。pages 为空时返回 None。"""
        if not pages:
            return None
        if page_no is None:
            return pages[0]
        for p in pages:
            if p.get("page") == page_no:
                return p
        # page 字段缺失/不一致时按列表下标兜底（B 站历史上两种都出现过）
        index = page_no - 1
        return pages[index] if 0 <= index < len(pages) else None

    @staticmethod
    def _compose_title(
        info: dict, selected: dict | None, page_no: int | None, total: int
    ) -> str:
        """多 P 时标题要带上是第几 P，否则列表里一堆同名任务分不清。"""
        base = info.get("title") or "未命名作品"
        part = (selected or {}).get("part")
        if total <= 1 or not part:
            return str(base)
        return f"{base} - P{selected.get('page')} {part}"

    @staticmethod
    def _compose_content(
        info: dict, pages: list[dict], selected: dict | None, page_no: int | None
    ) -> str:
        """作品简介 + 多 P 时的显式提示。

        不做这个提示的话，用户会以为整场讲座都转完了 —— 结果看起来成功、
        内容却缺了大半，这是最糟的失败方式。
        """
        desc = (info.get("desc") or "").strip()
        if len(pages) <= 1:
            return desc

        current = selected.get("page") if selected else 1
        # 示例用「下一个还没看的 P」，而不是写死 ?p=3：
        # 2 P 的视频提示 p=3 会让人以为链接写错了。
        others = [p.get("page") for p in pages if p.get("page") != current]
        example = others[0] if others else current
        lines = [
            f"⚠️ 这是多 P 视频（共 {len(pages)} P），本次只处理了 P{current}。",
            f"处理其他分 P 请在链接后加 ?p=序号，例如 …?p={example}。",
            "",
            "全部分 P：",
        ]
        for p in pages:
            mark = " ← 本次处理" if p.get("page") == current else ""
            dur = _as_float(p.get("duration"))
            dur_text = f"（{int(dur // 60)}:{int(dur % 60):02d}）" if dur else ""
            lines.append(f"  P{p.get('page')} {p.get('part') or ''}{dur_text}{mark}")
        if desc:
            lines.extend(["", desc])
        return "\n".join(lines)

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

    def _fetch_audio_url(self, client: httpx.Client, info: dict, cid: object) -> str:
        # cid 由调用方传入：多 P 场景下必须是选中那一 P 的 cid，
        # 用 pages[0] 会静默处理错的分 P（最难发现的 bug）。
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


def _as_float(value: object) -> float | None:
    try:
        got = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return got if got > 0 else None
