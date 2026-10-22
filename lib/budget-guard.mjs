import { existsSync, mkdirSync, readFileSync, writeFileSync, renameSync, openSync, closeSync, unlinkSync } from 'node:fs'
import { join } from 'node:path'
import { randomUUID } from 'node:crypto'
import { dayKey } from './state.mjs'

export const LIMIT_CNY = 2
export const HEADROOM_CNY = 0.08
export const OUTPUT_LIMIT = 8192
const SAFE_FACTOR = 1.1

const read = (path, fallback) => { try { return JSON.parse(readFileSync(path, 'utf8')) } catch { return fallback } }
const atomic = (path, value) => { const tmp = path + '.' + process.pid + '.tmp'; writeFileSync(tmp, JSON.stringify(value, null, 2)); renameSync(tmp, path) }
const failure = (reason) => Object.assign(new Error('BILI_DAILY_BUDGET_STOP: ' + reason), { code: 'BILI_DAILY_BUDGET_STOP' })
export function freeGeminiEnabled(home) {
  const routing = read(join(home, 'model-routing.json'), {})
  return routing.primaryProvider === 'google' && routing.primaryModel === 'gemini-3.8-flash' && routing.geminiFreeTier === true
}
function readLedger(home) {
  const path = join(home, 'cost-ledger.json')
  if (!existsSync(path)) return { version: 1, days: {}, pending: {} }
  try {
    const ledger = JSON.parse(readFileSync(path, 'utf8'))
    if (!ledger.days || !ledger.pending || Array.isArray(ledger.days) || Array.isArray(ledger.pending)) throw new Error('invalid ledger')
    return ledger
  } catch { throw failure('费用账本损坏或不可读，不能清零重试') }
}

export function parseOfficialPrices(html) {
  const table = (html.match(/<table\b[\s\S]*?<\/table>/gi) || []).find(part => part.includes('deepseek-flash') && part.includes('deepseek-v4-pro'))
  if (!table) throw failure('无法识别官方价格表')
  const values = [...table.replace(/<[^>]*>/g, ' ').matchAll(/(\d+(?:\.\d+)?)\s*元/g)].map(match => Number(match[1]))
  if (values.length !== 12 || values.some(value => !Number.isFinite(value) || value <= 0)) throw failure('官方价格表结构变化，需要重新核实')
  return { 'deepseek-flash': { cache: values[2], input: values[6], output: values[10] },
    'deepseek-v4-pro': { cache: values[3], input: values[7], output: values[11] } }
}

let pricingPromise
export async function refreshBudgetPricing(home, fetcher = fetch) {
  const current = read(join(home, 'budget-pricing.json'), {})
  if (current.verifiedDay === dayKey()) return true
  if (pricingPromise) return pricingPromise
  pricingPromise = (async () => {
    try {
      const source = 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'
      const response = await fetcher(source, { signal: AbortSignal.timeout(15000) })
      if (!response.ok) return false
      const models = parseOfficialPrices(await response.text())
      for (const alias of ['deepseek-v4-flash', 'deepseek-v4-flash-vision-exp']) models[alias] = models['deepseek-flash']
      atomic(join(home, 'budget-pricing.json'), { source, verifiedDay: dayKey(), verifiedAt: new Date().toISOString(), models })
      return true
    } catch { return false }
    finally { pricingPromise = null }
  })()
  return pricingPromise
}

async function locked(home, mutate) {
  mkdirSync(home, { recursive: true })
  const path = join(home, 'cost-ledger.json'), lock = path + '.lock'
  let fd
  for (let attempt = 0; attempt < 40; attempt++) {
    try { fd = openSync(lock, 'wx'); break } catch (error) {
      if (error.code !== 'EEXIST' || attempt === 39) throw failure('费用账本忙或锁未释放，停止请求')
      await new Promise(resolve => setTimeout(resolve, 50))
    }
  }
  try {
    const ledger = readLedger(home)
    const result = mutate(ledger)
    atomic(path, ledger)
    return result
  } finally { if (fd !== undefined) closeSync(fd); try { unlinkSync(lock) } catch {} }
}

function counts(ledger, day) {
  const row = ledger.days[day] ||= { spentCny: 0, requests: 0 }
  const reservedCny = Object.values(ledger.pending).reduce((sum, item) => sum + item.reserveCny, 0)
  return { row, reservedCny, remainingCny: LIMIT_CNY - row.spentCny - reservedCny }
}

export function budgetStatus(home, now = new Date()) {
  let ledger
  try { ledger = readLedger(home) } catch (error) { return { capCny: LIMIT_CNY, stopped: true, reason: error.message } }
  const { row, reservedCny, remainingCny } = counts(ledger, dayKey(now))
  const freePrimary = freeGeminiEnabled(home)
  const fallbackStopped = Boolean(row.stoppedReason) || remainingCny <= HEADROOM_CNY
  return { capCny: LIMIT_CNY, spentCny: row.spentCny, reservedCny, remainingCny: Math.max(0, remainingCny),
    stopped: freePrimary ? row.spentCny >= 5 : fallbackStopped,
    reason: freePrimary && row.spentCny < 5 ? null : row.stoppedReason || null,
    freePrimary, fallbackStopped, fallbackReason: row.stoppedReason || null,
    accounting: freePrimary ? 'Gemini按主人确认的免费额度使用，不设调用次数限制；DeepSeek失败后备按高峰价加10%保守记账，封顶2元' : '按已核实高峰价加10%保守计费；含所有付费模型调用，未知回执保留整笔预留' }
}

export function reserveUpperBound(options, pricing) {
  if (options.provider !== 'deepseek-official') throw failure('未核实的模型供应商')
  const model = options.model
  const rates = pricing.models[model]
  if (!rates) throw failure('未核实的模型价格')
  if (!Number.isSafeInteger(options.maxTokens) || options.maxTokens < 1 || options.maxTokens > OUTPUT_LIMIT) throw failure('单次输出上限未设为8192以内')
  if ((options.messages || []).some(message => (Array.isArray(message.content) ? message.content : []).some(block => ['image', 'file'].includes(block.type)))) throw failure('未计价的媒体输入')
  // UTF-8 bytes already exceed ordinary token counts; double them and reserve wrapper overhead.
  const bytes = Buffer.byteLength(JSON.stringify({ system: options.system, messages: options.messages, tools: options.tools, toolHistory: options.toolHistory }), 'utf8')
  const inputTokensUpperBound = bytes * 2 + 16384
  if (inputTokensUpperBound > 1_000_000) throw failure('上下文过大，无法安全预留费用')
  return (inputTokensUpperBound * rates.input + options.maxTokens * rates.output) / 1_000_000 * SAFE_FACTOR
}

export async function guardedStream(home, options, next) {
  return (async function* () {
    if (process.env.BILI_BUDGET_PROBE === '1') throw failure('预算保护已加载；这是零费用初始化自检，未发送模型请求')
    if (freeGeminiEnabled(home) && options.provider === 'google' && options.model === 'gemini-3.8-flash') {
      if (budgetStatus(home).stopped) throw failure('费用账本异常或已达到原总费用硬上限')
      yield* next()
      return
    }
    await refreshBudgetPricing(home)
    const pricing = read(join(home, 'budget-pricing.json'), {})
    if (pricing.verifiedDay !== dayKey() || !pricing.models) throw failure('今日价格尚未核实，不调用付费模型')
    const reserveCny = reserveUpperBound(options, pricing)
    const id = randomUUID(), startedDay = dayKey()
    const admission = await locked(home, ledger => {
      const { row, remainingCny } = counts(ledger, startedDay)
      if (freeGeminiEnabled(home) && row.stoppedReason === '剩余额度不足以安全完成下一次请求，当日停止'
          && remainingCny > HEADROOM_CNY && Object.keys(ledger.pending).length === 0) {
        // Earlier versions stopped the whole day when one large request did not fit.
        // Retain every charge and apply the new per-request admission rule instead.
        ledger.reconciliations ||= []
        ledger.reconciliations.push({ day: startedDay, at: new Date().toISOString(), reason: row.stoppedReason,
          resolution: '单次预留不足仅拒绝该请求；保留全部历史费用并按2元上限重新核验每次付费请求',
          spentCnyUnchanged: row.spentCny })
        delete row.stoppedReason
        delete row.stoppedAt
      }
      if (row.stoppedReason || reserveCny + HEADROOM_CNY > remainingCny) {
        row.lastRejectedRequest = { reserveCny, remainingCny, at: new Date().toISOString() }
        if (remainingCny <= HEADROOM_CNY) {
          row.stoppedReason ||= 'DeepSeek当日安全余量已用尽，停止付费后备'
          row.stoppedAt ||= new Date().toISOString()
        }
        return false
      }
      ledger.pending[id] = { reserveCny, startedDay, pid: process.pid, at: new Date().toISOString() }
      return true
    })
    if (!admission) throw failure('DeepSeek每日2元预算或预留余量不足')
    let usage, finished = false
    try {
      for await (const chunk of next()) {
        if (chunk.type === 'usage') usage = chunk.usage
        if (chunk.type === 'finish' && chunk.reason?.kind !== 'error') finished = true
        yield chunk
      }
    } finally {
      await locked(home, ledger => {
        const item = ledger.pending[id]
        if (!item) return
        const rates = pricing.models[options.model]
        const valid = finished && usage && ['inputTokens', 'outputTokens'].every(key => Number.isFinite(usage[key]) && usage[key] >= 0)
        const chargedCny = valid ? ((usage.inputTokens + (usage.cacheWriteTokens || 0)) * rates.input
          + (usage.cacheReadTokens || 0) * rates.cache + usage.outputTokens * rates.output) / 1_000_000 * SAFE_FACTOR : reserveCny
        delete ledger.pending[id]
        for (const day of new Set([item.startedDay, dayKey()])) {
          const { row } = counts(ledger, day)
          row.spentCny += Math.max(0, chargedCny)
          row.requests++
          if (!valid && freeGeminiEnabled(home)) {
            row.unknownReceiptRequests = (row.unknownReceiptRequests || 0) + 1
            row.lastUnknownReceiptAt = new Date().toISOString()
          }
          if ((!valid && !freeGeminiEnabled(home)) || chargedCny > reserveCny || row.spentCny >= LIMIT_CNY - HEADROOM_CNY) {
            row.stoppedReason = !valid ? '费用回执不明确，保留整笔预留并停止当日请求' : '每日预算余量不足，停止当日请求'
            row.stoppedAt = new Date().toISOString()
          }
        }
        ledger.lastRequest = { day: item.startedDay, model: options.model, chargedCny, exactUsageReceived: Boolean(valid), finishReceived: finished, usageKeys: Object.keys(usage || {}), at: new Date().toISOString() }
      })
    }
  })()
}

export const name = 'bili-budget-guard'
export const inject = ['llm']
export function apply(ctx, config = {}) {
  const home = config.home || process.env.BILI_DUTY_HOME
  if (!home) throw failure('缺少费用账本目录')
  atomic(join(home, 'budget-guard-status.json'), { loadedAt: new Date().toISOString(), pid: process.pid, capCny: LIMIT_CNY })
  ctx.on('llm/stream', (options, next) => (async function* () { yield* await guardedStream(home, options, next) })(), { global: true, prepend: true })
}
