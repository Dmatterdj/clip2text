# clip2text

![version](https://img.shields.io/badge/version-0.1.0-blue)
![python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
![license](https://img.shields.io/badge/license-Apache--2.0-green)

> **0.1.0 是早期版本。** 核心链路（链接/上传 → 转写 → 导出）已稳定，
> 但 HTTP API 与配置项在 1.0 之前可能调整。生产使用请锁定版本。

把视频、音频、图片里的内容，提取成可编辑的文字。**本地优先、默认零 API key、不上传第三方。**

一个可以完全跑在自己电脑上的"文案提取"工具。不需要注册、不需要充值，
文件不会离开你的机器。

主入口是**贴链接**——长视频播客、知识科普、读书、讲座这类内容几乎都以链接形式存在，
上传文件是并行的次选入口：

```
粘贴链接 / 选择文件 → 抽音轨 → 语音转写 → 导出 txt / md / docx / xlsx
图片 / 图文 → OCR → 同上
```

---

## 现在能做什么

| 能力 | 状态 | 需要额外安装 |
|---|---|---|
| **贴链接 → 转写 → 导出**（B 站 / 小宇宙 / YouTube 等） | ✅ **默认开启** | — |
| 上传音视频 → 转写 → 导出 | ✅ | 一个本地 ASR 引擎 |
| 图片 / 图文 OCR → 导出 | ✅ | `clip2text[ocr]` |
| 带时间戳的分句 | ✅ | 需 mlx / faster-whisper |
| 静音感知的音频分片 | ✅ | — |
| 长内容支持（分片重试 + 断点续跑 + 真实进度） | ✅ | — |
| B 站多 P / 合集（`?p=N` 指定分 P，或查看完整清单） | ✅ | — |
| 4 种导出格式 | ✅ | — |
| 批量提取（含自动去重、排队） | ✅ | — |
| AI 文案改写（5 种风格，流式） | ✅ 可选 | 任意 OpenAI 兼容服务 |
| SSE 进度流 | ✅ | — |
| 能力自检 `/capabilities` | ✅ | — |

### 支持的平台

`/api/v1/capabilities` 里的 `extractors.catalog` 会返回带 `available` 实测状态的清单。

| 平台 | 实现 | 说明 |
|---|---|---|
| B 站 | 内置 | 公开 web 接口；**多 P / 合集支持 `?p=N`** |
| 小宇宙 | 内置 | 中文播客主场；读页面内嵌 JSON 的公开数据 |
| 播客 RSS / Atom | 内置 | 泛用 feed，含 `feed.xyzfm.space`（小宇宙官方导出格式） |
| YouTube | yt-dlp | 国际知识类长视频 |
| 喜马拉雅 | yt-dlp | 国内长音频 |
| 网易云播客 | yt-dlp | `NetEaseMusicProgram` |
| 抖音 / 小红书 / 微博 / TikTok | yt-dlp | 短视频，顺手支持 |
| TED / Vimeo / Twitch / acast / Libsyn 等 | yt-dlp | yt-dlp 共覆盖 1754 个站点 |

解析顺序：插件 → 内置适配器（RSS / 小宇宙 / B 站）→ yt-dlp 兜底。
内置的排前面，因为它们能拿到比 yt-dlp 更完整的中文元信息（shownotes、分 P 清单）。

**不支持**：快手、微信视频号（yt-dlp 无对应 extractor）。它们的接口签名几天一变，
写进主仓库等于给自己挖维护坑。想要支持可以自己写插件（见下方「插件」）。

---

## 快速开始

### 1. 环境

需要 **Python 3.11 或 3.12**（不要用 3.13，OCR 依赖还不支持）。

```bash
python3.12 --version
```

推荐用 [uv](https://docs.astral.sh/uv/)：

```bash
uv venv --python 3.12
source .venv/bin/activate
```

### 2. 安装

```bash
pip install -e .
```

**再装你需要的可选能力**（按需组合，都不需要 API key）：

```bash
pip install -e ".[mlx]"       # Apple Silicon Mac 的 ASR，速度最快
pip install -e ".[faster]"    # Windows / Linux / Intel Mac 的 ASR
pip install -e ".[ocr]"       # 图片文字识别
```

一步到位：

```bash
pip install -e ".[mlx,ocr]"
```

> **链接解析（yt-dlp）已经是核心依赖**，`pip install -e .` 就带上了，
> 不需要再装 `[extract]`。它是主入口而不是附加能力：
> 少了它，YouTube / 抖音 / 小红书 / 喜马拉雅 / TED 全都用不了。

### 3. 跑起来

```bash
clip2text serve
# 打开 http://127.0.0.1:8000
```

首次启动可以看一眼本机能力：

```bash
clip2text capabilities
```

---

## 国内网络：模型下载会失败

首次运行会自动下载 whisper 模型（`small` 约 500MB，`large-v3-turbo` 约 1.6GB），
`huggingface.co` 在国内经常 SSL 中断。**必须先配镜像**：

```bash
export HF_ENDPOINT=https://hf-mirror.com
clip2text serve
```

或者写进 `.env`：

```ini
HF_ENDPOINT=https://hf-mirror.com
```

> 项目会自动处理一件事：模型下载完整后进入离线模式。
> 不这么做的话，`huggingface_hub` 每次转写都会先联网校验 revision，
> 国内网络下白等约 80 秒（实测 84.3s → 3.3s）。

模型对照（越大越准，越慢）：

| 模型 | 体积 | 适合 |
|---|---|---|
| `tiny` | 75MB | 只求大概内容 |
| `base` | 145MB | 草稿 |
| `small` | 500MB | 速度和准确率平衡 |
| `large-v3-turbo` | 1.6GB | **默认**，准确率最好 |

改模型：`.env` 里设 `ASR_MODEL=small`。

---

## 配置

全部可留空。复制 `.env.example` 为 `.env` 按需修改。

```ini
# 数据目录
DATA_DIR=./.data

# ASR：auto 会按本机环境自动挑
ASR_ENGINE=auto          # auto | mlx | faster-whisper | deepseek
ASR_MODEL=large-v3-turbo
ASR_LANGUAGE=            # 留空自动检测，中文建议填 zh

# OCR
OCR_ENGINE=auto          # auto | rapidocr | deepseek

# 上传、并发、分片
MAX_UPLOAD_MB=300
MAX_CONCURRENCY=2
MAX_BATCH_SIZE=100
CHUNK_MAX_SEC=110

# 长内容护栏（0 = 不限制）
MAX_MEDIA_DURATION_MIN=180
MAX_MEDIA_SIZE_MB=2048

# 分片级重试（2 小时播客切成 70 段，不能因一段失败全废）
CHUNK_RETRY_MAX=2
CHUNK_RETRY_BACKOFF_SEC=1.0

# AI 改写（OpenAI 兼容，可指向 Ollama）
LLM_BASE_URL=
LLM_API_KEY=
LLM_MODEL=

# 云服务（可选的 ASR / OCR 备用路线）
DEEPSEEK_API_KEY=

# 链接解析（默认开启；设为 false 可整体关闭）
EXTRACTORS_ENABLED=true
EXTRACTORS_ALLOW=
PROXY_URL=
COOKIE_FILE=

# 断点续跑（每段结果落盘，重启后跳过已完成分片）
RESUME_ENABLED=true
```

**ffmpeg 不用自己装。** `FFMPEG_MODE=auto` 会优先用系统 ffmpeg，
找不到就通过 `static-ffmpeg` 自动下载。

### 用 Ollama 做本地改写（完全离线）

```bash
ollama serve
ollama pull qwen2.5:7b
```

```ini
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen2.5:7b
```

---

## 命令行

```bash
# 检查本机能力
clip2text capabilities

# 转写单个文件
clip2text transcribe ./lecture.mp4
clip2text transcribe ./lecture.mp4 --format markdown -o lecture.md
```

---

## HTTP API

启动后访问 `http://127.0.0.1:8000/docs` 查看交互式文档。

### 单任务

| Method | Path | 说明 |
|---|---|---|
| `GET` | `/api/v1/capabilities` | 本机可用能力 |
| `GET` | `/api/v1/health` | 健康检查 |
| `POST` | `/api/v1/uploads` | 上传文件，返回 `path` |
| `POST` | `/api/v1/tasks` | 创建任务（upload 或 url） |
| `GET` | `/api/v1/tasks` | 列表 + 搜索 + 分页 |
| `GET` | `/api/v1/tasks/{id}` | 单任务状态与结果 |
| `GET` | `/api/v1/tasks/{id}/events` | SSE 进度流 |
| `POST` | `/api/v1/tasks/{id}/cancel` | 取消 |
| `DELETE` | `/api/v1/tasks/{id}` | 删除 |
| `POST` | `/api/v1/tasks/{id}/export` | 导出 `txt\|markdown\|docx\|xlsx` |

### 批量

| Method | Path | 说明 |
|---|---|---|
| `POST` | `/api/v1/batches` | 批量提交（自动去重、排队） |
| `GET` | `/api/v1/batches` | 批量列表 |
| `GET` | `/api/v1/batches/{id}` | 详情（含所有子任务） |
| `GET` | `/api/v1/batches/{id}/events` | SSE 进度流 |
| `POST` | `/api/v1/batches/{id}/cancel` | 取消 |
| `DELETE` | `/api/v1/batches/{id}` | 删除（级联子任务） |

### 改写

| Method | Path | 说明 |
|---|---|---|
| `GET` | `/api/v1/rewrite/styles` | 可用风格与是否启用 |
| `POST` | `/api/v1/rewrite` | 流式改写（SSE） |

风格：`summary`（提炼要点）/ `xiaohongshu`（小红书）/ `douyin`（口播脚本）
/ `formal`（书面化）/ `polish`（润色订正）。

最小示例：

```bash
# 上传 + 转写
curl -F "file=@lecture.mp4" http://127.0.0.1:8000/api/v1/uploads
curl -X POST http://127.0.0.1:8000/api/v1/tasks \
  -H 'Content-Type: application/json' \
  -d '{"source_type":"upload","source_value":".data/uploads/<id>/lecture.mp4"}'

# 批量（每行一条链接）
curl -X POST http://127.0.0.1:8000/api/v1/batches \
  -H 'Content-Type: application/json' \
  -d '{"source_type":"url","text":"https://www.bilibili.com/video/BV1xx411c7mD\nhttps://v.douyin.com/xxx"}'

# 改写（流式）
curl -N -X POST http://127.0.0.1:8000/api/v1/rewrite \
  -H 'Content-Type: application/json' \
  -d '{"task_id":"<id>","style":"summary"}'
```

---

## 设计要点

### 为什么一定要分片

不是性能优化，是硬约束：DeepSeek 云 ASR 单次音频 ≤ 120 秒，
多数云 ASR 单段 60s–5min。所以 `CHUNK_MAX_SEC` 默认 110 秒。

切分时用 `ffmpeg silencedetect` 找静音点，**尽量在句间切**，
不会把一句话劈成两半。静音点不足时才按固定时长硬切。

### 为什么链接解析默认开启

因为这个工具的主场景就是链接：长视频播客、知识科普、读书、讲座，这类内容
几乎只以链接形式存在。把默认值设成关闭，等于把产品的主入口锁在门外——
用户装完发现"只能上传文件"，然后放弃。

合规兜底仍然保留：`EXTRACTORS_ENABLED=false` 可以整体关掉，
关掉后 `/tasks` 会直接拒绝 `source_type=url`。

**边界（有意为之，不接受"优化"）：**

- 只调用页面/接口自带的**公开数据**，不带 Cookie、不注入登录态
- 不破解签名、不猜测私有接口
- 付费 / 私密内容直接报错并说明原因，**不做任何绕过**
- 拿不到就如实报错，并把已知信息（标题、简介、付费状态）还给用户

> **仅用于处理你拥有版权的内容，或已获得授权的内容。**
> 请自行确认符合目标平台服务条款与《著作权法》。

仓库内不含任何平台的签名密钥或登录 Cookie。解析器设计成插件
（`entry_points` 组 `clip2text.extractors`），社区可以单独发包支持新平台：

```toml
# 你自己的插件包
[project.entry-points."clip2text.extractors"]
kuaishou = "clip2text_kuaishou:KuaishouExtractor"
```

实现 `hosts` / `matches()` / `extract()` 三个成员即可，参考
`src/clip2text/extractors/bilibili.py`（只调用公开 web 接口，无任何绕过）
或 `src/clip2text/extractors/xiaoyuzhou.py`（解析页面内嵌 JSON）。

### 为什么长内容要单独处理

一个 2 小时的播客切成约 70 段。原来的实现有三个真问题，现在都修了：

| 问题 | 现在怎么做 |
|---|---|
| 段级无重试，中间一段失败整任务作废 | 每段最多重试 2 次（1x/2x/4x 指数退避），全失败才失败 |
| 长任务下载/转码阶段没有进度 | 下载按字节、转码按输出体积、切分按已生成文件、转写按完成片数，四个阶段都有推进 |
| 注释说能断点续跑，实现却是全标失败 | **真续跑**：每段结果落盘 `checkpoint.json`，重新提交跳过已完成分片；`audio.wav` 还在的话连下载和抽音轨都跳过 |

还有两道提前护栏：`MAX_MEDIA_DURATION_MIN`（默认 180 分钟）和
`MAX_MEDIA_SIZE_MB`（默认 2048MB）。超限在**下载/转码之前**就拒绝——
跑 40 分钟后 OOM 对用户最不友好。两者都可以设为 0 关闭。

检查点带指纹（时长 + 分片长度 + 引擎名）。指纹变了就丢弃，不按下标复用：
换了分片长度还把旧结果按序号拼进去，时间轴会静默接错，比重新跑一遍严重得多。

### 为什么 B 站要多 P 处理

长讲座在 B 站常常是一个 BV 号挂几十个 P，或者一个合集里几十个视频。
只默默取第一 P 是最糟的失败方式：结果看起来成功，内容却缺了大半。

所以：

- 链接带 `?p=N` 时按 N 取对应分 P
- 多 P 时在结果里写出完整的「P1/P2/P3…」清单（标题 + 时长），并明确标注本次处理了哪个
- 标题也带上 `- P3 分P标题`，否则任务列表里一堆同名条目分不清

### 为什么引擎要可替换

ASR / OCR / LLM 都是 Protocol + 多实现，配置切换。
`auto` 的规则统一：**本地优先，云端兜底**。
装了本地引擎就不会悄悄走付费云服务。

### 为什么不用框架写前端

`src/clip2text/web` 是一个 `index.html` + 一个 `style.css` + 一个 `app.js`，
没有构建步骤，没有 `node_modules`。整个页面就几屏，引入 React
只会让 clone 下来多等 3 分钟。

放在包内而不是仓库根的 `apps/` 下，是因为 wheel 只打包 `src/clip2text`：
放外面的话 `pip install` 装修完就没有界面了（这个坑踩过）。

---

## 开发

```bash
pip install -e ".[dev]"
pytest -q
ruff check src/ tests/

# 开发模式热重载
clip2text serve --reload
```

代码结构：

```
src/clip2text/
├─ config.py        设置（全默认值，无需 .env）
├─ models.py        数据契约（Task / Batch / Segment）
├─ db.py            SQLite 存储（tasks + batches）
├─ pipeline.py      任务编排（5 阶段流水线）
├─ batch.py         批量调度（去重、排队、汇总）
├─ llm.py           AI 改写（OpenAI 兼容 + 流式）
├─ app.py           FastAPI 装配
├─ state.py         进程级单例
├─ api/             HTTP 路由（单任务 + 批量 + 改写）
├─ media/           ffmpeg 封装 + 静音感知分片
├─ asr/             ASR 引擎（mlx / faster-whisper / deepseek）+ HF 镜像
├─ ocr/             OCR 引擎（rapidocr / deepseek）
├─ extractors/      链接解析（播客 RSS / 小宇宙 / B 站 / yt-dlp / 插件）
├─ exporters/       txt / md / docx / xlsx
└─ web/             前端静态文件（html/css/js，无构建）
```

---

## 常见问题

**Q: 报错 "没有任何可用的 ASR 引擎"**
装一个：`pip install -e ".[mlx]"`（Mac）或 `pip install -e ".[faster]"`（其他）。

**Q: 模型下载卡住 / SSL 报错**
配镜像：`export HF_ENDPOINT=https://hf-mirror.com`。

**Q: 转写很慢**
- 首次运行包含模型加载，之后会快很多
- 换小模型：`.env` 设 `ASR_MODEL=small`
- Apple Silicon 用 mlx 引擎，比 CPU 版快数倍
- 确认已配 `HF_ENDPOINT`（否则每次会先等联网校验超时）

**Q: 准确率不高**
换大模型（`large-v3-turbo`），或指定语言 `ASR_LANGUAGE=zh`。

**Q: 链接解析用不了**
默认是开着的，先用 `GET /api/v1/capabilities` 看 `extractors.enabled` 和
`extractors.catalog` 里目标平台那一项的 `available` / `reason`。

常见原因：
- `EXTRACTORS_ENABLED=false` 被显式设过（关掉后 `/tasks` 会直接拒绝 url 来源）
- `EXTRACTORS_ALLOW` 里没包含目标平台
- yt-dlp 版本太旧，缺某个平台的 extractor（`pip install -U yt-dlp`）
- 平台需要代理：配 `PROXY_URL`

B 站 / 小宇宙 / 播客 RSS 是内置适配器，不依赖 yt-dlp，其他平台走 yt-dlp。

**Q: 长视频 / 长播客跑很久或者直接失败**
先看是不是撞了护栏：`MAX_MEDIA_DURATION_MIN`（默认 180 分钟）和
`MAX_MEDIA_SIZE_MB`（默认 2048MB）。这两项都在下载之前判断，超限会直接告诉你，
不会先跑几十分钟。真需要处理超长内容就把它们调大，或设为 0 关闭。

如果是中途某一段转写失败：每段会自动重试 2 次；重试用尽后任务失败，
但**已完成的分片已经保存**，修复后重新提交同一链接会跳过它们继续，不用从头再来。

**Q: B 站多 P / 合集为什么只有第一 P**
一条链接出一份文字是这个工具的定位。多 P 时结果里会列出完整的 P1/P2/P3 清单，
并标明本次处理了哪个。要处理其他分 P，在链接后加 `?p=N`，例如
`https://www.bilibili.com/video/BVxxx?p=3`。

**Q: 快手 / 视频号能解析吗**
yt-dlp 不支持这两个，需要自己写插件（或等社区提供）。这是有意的：
它们的接口签名几天一变，写进主仓库等于给自己挖维护坑。

**Q: 支持哪些格式**
音频：wav / mp3 / m4a / aac / flac / ogg / opus / wma / amr / aiff
视频：mp4 / mov / mkv / avi / webm / flv / wmv / m4v / ts / 3gp
图片：jpg / png / webp / bmp / gif / tiff

---

## License

Apache-2.0，见 [LICENSE](LICENSE)。
