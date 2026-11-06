// 独立进程：只装载 channel-pack 并让它启动自己的 OpenAI 兼容网关。
//
// 为什么要单独一个进程（而不是在桥里再 `apply()` 一次）：
//   pack.js 的 `apply()` 会往 cordis Context 注册服务，同一个进程注册两遍会
//   互相覆盖——凭据、账号池、RPC 端点都会指向后注册的那份。桥需要保持自己那份
//   干净，所以网关放到独立进程里，两者只通过 127.0.0.1 上的 HTTP 通信。
//
// 为什么端口由环境变量给：
//   pack.js 自己读 `DSH_OPENAI_GATEWAY_PORT`。桥传 18326，绝不传 8326——8326
//   属于 DSH 桌面端，被抢走后 DSH 会 bind 失败并静默降级。
//
// 本脚本没有 RPC 语义，只有一个职责：把网关拉起来并保持进程存活。退出由父进程
// （桥）负责——桥退出后这里 stdin 关闭，直接结束。
import { pathToFileURL } from 'node:url'
import { existsSync } from 'node:fs'
import { homedir } from 'node:os'
import path from 'node:path'
import { dshRequire, findDshModules } from './dsh_modules.mjs'

const DEFAULT_PACK_HOME = path.join(
  homedir(), '.dsh', 'profiles', 'desktop', 'node_modules', 'dsh-our-free-model')
const DSH_HOME = process.env.DSH_PACK_HOME || DEFAULT_PACK_HOME
const PACK = process.env.DSH_PACK_PATCHED || path.join(DSH_HOME, 'vendor', 'channel-pack', 'pack.js')
const PORT = process.env.DSH_OPENAI_GATEWAY_PORT || '18326'

function fail(message) {
  process.stderr.write(`pack_gateway: ${message}\n`)
  process.exit(1)
}

async function main() {
  if (!process.env.DSH_NODE_MODULES) {
    const modules = findDshModules()
    if (modules) process.env.DSH_NODE_MODULES = modules
  }
  // 裸包名 @deepseek-ai/* 需要落到 DSH app 自带实现上，与桥走同一条解析路径。
  // 这里必须用 `dshRequire()` 而不是 `createRequire(import.meta.url)`：
  // 后者只会在本脚本所在目录向上找 node_modules，而 DSH 的实现在 app 目录里，
  // 直接跑会 `Cannot find module '@deepseek-ai/cordis'`。
  const req = dshRequire()
  const load = async (name) => import(pathToFileURL(req.resolve(name)).href)

  const { Context } = await load('@deepseek-ai/cordis')

  const ctx = new Context()
  ctx.logger = Object.assign(ctx.logger ?? {}, {
    info: () => {},
    warn: (...a) => process.stderr.write(`[warn] ${a.join(' ')}\n`),
    error: (...a) => process.stderr.write(`[error] ${a.join(' ')}\n`),
    debug: () => {},
  })

  // `ctx.llm` 必须是**真正**的 LlmRuntime。
  //
  // 桥里那个是空壳（`registerAdapter(){}`），因为桥只做登录/查询转发、从不发
  // 对话。但网关要发对话，空壳会让 pack.js 的 `registerAdapterIdempotent` 直接
  // 抛 `Cannot read properties of undefined (reading 'registerAdapter')`——
  // 适配器一个都没注册上，端口绑了也没用。
  //
  // `LlmRuntime` 是 cordis Service（不是普通类），必须用 `ctx.plugin()` 装载：
  // 直接 `new` 会缺 `reflect`，构造期就抛错。
  try {
    const llmMod = await load('@deepseek-ai/dsh-llm')
    ctx.plugin(llmMod.default ?? llmMod.LlmRuntime)
  } catch (error) {
    fail(`LLM 运行时不可用，无法启动备用网关：${error?.message ?? error}`)
  }
  let llm
  for (let i = 0; i < 120; i += 1) {
    llm = ctx.get('llm')
    if (typeof llm?.registerAdapter === 'function') break
    await new Promise((resolve) => setTimeout(resolve, 25))
  }
  if (typeof llm?.registerAdapter !== 'function') {
    fail('LLM 运行时未就绪（缺少 registerAdapter），无法启动备用网关')
  }
  ctx.commands = { register() {} }

  // 凭据：与桥一致，挂宿主真实的本地凭据库，否则网关读不到任何账号。
  try {
    const credentialsLocal = await load('@deepseek-ai/dsh-credentials-local')
    const credentialContext = new Context()
    credentialContext.logger = ctx.logger
    credentialContext.plugin(credentialsLocal.default, {
      path: process.env.DSH_CREDENTIALS_PATH || path.join(homedir(), '.dsh', '.credentials.yaml'),
    })
    let service
    for (let i = 0; i < 80; i += 1) {
      service = credentialContext.get('credentials')
      if (typeof service?.describe === 'function') break
      await new Promise((resolve) => setTimeout(resolve, 25))
    }
    if (typeof service?.describe !== 'function') throw new Error('凭据服务未就绪')
    ctx.credentials = service
  } catch (error) {
    fail(`凭据服务不可用：${error?.message ?? error}`)
  }

  // 附件服务：不收图就不影响对话，但缺了会让带图请求一律 400。
  try {
    const attachmentLocal = await load('@deepseek-ai/dsh-attachment-local')
    ctx.plugin(attachmentLocal.default, {
      dshHome: process.env.DSH_HOME || path.join(homedir(), '.dsh'),
    })
  } catch {
    // 可选增强：没有附件服务只会让图片请求失败，对话本身照常。
  }

  if (!existsSync(PACK)) fail(`找不到 pack.js：${PACK}`)
  const mod = await import(pathToFileURL(PACK).href)
  await mod.apply(ctx, {})
  process.stderr.write(`pack_gateway: 已请求在 127.0.0.1:${PORT} 启动网关\n`)

  // 保持存活。
  //
  // ⚠️ 不能靠 stdin 的 'close'/'end' 来退出：桥是用 `stdio: ['ignore', ...]`
  // 拉起本进程的，stdin 一开始就是关闭状态，那个事件会**立刻**触发，于是网关
  // 还没 bind 就退出了 —— 表现就是「ensure 报未就绪、18326 永远是 closed」。
  // 生命周期交给父进程：桥在退出时 kill 这个子进程。
  setInterval(() => {}, 1 << 30)
  if (process.stdin.isTTY === false) {
    // 有 stdin 管道时也只是忽略，不作为退出信号。
    try {
      process.stdin.resume()
    } catch {
      // 没有可用的 stdin 就纯靠定时器存活。
    }
  }
  process.on('SIGTERM', () => process.exit(0))
  process.on('SIGINT', () => process.exit(0))
  process.on('disconnect', () => process.exit(0))
}

main().catch((error) => fail(String(error?.stack ?? error)))
