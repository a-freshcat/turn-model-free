/**
 * A tiny test runner: no dependencies, deterministic output, one process.
 *
 * Each test file default-exports `{ title, tests: [{ name, run }] }` where `run`
 * receives helpers and may be async. Exit code is 1 when any test failed.
 */

import { pathToFileURL } from 'node:url'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { readFileSync } from 'node:fs'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..', '..')

export function createAssert() {
  const fail = (message, extra) => {
    const error = new Error(message)
    error.extra = extra
    throw error
  }
  const assert = {
    ok(value, message = 'expected truthy') { if (!value) fail(message, { value }) },
    notOk(value, message = 'expected falsy') { if (value) fail(message, { value }) },
    equal(actual, expected, message = 'expected equal') {
      if (actual !== expected) fail(`${message}: ${JSON.stringify(actual)} !== ${JSON.stringify(expected)}`, { actual, expected })
    },
    deepEqual(actual, expected, message = 'expected deep equal') {
      const a = JSON.stringify(actual)
      const b = JSON.stringify(expected)
      if (a !== b) fail(`${message}\n  actual:   ${a?.slice(0, 600)}\n  expected: ${b?.slice(0, 600)}`)
    },
    includes(haystack, needle, message = 'expected to include') {
      if (!String(haystack).includes(needle)) fail(`${message}: ${JSON.stringify(needle)} not found in ${JSON.stringify(String(haystack).slice(0, 300))}`)
    },
    notIncludes(haystack, needle, message = 'expected not to include') {
      if (String(haystack).includes(needle)) fail(`${message}: ${JSON.stringify(needle)} unexpectedly found`)
    },
    match(value, pattern, message = 'expected to match') {
      if (!pattern.test(String(value))) fail(`${message}: ${JSON.stringify(String(value).slice(0, 300))} !~ ${pattern}`)
    },
    throws(fn, message = 'expected to throw') {
      try { fn() } catch (error) { return error }
      return fail(message)
    },
  }
  return assert
}

const C = {
  green: text => `\u001b[32m${text}\u001b[0m`,
  red: text => `\u001b[31m${text}\u001b[0m`,
  dim: text => `\u001b[2m${text}\u001b[0m`,
  bold: text => `\u001b[1m${text}\u001b[0m`,
}

export async function runFiles(files, options = {}) {
  const results = []
  const started = Date.now()
  for (const file of files) {
    const path = resolve(ROOT, file)
    let suite
    try {
      suite = (await import(pathToFileURL(path).href)).default
    } catch (error) {
      results.push({ file, name: '<load>', ok: false, error })
      console.log(`${C.red('✖')} ${file} failed to load\n  ${error.stack ?? error.message}`)
      continue
    }
    const title = suite.title ?? file
    console.log(`\n${C.bold(title)} ${C.dim(`(${file})`)}`)
    for (const test of suite.tests) {
      const assert = createAssert()
      try {
        await test.run({ assert, readFile: relative => readFileSync(join(ROOT, relative), 'utf8'), ...options })
        results.push({ file, name: test.name, ok: true })
        console.log(`  ${C.green('✓')} ${test.name}`)
      } catch (error) {
        results.push({ file, name: test.name, ok: false, error })
        console.log(`  ${C.red('✖')} ${test.name}`)
        const detail = String(error.extra === undefined ? error.message : `${error.message}`)
        for (const line of detail.split('\n')) console.log(`      ${line}`)
      }
    }
  }
  const failed = results.filter(result => !result.ok)
  const passed = results.length - failed.length
  const seconds = ((Date.now() - started) / 1000).toFixed(2)
  console.log(`\n${failed.length === 0 ? C.green('PASS') : C.red('FAIL')} ${passed}/${results.length} tests in ${seconds}s`)
  return { results, failed, passed, total: results.length }
}

if (process.argv[1] !== undefined && pathToFileURL(process.argv[1]).href === import.meta.url) {
  const files = process.argv.slice(2)
  if (files.length === 0) {
    console.error('usage: node src/test/run.mjs <test-file.mjs> [more…]')
    process.exit(2)
  }
  const summary = await runFiles(files)
  process.exit(summary.failed.length === 0 ? 0 : 1)
}

export { C, join, ROOT }
