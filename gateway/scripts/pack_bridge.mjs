// Node 登录桥：把 dsh-our-free-model 的 channel-pack 暴露成 stdio JSON-RPC。
//
// 为什么是子进程而不是重写：
//   12 家渠道的登录是 pack.js 里约 2000 行实测可用的实现（厂商端点、PKCE、
//   DPoP、二维码页、轮询节奏都在里面）。用 Python 重写等于把这份代码抄一遍，
//   并且从此要跟着厂商改两次。这里只做**转发**：Python 侧发 JSON，桥接进程
//   调 pack.js 自己的 RPC，原样返回。
//
// 协议（行分隔 JSON，双向）：
//   请求  {"id":"1","method":"account.create","payload":{"provider":"buddy"}}
//   响应  {"id":"1","ok":true,"value":{...}}
//         {"id":"1","ok":false,"error":{"message":"..."}}
//   就绪  {"ready":true,"channels":[...],"pack":{...}}
//
// pack.js 装在用户目录下，是已安装插件的产物。本进程只读它，不改任何文件；
// 裸包名 @deepseek-ai/* 通过 pack_loader.mjs 的解析钩子指向 DSH app 自带实现。
import { pathToFileURL } from 'node:url'
import { createRequire } from 'node:module'
import { homedir } from 'node:os'
import path from 'node:path'
import readline from 'node:readline'
import { dshRequire, findDshModules } from './dsh_modules.mjs'

// 插件装在用户目录下。默认路径由 DSH_HOME 推出，不写死任何人的绝对路径。
const DEFAULT_PACK_HOME = path.join(
  homedir(), '.dsh', 'profiles', 'desktop', 'node_modules', 'dsh-our-free-model')
const DSH_HOME = process.env.DSH_PACK_HOME || DEFAULT_PACK_HOME
const PACK = path.join(DSH_HOME, 'vendor', 'channel-pack', 'pack.js')

// 宿主 node_modules 的探测逻辑与 loader 共用同一份实现。
const DSH_MODULES = findDshModules()
const CALL_TIMEOUT_MS = Number(process.env.PACK_BRIDGE_TIMEOUT_MS || 120000)

const CHANNELS = ['codearts', 'buddy', 'workbuddy', 'lobsterai', 'qoder', 'qodercn',
  'trae', 'cline', 'loomy', 'raccoon', 'minimax', 'zcode', 'antigravity']

// antigravity 在 pack.js 里叫 gemini；对外仍用网关的 antigravity 名字。
const PACK_PROVIDER = (provider) => provider === 'antigravity' ? 'gemini' : provider

const emit = (obj) => process.stdout.write(JSON.stringify(obj) + '\n')

const { Context } = dshRequire()('@deepseek-ai/cordis')

// pack.js 的每个驱动 extends Service，构造器会调 ctx.reflect.provide，
// 所以 ctx 必须是真正的 cordis Context，不能是普通对象。
const store = new Map()
const logs = []
const ctx = new Context()
ctx.logger = Object.assign(ctx.logger ?? {}, {
  info: (...a) => logs.push(['info', ...a]),
  warn: (...a) => logs.push(['warn', ...a]),
  error: (...a) => logs.push(['error', ...a]),
  debug: () => {},
})
ctx.credentials = {
  async get(key) { return store.get(key) },
  async set(key, value) { store.set(key, value) },
  async delete(key) { store.delete(key) },
  async list() { return [...store.keys()] },
  async resolve(key) {
    const value = store.get(key)
    return value === undefined ? undefined : { value, ref: key }
  },
}
ctx.llm = {
  registerAdapter() {},
  listModels() { return [] },
  listProviders() { return [] },
  resolveModelInfo() { return undefined },
  stream() { return (async function* () {})() },
}
ctx.commands = { register() {} }

const registered = []
ctx.reflect.provide('connection', { fetch: { register(spec) { registered.push(spec) } } })

let endpoint = null
let seq = 0

async function load() {
  const mod = await import(pathToFileURL(PACK).href)
  await mod.apply(ctx, {})
  endpoint = registered.find(r => r.path === '/api/channel-pack')
  if (!endpoint) throw new Error('channel-pack endpoint not registered')
  return { name: mod.name, inject: mod.inject }
}

// pack.js 的校验约定：type、rpcId、method==="channel-pack"、payload.method、payload.payload。
async function rpc(method, payload) {
  const body = JSON.stringify({
    type: 'client-request',
    rpcId: `bridge-${++seq}`,
    method: 'channel-pack',
    payload: { method, payload },
  })
  const response = await endpoint.fetch(new Request('http://localhost/api/channel-pack', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body,
  }))
  const parsed = JSON.parse(await response.text())
  const result = parsed?.result
  if (result?.ok === true) return result.value
  const message = result?.error?.message ?? 'channel-pack 调用失败'
  throw new Error(String(message))
}

function withTimeout(promise, ms, label) {
  let timer
  const guard = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} 超时（${ms}ms）`)), ms)
    timer.unref?.()
  })
  return Promise.race([promise, guard]).finally(() => clearTimeout(timer))
}

const handlers = {
  async ping() {
    return { channels: CHANNELS, bridge: 'pack', pack: PACK }
  },
  async 'account.create'(payload) {
    const provider = payload?.provider
    if (typeof provider !== 'string' || !CHANNELS.includes(provider)) {
      throw new Error('未知渠道')
    }
    const value = await rpc('account.create', {
      ...payload,
      provider: PACK_PROVIDER(provider),
    })
    return value
  },
  async 'login.poll'(payload) {
    const value = await rpc('login.poll', {
      accountId: payload?.accountId,
      provider: PACK_PROVIDER(payload?.provider ?? ''),
    })
    return value
  },
  async 'account.list'(payload) {
    const value = await rpc('account.list', { provider: PACK_PROVIDER(payload?.provider ?? '') })
    return value
  },
  async 'account.delete'(payload) {
    return await rpc('account.delete', { accountId: payload?.accountId })
  },
  async 'account.update'(payload) {
    return await rpc('account.update', {
      accountId: payload?.accountId,
      patch: payload?.patch,
    })
  },
  // 其余方法一律原样转发，避免桥接层成为能力瓶颈。
  async forward(method, payload) {
    return await rpc(method, payload)
  },
}

async function handle(message) {
  const id = message?.id
  const method = message?.method
  try {
    if (typeof method !== 'string') throw new Error('缺少 method')
    const known = Object.prototype.hasOwnProperty.call(handlers, method)
    const result = known
      ? await withTimeout(handlers[method](message.payload ?? {}), CALL_TIMEOUT_MS, method)
      : await withTimeout(handlers.forward(method, message.payload ?? {}), CALL_TIMEOUT_MS, method)
    emit({ id, ok: true, value: result })
  } catch (error) {
    emit({ id, ok: false, error: { message: String(error?.message ?? error) } })
  }
}

async function main() {
  try {
    const info = await load()
    emit({ ready: true, channels: CHANNELS, pack: info.name, path: PACK })
  } catch (error) {
    emit({ ready: false, error: String(error?.message ?? error) })
    process.exit(1)
  }
  const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity })
  let inFlight = 0
  let closing = null
  let queue = Promise.resolve()
  const drainIfDone = () => {
    if (closing !== null && inFlight === 0) {
      queue.then(() => process.exit(0))
    }
  }
  rl.on('line', (line) => {
    const text = line.trim()
    if (!text) return
    let message
    try { message = JSON.parse(text) } catch {
      emit({ id: null, ok: false, error: { message: '请求不是合法 JSON' } })
      return
    }
    inFlight += 1
    // 串行执行：pack.js 的 pool 与 pending 表不是并发安全的。
    queue = queue.then(() => handle(message)).catch((error) => {
      emit({ id: message?.id ?? null, ok: false, error: { message: String(error?.message ?? error) } })
    }).finally(() => { inFlight -= 1; drainIfDone() })
  })
  // stdin 关闭只是「不会再有请求」，不等于「可以立刻退出」：
  // 直接 exit 会把还在跑的 account.create 砍断，Python 侧永远等不到回复。
  rl.on('close', () => { closing = true; drainIfDone() })
}

await main()
