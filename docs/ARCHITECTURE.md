# clip2text 架构设计

> 状态：v0.1 草案（契约先行，代码未开始）
> 目标：**一个 key 都不填，clone 下来就能完整跑通「上传文件 → 转写 → 导出」**。
> 付费能力一律做成可插拔的可选项，而不是门槛。

---

## 1. 设计原则

| 原则 | 含义 | 反例 |
|---|---|---|
| **零 key 可跑** | 默认路径不依赖任何外部账号 | 一上来就要求填 ASR key |
| **能力自描述** | `/api/v1/capabilities` 汇报本机真实可用能力，前端据此禁用按钮 | 前端硬编码「功能都在」然后报错 |
| **引擎可替换** | ASR/OCR/LLM 都是接口 + 多实现，用配置切换 | 把 faster-whisper 写死在业务逻辑里 |
| **解析器插件化** | 每个平台一个 adapter，通过 entry_points 注册 | 把小红书逻辑塞进主流程 |
| **异步任务化** | 所有耗时操作进队列，前端只看状态 | 请求里同步跑 ASR 然后超时 |
| **契约先于实现** | 先定 Task 数据结构和 API，再写代码 | 各模块各自定义字段，最后对不上 |

---

## 2. 能力分档（A / B / C）

这是本项目的核心判断：**哪些是白送的，哪些要写代码，哪些必须花钱。**

### A 档 —— `pip install` 即可，0 key、0 成本

| 能力 | 依赖 | 已验证版本 | 备注 |
|---|---|---|---|
| 音视频转码 / 抽音频 | `static-ffmpeg` | 3.0 | 首次运行自动下二进制，规避系统无 ffmpeg |
| ASR 本地首选 | `faster-whisper` | 1.2.1 | CPU/GPU 通用 |
| ASR Apple 加速 | `mlx-whisper` | 0.4.3 | Apple Silicon 专用，本项目开发机首选 |
| ASR 中文优化 | `funasr` | 1.4.28 | SenseVoice，标点/语气词最好 |
| OCR | `rapidocr-onnxruntime` | 1.4.4 | ⚠️ 要求 Python < 3.13 |
| 文档导出 | `python-docx`、`openpyxl` | — | docx / xlsx |
| 任务队列 | SQLite + `asyncio.Semaphore` | 标准库 | 零外部依赖 |
| 进度推送 | SSE | 标准库 | 替代轮询 |

### B 档 —— 库已存在，但业务逻辑要自己写（花时间不花钱）

| 能力 | 工作量估计 | 说明 |
|---|---|---|
| yt-dlp 封装 | ~1 天 | 其已支持小红书 / 抖音 / B站 / 微博 / TikTok / YouTube / 头条 / 西瓜 / AcFun |
| 快手 adapter | 3-5 天 | yt-dlp 不支持，需处理 `v.kuaishou.com` 跳转与页面内嵌 JSON |
| 视频号 adapter | 1 周+ | 必须 Playwright，最脆弱 |
| 音频分片 | ~2 天 | **架构必需**，非优化项（见 §4） |
| 小红书原图直链 | ~1 天 | 剥离 CDN 的 `imageView2` / `!nd_dft_*` 后缀 |
| 已烧录水印去除 | 大 | `delogo` 需人工框坐标，一期不做 |

### C 档 —— 必须外部接入（花钱或申请 key）

| 能力 | 为什么躲不掉 | 个人侧成本 |
|---|---|---|
| 平台数据（高频抓取） | 小红书/抖音风控 + 签名（`X-s`、`a_bogus`） | 代理 ¥10-50/月起 |
| 云 ASR（可选） | 不想占用本地算力 / 想更快 | ¥0.78/小时音频（DeepSeek 现价） |
| LLM 改写（可选） | 本地小模型质量不足 | ¥0.005/条视频 |
| 商业数据 API（可选） | 完全不想维护解析器 | 按次计费 |

---

## 3. 分层架构

```
┌──────────────────────────────────────────────────────┐
│  apps/web   Next.js / Nuxt 3（SSR，可选）             │
│  粘贴框 · 任务列表 · 结果卡片 · 导出 · 能力探测        │
└───────────────────────┬──────────────────────────────┘
                        │ REST + SSE
┌───────────────────────▼──────────────────────────────┐
│  apps/api   FastAPI                                  │
│  鉴权(可关) · 配额 · 任务 CRUD · capabilities         │
└───────┬──────────────────────────────┬───────────────┘
        │ 入队                          │ 查询
┌───────▼────────────┐        ┌────────▼──────────────┐
│ SQLite 任务表       │        │ 状态机                │
│ + 进程内 worker pool│        │ QUEUED→RUNNING→...    │
└───────┬────────────┘        └───────────────────────┘
        │
┌───────▼──────────────────────────────────────────────┐
│  packages/core  任务编排（stage 流水线）              │
│   fetching → extracting → transcoding → transcribing  │
│            → ocr → done                              │
└───┬──────────┬──────────┬──────────┬─────────────────┘
    │          │          │          │
┌───▼───┐ ┌────▼────┐ ┌───▼────┐ ┌───▼──────┐
│extract│ │ media   │ │  asr   │ │  ocr     │
│ors    │ │ ffmpeg  │ │ 多引擎 │ │ 多引擎   │
│插件   │ │ chunker │ │        │ │          │
└───────┘ └─────────┘ └────────┘ └──────────┘
                        │
              ┌─────────▼──────────┐
              │ exporters          │
              │ txt/md/docx/xlsx   │
              └────────────────────┘
```

---

## 4. 关键约束：音频必须分片

这不是性能优化，是硬性技术约束：

| 服务 | 单次时长上限 | 影响 |
|---|---|---|
| DeepSeek ASR | **120 秒** | 10 分钟视频必须至少切 6 段 |
| 多数云 ASR | 60s – 5min / 段 | 同上 |
| 本地 faster-whisper | 无硬限制（受显存约束） | 但仍建议分片以显示进度、支持断点续跑 |

```python
# packages/media/chunker.py
def split_by_silence(wav: str, max_sec: int = 110) -> list[Chunk]:
    """用 ffmpeg silencedetect 找静音点，在句间切分，避免切碎一句话。

    返回的 Chunk 带 (index, start_sec, end_sec, path)，
    上层据此计算整体进度百分比，并支持中断后从任意 Chunk 续跑。
    """
```

同时注意：**DeepSeek ASR 只返回整段 `text`，没有时间戳/分段**。
若需要「带时间戳的 segments」（本项目的差异化能力），必须走本地 whisper。
这是真实的取舍点，实现时要在 `capabilities` 里如实上报。

---

## 5. 数据契约

### 5.1 Task

字段刻意对齐 AnyToCopy 的开放 API 响应，便于迁移与对照。

```python
class Task(BaseModel):
    id: str
    source_type: Literal["url", "upload"]
    source_value: str
    platform: str | None            # xhs / douyin / bilibili / local ...
    work_type: Literal["video", "image", "audio"] | None

    status: Literal["QUEUED", "RUNNING", "SUCCESS", "FAILED", "CANCELED"]
    stage: Literal[
        "queued", "fetching", "extracting", "transcoding",
        "transcribing", "ocr", "packaging", "done",
    ]
    progress: int                   # 0-100

    # ---- 结果字段（对齐 AnyToCopy）----
    title: str | None
    content: str | None             # 作品正文
    text_content: str | None        # 语音转写（他们的 textContent）
    cover: str | None
    video_url: str | None
    video_urls: list[str] = []
    image_urls: list[str] = []
    audio_url: str | None
    duration: float | None          # 秒，也是计费依据

    # ---- 差异化 ----
    segments: list[Segment] = []    # 带时间戳的分句，仅本地 whisper 提供
    language: str | None

    error_message: str | None
    created_at: datetime
    updated_at: datetime


class Segment(BaseModel):
    start: float
    end: float
    text: str
```

### 5.2 引擎接口

```python
# packages/asr/base.py
class ASREngine(Protocol):
    name: str
    supports_timestamps: bool
    max_chunk_sec: int | None

    def available(self) -> bool: ...
    def transcribe(self, wav_path: str, language: str | None) -> Transcript: ...


class Transcript(BaseModel):
    text: str
    segments: list[Segment]
    language: str | None
```

同理定义 `OCREngine`、`Extractor`、`LLMProvider`、`Exporter`。

### 5.3 解析器插件

社区可以单独发一个 pip 包来支持新平台，主仓库不必背锅。

```python
# packages/extractors/base.py
class Extractor(Protocol):
    hosts: tuple[str, ...]

    def matches(self, url: str) -> bool: ...
    def extract(self, url: str, ctx: ExtractContext) -> ExtractResult: ...
    def health(self) -> bool: ...
```

发现机制：

```python
importlib.metadata.entry_points(group="clip2text.extractors")
```

社区插件示例 `pyproject.toml`：

```toml
[project.entry-points."clip2text.extractors"]
kuaishou = "clip2text_kuaishou:KuaishouExtractor"
```

---

## 6. HTTP API 契约

| Method | Path | 说明 |
|---|---|---|
| `POST` | `/api/v1/uploads` | 上传文件，返回 `upload_id` |
| `POST` | `/api/v1/tasks` | 创建任务 `{source:{type,value}, options}` |
| `GET` | `/api/v1/tasks/{id}` | 单任务状态与结果 |
| `GET` | `/api/v1/tasks` | 列表 + 过滤 + 分页 |
| `DELETE` | `/api/v1/tasks/{id}` | 删除 |
| `POST` | `/api/v1/tasks/{id}/cancel` | 取消排队/运行中任务 |
| `GET` | `/api/v1/tasks/{id}/events` | **SSE** 进度流（替代 3s 轮询） |
| `POST` | `/api/v1/tasks/{id}/export` | `{format: txt\|markdown\|docx\|xlsx}` → 文件流 |
| `POST` | `/api/v1/rewrite` | `{task_id, style}` → SSE 流式改写 |
| `GET` | `/api/v1/capabilities` | **★ 本机可用引擎清单** |
| `GET` | `/api/v1/health` | 健康检查 |

### `/api/v1/capabilities` 响应示例

这是整个项目最该有的接口——前端据此决定渲染什么、提示什么。

```json
{
  "version": "0.1.0",
  "ffmpeg": { "available": true, "source": "static", "path": "..." },
  "asr": [
    { "name": "mlx", "available": true, "timestamps": true, "max_chunk_sec": null },
    { "name": "faster-whisper", "available": true, "timestamps": true },
    { "name": "deepseek", "available": false, "reason": "未配置 DEEPSEEK_API_KEY", "max_chunk_sec": 120 }
  ],
  "ocr": [
    { "name": "rapidocr", "available": true },
    { "name": "deepseek", "available": false, "reason": "未配置 DEEPSEEK_API_KEY" }
  ],
  "llm": { "available": false, "reason": "未配置 LLM_BASE_URL" },
  "extractors": { "enabled": false, "platforms": [] },
  "exporters": ["txt", "markdown", "docx", "xlsx"]
}
```

---

## 7. 成本模型（个人侧）

以**一条 3 分钟视频**（0.05 小时、约 800 字文案）为基准单位：

| 项目 | 纯本地 | 混合（本地 ASR + 云 LLM） | 全云 |
|---|---|---|---|
| ASR | ¥0 | ¥0 | ¥0.039（限时 ¥0.019） |
| OCR（3 张图） | ¥0 | ¥0 | ¥0.005 |
| LLM 改写 | ¥0（Ollama） | ¥0.005 | ¥0.005 |
| 平台抓取（含代理摊销） | ¥0 ～ 0.02 | ¥0.02 | ¥0.02 |
| **单条合计** | **≈¥0** | **≈¥0.025** | **≈¥0.064** |
| **100 条合计** | **≈¥0** | **≈¥2.5** | **≈¥6.4** |

云服务单价（2026-11 核实，汇率 1 USD ≈ 6.68 CNY）：

| API | 官方价 | 折人民币 |
|---|---|---|
| DeepSeek ASR | $0.116 / 小时音频 | ¥0.78（限时 5 折 ¥0.39） |
| DeepSeek OCR | $0.225 / 1000 页 | ¥0.0015 / 页 |
| DeepSeek LLM 输入（flash 错峰） | $0.15 / 百万 token | ¥1.0 / 百万 |
| DeepSeek LLM 输出（flash 错峰） | $0.30 / 百万 token | ¥4.0 / 百万 |

> 一个 key 覆盖 ASR + OCR + LLM 三项，对个人项目是最省心的接法。

---

## 8. 开发环境约定

| 项 | 值 | 原因 |
|---|---|---|
| **Python** | **3.12** | `rapidocr-onnxruntime` 要求 <3.13；FastAPI + pydantic v2 建议 ≥3.11 |
| Node | ≥20 | 仅前端需要 |
| 包管理 | `uv`（推荐）或 pip | uv 能锁版本并管理 Python 版本 |
| 数据库 | SQLite（默认） | 零依赖；预留 `DATABASE_URL` 切 Postgres |

⚠️ 开发机当前系统 Python 为 3.9.6，**过旧**，必须先装 3.12。

---

## 9. 里程碑

| 阶段 | 内容 | 依赖 | 可发布 |
|---|---|---|---|
| **P0** | 上传音视频 → 转写 → 导出 txt/md/docx | ffmpeg + faster-whisper | ✅ 零 key、零合规风险 |
| **P1** | 链接解析（yt-dlp，默认关闭） | yt-dlp | ✅ |
| **P2** | 批量 + 队列 + SSE + 历史搜索 | SQLite | ✅ |
| **P3** | 图片 OCR + LLM 改写 + 插件生态 | RapidOCR + OpenAI 兼容 | ✅ |

---

## 10. 合规边界（写进 README）

1. 默认关闭链接解析（`EXTRACTORS_ENABLED=false`），仅处理用户**自己上传**的文件
2. 打开链接解析时，README 明确声明：**仅供个人学习与处理自有内容，遵守各平台 ToS 与著作权法**
3. **不提供托管 SaaS 版本**，避免重蹈同类产品的法律风险
4. 主仓库 License：Apache-2.0；各平台 adapter 单独目录并附免责声明
5. 仓库内**不得**出现任何平台的签名密钥、登录 Cookie
