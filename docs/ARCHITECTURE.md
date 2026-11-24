# clip2text 架构梳理 v0.2

> 这份文档回答三个问题：**现在有什么、怎么连起来的、还缺什么。**
> 如果你要评估「还需补什么」，直接跳到 [§7 缺口分析](#7-缺口分析)。

---

## 1. 一句话概括

一个本地优先的「媒体 → 文字」工具：输入可以是平台链接（主入口）或上传的
音视频/图片；输出是带时间戳的文本，可导出 4 种格式。

**核心约束：不填任何 API key 也能完整跑通主链路。**

**定位修正（v0.2）**：v0.1 把「上传本地文件」当成了主路径，但产品真正要解决的是
**解析视频/音频链接**——长视频播客、知识科普、读书、讲座。这类内容几乎只以链接
形式存在。v0.2 把默认值和文档措辞都反过来：链接优先，上传是并行能力。

---

## 2. 分层图

```
┌────────────────────────────────────────────────────────────┐
│  Web UI  src/clip2text/web/                                │
│  index.html + style.css + app.js（无构建、无框架）           │
│  单文件 / 批量 / 结果 / 改写 / 历史 五块                      │
└───────────────────────┬────────────────────────────────────┘
                        │  REST + SSE
┌───────────────────────▼────────────────────────────────────┐
│  API  api/routes.py · api/batch_routes.py                  │
│  15 个端点：任务 / 上传 / 批量 / 导出 / 改写 / capabilities    │
└───────────────────────┬────────────────────────────────────┘
                        │
┌───────────────────────▼────────────────────────────────────┐
│  state.py   进程级单例（settings + db + pipeline + batches）  │
└───────────────────────┬────────────────────────────────────┘
        ┌───────────────┼───────────────┐
        │               │               │
┌───────▼──────┐ ┌──────▼──────┐ ┌──────▼──────┐
│  pipeline.py │ │  batch.py   │ │   llm.py    │
│  单任务编排   │ │  批量调度    │ │  流式改写    │
│  5 阶段状态机 │ │  去重/排队   │ │             │
└───────┬──────┘ └──────┬──────┘ └─────────────┘
        │               │
┌───────▼───────────────▼────────────────────────────────────┐
│  能力层（全部可插拔，Protocol + 多实现）                       │
│  ├─ extractors/  链接解析  RSS · 小宇宙 · B站 · yt-dlp · 插件 │
│  ├─ media/       ffmpeg 封装 · 静音感知分片                   │
│  ├─ asr/         mlx · faster-whisper · deepseek             │
│  ├─ ocr/         rapidocr · deepseek                         │
│  └─ exporters/   txt · markdown · docx · xlsx                │
└───────┬─────────────────────────────────────────────────────┘
        │
┌───────▼─────────────────────────────────────────────────────┐
│  db.py   SQLite（tasks + batches），WAL 模式                  │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. 数据流：三种输入殊途同归

### 路径 A：上传音视频
```
POST /uploads ──► .data/uploads/<id>/
POST /tasks   ──► 建任务（QUEUED）
                  │
                  ▼
              extract_audio  (ffmpeg → 16k mono wav)
                  │
                  ▼
              split_audio    (silencedetect 找切点，≤110s/段)
                  │
                  ▼
              engine.transcribe × N 段（时间戳平移为绝对时间，带重试与检查点）
                  │
                  ▼
              SUCCESS: text_content + segments[]
```

### 路径 B：上传图片 / 图文作品
```
POST /tasks (work_type=image)
    │
    ▼
image_urls 非空？ ──是──► 下载所有图到 work_dir/images/
    │否
    ▼
src 本身就是图片
    │
    ▼
recognize_images (逐张 OCR，单张失败不中断)
    │
    ▼
SUCCESS: text_content
```

### 路径 C：平台链接（默认开启，主入口）
```
POST /tasks (source_type=url)
    │
    ▼
EXTRACTORS_ENABLED? ──否──► 400 错误，提示怎么恢复（合规兜底）
    │是（默认）
    ▼
pick_url         (从分享文案里抽链接)
    │
    ▼
插件 → PodcastFeed → Xiaoyuzhou → Bilibili → YtDlp  (逐个试)
    │
    ▼
时长/体积护栏      (超限在下载前就拒绝，MAX_MEDIA_DURATION_MIN / SIZE_MB)
    │
    ▼
下载媒体到 work_dir/source.*
    │
    ▼
回到路径 A 的音频分支（带检查点）
```

### 路径 D：长内容（2~3 小时播客）的分片生命周期
```
split_audio  →  N 个分片（静音点切分，≤110s/段）
    │
    ▼
重跑时的快路（省掉长内容最耗时的部分）：
    检查点存在 + 指纹匹配 + audio.wav 还在  ──► 跳过下载与抽音轨，直接切分+转写
        │                                    （实测：ASR 只跑未完成的段，extract_audio 调用 0 次）
        └─ 任一条不满足 ──► 走完整流程

for chunk in chunks:
    ├─ checkpoint 命中？ ──是──► 直接复用，不喂给 ASR
    │否
    ▼
    engine.transcribe   失败 → 指数退避重试（默认 2 次）
    │                         └─ 仍失败 → 整任务失败，但已完成分片已落盘
    ▼
    checkpoint.record()  拿结果后立刻写盘（原子替换）
    │
    ▼
packaging → 按 index 顺序拼接 → 清掉检查点
```

为什么按 index 而不是按完成顺序拼接：续跑时已完成片段可能来自上一次运行，
按下标排能保证文字顺序与音轨一致。

---

## 4. 状态机

### 任务

```
QUEUED ──► RUNNING ──► SUCCESS
  │          │
  │          ├──► FAILED
  │          └──► CANCELED
  └──────────┘
```

阶段（`stage`）依次推进，每步都落库，崩溃后可识别中断点：

```
queued → fetching → transcoding → transcribing → packaging → done
                 ↘ ocr（图片走这里）
```

### 批量

```
PENDING ──► RUNNING ──► DONE      （全成功）
                   ├──► PARTIAL  （部分成功）
                   ├──► FAILED   （全失败）
                   └──► CANCELED
```

**批量的计数不自己维护，每次从子任务重算。**
这条是踩坑后的决定：之前单独维护计数器，出现「状态 DONE 但计数 0/2」，
进度条永远 0%。

---

## 5. 关键设计决策

| 决策 | 理由 | 代价 |
|---|---|---|
| 音频必须分片 | 云 ASR 有 120s 硬限制；分片还能显示进度、支持断点 | 多一次 ffmpeg 调用 |
| 静音点切分 | 避免把一句话劈成两半 | 分片前要跑一次 silencedetect |
| 引擎 Protocol 化 | 换引擎不改业务代码 | 多一层抽象 |
| auto = 本地优先 | 装了本地引擎就不该悄悄走付费云 | — |
| 链接解析默认开（v0.2 修正） | 主场景就是链接，默认关等于锁住主入口 | 需显式关闭做合规兜底 |
| 内置适配器排在 yt-dlp 前 | 中文元信息（shownotes/分 P 清单）yt-dlp 给不了 | 每个平台要单独维护 |
| RSS 也做成适配器 | feed 的 enclosure 是官方公开给订阅端的直链，比抓页面稳 | 只覆盖有 feed 的播客 |
| 检查点带指纹 | 换引擎/分片长度后按下标复用会把时间轴静默接错 | 指纹变了要整段重跑 |
| 检查点同时用作「前置产物已完成」的证据 | 长内容重新下载 244MB + 重跑 ffmpeg 是最浪费的部分 | 只信任检查点存在 + wav 仍在 |
| 续跑不自动触发 | 重启偷偷重跑会重复烧几小时算力，用户还不知道 | 用户需重新提交同一链接 |
| 多 P 只处理第一 P 但显式提示 | 「结果成功但内容缺一半」是最糟的失败方式 | 用户要手动 `?p=N` |
| 长内容超限提前拒绝 | 跑 40 分钟后 OOM 比一进门就说清楚更伤 | 超长内容默认不支持 |
| 解析器插件化 | 平台失效是常态，别让主仓库背锅 | 用户要额外装包 |
| 前端不用框架 | 三屏页面，引入 React 只会让 clone 变重 | 手写 DOM 更新 |
| SQLite 而非 PG | 单用户本地场景，零依赖比扩展性重要 | 多用户要换 |
| 前端放包内 | wheel 只打包 src/，放外面 pip 装完没界面 | 目录结构略怪 |

---

## 6. 失败处理策略

**分层降级，不让单点故障毁掉整次请求：**

| 层 | 策略 |
|---|---|
| 解析器 | 插件 → RSS → 小宇宙 → B 站 → yt-dlp 逐个试，全失败才报错，错误汇总前 3 条 |
| OCR | 单张图失败只标注 `[第 N 张图识别失败]`，继续处理余下 |
| 批量 | `gather(return_exceptions=True)`，单条失败不影响其它 |
| ASR | **分片级重试 2 次（指数退避）**，仍失败则任务失败；已完成分片落盘可续跑 |
| 长内容 | 时长/体积超限在下载前拒绝；下载循环里再按字节兜底一次 |
| ffmpeg | 三级定位：系统 → static-ffmpeg → 报错并给安装指引 |
| HF 模型 | 缓存残缺 → 给出三条可操作修复命令（镜像/换小模型/手动下载） |

**错误消息的写法约定：不写「失败」，写「怎么办」。**
例如不写 `yt-dlp 不支持`，而是
`yt-dlp 暂不支持这个链接。若是快手/视频号，需要装对应的插件包`。

---

## 7. 缺口分析

按「影响发布」排序。

### 🔴 阻塞发布

| # | 缺口 | 影响 | 建议 |
|---|---|---|---|
| 1 | **没有 CI 实际运行记录** | 配置文件写了但没跑过，可能一推就红 | 推上去看 Actions 结果，修掉即止 |
| 2 | **无 `NOTICE` / 无版权人署名** | Apache-2.0 的 `LICENSE` 里 `[yyyy] [name]` 还是占位符 | 填真实年份与署名 |
| 3 | **Windows 未实测** | 只用 `pathlib`，理论上没问题，但没跑过 | CI 里已有 windows smoke，看结果 |

### 🟡 影响体验，建议 0.2 补

| # | 缺口 | 说明 |
|---|---|---|
| 4 | ~~**分片级重试**~~ | ✅ **v0.2 已修**：每段最多重试 2 次，指数退避 1x/2x/4x。见 `pipeline._transcribe_chunk` |
| 5 | ~~**断点续跑未真正实现**~~ | ✅ **v0.2 已修**：真续跑。每段结果原子落盘 `work/<id>/checkpoint.json`，重新提交跳过已完成分片；检查点带指纹防错配。`recover_orphans` 的措辞已改准确 |
| 6 | **无进度持久化到前端** | SSE 端点存在，但前端用的是 3 秒轮询。SSE 没被用上 |
| 7 | **文件清理策略缺失** | `.data/uploads/` 和 `.data/work/` 只增不减，跑久了会占满盘 |
| 4b | ~~**长任务进度不真实**~~ | ✅ **v0.2 已修**：下载按字节、转码按输出体积、切分按已生成文件数、转写按完成片数，四个阶段都有可感知推进 |
| 4c | ~~**长内容无护栏**~~ | ✅ **v0.2 已加**：`MAX_MEDIA_DURATION_MIN`(180) / `MAX_MEDIA_SIZE_MB`(2048)，下载前拒绝，可设 0 关闭 |
| 4d | ~~**B 站只取 pages[0]**~~ | ✅ **v0.2 已修**：支持 `?p=N`，多 P 时返回完整分 P 清单并在结果里显式提示 |
| 8 | **导出没有「全部」格式** | 单任务导 4 种，批量只能导 txt/md |
| 9 | **没有字幕格式** | `srt` / `vtt` 是最常见需求，`segments` 已在手，加导出器只需 30 行 |
| 10 | **时间戳不精确** | 分片间用 `chunk.start` 平移，但 ffmpeg 切片可能有毫秒级偏移累积 |

### 🟢 生态与长期

| # | 缺口 | 说明 |
|---|---|---|
| 11 | **插件示例包缺失** | 文档说了怎么写插件，但没有可 clone 的模板仓库 |
| 12 | **没有 Docker** | 有人只想 `docker run`，不想装 Python |
| 13 | **没有 i18n** | 界面全中文，README 也是中文，不利于国际传播 |
| 14 | **无鉴权** | 绑定 127.0.0.1 是安全的，但如果 `--host 0.0.0.0` 就裸奔了。建议启动时若非 localhost 则警告或强制要求 token |
| 15 | **无并发压测** | `MAX_CONCURRENCY=2` 是拍的，没有实测过 MLX 在多线程下的内存峰值 |
| 16 | **LLM 输出不可保存** | 改写结果只在 textarea 里，刷新就没了 |

---

## 8. 我建议的 v0.1 发布范围

**现在就能发。** 理由：

- 主链路（上传 → 转写 → 导出）完全可用，且有真实样本验证
- 89 个测试、ruff 全绿、wheel 可安装
- 链接解析默认开启（主入口），并能整体关闭做合规兜底

**但发之前建议至少做这三件：**

1. 填 `LICENSE` 的版权人（10 秒）
2. 推上去看 CI 是否真的绿（5 分钟）
3. README 顶部加一句「0.1 是早期版本，接口可能变」（1 分钟）

**第 4 项分片重试**虽然是体验问题，但如果用户第一次跑就是 2 小时播客
并且中间失败了，印象会很差。可以考虑提前到 0.1.1。

---

## 9. 代码规模

| 层 | 文件数 | 说明 |
|---|---|---|
| API | 2 | routes + batch_routes |
| 核心 | 8 | app/state/config/models/db/pipeline/batch/llm |
| 能力层 | 15 | extractors(5) + media(2) + asr(5) + ocr(3) |
| 导出 | 1 | 4 种格式 |
| 前端 | 3 | html + css + js，无构建 |
| 测试 | 7 | 154 个用例 |

合计约 8300 行（v0.2）。
---

## 10. 验收中发现的转录质量问题（v0.2 已知）

真实 127 分钟中文播客端到端跑完后，发现两个上游模型层面的问题：

### 已修：破折号前缀 + 无标点

现象：每个分句都是 `- 我是森森`，整段无标点。

对比实验（真实 60 秒中文播客，同一段音频）：

| 方案 | 输出 |
|---|---|
| 默认 | `- 我是森森` |
| `condition_on_previous_text=False` | `- 我是森森`（无效） |
| **`initial_prompt` 给标点示范** | `我是森森，我是蓝白，我是石天，` |

已给 mlx / faster-whisper 两个引擎都加上中日文提示词（`_initial_prompt`）。

**但只加提示词还不够**：pipeline 原来把 `language=None` 传给引擎，
`_initial_prompt(None)` 返回 `None`，提示词根本不生效。第一次修完实测
仍 22/26 段带破折号就是这个原因。

已补 `_probe_language()`：未配置 `ASR_LANGUAGE` 时先用前 30 秒探一次语言，
再带明确 language 跑全量。实测后：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 破折号前缀 | 22/26 段 | **0/9 段** |
| language | None | **zh** |

代价：长内容多一次 30 秒转写（2 小时音频约多 3 秒）。

### 仍未解决：标点仍不稳定

修复后破折号消失，但**逗号句号仍常常缺失**（实测 0/9 段含标点），
原因是 whisper 自身在这个音频上的输出就不稳定（见下一节）。

### 未修：mlx-whisper 输出不稳定

现象：**同一段音频、同样参数，连续两次调用输出差异极大**。

```
第一次：'是暂时是我们四个 彭总在北京周五的马路上在驰场的'
第二次：'词itte。' / ' Woah, ATTUNE 大家好,我是 Ka illensi。'
```

原因在 whisper 的温度回退机制（temperature fallback）与 VAD 切分不稳定，
属于上游库行为，不是本项目代码问题。

**影响**：同一段音频可能得到质量差异明显的结果。长播客里个别分片
认错不会毁掉全局，但会让部分段落质量参差。

**可选缓解（未实施）**
- 指定 `temperature=0` 禁用回退，换取确定性（可能牺牲其他段落质量）
- 对低置信度分片重试并取较优结果（要跑两倍算力）
- 换 SenseVoice（funasr）——中文场景标点与稳定性通常更好

### 其他已知限制

- 时间戳精度：分片间用 `chunk.start` 平移，毫秒级误差会累加
- 长内容耗时：127 分钟音频在 M4 上约 25 分钟（mlx + turbo）

---

## 11. 内容解析（长文本结构化总结）

把 2 小时的转写文本变成「摘要 + 可跳转章节 + 要点 + 金句 + 实体」。
与「AI 改写」的边界：改写是换一种说法，**解析只提取结构，不改写原文**。
契约见 `docs/ANALYSIS-CONTRACT.md`。

### 为什么必须 map-reduce

3 万字一次性问模型，一是上下文放不下，二是模型会漏掉中段、时间戳乱编。
所以先按时间/字符把文本切成若干窗口逐窗结构化（map），
再把各窗的**摘要**（不是原文）拼起来做一次合并（reduce）。
reduce 只看到摘要，token 成本可控。

| 决策 | 理由 | 代价 |
|---|---|---|
| 12 分钟 **或** 6000 字先到者切窗 | 时间维度对齐播客节奏，字符维度防长句内容超长 | 两种预算都要测 |
| 窗口上限 40，超了加倍预算重切 | 3 小时音频切 100 窗等于让用户多付 10 倍钱 | 单窗内容变长，可能质量下降 |
| 窗口 > 20 时日志告警 | 用户看不见 token 消耗，至少运维/日志要留证据 | — |
| 单窗 JSON 失败跳过，过半才整体失败 | 全盘失败会让已成功窗口的 token 白花 | 少量窗口静默丢失 |
| quotes 只用 map 结果 | 原文经二次生成会失真，金句必须逐字 | reduce 不能润色金句 |
| 章节时间戳用**该窗真实边界**回填 | 模型编的时间戳会跳到错误位置，跳转就失去意义 | reduce 必须输出 `window` 序号 |

### 时间戳回填链路

```
分窗   Window(index=3, start=2157.0, end=2875.9, …)
  ↓    map 提示词里给出「本段对应 35:57–47:55」
模型   {"quotes":[{"text":"…","start":42.0}]}   ← 窗内相对秒数
  ↓    start = window.start + 42.0
结果   Quote(start=2199.0)                      ← 全局绝对秒数
```

`chapters` 同理：reduce 返回的 `window` 序号 → 该窗的 `start`/`end`。
序号越界或重号一律丢弃，绝不用猜的位置生成假章节。

### 一个 key 覆盖所有能力

用户往往只配了 `DEEPSEEK_API_KEY`（ASR/OCR 已经在用）。
如果解析非要再配一套 `LLM_BASE_URL`/`LLM_MODEL`，等于把产品承诺打破。
所以 `resolve_analysis_llm()` 做兜底：

| 配置情况 | 结果 | `capabilities.analysis.reason` |
|---|---|---|
| 有 `LLM_BASE_URL` + `LLM_MODEL` | 用用户指定的模型 | `null`（无需解释） |
| 只有 `DEEPSEEK_API_KEY` | `deepseek-chat` + `DEEPSEEK_BASE_URL` | 说明「已自动复用 DEEPSEEK_API_KEY」 |
| 都没有 | 不可用 | 说明两种配法任选其一 |

### 成本可见

`Analysis.token_usage = {prompt, completion} | None`：
上游响应带 `usage` 就累加各次调用之和填上；不带就留 `None`，
**不用估算值假装精确**。`windows` 也如实返回实际窗口数。

### 存储与生命周期

- 表 `analyses`：`task_id` 主键，一个任务只留一份，重新解析覆盖
- 删除任务时**同一事务**里级联删除，避免 `DELETE /tasks` 后还能读到分析
- 分析**脱离请求生命周期**跑：前端关掉页面不会让已付费的中间结果作废，
  重新连上 `GET /analysis` 也能看到
- 服务重启时 `analyses.state='RUNNING'` 一律标 `FAILED`（任务同理）

### HTTP

| 接口 | 行为 |
|---|---|
| `GET /api/v1/tasks/{id}/analysis` | 200 + Analysis；没有结果 → 404 |
| `POST /api/v1/tasks/{id}/analyze` | 已有 SUCCESS 且 `force=false` → **不走 SSE**，直接 200 + JSON（避免重复烧 token） |
| 同上，首次分析 | SSE：`progress(mapping)` → `progress(reducing)` → `result` / `error` |
| 未配置模型 / 无文本 | 400，`detail` 说清怎么配 |
| `GET /api/v1/capabilities` | 新增 `analysis: {available, reason, source, model}`，旧字段不变 |

### 输出护栏

模型输出失控会撑爆 UI，因此标题 40 字、摘要 200 字、全文摘要 600 字、
要点 10 条、金句 6 条（每窗最多采 3 条）、实体 15 个，超出截断。

### 已知限制

- 章节粒度 = 窗口粒度：reduce 只能引用已有窗口，无法把一窗拆成多章
- 窗口边界按分句切分，不保证落在语义边界上
- 金句去重按「去空白后完全相同」，近义重复仍会保留

---

## 12. 前端可配置 API Key + 用量统计

契约：`docs/SETTINGS-CONTRACT.md`。本节只记「为什么这么做」和踩过的坑。

### 配置三层，纵深覆盖

```
代码默认值  <  .env / 环境变量  <  .data/settings.json（界面写入）
```

不改 pydantic-settings 的 source 顺序（那要 monkeypatch 内部实现，脆弱且难测），
而是在 `config.get_settings()` 里显式叠一层：

```python
def get_settings():
    base = _env_settings()              # 环境变量 + .env，带 lru_cache
    overrides = load_overrides(base.data_dir)   # 每次重读
    return apply_overrides(base, overrides) if overrides else base
```

`apply_overrides()` 是**逐字段**判断「这一层有没有值」，有才覆盖。
所以 JSON 里只写 `api_key` 时，`base_url` / `model` 仍然来自 `.env`。
空字符串等于「没配置」——写空等于删掉，不留第三种状态。

`settings.json` 的路径取自基线里的 `data_dir`：文件自己就在 `.data` 下，
不能靠它定位自己。文件读坏 / 类型不对 / 有未知分节时**只 warning 不抛**，
退回 `.env`，用户还能从界面重新保存。

### 热更新：必须真的立刻生效

`AppState` 里最容易被搞坏的是「读构造时的快照」。现在：

- `_current_stamp()` 取 `settings.json` 的 `(mtime_ns, size)`
- `sync_settings()` 每个请求 stat 一次，变了就 `reload_settings()`
  （外部 `vim` 编辑也能捡到，不必走 API）
- `reload_settings()` 只覆盖 `HOT_FIELDS`（llm / deepseek / pricing），
  **不动** `data_dir`、并发数这类字段，否则正在跑的任务会找不到自己的目录
- 然后重建 `pipeline.engines`（缓存了旧配置）、`llm`、`analysis_llm`
- `analysis_config()` 每次请求都 `sync_settings()` + 重新 `resolve_analysis_llm()`
- `capabilities` 也先 sync，前端靠它刷新 tab 可用状态

`reset_settings_cache()` 只清 `_env_settings` 的缓存 —— `settings.json`
本来就每次重读，不需要缓存。

### 安全：key 只进不出

| 要求 | 实现 |
|---|---|
| GET 只给掩码 | `mask_key()` 是唯一出口；≥12 字符前 6 后 4 中间固定 8 个 `·`，<12 全掩码 |
| 响应体无原文 | `LLMSettings` 模型里根本没有 `api_key` 字段，只有 `api_key_masked` |
| 日志无原文 | 所有 `log.*` 只记「哪个分节改了」「key 设了没有」 |
| 落盘 0600 | `os.open(..., 0o600)` 创建 + 写完 `chmod` 双保险（open 的 mode 对已存在文件无效） |
| 写入前校验 | base_url 必须 http/https 且无空白；model ≤100；key ≤500，不合法 400 |

`POST /settings/test` 有一个额外发现：**上游的报错 body 可能回显 key**。
DeepSeek 自己会打码（`Your api key: ****9c1b`），但不能指望每家网关都这样。
所以返回前用 `_redact_secret()` 把当前 key 替换成掩码再交给前端。

### 用量：只追加的账本

`usage_events` 表只追加、只聚合，**删任务不删**这里的行
（删任务不该抹掉已经花过的钱）。清空只有 `DELETE /api/v1/usage`。

埋点的四个位置都走同一个 `usage.record()`，失败只 `log.warning`：

| 位置 | kind | 记什么 | 特殊处理 |
|---|---|---|---|
| `pipeline.run()` 转写成功 | `asr` | `task.duration` + `engine.name` | 整单命中检查点（`resumed >= total`）时**不记**，这轮没喂给引擎 |
| `pipeline._run_ocr()` 成功 | `ocr` | `len(images)` + `result.engine` | 按送去识别的张数，不是识别出的行数 |
| `analysis.build_job()` 完成 | `analysis` | `Analysis.token_usage` | FAILED 也记（map 阶段花了 token 才失败） |
| `llm.stream_rewrite()` 结束 | `rewrite` | 流式 usage 帧 | 请求带 `stream_options.include_usage`；没有就记 0，**不猜**；生成器被提前关闭也在 `finally` 上报（上游已计费） |

改写走 `llm.stream_rewrite()`，那边拿不到 `Database`，所以在 `llm` 里留了
模块级 sink（`set_usage_sink`），由 `AppState` 构造时接上、shutdown 时断开。

### 计价：估算，不假装精确

`estimate_cost()` 就是契约 §5 那条公式，单价来自 `DEFAULT_PRICING`
（可被 `settings.json` 的 `pricing` 覆盖）。费用保留 **6 位小数**：
OCR 单张 0.0015 元，四舍五入到 2 位会变成 0.00，让用户以为图片识别免费。

### 时区：契约没写，但会算错账

SQLite 的 `date()` **直接截取日期部分、忽略时区偏移**：

```sql
SELECT date('2026-11-23T23:59:00+00:00');            -- 2026-11-23
SELECT date('2026-11-23T23:59:00+00:00', '+8 hours');-- 2026-11-24  ← 正确
SELECT date('2026-11-23T23:59:00+00:00', '-8 hours');-- 2026-11-23  ← 方向反了
```

我们存的是 UTC（`utcnow()`），所以转本地要**加**偏移。
不硬编码 +8（别的时区部署会全错），用 `local_offset_minutes()`
从 `astimezone().utcoffset()` 算；用 **minutes** 不用 hours
（印度 +330、尼泊尔 +345 这类半小时时区，`'-5.5 hours'` 实测无效）。

`by_day` 的「最近 N 天」边界也按**本地**日期推：东八区用户
在 00:00–08:00 之间，UTC 的今天还是昨天，用 UTC 推会少算一天。

### HTTP

| 接口 | 行为 |
|---|---|
| `GET /api/v1/settings` | 200 + `SettingsView`（key 只有掩码） |
| `PUT /api/v1/settings/llm` | 空字符串 / null / 不传 = 保持不变；响应同 GET |
| `PUT /api/v1/settings/deepseek` | 同上 |
| `DELETE /api/v1/settings/llm` | 清掉该分节，**回落到 .env** |
| `PUT /api/v1/settings/pricing` | 支持四个键直接给，也支持 `{"pricing": {...}}` |
| `POST /api/v1/settings/test` | 发一次最小 chat 请求；失败返回 `ok=false` + 人话原因（不是 500） |
| `GET /api/v1/usage?days=30` | 全量 total + 最近 N 天 by_day + by_kind + by_model |
| `DELETE /api/v1/usage` | 清空账本（唯一会删 usage 的入口） |

`GET /api/v1/capabilities` 新增 `settings: {editable, config_path, hot_reload}`
和 `usage: {available}`，旧字段不变。

### 已知限制

- `by_day` 的「天」用服务端进程的时区；跨时区访问（比如远程连到别的机器）会按服务端算
- `total.estimated_cost` 是全量估算，`by_day` 只含最近 N 天，两者不能直接对账
- 部分命中检查点的任务仍按整段时长记 ASR（精确折算要额外维护每片时长）
