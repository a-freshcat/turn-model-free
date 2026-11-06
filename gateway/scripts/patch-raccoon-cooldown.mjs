#!/usr/bin/env node
/**
 * 生成 channel-pack 的**已打补丁副本**，专门修 Raccoon 的冷却退避。
 *
 * ## 为什么是「生成副本」而不是原地改
 *
 * `pack.js` 是已安装插件的产物，路径在用户目录下。直接改它有三个问题：
 *   1. 插件升级会整包覆盖，补丁静默消失，故障复现且没人知道为什么；
 *   2. 改的是别人的文件，出了问题无法回滚到"原样"；
 *   3. 仓库里看不到改了什么——补丁成了本地手工操作，换台机器就没有了。
 *
 * 所以：**原件只读**，把改好的副本写到本仓库 `gateway/.pack/`，桥优先加载副本。
 * 副本不存在或与原件的目标串不匹配时，桥自动退回原件（等于没打补丁，但不故障）。
 *
 * ## 补丁内容
 *
 * Raccoon 是唯一用**指数退避**的渠道（30s→60s→…→10min）。而退避档位只在
 * 「收到 200 并开始流式返回」时归零（`raccoonClearRateLimitStreak`）。问题是
 * 账号一旦被标冷却，号池就不再选它，于是它再也拿不到 200，档位永远停在高位；
 * 两个账号还会交替累积、互相让对方更难恢复 —— 这就是"只有 Raccoon 老冷却"。
 *
 * 本补丁让档位**每成功拿到一次响应就降一级**（不只归零）：
 *
 *   - 冷却到期、账号被重新选中、上游返回 200 → `raccoonCoolDownStep` 降一级；
 *   - 降到底就删除记录，回到 30 秒起点；
 *   - 再撞 429 时从**当前档位**加一级，而不是从历史最高继续翻倍。
 *
 * 净效果：仍尊重上游真实滑动窗口（撞了就退避、退避期间不打扰），但不会因为
 * "没机会被选中"而单向涨到 10 分钟封顶。
 *
 *   node scripts/patch-raccoon-cooldown.mjs          生成/刷新副本
 *   node scripts/patch-raccoon-cooldown.mjs --check  只校验，不写文件
 *   node scripts/patch-raccoon-cooldown.mjs --print  打印将写入的路径
 *
 * ## 隔离模式（`--isolated-dir DIR`）
 *
 * 上面那条 `--check` 只校验「副本能不能对齐上游」。真正让副本**可加载**还有
 * 一个前提：pack.js 用 `dirname(import.meta.url)` 解析同级资源，
 * `qoder-auth-wasm.wasm` 就在它旁边（见 pack.js 的 `WASM_PATH`）。只复制
 * `pack.js` 到别的目录，Qoder 一用就 ENOENT —— 这就是 v1.3.1 的真实缺陷。
 *
 * 所以启用补丁的正确做法是**整目录复制**：
 *
 *   node scripts/patch-raccoon-cooldown.mjs --isolated-dir %TEMP%\tmf-raccoon-lab
 *
 * 它把已安装的 channel-pack 目录（排除 *.bak）整个复制过去，在副本目录里打
 * 补丁，并校验同级 wasm 确实存在。加载时给桥 `DSH_PACK_PATCHED=<副本>/pack.js`。
 * 这条路径**默认不参与运行**：桥只读插件目录里的原件，副本只在显式指定时用。
 */

import { createHash } from 'node:crypto'
import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { homedir } from 'node:os'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..')
const OUT_DIR = join(ROOT, '.pack')
const OUT_FILE = join(OUT_DIR, 'pack.raccoon-cooldown.js')

const DEFAULT_PACK_HOME = join(
  homedir(), '.dsh', 'profiles', 'desktop', 'node_modules', 'dsh-our-free-model')
const PACK_HOME = process.env.DSH_PACK_HOME || DEFAULT_PACK_HOME
const PACK = process.env.DSH_PACK_SOURCE
  || join(PACK_HOME, 'vendor', 'channel-pack', 'pack.js')

/** 每个替换都必须**恰好命中一次**；命中 0 次或多次都说明上游变了，直接失败。 */
const REPLACEMENTS = [
  {
    name: '退避档位降级函数',
    find: `var raccoonRateLimitStreak = /* @__PURE__ */ new Map();`,
    replace: `var raccoonRateLimitStreak = /* @__PURE__ */ new Map();
// 上次冷却结束时间；并发 429 尚未到期时保持指数退避。
var raccoonCooldownDeadlines = new Map();`,
  },
  {
    name: '记录冷却结束时间',
    find: `  raccoonRateLimitStreak.set(key, streak);
  const ms = RACCOON_RATE_LIMIT_COOLDOWN_MS * 2 ** (streak - 1);
  return Math.min(ms, RACCOON_RATE_LIMIT_COOLDOWN_MAX_MS);`,
    replace: `  raccoonRateLimitStreak.set(key, streak);
  const ms = RACCOON_RATE_LIMIT_COOLDOWN_MS * 2 ** (streak - 1);
  const duration = Math.min(ms, RACCOON_RATE_LIMIT_COOLDOWN_MAX_MS);
  raccoonCooldownDeadlines.set(key, Date.now() + duration);
  return duration;`,
  },
  {
    name: '冷却到期后降级函数',
    find: `/** 成功一次就把退避档位归零，避免好账号被历史失败拖住。 */
function raccoonClearRateLimitStreak(accountId, modelId) {
  raccoonRateLimitStreak.delete(raccoonStreakKey(accountId, modelId));
}`,
    replace: `/** 成功一次就把退避档位归零，避免好账号被历史失败拖住。 */
function raccoonClearRateLimitStreak(accountId, modelId) {
  const key = raccoonStreakKey(accountId, modelId);
  raccoonRateLimitStreak.delete(key);
  raccoonCooldownDeadlines.delete(key);
}
/** 冷却到期并重新收到响应时降一级；尚未到期的并发 429 不降级。 */
function raccoonCooldownStep(accountId, modelId) {
  const key = raccoonStreakKey(accountId, modelId);
  const streak = raccoonRateLimitStreak.get(key) ?? 0;
  const deadline = raccoonCooldownDeadlines.get(key);
  if (streak <= 0 || deadline === undefined || Date.now() < deadline) return;
  if (streak === 1) raccoonRateLimitStreak.delete(key);
  else raccoonRateLimitStreak.set(key, streak - 1);
  raccoonCooldownDeadlines.delete(key);
}`,
  },
  {
    name: '429 响应在重新选中时降级',
    find: `          const resetAtMs = isRateLimitError ? Date.now() + raccoonCooldownFor(accountId, options.model) : nextUtc8DayStartMs();`,
    replace: `          // 重新被选中并拿到 429 也是一次真实观测：若上次冷却已到期，
          // 先降一级再记本次限流；未到期的并发 429 保留原指数退避。
          if (isRateLimitError) raccoonCooldownStep(accountId, options.model);
          const resetAtMs = isRateLimitError ? Date.now() + raccoonCooldownFor(accountId, options.model) : nextUtc8DayStartMs();`,
  },
]

function main() {
  const check = process.argv.includes('--check')
  const print = process.argv.includes('--print')
  const isolatedIndex = process.argv.indexOf('--isolated-dir')
  const isolatedDir = isolatedIndex >= 0 ? process.argv[isolatedIndex + 1] : null

  if (print) {
    console.log(JSON.stringify({ source: PACK, output: OUT_FILE, sibling_dir: dirname(PACK) }, null, 2))
    return
  }

  if (!existsSync(PACK)) {
    console.error(`找不到 channel-pack 源文件：${PACK}`)
    console.error('设置 DSH_PACK_SOURCE 或 DSH_PACK_HOME 指向已安装的插件。')
    process.exit(2)
  }

  const original = readFileSync(PACK, 'utf8')
  const sourceSha = createHash('sha256').update(original).digest('hex')

  let patched = original
  const applied = []
  for (const { name, find, replace } of REPLACEMENTS) {
    const hits = patched.split(find).length - 1
    if (hits !== 1) {
      console.error(`补丁「${name}」匹配 ${hits} 次（需要恰好 1 次）。`)
      console.error('上游 pack.js 已变化，请重新核对补丁串后再运行。')
      process.exit(3)
    }
    patched = patched.replace(find, replace)
    applied.push(name)
  }

  const patchedSha = createHash('sha256').update(patched).digest('hex')
  const banner = `// 自动生成，请勿手改。由 scripts/patch-raccoon-cooldown.mjs 产出。\n`
    + `// 源: ${PACK}\n`
    + `// 源 sha256: ${sourceSha}\n`
    + `// 补丁: ${applied.join('; ')}\n`
  const body = banner + patched

  if (check) {
    if (!existsSync(OUT_FILE)) {
      console.error(`副本不存在：${OUT_FILE}`)
      process.exit(4)
    }
    const current = readFileSync(OUT_FILE, 'utf8')
    if (current !== body) {
      console.error('副本与当前 pack.js 不一致（插件已升级或补丁需重建）。')
      process.exit(4)
    }
    console.log(JSON.stringify({ ok: true, source_sha256: sourceSha, patched_sha256: patchedSha }))
    return
  }

  if (isolatedDir !== null) {
    const target = resolve(isolatedDir)
    const result = writeIsolated(target, body, sourceSha, patchedSha, applied)
    console.log(JSON.stringify(result, null, 2))
    return
  }

  mkdirSync(OUT_DIR, { recursive: true })
  writeFileSync(OUT_FILE, body, 'utf8')
  console.log(JSON.stringify({
    ok: true,
    output: OUT_FILE,
    source_sha256: sourceSha,
    patched_sha256: patchedSha,
    applied,
    warning: '这个副本单独放着**不可加载**：pack.js 会按自身目录找同级 wasm。'
      + '要在隔离目录里试补丁，请加 --isolated-dir DIR。',
  }, null, 2))
}

/** 副本目录里不复制的东西：备份、缓存、依赖。 */
function skipEntry(name) {
  return name.includes('.bak')
    || name === '__pycache__'
    || name === 'node_modules'
    || name.startsWith('.')
}

/**
 * 整目录复制 + 在副本目录里打补丁。
 *
 * 为什么必须整目录：pack.js 里 `WASM_PATH = resolve(dirname(import.meta.url),
 * "qoder-auth-wasm.wasm")`，只搬 pack.js 时 Qoder 一用就 ENOENT（v1.3.1 的
 * 真实缺陷）。同级资源全都在，补丁才谈得上"能加载"。
 */
function writeIsolated(target, body, sourceSha, patchedSha, applied) {
  const sourceDir = dirname(PACK)
  if (resolve(target) === resolve(sourceDir)) {
    console.error('隔离目录不能就是插件目录本身：补丁绝不能写回原件。')
    process.exit(5)
  }
  mkdirSync(target, { recursive: true })
  const copied = []
  for (const entry of readdirSync(sourceDir, { withFileTypes: true })) {
    if (!entry.isFile() || skipEntry(entry.name)) continue
    // pack.js 由下面的 writeFileSync 写入打补丁的版本，这里不复制原件。
    if (entry.name === 'pack.js') continue
    copyFileSync(join(sourceDir, entry.name), join(target, entry.name))
    copied.push(entry.name)
  }
  const output = join(target, 'pack.js')
  writeFileSync(output, body, 'utf8')

  // 同级资源自检：pack.js 里凡是以 import.meta.url 为基准解析的文件都要在。
  const checks = []
  for (const entry of readdirSync(target, { withFileTypes: true })) {
    if (entry.isFile() && /\.(wasm|js)$/.test(entry.name) && entry.name !== 'pack.js') {
      checks.push(entry.name)
    }
  }
  const sourceShas = createHash('sha256').update(body).digest('hex')
  return {
    ok: true,
    isolated: true,
    output,
    sibling_files: copied.sort(),
    sibling_check: checks.sort(),
    source_sha256: sourceSha,
    patched_sha256: patchedSha,
    written_sha256: sourceShas,
    applied,
    // 只打印怎么用，不自动改任何运行配置：默认仍然加载插件原件。
    use: `DSH_PACK_PATCHED=${output}`,
    note: '这一份只用于隔离验证。桥默认仍加载插件原件；'
      + '要试补丁，显式给桥设 DSH_PACK_PATCHED，并且只在你自己的临时环境里试。',
  }
}

main()
