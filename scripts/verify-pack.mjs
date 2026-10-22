// 校验**已安装**插件里那份 pack.js：它对不对，是移植版是否可信的前提。
import { existsSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const path = process.env.OFM_PACK_INSTALLED
  ?? join(homedir(), '.dsh/profiles/desktop/node_modules/dsh-our-free-model/vendor/channel-pack/pack.js')

if (!existsSync(path)) {
  console.error(`找不到已安装的 pack.js: ${path}`)
  console.error('可用 OFM_PACK_INSTALLED=<path> 指定；未安装插件时此脚本无意义。')
  process.exit(2)
}
const src = readFileSync(path, 'utf8')

// 1) 标识符清单（对照文档里提到的设计）
const ids = [...new Set([...src.matchAll(/\b(TG_[A-Z0-9_]+)/g)].map(m => m[1]))].sort()
console.log(`TG_* 标识符 ${ids.length} 个: ${ids.join(', ')}`)

// 2) 三档防护：文档说的是 L1 字面量 / L2 容错变体 / L3 NFKC。
const levels = {
  'L1 字面量替换': /TG_BRAND\b/.test(src) && /replace/.test(src),
  'L2 容错变体（零宽/空白）': /TG_ZW/.test(src) && /TG_WS/.test(src),
  'L3 NFKC 规范化': /normalize\(\s*["']NFKC["']\s*\)/.test(src),
}
for (const [name, ok] of Object.entries(levels)) console.log(`  ${name.padEnd(26)} ${ok ? '✅' : '❌'}`)

// 3) 升档预算：默认值取自用户规则文件的 fallback，形如 `maxEscalations: 2`
const budget = /TENCENT_GATE_MAX_ESCALATIONS/.test(src)
console.log(`  升档预算常量                ${budget ? '✅' : '❌'}`)
const defaults = [...src.matchAll(/maxEscalations\s*:\s*(\d+)\s*,/g)].map(m => Number(m[1]))
const baseline = [...src.matchAll(/baseLevel\s*:\s*(\d+)/g)].map(m => Number(m[1]))
console.log(`  maxEscalations 默认值       ${defaults.length ? [...new Set(defaults)].join(' / ') : '(未找到)'}`)
console.log(`  baseLevel 默认值            ${baseline.length ? [...new Set(baseline)].join(' / ') : '(未找到)'}`)
// 预算必须被夹在合理区间，否则升档可能失控
const clamp = /Math\.min\(4,\s*Math\.max\(0,\s*Number\(parsed\.maxEscalations\)\)\)/.test(src)
console.log(`  预算夹取 [0,4]              ${clamp ? '✅' : '❌'}`)

// 4) 真实 ESM 解析
try {
  await import(pathToFileURL(path).href)
  console.log('\nESM 加载 ✅ 成功（模块被执行）')
} catch (error) {
  const message = String(error.message)
  const line = message.split('\n')[0]
  if (/Cannot find (package|module)|ERR_MODULE_NOT_FOUND/.test(message)) {
    console.log(`\nESM 语法 ✅ 通过（在解析宿主依赖时停下，符合预期）`)
    console.log(`   细节: ${line.slice(0, 120)}`)
  } else if (/Unexpected|SyntaxError|Invalid/.test(message)) {
    console.log(`\nESM 语法 ❌ 语法错误: ${line.slice(0, 160)}`)
    process.exitCode = 1
  } else {
    console.log(`\nESM 加载 ⚠ 其他错误（非语法）: ${line.slice(0, 160)}`)
  }
}
