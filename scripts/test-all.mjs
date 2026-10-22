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
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { runFiles } from '../src/test/run.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..')
const args = process.argv.slice(2)

function run(script, extra = []) {
  const result = spawnSync(process.execPath, [join(ROOT, script), ...extra], { cwd: ROOT, stdio: 'inherit' })
  if (result.error !== undefined) {
    console.error(`could not start ${script}: ${result.error.message}`)
    process.exit(1)
  }
  if (result.status !== 0) {
    console.error(`\n${script} failed (exit ${result.status})`)
    process.exit(result.status ?? 1)
  }
}

if (!args.includes('--no-build')) run('scripts/build-demo.mjs')

if (args.includes('--with-drift')) {
  run('scripts/extract.mjs', ['--check'])
  run('scripts/extract-theme.mjs', ['--check'])
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
