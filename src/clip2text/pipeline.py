"""任务编排：把「文件/链接」变成「文字」的完整流水线。

流程（stage）：
  queued → fetching → transcoding → transcribing → packaging → done

设计要点：
  - 每一阶段结束都落库，进程崩溃后可从中断处继续
  - 通过回调把进度推给上层（API 层转成 SSE）
  - 所有阻塞调用（ffmpeg、whisper）放线程池，不阻塞事件循环
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from .asr import ASREngine, ASRError, build_engines, select_engine
from .config import Settings, get_settings
from .db import Database
from .media import FFmpegError, extract_audio, probe_duration, split_audio
from .models import Segment, Stage, Task, TaskStatus, utcnow

log = logging.getLogger(__name__)

ProgressHook = Callable[[Task], Awaitable[None] | None]

AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".amr", ".aiff", ".aif"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".wmv", ".m4v", ".ts", ".3gp"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff", ".tif"}


class PipelineError(RuntimeError):
    pass


def detect_work_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in AUDIO_EXT:
        return "audio"
    if ext in VIDEO_EXT:
        return "video"
    if ext in IMAGE_EXT:
        return "image"
    return "video"


class Pipeline:
    def __init__(self, settings: Settings | None = None, db: Database | None = None) -> None:
        self.settings = settings or get_settings()
        self.settings.ensure_dirs()
        self.db = db or Database(self.settings.db_path)
        self._engines: dict[str, ASREngine] = {}
        self._semaphore = asyncio.Semaphore(self.settings.max_concurrency)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancelled: set[str] = set()
        self._hooks: list[ProgressHook] = []

    # ---------------- 引擎 ----------------
    def engines(self) -> dict[str, ASREngine]:
        if not self._engines:
            self._engines = build_engines(self.settings)
        return self._engines

    def refresh_engines(self) -> None:
        self._engines = {}

    # ---------------- 事件 ----------------
    def on_progress(self, hook: ProgressHook) -> None:
        self._hooks.append(hook)

    async def _emit(self, task: Task) -> None:
        for hook in list(self._hooks):
            try:
                result = hook(task)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001 - 订阅者异常不能影响主流程
                log.exception("进度回调失败")

    # ---------------- 状态更新 ----------------
    async def _set(
        self,
        task: Task,
        *,
        stage: Stage | None = None,
        status: TaskStatus | None = None,
        progress: int | None = None,
        **fields: object,
    ) -> None:
        if stage is not None:
            task.stage = stage
        if status is not None:
            task.status = status
        if progress is not None:
            task.progress = max(0, min(100, progress))
        for key, value in fields.items():
            setattr(task, key, value)
        task.updated_at = utcnow()

        params: dict[str, object] = {"status": task.status.value, "stage": task.stage.value, "progress": task.progress}
        for key in fields:
            params[key] = _serialize_field(key, getattr(task, key))
        await asyncio.to_thread(self.db.update_fields, task.id, **params)
        await self._emit(task)

    # ---------------- 创建任务 ----------------
    def create_task(
        self,
        *,
        source_type: str,
        source_value: str,
        title: str | None = None,
        work_type: str | None = None,
    ) -> Task:
        task_id = uuid.uuid4().hex
        if source_type == "upload":
            path = Path(source_value)
            wtype = work_type or detect_work_type(path)
            # 用文件名（去扩展名）兜底标题，避免导出成 "任务 <uuid>"
            if not title:
                title = path.stem or None
        else:
            wtype = work_type or "video"
        task = Task(
            id=task_id,
            source_type=source_type,  # type: ignore[arg-type]
            source_value=source_value,
            work_type=wtype,  # type: ignore[arg-type]
            title=title,
            status=TaskStatus.QUEUED,
            stage=Stage.QUEUED,
        )
        self.db.create(task)
        log.info("创建任务 %s（%s: %s）", task_id, source_type, source_value)
        return task

    def start(self, task_id: str) -> None:
        """把任务丢进事件循环执行。不等待结果。"""
        if task_id in self._tasks:
            return
        async_task = asyncio.create_task(self._run_guarded(task_id))
        self._tasks[task_id] = async_task
        async_task.add_done_callback(lambda _: self._tasks.pop(task_id, None))

    async def _run_guarded(self, task_id: str) -> None:
        try:
            async with self._semaphore:
                await self.run(task_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("任务 %s 未捕获异常", task_id)
            task = await asyncio.to_thread(self.db.get, task_id)
            if task is not None and not task.status.terminal:
                await self._set(
                    task,
                    status=TaskStatus.FAILED,
                    error_message=f"{type(exc).__name__}: {exc}",
                )

    def cancel(self, task_id: str) -> bool:
        self._cancelled.add(task_id)
        task = self.db.get(task_id)
        if task is None:
            return False
        handle = self._tasks.get(task_id)
        if handle is not None:
            handle.cancel()
        return True

    def _is_cancelled(self, task_id: str) -> bool:
        return task_id in self._cancelled

    # ---------------- 主流程 ----------------
    async def run(self, task_id: str) -> Task:
        task = await asyncio.to_thread(self.db.get, task_id)
        if task is None:
            raise PipelineError(f"任务不存在: {task_id}")

        work_dir = self.settings.work_dir / task_id
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            # ---- 1. 取得源文件 ----
            await self._set(task, stage=Stage.FETCHING, status=TaskStatus.RUNNING, progress=5)
            if task.source_type == "upload":
                src = Path(task.source_value)
                if not src.exists():
                    raise PipelineError(f"上传文件不存在: {src}")
            else:
                src = await self._fetch_remote(task, work_dir)

            # ---- 2. 音频抽取 ----
            if task.work_type == "image":
                raise PipelineError("图片 OCR 尚未实现（计划在 P3 提供）")

            await self._set(task, stage=Stage.TRANSCODING, progress=15)
            wav = await asyncio.to_thread(
                extract_audio, src, work_dir / "audio.wav", mode=self.settings.ffmpeg_mode
            )
            duration = await asyncio.to_thread(
                probe_duration, wav, self.settings.ffmpeg_mode
            )
            await self._set(task, progress=25, duration=duration)

            # ---- 3. 选引擎并分片 ----
            engine = await asyncio.to_thread(select_engine, self.settings, self.engines())
            max_chunk = engine.max_chunk_sec or self.settings.chunk_max_sec
            chunks = await asyncio.to_thread(
                split_audio,
                wav,
                work_dir / "chunks",
                total_duration=duration or 0.0,
                max_sec=float(max_chunk),
                mode=self.settings.ffmpeg_mode,
            )

            # ---- 4. 逐片转写 ----
            await self._set(
                task, stage=Stage.TRANSCRIBING, progress=30, engine=engine.name
            )
            texts: list[str] = []
            segments: list[Segment] = []
            language: str | None = None
            total = len(chunks)
            for i, chunk in enumerate(chunks):
                if self._is_cancelled(task_id):
                    raise asyncio.CancelledError
                result = await asyncio.to_thread(
                    engine.transcribe, chunk.path, self.settings.asr_language_or_none
                )
                if result.text:
                    texts.append(result.text)
                language = language or result.language
                # 把片内时间戳平移到整段音频的绝对时间
                for seg in result.segments:
                    segments.append(
                        Segment(
                            start=round(seg.start + chunk.start, 3),
                            end=round(seg.end + chunk.start, 3),
                            text=seg.text,
                        )
                    )
                pct = 30 + int(60 * (i + 1) / total)
                await self._set(task, progress=pct)

            # ---- 5. 组装结果 ----
            await self._set(task, stage=Stage.PACKAGING, progress=95)
            sep = "" if _is_cjk(language) else " "
            full_text = sep.join(t for t in texts if t).strip()
            await self._set(
                task,
                stage=Stage.DONE,
                status=TaskStatus.SUCCESS,
                progress=100,
                text_content=full_text,
                segments=segments,
                language=language,
                audio_url=None,
                error_message=None,
            )
            log.info("任务 %s 完成：%d 段，%d 字", task_id, len(segments), len(full_text))
            return task

        except asyncio.CancelledError:
            await self._set(
                task,
                status=TaskStatus.CANCELED,
                error_message="任务已取消",
            )
            raise
        except (PipelineError, FFmpegError, ASRError) as exc:
            log.warning("任务 %s 失败: %s", task_id, exc)
            await self._set(task, status=TaskStatus.FAILED, error_message=str(exc))
            return task
        except Exception as exc:  # noqa: BLE001
            log.exception("任务 %s 异常", task_id)
            await self._set(
                task, status=TaskStatus.FAILED, error_message=f"{type(exc).__name__}: {exc}"
            )
            return task
        finally:
            self._cancelled.discard(task_id)
            # 保留源文件，但清掉中间产物（分片很占空间）
            shutil.rmtree(work_dir / "chunks", ignore_errors=True)

    async def _fetch_remote(self, task: Task, work_dir: Path) -> Path:
        """链接解析。默认关闭，需要 EXTRACTORS_ENABLED=true。"""
        if not self.settings.extractors_enabled:
            raise PipelineError(
                "链接解析默认关闭。请设置 EXTRACTORS_ENABLED=true 后重启服务。"
                "请仅用于处理你拥有版权的内容，详见 README 合规说明。"
            )
        from .extractors import extract  # 延迟导入：未安装 yt-dlp 也能启动

        result = await asyncio.to_thread(
            extract, task.source_value, work_dir, self.settings
        )
        task.title = task.title or result.title
        task.content = result.content
        task.platform = result.platform
        task.cover = result.cover
        task.video_urls = result.video_urls
        task.image_urls = result.image_urls
        await asyncio.to_thread(
            self.db.update_fields,
            task.id,
            title=task.title,
            content=task.content,
            platform=task.platform,
            cover=task.cover,
            video_urls=_json_dumps(result.video_urls),
            image_urls=_json_dumps(result.image_urls),
        )
        if result.local_path is None:
            raise PipelineError("解析成功但没有可用的媒体文件")
        return result.local_path

    # ---------------- 恢复 ----------------
    def recover_orphans(self) -> int:
        """启动时把上次进程留下的 RUNNING 任务标记为失败，避免永远转圈。"""
        ids = self.db.all_ids_by_status([TaskStatus.RUNNING, TaskStatus.QUEUED])
        for task_id in ids:
            self.db.update_fields(
                task_id,
                status=TaskStatus.FAILED.value,
                error_message="服务重启导致任务中断，请重新提交",
            )
        if ids:
            log.warning("恢复：%d 个中断任务已标记为失败", len(ids))
        return len(ids)


def _serialize_field(key: str, value: object) -> object:
    if key == "segments":
        return _json_dumps([s.model_dump() if isinstance(s, Segment) else s for s in value])  # type: ignore[union-attr]
    if key in {"video_urls", "image_urls"}:
        return _json_dumps(value)  # type: ignore[arg-type]
    return value


def _json_dumps(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _is_cjk(lang: str | None) -> bool:
    return (lang or "").lower() in {"zh", "ja", "ko", "yue"}
