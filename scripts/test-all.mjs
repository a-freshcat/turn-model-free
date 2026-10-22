/**
 * Runs the whole suite in one process, in a fixed order, and prints a summary.
 *
 * Order matters: the demo tests exercise the *built* artifact, so the build is
 * refreshed first, and the provenance tests run last because they are the ones
 * that would notice the extractor being out of date.
 *
 * The generator scripts run as child processes with `stdio: 'inherit'` — this
 * sandbox forbids capturing another program's piped output, so a plain
 * `spawnSync` (default `pipe`) fails with EPERM. Inheriting the terminal gives
 * the same observable result without a pipe.
 *
 *   node scripts/test-all.mjs                # build, then every suite
 *   node scripts/test-all.mjs --with-drift   # also fail if the copies are stale
 *   node scripts/test-all.mjs --no-build     # test the artifact as it sits on disk
 */

import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { runFiles } from '../src/test/run.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..')
const args = process.argv.slice(2)

function run(script, extra = [], options = {}) {
  const result = spawnSync(process.execPath, [join(ROOT, script), ...extra], { cwd: ROOT, stdio: 'inherit' })
  if (result.error !== undefined) {
    console.error(`could not start ${script}: ${result.error.message}`)
    process.exit(1)
  }
  if (result.status !== 0) {
    console.error(`\n${script} failed (exit ${result.status})`)
    process.exit(result.status ?? 1)
  }
  void options
}

/**
 * 依赖不在仓库里（见 .gitignore），所以干净检出后第一步是装。
 *
 * 用 `npm ci` 而不是 `npm install`：lock 文件里有 integrity 校验和，装出来的
 * 是逐字节相同的那一份（实测 85 个文件哈希全等）。
 */
/**
 * 依赖不在仓库里（见 .gitignore），所以干净检出后第一步是装。
 *
 * 用 `npm ci` 而不是 `npm install`：lock 文件里有 integrity 校验和，装出来的
 * 是逐字节相同的那一份（实测 85 个文件哈希全等）。
 *
 * Windows 上必须走 shell：Node 从 20 起不允许直接 spawn `.cmd`（会 EINVAL）。
 * 为了不触发 DEP0190（shell + args 的拼接告警），把整条命令拼成一个字符串传给
 * shell，而不是 shell:true 再另传 args 数组。
 */
function ensureDependencies() {
  const marker = join(ROOT, 'node_modules', 'react', 'package.json')
  if (existsSync(marker)) return
  console.log('node_modules 不存在，按 package-lock.json 安装依赖…')
  const command = `npm ci --no-audit --no-fund`
  const result = spawnSync(command, {
    cwd: ROOT,
    stdio: 'inherit',
    shell: process.platform === 'win32' ? true : false,
    // 非 Windows 上直接跑命令，不要多余 shell
    ...(process.platform === 'win32' ? {} : { shell: false }),
  })
  if (result.error !== undefined || result.status !== 0) {
    console.error('\n依赖安装失败。请在仓库根目录手动执行：npm ci')
    console.error('（测试需要 react / react-dom / scheduler，版本与校验和都在 package-lock.json 里）')
    process.exit(1)
  }
}

ensureDependencies()

if (!args.includes('--no-build')) run('scripts/build-demo.mjs')

if (args.includes('--with-drift')) {
  run('scripts/extract.mjs', ['--check'])
  run('scripts/extract-theme.mjs', ['--check'])
  run('scripts/merge-guard.mjs', ['--check'])
}

const suites = [
  // 防护层先跑：页面测试不依赖它，但它是最容易"看起来通过"的一块
  'src/test/guard.test.mjs',
  'src/test/provenance.test.mjs',
  'src/test/parity.test.mjs',
  'src/test/behaviour.test.mjs',
  'src/test/demo.test.mjs',
  'src/test/guard-demo.test.mjs',
]

const summary = await runFiles(suites)
process.exit(summary.failed.length === 0 ? 0 : 1)
