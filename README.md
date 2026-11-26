# clip2text

![version](https://img.shields.io/badge/version-1.0.0-blue)
![python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
![license](https://img.shields.io/badge/license-Apache--2.0-green)

> 遇到感兴趣但难啃、没时间看完的中长视频，
> 贴个链接，几分钟后拿到带时间戳的文字、章节小结和要点，
> 快速判断哪些部分值得细看。

**本地优先、默认零 API key、不上传第三方。**
一个跑在自己电脑上的小工具，不需要注册，不需要充值，文件不出本机。

---

## 这是什么

一个「长内容 → 结构化文字」的提取工具。

你给它一个视频/音频链接（或一个本地文件），它帮你：

1. **抽音轨、语音转文字**，带上时间戳
2. **分章节**，每章一个标题和一小段摘要
3. **列出要点和金句**，可以点回原文对应位置
4. **导出**成 txt / markdown / Word / Excel

2 小时的播客，读不完的；但你可以花 3 分钟扫一遍章节，
找到「这一段在讲我关心的事」，然后**点一下时间戳，跳到那段**。

---

## 适合谁

| 场景 | 用法 |
|---|---|
| **追知识类播客的人** | 一集 2 小时，没空天天听。先看摘要和章节，挑感兴趣的那几段。 |
| **看长讲座的人** | B 站几十分钟的教程、上百页的合集，直接定位到讲「我要的那个知识点」那一节。 |
| **研究型学习者** | 把一批同主题视频转成文字，方便检索、引用、整理笔记。 |
| **内容创作者** | 看到好内容想借鉴，需要拿到原文、金句和结构，而不是重看一遍。 |

**它不是**用来替代看视频的。它是**让你决定哪些视频值得看**的工具。

---

## 怎么用

```bash
# 装
pip install -e ".[mlx]"        # Apple Silicon Mac
pip install -e ".[faster]"     # 其他平台

# 跑
clip2text serve
# 打开 http://127.0.0.1:8000
```

粘贴一条链接 → 点「开始提取」→ 等转写 → 点「解析」拿章节和要点。

```
粘贴链接 → 抽音轨 → 语音转写 → 导出 txt/md/docx/xlsx
图片 / 图文 → OCR → 同上
```

---

## 一个真实的例子

一条 127 分钟的小宇宙播客，转写出来 **4886 段、36985 字**，约 25 分钟处理完。

打开「解析」，拿到这样的结构：

```
摘要：这是假服务生成的全文摘要，用于验证 reduce 阶段与前端渲染。共 11 个窗口参与合并。

章节
  00:00  章节 1
  11:59  章节 2
  23:59  章节 3
  ...

要点：全局要点 1 / 全局要点 2 / 全局要点 3
金句：「这是第 0 窗里的一句原话。」  02:59
```

点一下 `1:11:55`，界面直接跳到那一句话。
**你不需要读 3 万字，就能找到那 200 字值得看的地方。**

---

## 支持的平台

| 平台 | 实现 | 说明 |
|---|---|---|
| **B 站** | 内置 | 公开 web 接口；**多 P / 合集支持 `?p=N`** |
| **小宇宙** | 内置 | 中文播客主场，读页面内嵌 JSON 的公开数据 |
| **播客 RSS / Atom** | 内置 | 通用 feed，含小宇宙官方导出格式 |
| **YouTube** | yt-dlp | 国际知识类长视频 |
| **喜马拉雅 / 网易云播客** | yt-dlp | 国内长音频 |
| **抖音 / 小红书 / 微博 / TikTok** | yt-dlp | 短视频，顺手支持 |
| TED / Vimeo / Twitch 等 | yt-dlp | 共覆盖 1700+ 站点 |

**不支持**：快手、微信视频号（它们的接口签名几天一变，写进主仓库等于给自己挖维护坑）。需要的话可以自己写插件。

解析顺序：内置适配器优先（能拿到更完整的中文元信息），yt-dlp 兜底。

---

## 详细安装

### 环境

需要 **Python 3.11 或 3.12**（不要用 3.13，OCR 依赖还不支持）。

推荐用 [uv](https://docs.astral.sh/uv/)：

```bash
uv venv --python 3.12
source .venv/bin/activate
```

### 安装

```bash
pip install -e .
```

**再装需要的可选能力**（都不需要 API key）：

```bash
pip install -e ".[mlx]"       # Apple Silicon Mac 的 ASR，速度最快
pip install -e ".[faster]"    # Windows / Linux / Intel Mac 的 ASR
pip install -e ".[ocr]"       # 图片文字识别
```

一步到位：

```bash
pip install -e ".[mlx,ocr]"
```

> 链接解析（yt-dlp）已经是核心依赖，`pip install -e .` 就带上了。

### 启动

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

> 模型下载完整后会自动进入离线模式。
> 不这么做的话，`huggingface_hub` 每次转写都会先联网校验 revision，
> 国内网络下白等约 80 秒（实测 84.3s → 3.3s）。

模型对照：

| 模型 | 体积 | 适合 |
|---|---|---|
| `tiny` | 75MB | 只求大概内容 |
| `base` | 145MB | 草稿 |
| `small` | 500MB | 速度和准确率平衡 |
| `large-v3-turbo` | 1.6GB | **默认**，准确率最好 |

改模型：`.env` 里设 `ASR_MODEL=small`。

---

## 配置（进阶）

默认配置即可用，不需要改文件。下面是完整配置项，供需要的人参考。

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

# 分片级重试
CHUNK_RETRY_MAX=2
CHUNK_RETRY_BACKOFF_SEC=1.0

# AI 改写 / 内容解析（OpenAI 兼容，可指向 Ollama）
LLM_BASE_URL=
LLM_API_KEY=
LLM_MODEL=

# 云服务（可选的 ASR / OCR / 分析 备用路线，一个 key 覆盖）
DEEPSEEK_API_KEY=

# 链接解析（默认开启；设为 false 可整体关闭）
EXTRACTORS_ENABLED=true
EXTRACTORS_ALLOW=
PROXY_URL=
COOKIE_FILE=

# 断点续跑
RESUME_ENABLED=true
```

**ffmpeg 不用自己装。** `FFMPEG_MODE=auto` 会优先用系统 ffmpeg，找不到就通过 `static-ffmpeg` 自动下载。

### 用 Ollama 做本地分析（完全离线）

```bash
ollama serve
ollama pull qwen2.5:7b
```

```ini
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen2.5:7b
```

---

## 常见问题

**Q: 改完代码/更新后，界面没变化？**
先重启服务。`clip2text serve` 启动时把代码载入内存，改了文件不会自动生效（除非用 `--reload`）。

**Q: 找不到「解析」（内容总结）入口？**
解析 tab 始终显示。如果提示「内容解析尚未启用」，说明没配大模型：

```ini
# 二选一
LLM_BASE_URL=http://localhost:11434/v1   # 任意 OpenAI 兼容服务，可指向 Ollama
LLM_MODEL=qwen2.5:7b

# 或者只配这个（与语音识别共用同一个 key）
DEEPSEEK_API_KEY=sk-xxx
```

配完重启服务即可。

**Q: 报错 "没有任何可用的 ASR 引擎"**
装一个：`pip install -e ".[mlx]"`（Mac）或 `pip install -e ".[faster]"`（其他）。

**Q: 模型下载卡住 / SSL 报错**
配镜像：`export HF_ENDPOINT=https://hf-mirror.com`。

**Q: 转写很慢**
- 首次运行包含模型加载，之后会快很多
- 换小模型：`.env` 设 `ASR_MODEL=small`
- Apple Silicon 用 mlx 引擎，比 CPU 版快数倍
- 确认已配 `HF_ENDPOINT`

**Q: 准确率不高**
换大模型（`large-v3-turbo`），或指定语言 `ASR_LANGUAGE=zh`。

**Q: B 站多 P / 合集为什么只有第一 P**
一条链接出一份文字是这个工具的定位。多 P 时结果里会列出完整的 P1/P2/P3 清单，并标明本次处理了哪个。要处理其他分 P，在链接后加 `?p=N`，例如 `https://www.bilibili.com/video/BVxxx?p=3`。

**Q: 长视频 / 长播客跑很久或者直接失败**
先看是不是撞了护栏：`MAX_MEDIA_DURATION_MIN`（默认 180 分钟）和 `MAX_MEDIA_SIZE_MB`（默认 2048MB）。这两项都在下载之前判断。需要处理超长内容就把它们调大，或设为 0 关闭。

中途某一段失败会自动重试 2 次；已完成的分片已保存，重新提交同一链接会跳过它们继续。

**Q: 支持哪些格式**
音频：wav / mp3 / m4a / aac / flac / ogg / opus / wma / amr / aiff
视频：mp4 / mov / mkv / avi / webm / flv / wmv / m4v / ts / 3gp
图片：jpg / png / webp / bmp / gif / tiff

---

## 设计与取舍

### 为什么一定要分片

不是性能优化，是硬约束：DeepSeek 云 ASR 单次音频 ≤ 120 秒。切分时用 `ffmpeg silencedetect` 找静音点，**尽量在句间切**，不会把一句话劈成两半。

### 为什么链接解析默认开启

主场景就是链接：长视频播客、知识科普、读书、讲座，这类内容几乎只以链接形式存在。把默认值设成关闭，等于把主入口锁在门外。

合规兜底：`EXTRACTORS_ENABLED=false` 可以整体关掉。

**边界（有意为之）：**
- 只调用页面/接口自带的**公开数据**，不带 Cookie、不注入登录态
- 不破解签名、不猜测私有接口
- 付费 / 私密内容直接报错并说明原因，**不做任何绕过**
- 拿不到就如实报错，并把已知信息还给用户

> **仅用于处理你拥有版权的内容，或已获得授权的内容。**
> 请自行确认符合目标平台服务条款与《著作权法》。

仓库内不含任何平台的签名密钥或登录 Cookie。解析器设计成插件（`entry_points` 组 `clip2text.extractors`），社区可以单独发包支持新平台：

```toml
[project.entry-points."clip2text.extractors"]
kuaishou = "clip2text_kuaishou:KuaishouExtractor"
```

实现 `hosts` / `matches()` / `extract()` 即可，参考 `src/clip2text/extractors/bilibili.py`。

### 为什么长内容要单独处理

一个 2 小时的播客切成约 70 段。原来的实现有三个真问题：

| 问题 | 现在怎么做 |
|---|---|
| 段级无重试，中间一段失败整任务作废 | 每段最多重试 2 次（指数退避），全失败才失败 |
| 长任务没有进度 | 下载按字节、转码按输出体积、切分按已生成文件、转写按完成片数 |
| 断点续跑名不副实 | **真续跑**：每段结果落盘，重新提交跳过已完成分片 |

护栏在**下载/转码之前**就拒绝超限内容，不会跑 40 分钟后 OOM。

### 为什么 B 站要多 P 处理

长讲座在 B 站常常是一个 BV 号挂几十个 P。只默默取第一 P 是最糟的失败方式：结果看起来成功，内容却缺了大半。所以：

- `?p=N` 时按 N 取对应分 P
- 多 P 时写出完整清单，明确标注本次处理了哪个

### 为什么引擎要可替换

ASR / OCR / LLM 都是 Protocol + 多实现，配置切换。`auto` 的规则统一：**本地优先，云端兜底**——装了本地引擎就不会悄悄走付费云服务。

### 为什么不用框架写前端

`src/clip2text/web` 是 `index.html` + `style.css` + `app.js`，没有构建步骤，没有 `node_modules`。整个页面就几屏，引入 React 只会让 clone 下来多等 3 分钟。

放在包内而不是仓库根的 `apps/` 下，是因为 wheel 只打包 `src/clip2text`。

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

## 更新日志

各版本变更见 [CHANGELOG.md](CHANGELOG.md)。

---

## License

Apache-2.0，见 [LICENSE](LICENSE)。