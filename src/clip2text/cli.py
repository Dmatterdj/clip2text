"""命令行入口。

  clip2text serve              启动 API + 前端
  clip2text transcribe FILE    直接转写一个文件（不启服务，适合脚本化）
  clip2text capabilities       打印本机可用能力
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clip2text", description="本地优先的文案提取工具")
    sub = parser.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="启动 Web 服务")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--reload", action="store_true", help="开发模式热重载")

    p_tr = sub.add_parser("transcribe", help="直接转写单个文件")
    p_tr.add_argument("file", type=Path)
    p_tr.add_argument("--format", default="txt", choices=["txt", "markdown", "docx", "xlsx"])
    p_tr.add_argument("-o", "--output", type=Path, default=None)

    sub.add_parser("capabilities", help="打印本机可用能力")

    args = parser.parse_args(argv)

    if args.cmd == "serve":
        return _serve(args)
    if args.cmd == "transcribe":
        return _transcribe(args)
    if args.cmd == "capabilities":
        return _capabilities()
    parser.print_help()
    return 0


def _serve(args) -> int:
    import uvicorn

    from .config import get_settings

    settings = get_settings()
    uvicorn.run(
        "clip2text.app:app",
        host=args.host or settings.host,
        port=args.port or settings.port,
        reload=args.reload,
    )
    return 0


def _capabilities() -> int:
    import json

    from .asr import build_engines, engine_statuses
    from .config import get_settings
    from .media import get_resolve_error, resolve_ffmpeg

    settings = get_settings()
    ff = resolve_ffmpeg(settings.ffmpeg_mode)
    info = {
        "ffmpeg": {
            "available": ff is not None,
            "source": ff.source if ff else None,
            "reason": None if ff else get_resolve_error(),
        },
        "asr": [
            {
                "name": e.name,
                "available": e.available,
                "reason": e.reason,
                "timestamps": e.timestamps,
            }
            for e in engine_statuses(settings, build_engines(settings))
        ],
    }
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def _transcribe(args) -> int:
    from .exporters import export, safe_filename
    from .state import init_state

    path: Path = args.file
    if not path.exists():
        print(f"文件不存在: {path}", file=sys.stderr)
        return 1

    state = init_state()
    try:
        task = state.pipeline.create_task(source_type="upload", source_value=str(path.resolve()))
        asyncio.run(state.pipeline.run(task.id))
        final = state.db.get(task.id)
        if final is None or final.status.value != "SUCCESS":
            print(f"转写失败: {final.error_message if final else '任务丢失'}", file=sys.stderr)
            return 1

        data = export(final, args.format)
        out = args.output or path.with_name(safe_filename(final, args.format))
        out.write_bytes(data)
        print(f"✓ {out}（{len(final.text_content or '')} 字，{len(final.segments)} 段）")
        return 0
    finally:
        from .state import reset_state

        reset_state()


if __name__ == "__main__":
    raise SystemExit(main())
