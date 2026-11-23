# 内容解析（分析长文本）契约 v1

> 本文是两个并行开发任务（后端分析引擎 / 前端解析面板）的**唯一事实来源**。
> 两侧都必须严格按此实现，不要各自发明字段。

---

## 1. 目标

把一份长转写文本（可能是 2 小时播客、3 万字）变成**结构化、可跳转**的内容概览：

- **一句话摘要**
- **章节**：带起止时间戳的段落标题 + 各自小结 → 用户能跳到感兴趣的位置
- **关键要点**：全局要点列表
- **金句**：值得摘录的原话 + 时间戳
- **实体/术语**：人名、产品、概念

与现有「改写」的区别：
- 改写 = 换一种表达风格（营销、书面化等），全文重写
- **解析 = 提取结构，不重写原文，必须带时间戳**

---

## 2. 数据模型（后端定义，前端照此渲染）

```python
class Chapter(BaseModel):
    start: float          # 秒，相对音频开头
    end: float            # 秒
    title: str            # 8-20 字的小标题
    summary: str = ""     # 1-3 句，这一章讲了什么

class Quote(BaseModel):
    start: float          # 秒
    text: str             # 原话，不改写
    note: str | None      # 可选：为什么值得摘

class Analysis(BaseModel):
    task_id: str
    abstract: str                       # 全文摘要，120-300 字
    chapters: list[Chapter]
    key_points: list[str]               # 3-8 条
    quotes: list[Quote]                 # 0-6 条
    entities: list[str]                 # 0-15 个
    language: str | None = None
    model: str = ""                     # 实际使用的模型名
    windows: int = 0                    # 实际分了几窗（长文本 map 阶段）
    state: Literal["RUNNING","SUCCESS","FAILED"] = "SUCCESS"
    error: str | None = None
    created_at: datetime
```

---

## 3. 长文本策略：map-reduce（必须实现）

单次 LLM 调用放不下 3 万字，且一次问完质量会塌。分两步：

### 3.1 分窗

- 优先用 `task.segments`（有时间戳），按**时间窗口**切：默认每窗 ≈ 12 分钟，
  或按字符预算 ≈ 6000 字，取先到者
- 没有 segments 时（如 OCR 结果），按字符预算切，时间戳留 0
- 单窗内容本身超预算时，继续按字符硬切
- 窗口数上限 40，超过则加大单窗预算（避免 3 小时音频切出 100 窗烧钱）

### 3.2 Map：逐窗结构化

对每窗单独调 LLM，要求返回 JSON：

```json
{
  "title": "这一窗的小标题",
  "summary": "这一窗讲了什么，2-3 句",
  "key_points": ["要点1", "要点2"],
  "quotes": [{"text": "原话", "start": 123.4}]
}
```

提示词里必须给出该窗的时间范围（如「本段对应 12:00–24:00」），
并要求 quotes 的 start 用该窗内的**相对秒数**，便于回填。

### 3.3 Reduce：合并成整体

把各窗的 `title + summary + key_points` 拼起来（不要拼原文，省 token），
再调一次 LLM，要求返回：

```json
{
  "abstract": "全文摘要 120-300 字",
  "chapters": [{"title": "...", "summary": "...", "window": 0}],
  "key_points": ["..."],
  "quotes": [],
  "entities": ["..."]
}
```

Reduce 阶段的 `chapters[].window` 指向 map 阶段的窗口序号，
后端据此回填 `start`/`end`（用该窗的真实起止时间）。

**quotes 不要交给 reduce 重写**：直接把 map 阶段各窗收集到的 quotes
按 start 排序、去重、截断到 6 条即可。原文引用经二次生成容易失真。

### 3.4 单窗短路

只有 1 个窗口时，跳过 reduce，直接用 map 结果组装
（`chapters` 一条，`start=0`, `end=duration`）。

---

## 4. JSON 可靠性（必须处理）

不同 OpenAI 兼容服务的 JSON 能力差异很大，不能假设 `response_format` 可用。

- 提示词里明确「只输出 JSON，不要 markdown 代码块」
- 解析时**宽松处理**：先 `json.loads`；失败则正则抓第一个 `{...}` 块再试；
  再失败则记录该窗失败并继续（不中断整体）
- 单个窗失败超过半数 → 整体 FAILED，错误信息说清是「模型返回无法解析」
- 章节标题、摘要必须做长度截断，防止模型输出失控

---

## 5. HTTP API

### 5.1 发起分析（SSE）

```
POST /api/v1/tasks/{task_id}/analyze
Body: {"force": false}     # force=true 时忽略已有结果，重新分析
→ text/event-stream
```

事件序列：

```
event: progress
data: {"phase":"mapping","done":0,"total":8,"message":"正在分析第 1/8 段"}

event: progress
data: {"phase":"reducing","done":0,"total":1,"message":"正在汇总"}

event: result
data: { ...完整 Analysis JSON... }

event: error
data: {"detail":"..."}
```

- **未配置任何可用模型** → 直接 400，`detail` 说清怎么配。

  ⚠️ 注意「可用模型」的定义（实现时按此，前端也必须按此判断）：
  1. `LLM_BASE_URL` + `LLM_MODEL` 都配了 → 可用，`source="llm"`
  2. 否则若 `DEEPSEEK_API_KEY` 有值 → **可用**（自动用 `deepseek-chat`），
     `source="deepseek"`。这是项目「一个 key 覆盖多个能力」的设计。
  3. 两者都没有 → 不可用

  **前端隐藏「解析」tab 的条件必须是 `capabilities.analysis.available == false`，
  不能自己去判断 `LLM_BASE_URL` 是否为空** —— 否则只配了 DeepSeek key 的用户
  会看不到这个功能。
- 已有 SUCCESS 结果且 `force=false` → 不发 SSE，直接返回 200 + 结果 JSON
  （前端应先 GET 探一下，避免重复烧 token）
- 任务没有文本内容 → 400

### 5.2 读取已有结果

```
GET /api/v1/tasks/{task_id}/analysis
→ 200 + Analysis JSON
→ 404 {"detail":"还没有分析结果"}
```

### 5.3 能力上报

`GET /api/v1/capabilities` 的 `rewrite` 字段旁**新增** `analysis` 字段（不改旧的）：

```json
"analysis": {
  "available": true,
  "reason": null,
  "source": "llm",        // "llm" | "deepseek"
  "model": "deepseek-chat"
}
```

- `source` / `model` 用于让用户知道当前用哪个模型（可选展示，但强烈建议展示）
- `available=false` 时 `reason` 必须给出可操作的提示

---

## 6. 存储

新增 SQLite 表：

```sql
CREATE TABLE IF NOT EXISTS analyses (
    task_id     TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,     -- Analysis 的 JSON
    model       TEXT,
    state       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
```

- 一个任务只保留一份分析结果，重新分析则覆盖
- 删除任务时**级联删除**对应 analysis
- 服务重启时，`state=RUNNING` 的分析标记为 FAILED（与任务的处理一致）

---

## 7. 前端要求

在结果卡的 tab 行**新增一个 tab「解析」**（放在「正文」「分句」之后、「改写」之前）：

- 未配置 LLM 时该 tab 隐藏（与「改写」一致）
- 打开 tab 时先 `GET /analysis`：
  - 有结果 → 直接渲染
  - 404 → 显示「开始解析」按钮
- 点「开始解析」→ `POST /analyze`，用 SSE 读进度：
  - mapping 阶段显示 `正在分析 3/8 段` + 进度条
  - reducing 阶段显示「正在汇总」
  - 收到 result 渲染，收到 error 显示错误
- 已有结果时提供「重新解析」按钮（`force=true`，需二次确认）

### 渲染结构（必须带时间戳且可点击）

```
┌ 摘要 ────────────────────────────┐
│ 120-300 字的全文摘要              │
└──────────────────────────────────┘

章节
┌──────────────────────────────────┐
│ 00:00  开场与问题提出             │  ← 时间戳可点击
│        这一章讲了…                │
├──────────────────────────────────┤
│ 12:34  零宽字符的原理             │
└──────────────────────────────────┘

关键要点
• 要点 1
• 要点 2

金句
│ "原话内容"            12:34  ⧉   ← 点击复制，时间戳可跳转

实体
AI水印  零宽字符  GPT  欧盟
```

**点击时间戳 → 滚动到「分句」tab 对应分句并高亮**（若当前不在分句 tab，
先切过去）。这是本功能的核心价值，必须实现。

---

## 8. 后端实现约束

- 复用 `llm.py` 的 `LLMConfig`（`base_url` / `api_key` / `model` / `endpoint()`），
  不要另造配置
- 非流式调用即可（`stream=False`），因为要解析完整 JSON；但要设合理超时
- 新增文件 `src/clip2text/analysis.py`，不要塞进 `llm.py`（职责不同）
- 新增路由放 `src/clip2text/api/analysis_routes.py`，在 `app.py` 注册
- 中文注释，说明「为什么」而非「做了什么」
- Python 3.11/3.12，不引入新依赖
- 所有 LLM 调用必须可在测试中用假实现替换（便于 CI 不花钱）

---

## 9. 测试要求（后端）

必须覆盖（用假 LLM，不联网）：

1. 短文本（单窗）→ 跳过 reduce
2. 长文本（多窗）→ map 次数正确、reduce 被调用一次
3. 单窗 JSON 解析失败 → 该窗跳过，其余继续
4. 超半数窗失败 → 整体 FAILED
5. JSON 被 markdown 代码块包裹 → 仍能解析
6. 时间戳回填正确（reduce 的 window 序号 → 真实 start/end）
7. quotes 来自 map 且未经过 reduce 重写
8. 窗口数上限生效
9. 存储读写、覆盖、级联删除
10. 未配置 LLM → 明确报错

---

## 10. 验收标准（我会亲自验）

- `pytest` 全绿、`ruff` 无告警
- 用**真实 127 分钟小宇宙播客**（task id 在 .data/clip2text.db 里可查）跑一次真实分析，
  检查章节时间戳是否落在合理位置、摘要是否说了人话
- 前端实际打开截图，确认 tab、进度、章节列表、时间戳跳转都可用
- 如果没有 LLM key：两侧都必须优雅隐藏/禁用，不能报错崩掉
