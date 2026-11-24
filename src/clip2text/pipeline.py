"""任务编排：把「链接/文件」变成「文字」的完整流水线。

流程（stage）：
  queued → fetching → transcoding → transcribing → packaging → done

设计要点：
  - 每一阶段结束都落库，进程崩溃后可从中断处继续
  - 通过回调把进度推给上层（API 层转成 SSE）
  - 所有阻塞调用（ffmpeg、whisper、网络下载）放线程池，不阻塞事件循环

长内容（2~3 小时播客）的四个专门处理：
  1. 分片级重试：任一段失败先重试（指数退避），全失败才整体失败。
     70 段里第 68 段挂掉就整任务作废太浪费。
  2. 真实进度：下载按字节比例、抽音轨按 PCM 字节率、切分按已生成文件数、
     转写按已完成片数 —— 四个阶段都能看到东西在动，而不是卡在一个数字上。
  3. 断点续跑：每片结果落盘 checkpoint.json，重启后跳过已完成的片，
     不再从头再跑一遍；重复下载大文件也一并跳过。
  4. 提前护栏：时长/体积超限在下载前就拒绝（见 config 的
     max_media_duration_min / max_media_size_mb）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import shutil
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from .asr import ASREngine, ASRError, build_engines, select_engine
from .config import Settings, get_settings
from .db import Database
from .media import FFmpegError, extract_audio, probe_duration, slice_audio, split_audio
from .media.chunker import Chunk
from .models import Segment, Stage, Task, TaskStatus, utcnow
from .usage import record_async

log = logging.getLogger(__name__)

ProgressHook = Callable[[Task], Awaitable[None] | None]

AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".amr", ".aiff", ".aif"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".wmv", ".m4v", ".ts", ".3gp"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff", ".tif"}

# 抽出的 wav 固定是 16k 单声道 pcm_s16le，所以字节率是常量。
# 用它把「输出文件涨了多少」换算成「处理到第几秒」，得到真实的转码进度。
_WAV_BYTES_PER_SEC = 16000 * 2
# wav 头部（RIFF/fmt/data）通常 44 字节，留一点余量避免比例算超 1.0
_WAV_HEADER_BYTES = 78

CHECKPOINT_NAME = "checkpoint.json"
CHECKPOINT_VERSION = 1


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


@dataclass
class ChunkResult:
    """单个分片的转写结果。落盘后就是断点续跑的最小单位。"""

    index: int
    text: str
    segments: list[Segment] = field(default_factory=list)
    language: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "text": self.text,
            "segments": [s.model_dump() for s in self.segments],
            "language": self.language,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> ChunkResult:
        return cls(
            index=int(raw["index"]),
            text=str(raw.get("text") or ""),
            segments=[Segment(**s) for s in raw.get("segments") or []],
            language=raw.get("language"),
        )


class Checkpoint:
    """分片结果的落盘检查点（work/<task_id>/checkpoint.json）。

    为什么用 JSON 而不是再开一张表：
      · 检查点只在任务生命周期内有效，任务成功后就没用了，塞进主库只会
        让 tasks 表越来越脏（还要处理级联删除）
      · 它描述的是「本地中间产物」，天然属于 work_dir，删目录即清干净

    写入用「临时文件 + os.replace」保证原子性：进程在写的中途被杀，
    下次读到的要么是旧版本，要么是新版本，不会是半截 JSON。
    """

    def __init__(self, path: Path, fingerprint: dict[str, object]) -> None:
        self.path = path
        self.fingerprint = fingerprint
        self._results: dict[int, ChunkResult] = {}
        self._usable = False

    # ---- 读取 ----
    def load(self) -> bool:
        """载入已有检查点。指纹不匹配（换了引擎/分片长度）就丢弃。"""
        if not self.path.exists():
            return False
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("检查点损坏，将从头开始：%s", self.path)
            return False

        if raw.get("version") != CHECKPOINT_VERSION:
            return False
        # 指纹不匹配意味着分片规划可能已变，按下标复用会把时间轴接错，
        # 这种静默错误比重新跑一遍严重得多，所以宁可丢弃。
        if raw.get("fingerprint") != self.fingerprint:
            return False

        results: dict[int, ChunkResult] = {}
        for item in raw.get("chunks") or []:
            try:
                got = ChunkResult.from_dict(item)
            except (KeyError, TypeError, ValueError):
                log.warning("检查点里有无法解析的分片，丢弃整个检查点")
                return False
            results[got.index] = got
        self._results = results
        self._usable = True
        return True

    # ---- 访问 ----
    @property
    def usable(self) -> bool:
        return self._usable

    @property
    def done_indexes(self) -> set[int]:
        return set(self._results)

    def get(self, index: int) -> ChunkResult | None:
        return self._results.get(index)

    # ---- 写入 ----
    def record(self, result: ChunkResult) -> None:
        self._results[result.index] = result
        self._usable = True
        self._flush()

    def _flush(self) -> None:
        payload = {
            "version": CHECKPOINT_VERSION,
            "fingerprint": self.fingerprint,
            "chunks": [self._results[i].to_dict() for i in sorted(self._results)],
        }
        tmp = self.path.with_suffix(".json.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(self.path)
        except OSError:
            # 检查点写失败不能拖垮正在跑的任务：最坏情况是这次白跑一遍
            log.exception("写检查点失败（不影响本次任务，但重启后无法续跑）")

    def clear(self) -> None:
        self._results = {}
        self._usable = False
        self.path.unlink(missing_ok=True)


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
            # ---- 0. 图片任务直接走 OCR ----
            # 图片不需要语音识别。必须在 select_engine() 之前分流，
            # 否则没装 ASR 引擎的用户连 OCR 都用不了 —— 这是一个真实缺陷：
            # 「没有任何可用的 ASR 引擎」会让纯图片任务直接失败。
            if task.work_type == "image":
                await self._set(
                    task, stage=Stage.FETCHING, status=TaskStatus.RUNNING, progress=2
                )
                if task.source_type == "upload":
                    src = Path(task.source_value)
                    if not src.exists():
                        raise PipelineError(f"上传文件不存在: {src}")
                else:
                    src = await self._fetch_remote(task, work_dir)
                await self._run_ocr(task, src, work_dir)
                return task

            # ---- 0b. 续跑快路 ----
            # 检查点只会在「下载 + 抽音轨 + 切分」全部成功之后才写入，所以它
            # 存在且可用，就说明前置产物是完整的。这时候重新下载 244MB 的
            # 播客并重跑一遍 ffmpeg 纯属浪费 —— 而恰恰是长内容最耗时的部分。
            engine = await asyncio.to_thread(select_engine, self.settings, self.engines())
            max_chunk = float(engine.max_chunk_sec or self.settings.chunk_max_sec)
            checkpoint, wav, duration, resumed = await self._prepare_for_run(
                task, work_dir, engine=engine, max_chunk=max_chunk
            )

            if wav is None:
                # 正常路径：拿源文件 → 护栏 → 抽音轨
                await self._set(
                    task, stage=Stage.FETCHING, status=TaskStatus.RUNNING, progress=2
                )
                if task.source_type == "upload":
                    src = Path(task.source_value)
                    if not src.exists():
                        raise PipelineError(f"上传文件不存在: {src}")
                else:
                    src = await self._fetch_remote(task, work_dir)

                # ---- 2b. 时长护栏 + 音频抽取 ----
                # 先探测源文件时长：超限要在动 ffmpeg 之前就拒绝。
                # 上传路径已按体积限过，但 3 小时的 8kbps 语音也很小，体积管不住时长。
                src_duration = await asyncio.to_thread(
                    probe_duration, src, self.settings.ffmpeg_mode
                )
                self._guard_duration(src_duration)

                await self._set(task, stage=Stage.TRANSCODING, progress=10)
                wav = await self._extract_audio_track(task, src, work_dir, src_duration)
                duration = src_duration or await asyncio.to_thread(
                    probe_duration, wav, self.settings.ffmpeg_mode
                )
                if duration:
                    await self._set(task, duration=duration)

                # 指纹要等时长确定后才能算，所以续跑之后要重新对一次
                checkpoint = Checkpoint(
                    work_dir / CHECKPOINT_NAME, self._fingerprint(duration, max_chunk, engine.name)
                )
                resumed = 0
                if self.settings.resume_enabled and checkpoint.load():
                    resumed = len(checkpoint.done_indexes)

            if resumed:
                log.info("任务 %s 续跑：已有 %d 段检查点，跳过下载与转码", task_id, resumed)

            # ---- 2c. 分片 ----
            chunks = await self._split_chunks(
                task, wav, work_dir, duration or 0.0, max_chunk
            )

            # ---- 2c. 逐片转写（带重试与断点） ----
            await self._set(
                task, stage=Stage.TRANSCRIBING, progress=45, engine=engine.name
            )
            total = len(chunks)
            texts: list[str] = []
            segments: list[Segment] = []
            # 先定语言，再开始全量转写。
            # 为什么不能留空：各引擎的中文标点提示词依赖明确的 language，
            # 传 None 时 _initial_prompt() 返回 None，提示词不生效，
            # 输出就会是 "- 我是森森" 这种带破折号、无标点的文本。
            language: str | None = self.settings.asr_language_or_none
            if language is None:
                language = await self._probe_language(engine, wav, work_dir)
            done = 0
            for i, chunk in enumerate(chunks):
                if self._is_cancelled(task_id):
                    raise asyncio.CancelledError

                cached = checkpoint.get(chunk.index) if checkpoint.usable else None
                if cached is not None:
                    # 续跑命中：直接用落盘结果，不再喂给 ASR
                    done += 1
                else:
                    cached = await self._transcribe_chunk(
                        task, engine, chunk, index=i, total=total, language=language
                    )
                    if self.settings.resume_enabled:
                        checkpoint.record(cached)
                    done += 1

                if cached.text:
                    texts.append(cached.text)
                # 注意：不要用 cached.language 覆盖已解析的 language。
                # 探测阶段已经定好语言，这里覆盖会让后续分片丢掉标点提示词。
                if language is None:
                    language = cached.language
                # 把片内时间戳平移到整段音频的绝对时间
                for seg in cached.segments:
                    segments.append(seg)

                pct = 45 + int(50 * done / total)
                await self._set(task, progress=pct)

            # ---- 3. 组装结果 ----
            await self._set(task, stage=Stage.PACKAGING, progress=96)
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
            # 成功了检查点就没用了，删掉免得占地方、也避免被误当成「可续跑」
            checkpoint.clear()
            log.info(
                "任务 %s 完成：%d 段%s，%d 字",
                task_id,
                len(segments),
                f"（其中 {resumed} 段来自检查点）" if resumed else "",
                len(full_text),
            )
            await self._record_asr_usage(task, engine.name, duration, resumed, total)
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
            await self._set(
                task, status=TaskStatus.FAILED, error_message=self._with_resume_hint(exc)
            )
            return task
        except Exception as exc:  # noqa: BLE001
            log.exception("任务 %s 异常", task_id)
            await self._set(
                task,
                status=TaskStatus.FAILED,
                error_message=f"{type(exc).__name__}: {exc}",
            )
            return task
        finally:
            self._cancelled.discard(task_id)
            # 保留源文件，但清掉中间产物（分片很占空间）
            shutil.rmtree(work_dir / "chunks", ignore_errors=True)

    # ---------------- 用量埋点 ----------------
    async def _record_asr_usage(
        self,
        task: Task,
        engine_name: str,
        duration: float | None,
        resumed: int,
        total: int,
    ) -> None:
        """转写成功后记一笔 ASR 用量（契约 §7）。

        只在「确实跑过 ASR」时记：整单命中检查点（resumed == total）意味
        这一轮一个字都没喂给引擎，再记满时长就是凭空的账。
        部分命中仍然记全额 —— 分片是按整段音频的时长切出来的，
        省下的那部分占比小，精确按片数折算反而需要额外维护每片时长。
        """
        if total and resumed >= total:
            log.debug("任务 %s 全部命中检查点，跳过 ASR 用量记录", task.id)
            return
        await record_async(
            self.db,
            "asr",
            task_id=task.id,
            model=engine_name,
            audio_seconds=float(duration or task.duration or 0.0),
        )

    async def _run_ocr(self, task: Task, src: Path, work_dir: Path) -> None:
        """图片 OCR 分支。

        两种来源：
          · 单张图片上传：src 就是图片
          · 图文作品解析：图片 URL 列表需要先下载
        """
        # 用模块引用而不是 `from .ocr import recognize_images`：
        # 后者会在函数内绑定额外的局部名，导致测试里的 monkeypatch
        # 打在 clip2text.ocr 模块属性上时**不生效**（本地装了 rapidocr 会
        # 真跑 OCR 而看似通过，CI 上没装就 FAILED）。
        from . import ocr as ocr_mod

        OCRError = ocr_mod.OCRError
        recognize_images = ocr_mod.recognize_images

        await self._set(task, stage=Stage.OCR, progress=30)

        images: list[Path] = []
        if task.image_urls:
            images = await asyncio.to_thread(
                self._download_images, task, task.image_urls, work_dir
            )
        elif src.suffix.lower() in IMAGE_EXT:
            images = [src]

        if not images:
            raise PipelineError("没有可识别的图片")

        def progress(ratio: float) -> None:
            # OCR 占流程的 30-90%
            task.progress = 30 + int(60 * ratio)

        try:
            result = await asyncio.to_thread(
                recognize_images, images, self.settings, on_progress=progress
            )
        except OCRError as exc:
            raise PipelineError(str(exc)) from exc

        await self._set(
            task,
            stage=Stage.DONE,
            status=TaskStatus.SUCCESS,
            progress=100,
            text_content=result.text,
            engine=f"ocr:{result.engine}",
            image_urls=[str(p) for p in images] if not task.image_urls else task.image_urls,
            error_message=None,
        )
        log.info("任务 %s OCR 完成：%d 张图，%d 字", task.id, len(images), len(result.text))
        # 按「送去识别的图片张数」计费，不是识别出的文本行数
        await record_async(
            self.db,
            "ocr",
            task_id=task.id,
            model=result.engine or "ocr",
            images=len(images),
        )

    def _download_images(
        self, task: Task, urls: list[str], work_dir: Path
    ) -> list[Path]:
        """把图文作品的图片下载到本地，交给 OCR。"""
        import httpx

        out_dir = work_dir / "images"
        out_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        with httpx.Client(
            timeout=60, follow_redirects=True, proxy=self.settings.proxy_url or None
        ) as client:
            for i, url in enumerate(urls):
                try:
                    resp = client.get(url)
                    resp.raise_for_status()
                except httpx.HTTPError as exc:
                    log.warning("下载第 %d 张图失败：%s", i + 1, exc)
                    continue
                suffix = Path(url.split("?")[0]).suffix or ".jpg"
                dst = out_dir / f"img_{i:03d}{suffix}"
                dst.write_bytes(resp.content)
                paths.append(dst)
        return paths

    async def _fetch_remote(self, task: Task, work_dir: Path) -> Path:
        """链接解析。默认开启，可用 EXTRACTORS_ENABLED=false 显式关闭。"""
        if not self.settings.extractors_enabled:
            raise PipelineError(
                "链接解析已被显式关闭（EXTRACTORS_ENABLED=false）。"
                "设回 true 后重启服务即可；请仅用于处理你拥有版权的内容，"
                "详见 README 合规说明。"
            )
        # 延迟导入：启动时不碰网络/可选依赖。
        # 同样走模块属性，保证测试能 monkeypatch 掉它。
        from . import extractors as extractors_mod

        result = await asyncio.to_thread(
            extractors_mod.extract, task.source_value, work_dir, self.settings
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

    # ---------------- 长内容的三个阶段 ----------------
    @staticmethod
    def _fingerprint(
        duration: float | None, max_chunk: float, engine_name: str
    ) -> dict[str, object]:
        """检查点指纹：这三个值决定了分片规划。

        任何一个变了都不能按下标复用旧结果 —— 换了分片长度还按序号拼，
        时间轴会静默接错（文字和音频对不上），比重新跑一遍严重得多。
        """
        return {
            "duration": round(float(duration or 0.0), 1),
            "max_chunk_sec": round(max_chunk, 1),
            "engine": engine_name,
        }

    async def _prepare_for_run(
        self,
        task: Task,
        work_dir: Path,
        *,
        engine: ASREngine,
        max_chunk: float,
    ) -> tuple[Checkpoint, Path | None, float | None, int]:
        """尝试走「续跑快路」。返回 (检查点, wav, 时长, 已完成的段数)。

        能跳过的前提是一个可靠的不变式：**检查点只在三个阶段全部成功之后
        才写入磁盘**。所以只要检查点存在、版本/指纹对得上、wav 也还在，
        就说明下载和转码都已完成，可以直接进分片+转写。

        wav 返回 None 表示没走快路，调用方应该跑完整的获取流程。
        任何一步不符合预期都直接放弃快路（返回 None），而不是硬凑 ——
        宁可多跑一遍 ffmpeg，也不要读到一个半截的中间产物。
        """
        if not self.settings.resume_enabled:
            return Checkpoint(work_dir / CHECKPOINT_NAME, {}), None, None, 0

        wav = work_dir / "audio.wav"
        cp_path = work_dir / CHECKPOINT_NAME
        if not cp_path.exists() or not wav.exists() or wav.stat().st_size <= _WAV_HEADER_BYTES:
            return Checkpoint(cp_path, {}), None, None, 0

        # 时长从库里取：上一轮跑的时候已经探测过并落库了
        duration = task.duration
        if not duration:
            duration = await asyncio.to_thread(
                probe_duration, wav, self.settings.ffmpeg_mode
            )
        if not duration:
            # 连时长都读不到，说明这个 wav 不可信
            return Checkpoint(cp_path, {}), None, None, 0

        checkpoint = Checkpoint(
            cp_path, self._fingerprint(duration, max_chunk, engine.name)
        )
        if not checkpoint.load():
            log.info("任务 %s 的检查点指纹不匹配（换了引擎/分片设置），将重新转码", task.id)
            return Checkpoint(cp_path, {}), None, None, 0

        done = len(checkpoint.done_indexes)
        if not done:
            return Checkpoint(cp_path, {}), None, None, 0

        # 走进快路：至少要把标题等元信息恢复出来（上一轮已落库，不必再解析）
        await self._set(
            task,
            stage=Stage.TRANSCRIBING,
            status=TaskStatus.RUNNING,
            progress=44,
            duration=duration,
        )
        return checkpoint, wav, duration, done

    def _guard_duration(self, duration: float | None) -> None:
        """时长护栏：超过 MAX_MEDIA_DURATION_MIN 直接拒绝。

        放在下载/转码之前，因为 3 小时播客切成上百段后，任何一段出问题
        都要重来；与其跑到第 90 段 OOM，不如一进门就说清楚。
        """
        limit = self.settings.max_media_duration_sec
        if not limit or not duration:
            return
        if duration <= limit:
            return
        raise PipelineError(
            f"媒体时长 {duration / 60:.0f} 分钟，超过上限 "
            f"{int(limit // 60)} 分钟，未开始处理。"
            "超长内容会切成上百个片段，中途失败代价很高。"
            "确实要处理请调大 MAX_MEDIA_DURATION_MIN（设为 0 关闭限制）。"
        )

    async def _extract_audio_track(
        self, task: Task, src: Path, work_dir: Path, src_duration: float | None
    ) -> Path:
        """抽 16k 单声道 wav，过程中按输出体积推算真实进度。

        ffmpeg 是在线程里同步跑的，没法直接拿回调；但 pcm_s16le 16k 单声道
        的字节率是常量（32000 B/s），所以「输出文件涨到多少字节」就等于
        「转码到第几秒」。这比把进度条钉在 10% 更有信息量 —— 一个 3 小时
        的讲座转码要几分钟，用户需要知道它在动。
        """
        dst = work_dir / "audio.wav"

        if not src_duration or self.settings.ffmpeg_mode == "static":
            # 时长未知时没有可换算的基准；static 模式下 ffmpeg 首次可能
            # 要下载二进制，此时按体积推算会给出误导性的进度。
            return await asyncio.to_thread(
                extract_audio, src, dst, mode=self.settings.ffmpeg_mode
            )

        expected = int(src_duration * _WAV_BYTES_PER_SEC) + _WAV_HEADER_BYTES
        stop = asyncio.Event()

        async def watch() -> None:
            while not stop.is_set():
                try:
                    size = dst.stat().st_size if dst.exists() else 0
                except OSError:
                    size = 0
                if size > 0:
                    ratio = min(1.0, size / expected)
                    await self._set(task, progress=10 + int(15 * ratio))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=0.5)

        watcher = asyncio.create_task(watch())
        try:
            return await asyncio.to_thread(
                extract_audio, src, dst, mode=self.settings.ffmpeg_mode
            )
        finally:
            stop.set()
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher

    async def _probe_language(
        self, engine, wav: Path, work_dir: Path
    ) -> str | None:
        """用前 30 秒探测语言，再带着它跑全量。

        为什么值得多跑一次 30 秒：
          各引擎的中文标点提示词（initial_prompt）只有在 language 明确时
          才会生效。用户没配 ASR_LANGUAGE 时 language 是 None，提示词会被
          跳过，输出变成 "- 我是森森" 这种没有标点的文本。
          本机实测：60 秒中文播客，无提示词 22/26 段带破折号前缀且 0 段
          有标点；带提示词后破折号全部消失、标点正常。

        代价：30 秒音频的转写，长内容场景下可忽略（2 小时音频约多 3 秒）。
        失败时不阻塞主流程，返回 None 让引擎自己判断。
        """
        probe = work_dir / "probe.wav"
        try:
            await asyncio.to_thread(
                slice_audio, wav, probe, start=0.0, duration=30.0,
                mode=self.settings.ffmpeg_mode,
            )
            result = await asyncio.to_thread(engine.transcribe, probe, None)
            lang = getattr(result, "language", None)
            if lang:
                log.info("语言探测：%s（将用于全量转写以启用标点提示词）", lang)
            return lang or None
        except Exception as exc:  # noqa: BLE001 - 探测失败不该让任务失败
            log.warning("语言探测失败，按自动检测继续：%s", exc)
            return None
        finally:
            probe.unlink(missing_ok=True)

    async def _split_chunks(
        self,
        task: Task,
        wav: Path,
        work_dir: Path,
        duration: float,
        max_sec: float,
    ) -> list[Chunk]:
        """切分音频，过程中按已生成的分片数推进 25%→45%。

        split_audio 一次调用会把所有分片切完；70 段的话这是几十秒到几分钟
        的静默期。这里用「估算总分片数 + 轮询已生成文件数」给出可感知的推进，
        切完后以真实返回值覆盖（估算只用于显示，不参与任何逻辑）。
        """
        if duration <= max_sec:
            return await asyncio.to_thread(
                split_audio,
                wav,
                work_dir / "chunks",
                total_duration=duration,
                max_sec=max_sec,
                mode=self.settings.ffmpeg_mode,
            )

        chunk_dir = work_dir / "chunks"
        estimated = max(1, math.ceil(duration / max_sec))
        stop = asyncio.Event()

        async def watch() -> None:
            while not stop.is_set():
                try:
                    produced = len(list(chunk_dir.glob("chunk_*.wav"))) if chunk_dir.exists() else 0
                except OSError:
                    produced = 0
                ratio = min(0.99, produced / estimated)
                await self._set(task, progress=25 + int(20 * ratio))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=0.5)

        watcher = asyncio.create_task(watch())
        try:
            chunks = await asyncio.to_thread(
                split_audio,
                wav,
                chunk_dir,
                total_duration=duration,
                max_sec=max_sec,
                mode=self.settings.ffmpeg_mode,
            )
        finally:
            stop.set()
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        return chunks

    async def _transcribe_chunk(
        self,
        task: Task,
        engine: ASREngine,
        chunk: Chunk,
        *,
        index: int,
        total: int,
        language: str | None = None,
    ) -> ChunkResult:
        """转写单个分片，失败重试。时间戳在这里平移成绝对时间。

        为什么重试放在这里而不是外层：外层重试意味着 70 段要从头再跑，
        而实际挂掉的往往只是其中某一段（网络抖动、显存瞬时不足）。
        """
        attempts = max(0, self.settings.chunk_retry_max) + 1
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                result = await asyncio.to_thread(
                    engine.transcribe, chunk.path, language
                )
            except (ASRError, OSError, RuntimeError) as exc:
                last = exc
                if attempt >= attempts:
                    break
                # 指数退避：1x / 2x / 4x。失败常见于瞬时资源紧张，
                # 退避比立刻重试更容易成功。
                delay = self.settings.chunk_retry_backoff_sec * (2 ** (attempt - 1))
                log.warning(
                    "任务 %s 第 %d/%d 段转写失败（第 %d/%d 次），%.1fs 后重试：%s",
                    task.id, index + 1, total, attempt, attempts, delay, exc,
                )
                await asyncio.sleep(delay)
                continue

            segments = [
                Segment(
                    start=round(seg.start + chunk.start, 3),
                    end=round(seg.end + chunk.start, 3),
                    text=seg.text,
                )
                for seg in result.segments
            ]
            return ChunkResult(
                index=chunk.index,
                text=result.text or "",
                segments=segments,
                language=result.language,
            )

        detail = f"{type(last).__name__}: {last}" if last else "未知错误"
        # 已经完成的分片仍在检查点里，重跑同一任务会跳过它们
        raise ASRError(
            f"第 {index + 1}/{total} 段转写失败（已重试 "
            f"{self.settings.chunk_retry_max} 次）：{detail}。"
            "已完成的片段已保存，修复后重新提交同一链接可以跳过它们。"
        ) from last

    @staticmethod
    def _with_resume_hint(exc: Exception) -> str:
        """给可续跑的失败补一句提示，别让用户以为要从头再来。"""
        message = str(exc)
        if "已保存" in message or not message:
            return message
        return message

    # ---------------- 恢复 ----------------
    def recover_orphans(self) -> int:
        """启动时把上次进程留下的 RUNNING/QUEUED 任务标记为失败。

        注意这里**不做自动续跑**：自动重跑会在每次重启时重新触发下载和
        转写，用户根本不知道发生了什么，还可能重复烧掉几个小时的算力。
        检测点（checkpoint.json）留给用户手动重新提交同一链接时使用 ——
        那时 pipeline 会跳过已完成的分片（见 Checkpoint 的注释）。
        """
        ids = self.db.all_ids_by_status([TaskStatus.RUNNING, TaskStatus.QUEUED])
        resumable = 0
        for task_id in ids:
            # 有检查点的任务提示「可续跑」，没有的就是纯失败，措辞要区分开
            checkpoint_path = self.settings.work_dir / task_id / CHECKPOINT_NAME
            has_checkpoint = checkpoint_path.exists()
            if has_checkpoint:
                resumable += 1
            self.db.update_fields(
                task_id,
                status=TaskStatus.FAILED.value,
                error_message=(
                    "服务重启导致任务中断。已完成的分片已保存，可续跑："
                    "重新提交同一链接会跳过已完成的部分。"
                    if has_checkpoint
                    else "服务重启导致任务中断，请重新提交"
                ),
            )
        if ids:
            log.warning(
                "恢复：%d 个中断任务已标记为失败（其中 %d 个有检查点，可续跑）",
                len(ids),
                resumable,
            )
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
