"""链接解析的公共契约。

⚠️ 合规提示
    本包仅用于处理你自己拥有版权的内容，或已获得授权的内容。
    默认关闭（EXTRACTORS_ENABLED=false）。仓库内不含任何平台的
    签名密钥、登录 Cookie 或其他绕过手段。
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
