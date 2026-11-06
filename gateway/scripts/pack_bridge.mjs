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
import { fileURLToPath, pathToFileURL } from 'node:url'
import { existsSync, readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { homedir } from 'node:os'
import net from 'node:net'
import path from 'node:path'
import readline from 'node:readline'
import { spawn } from 'node:child_process'
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

const req = dshRequire()
const { Context } = req('@deepseek-ai/cordis')

// pack.js 的每个驱动 extends Service，构造器会调 ctx.reflect.provide，
// 所以 ctx 必须是真正的 cordis Context，不能是普通对象。
const logs = []
const ctx = new Context()
ctx.logger = Object.assign(ctx.logger ?? {}, {
  info: (...a) => logs.push(['info', ...a]),
  warn: (...a) => logs.push(['warn', ...a]),
  error: (...a) => logs.push(['error', ...a]),
  debug: () => {},
})

// ── 凭据必须是真的那个库 ──
//
// 曾经这里是一个内存 `Map`：桥启动时为空，`credits.balances` 对每个账号都
// 报「凭据未配置」，对话也发不出去 —— 而账号明明存在、状态也正常。根因是
// 账号凭据（`WORKBUDDY_ACCOUNT_*` 之类）由 DSH 写在 `~/.dsh/.credentials.yaml`
// 的 `refs` 下，这个 Map 从来没有读过那个文件。
//
// 挂载宿主自己的 `dsh-credentials-local` 才是对的：读走文件，写（登录完成后
// `ctx.credentials.set`）也落回同一个文件，于是桥里登录的账号 DSH 那边立即
// 可见，反之亦然。
//
// **为什么要在独立的 Context 里挂。** `dsh-credentials-local` 是个 cordis
// Service，`ctx.plugin()` 会把 `credentials` 注册进 fiber store；而 pack.js 是
// 直接 `apply(ctx)` 调用的裸模块，它读 `ctx.credentials` 时走的不是同一套
// inject 解析，于是抛 `cannot get property "credentials" without inject`。
// 在一个隔离的 Context 里完成初始化，再把**已成形的服务对象**作为普通属性
// 交给主 ctx，两边各自成立，不需要谁迁就谁的解析规则。
const CREDENTIALS_FILE = process.env.DSH_CREDENTIALS_PATH
  || path.join(homedir(), '.dsh', '.credentials.yaml')
let credentialsReady = 'unavailable'
try {
  const credentialsLocal = await import(
    pathToFileURL(req.resolve('@deepseek-ai/dsh-credentials-local')).href)
  const credentialContext = new Context()
  credentialContext.logger = ctx.logger
  credentialContext.plugin(credentialsLocal.default, { path: CREDENTIALS_FILE })
  // 服务在 plugin() 之后异步就绪；文件解析失败不会抛到这里，故以一次真实
  // describe 作为可用性判据（缺失的 ref 返回 {configured:false}，不抛）。
  let service
  for (let i = 0; i < 80; i += 1) {
    service = credentialContext.get('credentials')
    if (typeof service?.describe === 'function') break
    await new Promise(resolve => setTimeout(resolve, 25))
  }
  if (typeof service?.describe !== 'function') throw new Error('credentials service did not start')
  await service.describe('__BRIDGE_PROBE__')
  ctx.credentials = service
  credentialsReady = CREDENTIALS_FILE
} catch (error) {
  const fallback = new Map()
  ctx.credentials = {
    async get(key) { return fallback.get(key) },
    async set(key, value) { fallback.set(key, value) },
    async delete(key) { fallback.delete(key) },
    async list() { return [...fallback.keys()] },
    async resolve(key) {
      const value = fallback.get(key)
      return value === undefined ? undefined : { value, ref: key }
    },
    async describe() { return { configured: false, writable: true } },
    async unset(key) { fallback.delete(key) },
  }
  credentialsReady = `memory-fallback: ${String(error?.message ?? error)}`
  logs.push(['error', `credentials unavailable, falling back to memory: ${credentialsReady}`])
}
ctx.llm = {
  registerAdapter() {},
  listModels() { return [] },
  listProviders() { return [] },
  resolveModelInfo() { return undefined },
  stream() { return (async function* () {})() },
}
ctx.commands = { register() {} }

// ── 附件服务：图片入站的唯一通道 ──
//
// pack.js 的 OpenAI 网关解析 `image_url` 时先取 `ctx.get("attachments")`：拿不到
// 就明确回 400「当前 profile 未装载附件服务（@deepseek-ai/dsh-attachment-local）」。
// DSH 桌面端的 8326 网关挂着这个服务，桥自己拉起的 18326 网关没有 —— 同一份
// pack.js，桌面端能收图，桥这边一律 400。
//
// `ctx.get()` 读的是 cordis 的 isolate store（`_getImpl`），属性赋值
// （`ctx.attachments = x`，credentials 那种用法）它看不见。所以必须用真正的
// `ctx.plugin()` 注册到**主 ctx** 的 store —— pack.js 的 `mountOpenAiGateway(ctx)`
// 闭包捕获的就是这个 ctx。
const ATTACHMENTS_HOME = process.env.DSH_HOME || path.join(homedir(), '.dsh')
let attachmentsReady = 'unavailable'
try {
  const attachmentLocal = await import(
    pathToFileURL(req.resolve('@deepseek-ai/dsh-attachment-local')).href)
  ctx.plugin(attachmentLocal.default, { dshHome: ATTACHMENTS_HOME })
  let attachments
  for (let i = 0; i < 200; i += 1) {
    attachments = ctx.get('attachments')
    if (typeof attachments?.saveImage === 'function') break
    await new Promise(resolve => setTimeout(resolve, 25))
  }
  if (typeof attachments?.saveImage !== 'function') throw new Error('attachments service did not start')
  attachmentsReady = path.join(ATTACHMENTS_HOME, 'attachments', 'v1')
} catch (error) {
  attachmentsReady = `unavailable: ${String(error?.message ?? error)}`
  logs.push(['warn', `attachments unavailable, image input will be rejected: ${attachmentsReady}`])
}

// ── 绝不让桥抢走 DSH 的 openai-gateway 端口 ──
//
// pack.js 的 `apply()` 会顺带启动它自己的 OpenAI 兼容网关，默认监听
// 127.0.0.1:8326 —— 那正是 DSH 里这个网关的端口。桥是个只做登录/查询转发的
// 短命子进程，两个进程抢同一个端口的结果是：谁先起来谁占着，另一个 bind 失败
// 后**静默**降级。实测的现象是 DSH 的 8326 被桥占据，`/v1/models` 回空、
// 对话回 502 `upstream stream ended before finish`——看起来像插件坏了，
// 其实是端口被一个本该无足轻重的辅助进程占了。
//
// 桥不需要那个网关：它只用 `channel-pack` 这一组 RPC，模型清单由网关进程
// 自己去问 8326。所以这里显式关掉。调用方若确实想让桥带网关，给
// `DSH_OPENAI_GATEWAY_ENABLED=1` 即可，但那不是默认。
if (process.env.DSH_OPENAI_GATEWAY_ENABLED === undefined) {
  process.env.DSH_OPENAI_GATEWAY_ENABLED = '0'
}

const registered = []
ctx.reflect.provide('connection', { fetch: { register(spec) { registered.push(spec) } } })

let endpoint = null
let seq = 0

// 默认**只加载插件目录里的原件**。
//
// 曾经的错误做法：把打过补丁的 pack.js 复制到 `gateway/.pack/` 再加载。pack.js
// 会按**自身所在目录**读取同级资源（如 `qoder-auth-wasm.wasm`），副本目录里没有
// 这些文件，于是 Qoder 等渠道直接报 ENOENT。补丁只能在与原件同一目录、且同级
// 资源可见的前提下生效；在那之前保持原件不动。
//
// `DSH_PACK_PATCHED` 仅供隔离实验显式指定，默认绝不启用。
function patchedPackPath() {
  const explicit = process.env.DSH_PACK_PATCHED
  if (explicit && existsSync(explicit)) return explicit
  return null
}

// ── 备用网关（`gateway.ensure`）──
//
// 与 8326 的归属关系见上方注释：8326 永远是 DSH 的，桥只用 18326。
const FALLBACK_GATEWAY_PORT = Number(process.env.DSH_FALLBACK_GATEWAY_PORT || 18326)
let gatewayStarting = null
let gatewayStarted = false
let gatewayChild = null

/** 端口是否已有人在监听。用一次真实 TCP 连接判断，比自己记状态可靠。 */
function isPortOpen(port) {
  return new Promise((resolve) => {
    const socket = net.connect({ host: '127.0.0.1', port })
    const done = (ok) => {
      socket.removeAllListeners()
      socket.destroy()
      resolve(ok)
    }
    socket.setTimeout(800)
    socket.once('connect', () => done(true))
    socket.once('timeout', () => done(false))
    socket.once('error', () => done(false))
  })
}

/** 插件网关的调用密钥：与 Python 侧读的是同一个文件。 */
function readGatewayKey() {
  try {
    const file = path.join(homedir(), '.dsh', 'openai-gateway', 'api-key')
    const value = readFileSync(file, 'utf8').trim()
    return value || undefined
  } catch {
    return undefined
  }
}

/**
 * 在指定端口拉起 pack.js 自带的 OpenAI 网关。
 *
 * 做法是**另起一个进程**，而不是在当前进程里再 `apply()` 一次：pack.js 的
 * `apply()` 会注册 cordis 服务，同一个进程注册两遍会互相覆盖，反而是新的
 * 故障源。子进程只需带上端口环境变量，其余沿用同一套 pack 与 loader。
 */
async function startGatewayOn(port) {
  if (gatewayStarted) return
  const script = fileURLToPath(new URL('./pack_gateway.mjs', import.meta.url))
  const child = spawn(process.execPath, ['--import', loaderUrl(), script], {
    env: {
      ...process.env,
      DSH_PACK_HOME: DSH_HOME,
      // 不传 DSH_PACK_PATCHED：备用网关必须和桥使用同一份**原件**，
      // 否则 pack.js 的同级资源（wasm 等）又会对不上。
      DSH_OPENAI_GATEWAY_ENABLED: '1',
      DSH_OPENAI_GATEWAY_PORT: String(port),
    },
    // stdin 给 'ignore'：这个子进程不需要任何输入，只等一个退出信号。
    // 它**不能**把 stdin 关闭当成退出条件（曾因此刚启动就退出）。
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  })
  gatewayChild = child
  let stderr = ''
  child.stderr.on('data', (chunk) => { stderr += String(chunk) })
  child.stdout.on('data', () => {})
  child.on('exit', () => {
    // 子进程自己退了（崩溃/端口被占）：允许下一次 ensure 重新拉起。
    gatewayStarted = false
    gatewayStarting = null
    if (gatewayChild === child) gatewayChild = null
  })
  gatewayStarted = true
  // 等端口真的可连，而不是等一个固定的 sleep。首次启动要装载凭据/附件服务，
  // 实测 3~8 秒，所以给到 30 秒。
  for (let i = 0; i < 300; i += 1) {
    if (await isPortOpen(port)) return
    if (!gatewayStarted) {
      throw new Error(`备用网关进程提前退出${stderr ? `：${stderr.slice(0, 300)}` : ''}`)
    }
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
  throw new Error(`备用网关未能在端口 ${port} 上就绪${stderr ? `：${stderr.slice(0, 300)}` : ''}`)
}

/** 桥退出时把网关子进程一并带走，别留孤儿占着端口。 */
function killGatewayChild() {
  if (gatewayChild === null) return
  try {
    gatewayChild.kill()
  } catch {
    // 已经退出就忽略。
  }
  gatewayChild = null
  gatewayStarted = false
  gatewayStarting = null
}

function loaderUrl() {
  return pathToFileURL(fileURLToPath(new URL('./pack_register.mjs', import.meta.url))).href
}

async function load() {
  if (credentialsReady === 'unavailable' || credentialsReady.startsWith('memory-fallback:')) {
    throw new Error('Persistent credential store is unavailable; bridge startup stopped to protect existing accounts')
  }
  const patched = patchedPackPath()
  const target = patched || PACK
  const mod = await import(pathToFileURL(target).href)
  await mod.apply(ctx, {})
  endpoint = registered.find(r => r.path === '/api/channel-pack')
  if (!endpoint) throw new Error('channel-pack endpoint not registered')
  return { name: mod.name, inject: mod.inject, patched: Boolean(patched) }
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
  // 哪些账号真的有凭据。
  //
  // `account.create` 会先往账号表里写一条「昵称等于 id」的占位记录，登录完成后
  // 才补上凭据。如果登录窗口在中途丢了（浏览器被关、进程重启、用户走开），
  // 那条记录会永久留着，形状与「正在登录」完全一致——网关只看昵称分不出来，
  // 于是页面上会多出一个点不动的「等待授权」账号。
  //
  // 判据取**凭据是否存在**：占位记录没有凭据，真账号有。这比「本次会话记不
  // 记得这个 id」可靠得多，后者会在进程重启后把旧占位当成新的。
  async 'credential.presence'(payload) {
    // An unavailable persistent store is unknown, not proof of missing tokens.
    if (credentialsReady === 'unavailable' || credentialsReady.startsWith('memory-fallback:')) {
      throw new Error('Persistent credential store is unavailable; account cleanup is suspended')
    }
    const ids = Array.isArray(payload?.accountIds) ? payload.accountIds : []
    const provider = payload?.provider
    const listed = await rpc('account.list', { provider: PACK_PROVIDER(provider ?? '') })
    const byId = new Map((listed?.accounts ?? []).map(row => [row.id, row]))
    const present = {}
    for (const id of ids) {
      const ref = byId.get(id)?.credentialRef
      if (typeof ref !== 'string' || ref === '') { present[id] = false; continue }
      const described = await ctx.credentials.describe(ref)
      present[id] = described?.configured === true
    }
    return { present }
  },
  async 'account.create'(payload) {
    if (credentialsReady === 'unavailable' || credentialsReady.startsWith('memory-fallback:')) {
      throw new Error('Persistent credential store is unavailable; repair it before adding accounts')
    }
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
    if (credentialsReady === 'unavailable' || credentialsReady.startsWith('memory-fallback:')) {
      throw new Error('Persistent credential store is unavailable; login cannot be saved safely')
    }
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
  /**
   * 把插件自己的 OpenAI 兼容网关拉到**备用端口**上。
   *
   * 为什么需要它：对话转发目标是插件网关（默认 8326，由 DSH 桌面端持有）。
   * DSH 没开时那个端口是空的，网关于是只能回 `ConnectError` —— 而账号、令牌、
   * 积分全都正常，用户看到的是「转发失败」，会误以为号池坏了。
   *
   * 为什么必须是备用端口：8326 归 DSH，桥抢过来会让 DSH 自己 bind 失败并静默
   * 降级（见上方 `DSH_OPENAI_GATEWAY_ENABLED` 那段）。所以这里用 18326，只
   * 在 8326 确实没人监听时才拉起，且**绝不**去碰 8326。
   *
   * 实现上不能指望 pack.js 提供这个 RPC —— 它没有（`gateway.ensure` 会回
   * unknown method，Python 侧原先就是被这条空路径骗了）。桥必须自己起：
   * `apply()` 会启动网关，端口由 `DSH_OPENAI_GATEWAY_PORT` 决定。
   */
  async 'gateway.ensure'(payload) {
    const requested = Number(payload?.port ?? FALLBACK_GATEWAY_PORT)
    const port = Number.isInteger(requested) && requested > 1024 && requested < 65536
      ? requested
      : FALLBACK_GATEWAY_PORT
    // 目标端口已在监听：说明网关已经就绪（可能是别处起的），直接用。
    if (await isPortOpen(port)) {
      return { running: true, address: { host: '127.0.0.1', port }, apiKey: readGatewayKey(), spawned: false }
    }
    // 同一进程只起一次；重复调用复用同一个 Promise。
    if (gatewayStarting === null) {
      gatewayStarting = startGatewayOn(port).catch((error) => {
        gatewayStarting = null
        throw error
      })
    }
    await gatewayStarting
    const running = await isPortOpen(port)
    return {
      running,
      address: { host: '127.0.0.1', port },
      apiKey: readGatewayKey(),
      spawned: true,
    }
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
    emit({ ready: true, channels: CHANNELS, pack: info.name, path: PACK,
           patched: info.patched,
           credentials: credentialsReady, attachments: attachmentsReady })
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
      queue.then(() => { killGatewayChild(); process.exit(0) })
    }
  }
  // 桥被强杀/父进程消失时也要带走网关子进程，否则孤儿会占着 18326，
  // 下一次 ensure 会误判「已在运行」而连到一个即将死掉的监听。
  process.on('exit', killGatewayChild)
  process.on('SIGTERM', () => { killGatewayChild(); process.exit(0) })
  process.on('SIGINT', () => { killGatewayChild(); process.exit(0) })
  process.on('SIGHUP', () => { killGatewayChild(); process.exit(0) })
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
