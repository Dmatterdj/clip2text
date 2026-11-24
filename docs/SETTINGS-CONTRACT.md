# 前端可配置 API Key + 用量查看 契约 v1

> 两个并行任务（后端 / 前端）的唯一事实来源。字段名严格按此实现。

---

## 1. 目标

用户在**网页里**填写 API Key，不用改 `.env`、不用重启；
并且能**看到自己消耗了多少**（token / 音频时长 / 图片张数 / 估算费用）。

### 为什么不能只改 .env

改 `.env` 要重启服务，而普通用户不会重启，也不知道该改哪一行。
上个迭代就因此让用户以为「功能不存在」。配置必须能在界面上完成。

---

## 2. 安全约束（必须遵守，不可协商）

1. **API Key 永不完整返回给浏览器**。GET 接口只能返回掩码，
   形如 `sk-4f2a··········9c1b`（保留前 6 后 4，中间固定 8 个 `·`）。
   短于 12 字符的 key 返回 `········`（全掩码，不泄露任何字符）。
2. **永不写入日志**。任何 `log.*` 调用都不得包含 key 原文。
3. **落盘权限 0600**（仅属主可读写）。文件不存在时创建即为 0600；
   已存在时先 `chmod` 再写。
4. **文件位置** `.data/settings.json`（与数据库同目录，已在 .gitignore 内）。
5. **不返回 `.env` 里的 key**。如果 key 来自环境变量而不是 settings.json，
   GET 返回 `source: "env"` + 掩码后的值，用户知道「已经在用了，但
   是通过环境变量配的，界面上改会覆盖它」。
6. **写入前校验**：`base_url` 必须是 http/https 且无空白；
   `model` 非空且 ≤ 100 字符；key ≤ 500 字符。不合法返回 400。

---

## 3. 配置优先级

```
settings.json（界面写入）  >  .env / 环境变量  >  代码默认值
```

生效后**立即应用，无需重启**：写入 settings.json 后，同一进程内的
`AppState.settings` 与 `analysis_llm` 都要重新解析。

---

## 4. 数据模型

```python
class LLMSettings(BaseModel):
    base_url: str = ""
    api_key_masked: str = ""      # 只读，GET 用
    api_key_set: bool = False     # 是否已配置（不暴露原文）
    model: str = ""
    source: Literal["settings", "env", "none"] = "none"

class DeepSeekSettings(BaseModel):
    api_key_masked: str = ""
    api_key_set: bool = False
    base_url: str = "https://api.deepseek.com"
    source: Literal["settings", "env", "none"] = "none"

class SettingsView(BaseModel):
    llm: LLMSettings
    deepseek: DeepSeekSettings
    # 有没有任一可用，等价于 analysis.available
    analysis_ready: bool = False
    # 配置文件路径（让用户知道东西存哪了）
    config_path: str = ""
```

### 用量

```python
class UsageEvent(BaseModel):
    id: int
    kind: Literal["asr", "ocr", "analysis", "rewrite"]
    task_id: str | None = None
    model: str = ""
    # 以下按 kind 填充，未使用为 0
    audio_seconds: float = 0
    images: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    created_at: datetime

class UsageBucket(BaseModel):
    """按维度聚合的一行。"""
    key: str          # 日期 2026-11-24 / 类型 asr / 模型 deepseek-chat
    audio_seconds: float = 0
    images: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost: float = 0     # 单位：人民币元

class UsageSummary(BaseModel):
    total: UsageBucket                    # key 固定为 "total"
    by_day: list[UsageBucket]             # 最近 30 天，倒序
    by_kind: list[UsageBucket]            # asr/ocr/analysis/rewrite
    by_model: list[UsageBucket]
    event_count: int = 0
    currency: str = "CNY"
    # 计价依据，让用户知道估算怎么来的
    pricing: dict[str, float]
```

---

## 5. 计价（估算，必须标注「估算」）

单位价格存进 settings.json 的 `pricing` 字段，用户可改，给默认值：

```python
DEFAULT_PRICING = {
    # 元 / 小时音频
    "asr_per_hour": 0.78,
    # 元 / 张图
    "ocr_per_image": 0.0015,
    # 元 / 百万 token
    "llm_input_per_mtok": 1.0,
    "llm_output_per_mtok": 4.0,
}
```

计算：

```
cost = audio_seconds / 3600 * asr_per_hour
     + images * ocr_per_image
     + prompt_tokens / 1e6 * llm_input_per_mtok
     + completion_tokens / 1e6 * llm_output_per_mtok
```

**不要假装精确**。前端展示时必须带「估算」字样，并说明单价可在设置里改。

---

## 6. HTTP API

| Method | Path | 说明 |
|---|---|---|
| `GET` | `/api/v1/settings` | 读配置（key 掩码） |
| `PUT` | `/api/v1/settings/llm` | 写 LLM 配置，立即生效 |
| `PUT` | `/api/v1/settings/deepseek` | 写 DeepSeek 配置，立即生效 |
| `DELETE` | `/api/v1/settings/llm` | 清除 LLM 配置 |
| `PUT` | `/api/v1/settings/pricing` | 改单价 |
| `POST` | `/api/v1/settings/test` | 测试当前配置是否可用（可选实现） |
| `GET` | `/api/v1/usage` | 用量汇总，支持 `?days=30` |

### PUT 请求体

```json
// PUT /settings/llm
{"base_url": "https://api.deepseek.com", "api_key": "sk-xxx", "model": "deepseek-chat"}

// api_key 传空字符串 = 保持不变；传 null 或不传 = 保持不变
// 要清除用 DELETE
```

响应统一返回 `SettingsView`。

### GET /usage

```json
{
  "total": {"key":"total","audio_seconds":7594.2,"images":0,"prompt_tokens":41308,"completion_tokens":1903,"estimated_cost":1.72},
  "by_day": [...],
  "by_kind": [...],
  "by_model": [...],
  "event_count": 14,
  "currency": "CNY",
  "pricing": {"asr_per_hour":0.78,"ocr_per_image":0.0015,"llm_input_per_mtok":1.0,"llm_output_per_mtok":4.0}
}
```

---

## 7. 用量埋点（后端必须接入的位置）

| 位置 | kind | 记录什么 |
|---|---|---|
| `pipeline.py` 转写成功后 | `asr` | `audio_seconds=task.duration`, `model=engine.name` |
| `pipeline.py` OCR 成功后 | `ocr` | `images=len(images)`, `model=ocr engine` |
| `analysis.py` 分析完成后 | `analysis` | 累计的 prompt/completion tokens, model |
| `llm.py` 改写完成后 | `rewrite` | 若上游返回 usage 则记录，否则 tokens 记 0 |

埋点**不能影响主流程**：记录失败只记日志，不能让任务失败。

---

## 8. 存储

```sql
CREATE TABLE IF NOT EXISTS usage_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    kind              TEXT NOT NULL,
    task_id           TEXT,
    model             TEXT NOT NULL DEFAULT '',
    audio_seconds     REAL NOT NULL DEFAULT 0,
    images            INTEGER NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_kind ON usage_events(kind);
```

- 删除任务时**不删**用量记录（用量是历史账本，删任务不该抹掉花过的钱）
- 提供 `delete_all_usage()` 供用户清空（可选端点 `DELETE /usage`）

---

## 9. 前端要求

### 9.1 设置入口

右上角加一个齿轮按钮（用现有 sprite 的 `#i-setting`，没有就新加），
点击打开设置面板（用现有 modal/抽屉模式，不要 `alert`）。

面板分两块：

**A. 模型配置**
```
内容解析与改写需要一个大模型

方式一：任意 OpenAI 兼容服务
  Base URL  [https://api.deepseek.com        ]
  API Key   [sk-4f2a··········9c1b          ]  ← 已有值时显示掩码
  模型名    [deepseek-chat                  ]
  [保存]

方式二：只配 DeepSeek（语音识别也用它）
  API Key   [········                        ]
  [保存]

[测试连接]
```

- 输入框 `type="password"`，带「显示」切换
- 已配置时输入框 placeholder 显示掩码，留空保存 = 不修改
- 明确提示「Key 只存在本机 `.data/settings.json`，不会上传」
- 保存后 toast「已生效」，并**刷新 capabilities**（tab 状态要跟着变）

**B. 用量**
```
用量（估算）
总消耗 ¥1.72   ·  14 次调用

语音识别   2h06m   ¥1.64
内容解析   43,211 tokens  ¥0.08

最近 7 天
11-24  ████  ¥1.20
11-23  ██    ¥0.52

单价设置  [语音识别 ¥0.78/小时] [OCR ¥0.0015/张]
          [输入 ¥1.0/百万token] [输出 ¥4.0/百万token]
          [保存单价]
```

- 「估算」二字必须出现在显眼位置
- 单价可编辑，改完重算

### 9.2 硬约束

- 无构建步骤、图标内联、不引外部库
- 深色/浅色都要正常
- 键盘可达（Esc 关闭面板）
- **Key 输入框绝不能有 autocomplete 把 key 存进浏览器**（`autocomplete="new-password"`）

---

## 10. 测试要求（后端）

用临时目录，不联网：

1. 写入后文件权限是 0600
2. GET 不返回 key 原文（断言响应 JSON 里不含完整 key）
3. 掩码格式正确（前 6 后 4；短 key 全掩码）
4. 空字符串/不传 api_key = 不修改
5. DELETE 清除后可回落 .env
6. 优先级：settings.json 覆盖 env
7. 写入后 `analysis.available` 立即变 true（不重启）
8. 非法 base_url / 空 model → 400
9. 用量记录：asr / ocr / analysis 三类分别埋点成功
10. 用量聚合：by_day / by_kind / by_model 正确
11. 计价计算正确
12. 埋点失败不影响任务（模拟记录异常）

---

## 11. 验收（我会亲自验）

- `pytest` 全绿、`ruff` 无告警
- 实际在页面上填一个假 key，确认：文件生成、权限 0600、
  刷新后 tab 变为可用、`capabilities.analysis.available` 变 true
- 实际看用量面板渲染
- 检查响应里**没有** key 原文
