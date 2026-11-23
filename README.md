# clip2text

![version](https://img.shields.io/badge/version-0.1.0-blue)
![python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
![license](https://img.shields.io/badge/license-Apache--2.0-green)

> **0.1.0 是早期版本。** 核心链路（上传 → 转写 → 导出）已稳定，
> 但 HTTP API 与配置项在 1.0 之前可能调整。生产使用请锁定版本。

把视频、音频、图片里的内容，提取成可编辑的文字。**本地优先、默认零 API key、不上传第三方。**

一个可以完全跑在自己电脑上的"文案提取"工具。不需要注册、不需要充值，
文件不会离开你的机器。

```
选择文件 / 粘贴链接 → 抽音轨 → 语音转写 → 导出 txt / md / docx / xlsx
图片 / 图文 → OCR → 同上
```

---

## 现在能做什么

| 能力 | 状态 | 需要额外安装 |
|---|---|---|
| 上传音视频 → 转写 → 导出 | ✅ | 一个本地 ASR 引擎 |
| 图片 / 图文 OCR → 导出 | ✅ | `clip2text[ocr]` |
| 带时间戳的分句 | ✅ | 需 mlx / faster-whisper |
| 静音感知的音频分片 | ✅ | — |
| 4 种导出格式 | ✅ | — |
| 批量提取（含自动去重、排队） | ✅ | — |
| 链接解析（B 站 + yt-dlp 覆盖的平台） | ✅ 默认关闭 | `clip2text[extract]` |
| AI 文案改写（5 种风格，流式） | ✅ 可选 | 任意 OpenAI 兼容服务 |
| SSE 进度流 | ✅ | — |
| 能力自检 `/capabilities` | ✅ | — |

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
pip install -e ".[extract]"   # 链接解析（yt-dlp）
```

一步到位：

```bash
pip install -e ".[mlx,ocr,extract]"
```

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

# AI 改写（OpenAI 兼容，可指向 Ollama）
LLM_BASE_URL=
LLM_API_KEY=
LLM_MODEL=

# 云服务（可选的 ASR / OCR 备用路线）
DEEPSEEK_API_KEY=

# 链接解析（默认关闭，见下方合规说明）
EXTRACTORS_ENABLED=false
EXTRACTORS_ALLOW=
PROXY_URL=
COOKIE_FILE=
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

### 为什么默认关闭链接解析

`EXTRACTORS_ENABLED=false` 是有意的。绝大多数人只需要处理自己的文件，
而抓取他人平台内容涉及 ToS 与著作权风险。

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
`src/clip2text/extractors/bilibili.py`（只调用公开 web 接口，无任何绕过）。

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
├─ extractors/      链接解析（bilibili / yt-dlp / 插件）
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
先 `pip install -e ".[extract]"`，再在 `.env` 设 `EXTRACTORS_ENABLED=true`。
B 站是内置适配器（不依赖 yt-dlp），其他平台走 yt-dlp。

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
