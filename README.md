# clip2text

把视频、音频里的内容，提取成可编辑的文字。**本地优先、默认零 API key、不上传第三方。**

一个可以完全跑在自己电脑上的"文案提取"工具。不需要注册、不需要充值，
文件不会离开你的机器。

```
选择文件 → 自动抽音轨 → 语音转写 → 导出 txt / md / docx / xlsx
```

---

## 现在能做什么

| 能力 | 状态 | 需要额外安装 |
|---|---|---|
| 上传音视频 → 转写 → 导出 | ✅ 可用 | 一个本地 ASR 引擎 |
| 带时间戳的分句 | ✅ 可用 | 需 mlx / faster-whisper |
| 静音感知的音频分片 | ✅ 可用 | — |
| 4 种导出格式 | ✅ 可用 | — |
| SSE 进度流 | ✅ 可用 | — |
| 能力自检 `/capabilities` | ✅ 可用 | — |
| 图片 OCR | ⬜ P3 | — |
| AI 文案改写 | ⬜ P3 | — |
| 平台链接解析 | ⬜ P1（默认关闭） | yt-dlp |

架构与设计取舍见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

---

## 快速开始

### 1. 环境

需要 **Python 3.11 或 3.12**（不要用 3.13，OCR 依赖还不支持）。

```bash
python3.12 --version   # 确认版本
```

推荐用 [uv](https://docs.astral.sh/uv/) 管理：

```bash
uv venv --python 3.12
source .venv/bin/activate
```

### 2. 安装

```bash
pip install -e .
```

**再装一个语音识别引擎**（二选一，都不需要 API key）：

```bash
# Apple Silicon Mac，速度最快
pip install -e ".[mlx]"

# 或：Windows / Linux / Intel Mac 通用
pip install -e ".[faster]"
```

### 3. 跑起来

```bash
clip2text serve
# 打开 http://127.0.0.1:8000
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

模型大小对照（越大越准，越慢）：

| 模型 | 体积 | 5 分钟中文音频 | 适合 |
|---|---|---|---|
| `tiny` | 75MB | 很快 | 只求大概内容 |
| `base` | 145MB | 快 | 草稿 |
| `small` | 500MB | 中等 | **默认推荐**，速度和准确率平衡 |
| `medium` | 1.5GB | 慢 | — |
| `large-v3-turbo` | 1.6GB | 中 | **准确率最好**，Apple Silicon 上很快 |

改模型：`.env` 里设 `ASR_MODEL=small`。

---

## 配置

全部可留空。复制 `.env.example` 为 `.env` 按需修改。

```ini
# 数据目录（数据库、上传文件、导出产物）
DATA_DIR=./.data

# ASR：auto 会按本机环境自动挑
ASR_ENGINE=auto          # auto | mlx | faster-whisper | funasr | deepseek
ASR_MODEL=large-v3-turbo
ASR_LANGUAGE=            # 留空自动检测，中文建议填 zh

# 单文件上限、并发、分片时长
MAX_UPLOAD_MB=300
MAX_CONCURRENCY=2
CHUNK_MAX_SEC=110

# 可选：云 ASR（不占本地算力，约 ¥0.78/小时音频）
DEEPSEEK_API_KEY=

# 可选：链接解析（默认关闭，见下方合规说明）
EXTRACTORS_ENABLED=false
```

**ffmpeg 不用自己装。** `FFMPEG_MODE=auto` 会优先用系统 ffmpeg，
找不到就通过 `static-ffmpeg` 自动下载。

---

## 命令行

不想开网页的话：

```bash
# 检查本机能力
clip2text capabilities

# 直接转写单个文件
clip2text transcribe ./lecture.mp4
clip2text transcribe ./lecture.mp4 --format markdown -o lecture.md
```

---

## HTTP API

启动后访问 `http://127.0.0.1:8000/docs` 查看交互式文档。

| Method | Path | 说明 |
|---|---|---|
| `GET` | `/api/v1/capabilities` | 本机可用能力（前端据此禁用按钮） |
| `GET` | `/api/v1/health` | 健康检查 |
| `POST` | `/api/v1/uploads` | 上传文件，返回 `path` |
| `POST` | `/api/v1/tasks` | 创建任务 |
| `GET` | `/api/v1/tasks` | 列表 + 搜索 + 分页 |
| `GET` | `/api/v1/tasks/{id}` | 单任务状态与结果 |
| `GET` | `/api/v1/tasks/{id}/events` | SSE 进度流 |
| `POST` | `/api/v1/tasks/{id}/cancel` | 取消 |
| `DELETE` | `/api/v1/tasks/{id}` | 删除 |
| `POST` | `/api/v1/tasks/{id}/export` | 导出 `txt\|markdown\|docx\|xlsx` |

最小示例：

```bash
# 上传
curl -F "file=@lecture.mp4" http://127.0.0.1:8000/api/v1/uploads
# → {"path": ".data/uploads/<id>/lecture.mp4", ...}

# 创建任务
curl -X POST http://127.0.0.1:8000/api/v1/tasks \
  -H 'Content-Type: application/json' \
  -d '{"source_type":"upload","source_value":".data/uploads/<id>/lecture.mp4"}'
# → {"id": "...", "status": "QUEUED", ...}

# 查询
curl http://127.0.0.1:8000/api/v1/tasks/<id>

# 导出
curl -X POST http://127.0.0.1:8000/api/v1/tasks/<id>/export \
  -H 'Content-Type: application/json' -d '{"format":"docx"}' -o out.docx
```

---

## 设计要点

### 为什么一定要分片

不是性能优化，是硬约束：

- DeepSeek 云 ASR 单次音频 **≤ 120 秒**
- 多数云 ASR 单段 60s–5min
- 长音频一次性处理会失去进度显示和断点续跑能力

所以 `CHUNK_MAX_SEC` 默认 110 秒。切分时用 `ffmpeg silencedetect`
找静音点，**尽量在句间切**，不会把一句话劈成两半。

### 为什么默认关闭链接解析

`EXTRACTORS_ENABLED=false` 是有意的。绝大多数人只需要处理自己的文件，
而抓取他人平台内容涉及 ToS 与著作权风险。要开就显式开，并且：

> **仅用于处理你拥有版权的内容，或已获得授权的内容。**
> 请自行确认符合目标平台服务条款与《著作权法》。

仓库内不含任何平台的签名密钥或登录 Cookie。解析器设计成插件
（`entry_points` 组 `clip2text.extractors`），社区可以单独发包支持新平台。

### 为什么不用框架写前端

`apps/web` 是一个 `index.html` + 一个 `style.css` + 一个 `app.js`，
没有构建步骤，没有 `node_modules`。整个页面就三屏，引入 React
只会让 clone 下来多等 3 分钟。

---

## 开发

```bash
pip install -e ".[dev]"
pytest -q

# 开发模式热重载
clip2text serve --reload
```

代码结构：

```
src/clip2text/
├─ config.py        设置（全默认值，无需 .env）
├─ models.py        数据契约（Task / Segment / Transcript）
├─ db.py            SQLite 存储
├─ pipeline.py      任务编排（stage 流水线）
├─ app.py           FastAPI 装配
├─ api/routes.py    HTTP 路由
├─ media/           ffmpeg 封装 + 静音感知分片
├─ asr/             ASR 引擎（mlx / faster-whisper / deepseek）+ HF 镜像支持
├─ ocr/             图片文字（P3）
├─ extractors/      链接解析（默认关闭）
└─ exporters/       txt / md / docx / xlsx
```

---

## 常见问题

**Q: 报错 "没有任何可用的 ASR 引擎"**
装一个：`pip install -e ".[mlx]"`（Mac）或 `pip install -e ".[faster]"`（其他）。

**Q: 模型下载卡住 / SSL 报错**
配镜像：`export HF_ENDPOINT=https://hf-mirror.com`。

**Q: 转写很慢**
- 首次运行包含模型加载（1-2 分钟），之后会快很多
- 换小模型：`.env` 里设 `ASR_MODEL=small`
- Apple Silicon 用 mlx 引擎，比 CPU 版快数倍

**Q: 准确率不高**
换大模型（`large-v3-turbo`），或者 `.env` 里指定语言 `ASR_LANGUAGE=zh`。

**Q: 支持哪些格式**
音频：wav / mp3 / m4a / aac / flac / ogg / opus / wma / amr / aiff
视频：mp4 / mov / mkv / avi / webm / flv / wmv / m4v / ts / 3gp
图片：jpg / png / webp / bmp / gif / tiff（OCR 待实现）

---

## License

Apache-2.0
