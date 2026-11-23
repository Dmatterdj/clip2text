# clip2text

把视频、音频、图片里的内容，提取成可编辑的文字。**本地优先、默认零 API key。**

> 状态：`v0.1` 设计阶段。`docs/ARCHITECTURE.md` 已定稿，代码尚未开始。

---

## 这是什么

一个可以自己跑在电脑上的"文案提取"工具：

- **上传本地文件** → 自动抽取音轨 → 语音转文字 → 导出 `txt / md / docx / xlsx`
- **可选**：粘贴平台链接解析（默认关闭，见「合规」）
- **可选**：图片文字识别
- **可选**：接大模型做文案改写

和同类在线服务的区别：**不需要注册、不需要充值、不上传你的文件到别人的服务器。**
所有 key 都是可选项，不是门槛。

---

## 现在能做什么

| 能力 | 状态 |
|---|---|
| 项目骨架 + 架构文档 + 环境变量契约 | ✅ 已完成 |
| `.env.example` / `.gitignore` / 目录结构 | ✅ 已完成 |
| 会话迁移到本项目工作区 | ✅ 脚本就绪（见下） |
| P0：上传 → 转写 → 导出 | ⬜ 待开发 |
| P1：链接解析 | ⬜ 待开发 |
| P2：批量 / 队列 / SSE | ⬜ 待开发 |
| P3：OCR / LLM 改写 / 插件 | ⬜ 待开发 |

路线图和能力分档见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

---

## 目录结构

```
clip2text/
├─ apps/             # web（前端）与 api（FastAPI）
├─ packages/         # core / extractors / media / asr / ocr / exporters
├─ docs/
│  └─ ARCHITECTURE.md
├─ scripts/
│  └─ migrate-session.mjs
├─ .env.example
└─ .gitignore
```

---

## 环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| **Python** | **3.12** | `rapidocr-onnxruntime` 要求 <3.13；FastAPI + pydantic v2 建议 ≥3.11 |
| Node | ≥20 | 仅前端与脚本需要 |
| ffmpeg | 可不装 | `FFMPEG_MODE=auto` 时用 `static-ffmpeg` 自动下载 |

---

## 快速开始（P0 完成后生效）

```bash
git clone <repo> && cd clip2text
cp .env.example .env
# 全部留空即可跑通主链路
```

---

## 把 DSH 会话迁移到本项目

本会话最初在 `个人/` 工作区创建，现已切换到 `clip2text/`。
要把会话文件真正搬到新工作区（并让侧栏出现 clip2text 条目）：

```bash
# 1. 完全退出 DeepSeek Harness（必须，否则会话会被拆成两段）

# 2. 在项目根目录执行
node scripts/migrate-session.mjs --dry-run   # 先看计划
node scripts/migrate-session.mjs             # 再执行

# 3. 重新打开 DSH，侧栏选择工作区 clip2text
```

脚本做的事：备份 → 改写会话头部 `cwd` → 移到新桶目录 →
在 `workspace.json` 里新建工作区并挂载该会话 → 清理旧位置。

回滚：

```bash
node scripts/migrate-session.mjs --rollback ~/.dsh/session-migrate-backup-<时间戳>
```

> 原理与风险说明见脚本头部注释。

---

## 合规

- 默认**不开启**任何平台链接解析（`EXTRACTORS_ENABLED=false`），只处理你自己上传的文件。
- 开启链接解析后，请仅用于**学习研究与处理你拥有版权的内容**，遵守各平台服务条款与《著作权法》。
- 本项目**不提供托管服务**，仓库内不包含任何平台的签名密钥或登录 Cookie。

---

## License

Apache-2.0（待添加 `LICENSE` 文件）
