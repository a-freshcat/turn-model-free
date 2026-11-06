#!/usr/bin/env node
/**
 * 验证 Raccoon 退避补丁的行为。
 *
 * 直接从打好的副本里取出 `raccoonCooldownFor` / `raccoonCooldownStep` /
 * `raccoonClearRateLimitStreak` 三个函数来跑，而不是复制一份逻辑再测——
 * 那样测的是副本的逻辑，不是实际会加载的代码。
 *
 *   node scripts/test-raccoon-cooldown.mjs
 *   node scripts/test-raccoon-cooldown.mjs <隔离目录>/pack.js
 *
 * 默认读 `gateway/.pack/pack.raccoon-cooldown.js`（默认不启用，通常不存在）。
 * 显式给路径就能验证 `--isolated-dir` 产出的那一份：补丁真正的加载形态是
 * 「整目录复制后加载副本」，所以行为验证也该对着那份跑。
 */

import { existsSync, readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..')
const PATCHED = process.argv[2]
  ? resolve(process.argv[2])
  : join(ROOT, '.pack', 'pack.raccoon-cooldown.js')

if (!existsSync(PATCHED)) {
  console.error(`副本不存在，先跑 node scripts/patch-raccoon-cooldown.mjs --isolated-dir DIR\n缺少: ${PATCHED}`)
  process.exit(2)
}

const source = readFileSync(PATCHED, 'utf8')

// 抠出需要的片段原样求值：只依赖一个 Map，没有外部引用。
const start = source.indexOf('var RACCOON_RATE_LIMIT_COOLDOWN_MS')
const end = source.indexOf('function raccoonThinkingExtraBody')
if (start < 0 || end < 0 || end <= start) {
  console.error('无法在副本里定位退避相关代码段（上游结构已变化）。')
  process.exit(3)
}
const snippet = source.slice(start, end)

const clock = { now: 1_000_000 }
const factory = new Function('Date', `
  ${snippet}
  return { raccoonCooldownFor, raccoonCooldownStep, raccoonClearRateLimitStreak,
           raccoonRateLimitStreak, raccoonCooldownDeadlines,
           RACCOON_RATE_LIMIT_COOLDOWN_MS, RACCOON_RATE_LIMIT_COOLDOWN_MAX_MS };
`)
const api = factory({ now: () => clock.now })
function expire(account = ACCOUNT) {
  clock.now = api.raccoonCooldownDeadlines.get(`${account}|${MODEL}`)
}

const ACCOUNT = 'raccoon-test'
const MODEL = 'raccoon-model'
const failures = []

function check(name, actual, expected) {
  const ok = actual === expected
  if (!ok) failures.push(`${name}: 期望 ${expected}，实际 ${actual}`)
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}  (${actual}${ok ? '' : ` != ${expected}`})`)
}

const base = api.RACCOON_RATE_LIMIT_COOLDOWN_MS
const cap = api.RACCOON_RATE_LIMIT_COOLDOWN_MAX_MS

// 1) 无补丁时的原始行为：连续撞限流应逐次翻倍并封顶。
api.raccoonClearRateLimitStreak(ACCOUNT, MODEL)
const sequence = [1, 2, 3, 4, 5, 6, 7].map(() => api.raccoonCooldownFor(ACCOUNT, MODEL))
check('第 1 次限流 = 30s', sequence[0], base)
check('第 2 次限流 = 60s', sequence[1], base * 2)
check('第 3 次限流 = 120s', sequence[2], base * 4)
check('封顶不超过 10 分钟', Math.max(...sequence) <= cap, true)
check('第 7 次已到封顶', sequence[6], cap)

// 2) 新增行为：拿不到 200 时档位只能靠 step 下降。
api.raccoonClearRateLimitStreak(ACCOUNT, MODEL)
for (let i = 0; i < 5; i += 1) api.raccoonCooldownFor(ACCOUNT, MODEL) // 涨到档位 5
const beforeStep = api.raccoonRateLimitStreak.get(`${ACCOUNT}|${MODEL}`)
check('已累积到档位 5', beforeStep, 5)

api.raccoonCooldownStep(ACCOUNT, MODEL)
check('未到期的并发 429 不降级', api.raccoonRateLimitStreak.get(`${ACCOUNT}|${MODEL}`), 5)
expire()
api.raccoonCooldownStep(ACCOUNT, MODEL)
check('冷却到期的 429 降一级', api.raccoonRateLimitStreak.get(`${ACCOUNT}|${MODEL}`), 4)
check('降级后再撞 = 档位 5 的时长', api.raccoonCooldownFor(ACCOUNT, MODEL), base * 2 ** 4)

// 3) 到期后重选多次不再单向升级；即使每次仍是 429，也维持档位而非封顶。
api.raccoonClearRateLimitStreak(ACCOUNT, MODEL)
for (let i = 0; i < 5; i += 1) api.raccoonCooldownFor(ACCOUNT, MODEL)
for (let i = 0; i < 6; i += 1) {
  expire()
  api.raccoonCooldownStep(ACCOUNT, MODEL)
  api.raccoonCooldownFor(ACCOUNT, MODEL)
}
check('重复到期重选后仍维持档位 5', api.raccoonRateLimitStreak.get(`${ACCOUNT}|${MODEL}`), 5)
api.raccoonClearRateLimitStreak(ACCOUNT, MODEL)
check('200 成功后回到起点', api.raccoonCooldownFor(ACCOUNT, MODEL), base)

// 4) 空档位时 step 不应报错或产生负数。
api.raccoonClearRateLimitStreak(ACCOUNT, MODEL)
api.raccoonCooldownStep(ACCOUNT, MODEL)
check('空档位 step 不产生记录', api.raccoonRateLimitStreak.has(`${ACCOUNT}|${MODEL}`), false)

// 5) 成功路径仍归零（原有语义保留）。
api.raccoonClearRateLimitStreak(ACCOUNT, MODEL)
for (let i = 0; i < 4; i += 1) api.raccoonCooldownFor(ACCOUNT, MODEL)
api.raccoonCooldownStep(ACCOUNT, MODEL)
api.raccoonClearRateLimitStreak(ACCOUNT, MODEL)
check('归零后再撞 = 30s', api.raccoonCooldownFor(ACCOUNT, MODEL), base)

// 6) 两个账号互不干扰。
api.raccoonClearRateLimitStreak('a1', MODEL)
api.raccoonClearRateLimitStreak('a2', MODEL)
api.raccoonCooldownFor('a1', MODEL)
api.raccoonCooldownFor('a1', MODEL)
api.raccoonCooldownFor('a2', MODEL)
check('a1 档位 2', api.raccoonRateLimitStreak.get(`a1|${MODEL}`), 2)
check('a2 档位 1', api.raccoonRateLimitStreak.get(`a2|${MODEL}`), 1)

console.log('')
if (failures.length) {
  console.error(`失败 ${failures.length} 项：`)
  for (const line of failures) console.error('  - ' + line)
  process.exit(1)
}
console.log('全部通过')
