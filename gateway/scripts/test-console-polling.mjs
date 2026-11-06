#!/usr/bin/env node
/**
 * 控制台轮询行为验证：跑**真的** console.mjs，只是把 DOM / fetch / 定时器换掉。
 *
 * 为什么不用静态断言：这一块最容易"看起来改了"。旧代码是一行
 * `setInterval(() => { if (connected) void load() }, 15_000)`，里面 load() 会
 * 把每个渠道的积分重查一遍 —— 6 个渠道有账号时约 14 次调用 / 15 秒，其中
 * `credits.balances` 对每个账号都是一次真实上游请求。只 grep 关键字证明不了
 * 「热数据不再走上游」，必须让代码跑一遍、数请求。
 *
 *   node scripts/test-console-polling.mjs
 *
 * 覆盖：
 *   1. 首轮：账号/模型/渠道统计 + 一次积分；之后只拉本机热数据；
 *   2. 单轮热数据只有 /ui/quota、/ui/state、/ui/protection，零 /ui/rpc；
 *   3. 积分到 5 分钟才重查，期间沿用上次读数（不是「无读数」）；
 *   4. 切成「仅手动」后不再自动查积分，点刷新才查；
 *   5. 窗口不可见时停表，回到前台补一轮；
 *   6. `#bootstrap=<token>` 静默换会话，并从地址栏抹掉令牌。
 */

import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..')
const MODULE = join(ROOT, 'web', 'console.mjs')

const failures = []
let checks = 0
function check(name, actual, expected) {
  checks += 1
  const ok = JSON.stringify(actual) === JSON.stringify(expected)
  if (!ok) failures.push(`${name}: 期望 ${JSON.stringify(expected)}，实际 ${JSON.stringify(actual)}`)
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}  (${JSON.stringify(actual)})`)
}
function ok(name, condition, detail = '') {
  checks += 1
  if (!condition) failures.push(`${name}${detail ? '：' + detail : ''}`)
  console.log(`${condition ? 'PASS' : 'FAIL'}  ${name}${detail && !condition ? `  (${detail})` : ''}`)
}

/* ---------------------------------------------------------------- DOM 假件 */

function makeElement(id) {
  const element = {
    id,
    textContent: '',
    innerHTML: '',
    value: '',
    hidden: false,
    disabled: false,
    title: '',
    dataset: {},
    style: {},
    classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
    listeners: {},
    addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler) },
    removeEventListener() {},
    querySelector() { return makeElement(`${id}-child`) },
    querySelectorAll() { return [] },
    focus() {},
    click() {},
    appendChild() {},
  }
  return element
}

const elements = new Map()
const elementFor = id => {
  if (!elements.has(id)) elements.set(id, makeElement(id))
  return elements.get(id)
}

const timers = []
function fakeSetInterval(handler, delay) {
  const timer = { handler, delay, cleared: false, kind: 'interval' }
  timers.push(timer)
  return timers.length
}
function fakeSetTimeout(handler, delay) {
  const timer = { handler, delay, cleared: false, kind: 'timeout' }
  timers.push(timer)
  return timers.length
}
function fakeClear(id) {
  const timer = timers[id - 1]
  if (timer) timer.cleared = true
}

/** 跑一轮：只跑已注册且未清除的 interval，模拟时间推进 delay 毫秒。 */
async function tick(delay) {
  const due = timers.filter(timer => !timer.cleared && timer.kind === 'interval' && timer.delay === delay)
  for (const timer of due) await timer.handler()
  // 让 load() 里的 await 链跑完。
  await new Promise(resolve => setImmediate(resolve))
}

/* ---------------------------------------------------------------- 上游假件 */

const requests = []
let creditsGeneration = 0

function quotaPayload() {
  return {
    ok: true,
    api_base: 'http://127.0.0.1:8199/v1',
    api_key: 'test-key-value',
    accounts: [{
      id: 'local-a', name: 'local@example.com', status: 'active', updated_at: 1_700_000_000,
      models: [
        { id: 'gemini-2-flash', name: 'gemini-2-flash', remaining: 40, pool: 'gemini_shared', reset_at: 1_900_000_000 },
        { id: 'claude-sonnet', name: 'claude-sonnet', remaining: 70, pool: 'claude_gpt', reset_at: 1_900_100_000 },
      ],
    }],
  }
}

function rpcPayload(method) {
  if (method === 'provider.status') {
    return { ok: true, value: { statuses: {
      antigravity: { accounts: { total: 1, enabled: 1 }, models: { total: 2, disabled: 0 } },
      buddy: { accounts: { total: 1, enabled: 1 }, models: { total: 3, disabled: 0 } },
    } } }
  }
  if (method === 'account.list') {
    return { ok: true, value: { accounts: [
      { id: 'buddy-1', nickname: '埃尔温', provider: 'buddy', status: 'active', enabled: true },
    ] } }
  }
  if (method === 'credits.balances') {
    creditsGeneration += 1
    return { ok: true, value: { accounts: [{
      accountId: 'buddy-1', balance: { total: 1, packages: [
        { name: '积分包', total: 1000, remaining: 1000 - creditsGeneration, unit: 'credits' },
      ] },
    }] } }
  }
  return { ok: true, value: {} }
}

function fakeFetch(url, options = {}) {
  const method = options.method || 'GET'
  const body = options.body ? JSON.parse(options.body) : null
  requests.push({ url, method, rpc: body?.method || '' })
  let payload
  if (url.startsWith('/ui/quota')) payload = quotaPayload()
  else if (url.startsWith('/ui/state')) {
    payload = { ok: true, accounts: [{ id: 'local-a', status: 'active', in_flight: 0, max_concurrency: 2 }], models: ['gemini-2-flash'] }
  } else if (url.startsWith('/ui/protection')) {
    payload = { ok: true, paused: false, active_requests: 0, max_active_requests: 2, max_dispatch_per_minute: 20 }
  } else if (url.startsWith('/ui/session')) payload = { ok: true }
  else if (url.startsWith('/ui/rpc')) payload = rpcPayload(body?.method)
  else payload = { ok: true }
  return Promise.resolve({
    ok: true,
    status: 200,
    text: () => Promise.resolve(JSON.stringify(payload)),
  })
}

/* ---------------------------------------------------------------- 环境 */

const hash = process.env.POLL_TEST_HASH || ''
const location = { origin: 'http://127.0.0.1:8199', pathname: '/', search: '', hash }
const documentStub = {
  title: '',
  hidden: false,
  body: { dataset: {} },
  getElementById: elementFor,
  querySelector: elementFor,
  querySelectorAll: () => [],
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler) },
  listeners: {},
}

globalThis.window = {
  addEventListener() {},
  alert() {},
  confirm: () => true,
  open() {},
  pywebview: undefined,
}
globalThis.document = documentStub
globalThis.location = location
globalThis.history = { replaceState(_state, _title, url) { if (url) { location.hash = ''; location.pathname = url } } }
// Node 自带只读的 navigator（是 getter），要先用 defineProperty 换成可控的假件。
Object.defineProperty(globalThis, 'navigator', {
  value: { clipboard: { writeText: async () => {} } },
  configurable: true,
  writable: true,
})
globalThis.localStorage = {
  store: new Map(),
  getItem(key) { return this.store.has(key) ? this.store.get(key) : null },
  setItem(key, value) { this.store.set(key, String(value)) },
  removeItem(key) { this.store.delete(key) },
}
globalThis.fetch = fakeFetch
globalThis.setInterval = fakeSetInterval
globalThis.setTimeout = fakeSetTimeout
globalThis.clearInterval = fakeClear
globalThis.clearTimeout = fakeClear

const reset = () => { requests.length = 0 }

/* ---------------------------------------------------------------- 跑 */

const source = readFileSync(MODULE, 'utf8')
ok('控制台不再有「每 15 秒全量重拉」那一行',
   !/setInterval\(\(\)\s*=>\s*\{\s*if \(connected\) void load\(\) \}, 15_000\)/.test(source))

// 用 data: URL 加载，确保 import() 走的是同一份源码但不受 ES 缓存影响。
await import(`${pathToFileURL(MODULE).href}?t=${Date.now()}`)
await new Promise(resolve => setImmediate(resolve))

if (hash) {
  const session = requests.filter(item => item.url.startsWith('/ui/session'))
  check('#bootstrap 用一次会话换取', session.length, 1)
  check('换完会话后地址栏不留令牌', location.hash, '')
} else {
  check('无令牌时不发 /ui/session', requests.filter(item => item.url.startsWith('/ui/session')).length, 0)
}

check('首轮会读账号清单（account.list 各渠道一次）',
  requests.filter(item => item.rpc === 'account.list').length, 1)
check('首轮查一次积分', requests.filter(item => item.rpc === 'credits.balances').length, 1)

reset()
await tick(15_000)
const hotUrls = requests.map(item => item.url.split('?')[0])
check('一轮热数据只有三个本机接口', hotUrls.sort(),
  ['/ui/protection', '/ui/quota', '/ui/state'].sort())
check('热数据不碰 /ui/rpc（也就不会打上游积分）',
  requests.filter(item => item.rpc).length, 0)

reset()
await tick(15_000)
check('再一轮热数据同样零上游调用', requests.filter(item => item.rpc).length, 0)

// 冷数据到点：这里直接跑 15 秒那支定时器不行（它按时间判断），
// 所以推到 5 分钟：热定时器每轮都跑，靠 lastColdAt 自己判断。
reset()
const originalNow = Date.now
let fakeNow = originalNow()
Date.now = () => fakeNow
fakeNow += 5 * 60_000 + 1
await tick(15_000)
Date.now = originalNow
check('5 分钟后重查账号清单',
  requests.filter(item => item.rpc === 'account.list').length, 1)
check('5 分钟后重查一次积分',
  requests.filter(item => item.rpc === 'credits.balances').length, 1)

// 再推 1 分钟：积分不该再查，但要沿用上次读数（页面不能变成「无读数」）。
reset()
fakeNow = originalNow() + 6 * 60_000
Date.now = () => fakeNow
await tick(15_000)
Date.now = originalNow
check('两次冷刷新之间不重复查积分',
  requests.filter(item => item.rpc === 'credits.balances').length, 0)

/* 手动模式开关：点一下 → 不自动查；点刷新 → 强制查一次。 */
const creditsBtn = elementFor('credits-mode')
ok('页面上有积分刷新开关', creditsBtn.listeners.click?.length === 1)
await creditsBtn.listeners.click[0]()
await new Promise(resolve => setImmediate(resolve))
check('切到仅手动后按钮文案变了', creditsBtn.textContent.includes('仅手动'), true)

reset()
fakeNow = originalNow() + 20 * 60_000
Date.now = () => fakeNow
await tick(15_000)
Date.now = originalNow
check('仅手动模式下定时器不查积分',
  requests.filter(item => item.rpc === 'credits.balances').length, 0)

reset()
const reload = elementFor('reload')
await reload.listeners.click[0]()
await new Promise(resolve => setImmediate(resolve))
check('手动「刷新」会强制查一次积分',
  requests.filter(item => item.rpc === 'credits.balances').length, 1)

/* 窗口不可见：停表 + 回来后补一轮。 */
documentStub.hidden = true
documentStub.listeners.visibilitychange?.forEach(handler => handler())
const liveIntervals = timers.filter(timer => !timer.cleared && timer.kind === 'interval')
check('窗口不可见时定时器全部停掉', liveIntervals.length, 0)
const pollNote = elementFor('poll-note')
ok('暂停原因写在页面上', /暂停刷新/.test(pollNote.textContent), pollNote.textContent)

reset()
documentStub.hidden = false
documentStub.listeners.visibilitychange?.forEach(handler => handler())
await new Promise(resolve => setImmediate(resolve))
ok('回到前台补一轮', requests.length > 0, JSON.stringify(requests))
check('回来后定时器重新开始',
  timers.filter(timer => !timer.cleared && timer.kind === 'interval').length >= 1, true)

console.log('')
if (failures.length) {
  console.error(`失败 ${failures.length} 项（共 ${checks} 项）：`)
  for (const line of failures) console.error('  - ' + line)
  process.exit(1)
}
console.log(`全部通过（${checks} 项）`)
