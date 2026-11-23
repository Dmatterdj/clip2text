"""链接解析的公共契约。

⚠️ 合规提示
    链接解析默认开启（EXTRACTORS_ENABLED=true）—— 这个工具的定位就是
    「贴链接 → 出文字」，上传文件只是并行能力。合规兜底保留：显式设为
    false 可整体关闭。仓库内不含任何平台的签名密钥、登录 Cookie 或其他绕过手段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


class ExtractError(RuntimeError):
    """解析失败。消息会直接展示给用户，所以要写清楚怎么办。"""


@dataclass
class ExtractResult:
    platform: str
    work_type: str = "video"  # video | image
    title: str | None = None
    content: str | None = None
    cover: str | None = None
    duration: float | None = None
    video_urls: list[str] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    local_path: Path | None = None
    raw: dict | None = None


@dataclass
class ExtractContext:
    """解析时能用的上下文，避免适配器自己去读全局配置。"""

    work_dir: Path
    proxy: str | None = None
    cookie_file: str | None = None
    timeout: int = 60
    # 长内容护栏（None 表示不限制）。适配器在「能提前知道时长/体积」时
    # 应该先自查再下载——两小时播客的 m4a 有两百多 MB，下完再拒绝很浪费。
    max_duration_sec: float | None = None
    max_bytes: int | None = None
    site: str = ""

    def check_duration(self, seconds: float | None, *, site: str = "") -> None:
        """时长超限直接抛错。seconds 未知（None/0）时放行。"""
        if not seconds or not self.max_duration_sec:
            return
        if seconds <= self.max_duration_sec:
            return
        name = site or self.site or "该内容"
        limit_min = int(self.max_duration_sec // 60)
        raise ExtractError(
            f"{name}时长 {seconds / 60:.0f} 分钟，超过上限 {limit_min} 分钟，未开始处理。"
            f"超长内容会切分成上百个片段，失败成本很高。"
            f"确实要处理请调大 MAX_MEDIA_DURATION_MIN（设为 0 关闭限制）。"
        )

    def check_content_length(self, header: object, *, site: str = "") -> None:
        """按 Content-Length 预判体积。拿不到这个头时放行，由下载循环兜底。"""
        if not self.max_bytes:
            return
        try:
            size = int(str(header))
        except (TypeError, ValueError):
            return
        if size <= self.max_bytes:
            return
        name = site or self.site or "该内容"
        raise ExtractError(
            f"{name}媒体体积约 {size // (1024 * 1024)}MB，超过上限 "
            f"{self.max_bytes // (1024 * 1024)}MB。"
            f"可调大 MAX_MEDIA_SIZE_MB（设为 0 关闭限制）。"
        )


@runtime_checkable
class Extractor(Protocol):
    """平台适配器协议。

    社区可以单独发一个 pip 包注册新平台，主仓库不必背锅：

        # pyproject.toml
        [project.entry-points."clip2text.extractors"]
        kuaishou = "clip2text_kuaishou:KuaishouExtractor"

    实现要点：
      · hosts 用来快速匹配，命中后再调 matches() 精判
      · extract() 必须把媒体落到 ctx.work_dir，并填 local_path
      · 失败抛 ExtractError，消息要可操作
    """

    hosts: tuple[str, ...]

    def matches(self, url: str) -> bool: ...

    def extract(self, url: str, ctx: ExtractContext) -> ExtractResult: ...
