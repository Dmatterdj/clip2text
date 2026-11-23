#!/usr/bin/env node
/**
 * 把某个 DSH 会话从旧工作区迁移到新工作区。
 *
 * 为什么必须「先退出 DSH」再跑：
 *   DSH 运行期间会持续把新事件追加写入会话文件（当前正在对话的会话尤其如此），
 *   此时移动文件会让新旧两处各写一份，会话内容被拆成两段。
 *
 * 用法（在项目根目录执行）：
 *   node scripts/migrate-session.mjs --dry-run          # 只看计划，不动任何文件
 *   node scripts/migrate-session.mjs                    # 真正执行
 *   node scripts/migrate-session.mjs --rollback <备份目录>
 *
 * 需要 Node >= 22.15（内置 zstd）。若系统 node 版本不够，用 DSH 自带的：
 *   ELECTRON_RUN_AS_NODE=1 "$DSH_NODE_EXECUTABLE" scripts/migrate-session.mjs --dry-run
 */

import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import zlib from 'node:zlib'
import { randomUUID } from 'node:crypto'

// ---------------- 配置（可用环境变量覆盖） ----------------
const SESSION_ID =
  process.env.C2T_SESSION_ID || 'session-e3d127a4-0552-49c1-923e-5460d6f60419'
const FROM_PATH = process.env.C2T_FROM || '/Users/dengjie/Documents/deepseek-harness/个人'
const TO_PATH = process.env.C2T_TO || '/Users/dengjie/Documents/deepseek-harness/个人/clip2text'

const DSH_HOME = process.env.DSH_HOME || path.join(os.homedir(), '.dsh')
const SESSIONS_ROOT = path.join(DSH_HOME, 'sessions')
const WORKSPACE_JSON = path.join(DSH_HOME, 'storages', 'workspace.json')

const dryRun = process.argv.includes('--dry-run')
const rollbackIdx = process.argv.indexOf('--rollback')
const rollbackDir = rollbackIdx > -1 ? process.argv[rollbackIdx + 1] : null

const log = (...a) => console.log(...a)
const die = (msg) => {
  console.error('\n✗ ' + msg)
  process.exit(1)
}

// ---------------- 工具 ----------------
/**
 * DSH 会话桶目录名 = 绝对路径去掉开头的 /，把 / 换成 -，
 * 每个非 ASCII 字符换成 ~XXXX（UTF-16 码元）。
 * 例：/Users/x/个人/clip2text -> --Users-x-~4E2A~4EBA-clip2text--
 */
function bucketName(absPath) {
  const body = absPath
    .replace(/^\//, '')
    .replace(/\//g, '-')
    .replace(/[^\x00-\x7F]/g, (ch) => '~' + ch.charCodeAt(0).toString(16).toUpperCase().padStart(4, '0'))
  return `--${body}--`
}

const ZSTD_MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd])

/** 会话文件是多帧拼接的 zstd，逐帧解压后拼成完整 JSONL 文本 */
function decodeSessionFile(file) {
  const raw = fs.readFileSync(file)
  const starts = []
  let p = 0
  while (true) {
    const i = raw.indexOf(ZSTD_MAGIC, p)
    if (i < 0) break
    starts.push(i)
    p = i + 1
  }
  if (starts.length === 0) throw new Error(`不是 zstd 文件：${file}`)
  let out = ''
  for (let k = 0; k < starts.length; k++) {
    const from = starts[k]
    const to = k + 1 < starts.length ? starts[k + 1] : raw.length
    try {
      out += zlib.zstdDecompressSync(raw.subarray(from, to)).toString('utf8')
    } catch {
      // 末尾可能是写了一半的帧，忽略
    }
  }
  return out
}

const encodeSessionFile = (text) => zlib.zstdCompressSync(Buffer.from(text, 'utf8'))

// ---------------- 回滚 ----------------
if (rollbackDir) {
  const bSession = path.join(rollbackDir, 'session')
  const bWorkspace = path.join(rollbackDir, 'workspace.json')
  if (!fs.existsSync(bSession)) die(`备份目录里没有 session：${bSession}`)
  const target = path.join(SESSIONS_ROOT, bucketName(FROM_PATH), SESSION_ID)
  log('回滚中')
  log('  会话 ->', target)
  fs.mkdirSync(path.dirname(target), { recursive: true })
  fs.cpSync(bSession, target, { recursive: true })
  if (fs.existsSync(bWorkspace)) {
    fs.copyFileSync(bWorkspace, WORKSPACE_JSON)
    log('  workspace.json 已还原')
  }
  log('\n✓ 回滚完成，重新打开 DSH 即可。')
  process.exit(0)
}

// ---------------- 迁移 ----------------
const fromBucket = bucketName(FROM_PATH)
const toBucket = bucketName(TO_PATH)
const srcDir = path.join(SESSIONS_ROOT, fromBucket, SESSION_ID)
const dstDir = path.join(SESSIONS_ROOT, toBucket, SESSION_ID)

log('迁移计划')
log('  会话      :', SESSION_ID)
log('  原工作区  :', FROM_PATH)
log('  原桶目录  :', fromBucket)
log('  新工作区  :', TO_PATH)
log('  新桶目录  :', toBucket)
log('  模式      :', dryRun ? 'DRY-RUN（不改动任何文件）' : '执行')
log('')

if (!fs.existsSync(TO_PATH)) die(`目标目录不存在：${TO_PATH}`)

if (!fs.existsSync(srcDir)) {
  if (fs.existsSync(dstDir)) {
    log('✓ 会话已在新位置，无需迁移：', dstDir)
    process.exit(0)
  }
  die(`找不到会话目录：${srcDir}`)
}

// 0. 运行中检测：会话文件近期被改动则大概率 DSH 还开着
const mtimeMs = fs.statSync(srcDir).mtimeMs
const ageMin = (Date.now() - mtimeMs) / 60000
if (ageMin < 1 && !dryRun) {
  die(
    '会话文件 1 分钟内还有写入，DSH 很可能仍在运行。\n' +
      '  请先完全退出 DSH 再执行，否则会话会被拆成两段。\n' +
      '  确实要继续请加环境变量 C2T_FORCE=1'
  )
}

// 1. 备份
const stamp = new Date().toISOString().replace(/[:.]/g, '-')
const backupDir = path.join(DSH_HOME, `session-migrate-backup-${stamp}`)
log('1) 备份到', backupDir)
if (!dryRun) {
  fs.mkdirSync(backupDir, { recursive: true })
  fs.cpSync(srcDir, path.join(backupDir, 'session'), { recursive: true })
  fs.copyFileSync(WORKSPACE_JSON, path.join(backupDir, 'workspace.json'))
}

// 2. 读会话、改写头部 cwd
const sessionFile = path.join(srcDir, 'session.v4.jsonl.zstd')
log('2) 解析会话文件', path.basename(sessionFile))
let text
try {
  text = decodeSessionFile(sessionFile)
} catch (e) {
  die(`解析会话失败：${e.message}`)
}
const lines = text.split('\n')
const headIdx = lines.findIndex((l) => l.trim().startsWith('{'))
if (headIdx < 0) die('会话文件为空或格式异常')
let head
try {
  head = JSON.parse(lines[headIdx])
} catch (e) {
  die(`头部事件不是合法 JSON：${e.message}`)
}
if (head.type !== 'session') die(`第一条事件不是 session，而是 ${head.type}`)
log('   原 cwd:', head.cwd)
head.cwd = TO_PATH
lines[headIdx] = JSON.stringify(head)
const newText = lines.join('\n')
log('   新 cwd:', head.cwd)
log('   事件行数:', lines.length)

// 3. 复制到新桶并改写
log('3) 写入新位置', dstDir)
if (!dryRun) {
  fs.mkdirSync(dstDir, { recursive: true })
  fs.writeFileSync(path.join(dstDir, 'session.v4.jsonl.zstd'), encodeSessionFile(newText))
  const verifyText = decodeSessionFile(path.join(dstDir, 'session.v4.jsonl.zstd'))
  const verify = JSON.parse(verifyText.split('\n').find((l) => l.trim().startsWith('{')))
  if (verify.cwd !== TO_PATH) die('写入校验失败：cwd 未更新')
  if (verify.id !== SESSION_ID) die('写入校验失败：id 不匹配')
  log('   ✓ 校验通过')
}

// 4. 更新 workspace.json
log('4) 更新 workspace 索引')
const ws = JSON.parse(fs.readFileSync(WORKSPACE_JSON, 'utf8'))
const table = ws.tables.workspaces

let toId = Object.entries(table).find(([, v]) => v.path === TO_PATH)?.[0]
if (!toId) {
  toId = randomUUID()
  log('   新建工作区条目:', toId)
  table[toId] = {
    path: TO_PATH,
    title: path.basename(TO_PATH),
    sessionIds: [],
    createdAt: Date.now(),
    updatedAt: Date.now(),
  }
  if (!ws.global.workspaceIds.includes(toId)) ws.global.workspaceIds.push(toId)
} else {
  log('   复用已有工作区条目:', toId)
}
if (!table[toId].sessionIds.includes(SESSION_ID)) table[toId].sessionIds.push(SESSION_ID)
table[toId].updatedAt = Date.now()

for (const [id, v] of Object.entries(table)) {
  if (id === toId) continue
  if (Array.isArray(v.sessionIds) && v.sessionIds.includes(SESSION_ID)) {
    log('   从工作区移除:', v.path)
    v.sessionIds = v.sessionIds.filter((s) => s !== SESSION_ID)
    v.updatedAt = Date.now()
  }
}

if (!dryRun) {
  fs.writeFileSync(WORKSPACE_JSON, JSON.stringify(ws, null, 2))
  log('   ✓ 已写入')
}

// 5. 删除旧位置
log('5) 删除旧位置', srcDir)
if (!dryRun) {
  fs.rmSync(srcDir, { recursive: true, force: true })
  const oldBucketDir = path.join(SESSIONS_ROOT, fromBucket)
  if (fs.existsSync(oldBucketDir) && fs.readdirSync(oldBucketDir).length === 0) {
    fs.rmdirSync(oldBucketDir)
    log('   旧桶目录已空，一并删除')
  }
}

log('')
if (dryRun) {
  log('DRY-RUN 完成，未改动任何文件。去掉 --dry-run 即执行。')
} else {
  log('✓ 迁移完成')
  log('  备份：', backupDir)
  log('  回滚：node scripts/migrate-session.mjs --rollback', backupDir)
  log('  现在重新打开 DSH，侧栏选择工作区 clip2text。')
}
