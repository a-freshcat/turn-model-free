/**
 * Bundles the standalone demo into one file that runs straight from `file://`.
 *
 * There is no bundler on this machine (no network, no pnpm), and the browser
 * cannot `import` a directory tree of ES modules from `file://` either. So this
 * script does the one thing needed: rewrite every local `import`/`export` into
 * a tiny registry, and inline the two npm packages the page needs (React 19 and
 * react-dom/client, production builds) with a CommonJS shim.
 *
 * Output: `dist/demo.js` (classic script) + `dist/demo.css`.
 *
 *   node scripts/build-demo.mjs
 *   node scripts/build-demo.mjs --check   # fail if the bundle is stale
 */

import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..')
const CHECK = process.argv.includes('--check')

const read = path => readFileSync(join(ROOT, path), 'utf8')

/* ── module registry ─────────────────────────────────────────────────────── */

/**
 * Modules are keyed by their repo-relative path. Each module's body runs inside
 * a function that receives `require`, `module` and `exports`, exactly like the
 * npm packages do; ES module syntax is rewritten to that shape.
 */
const MODULES = [
  'src/extracted/channels-page.js',
  'src/extracted/channels-dict.js',
  'src/app/page.mjs',
  'src/mock/pack.mjs',
  'src/demo/app.mjs',
  'src/guard/tencent-gate.js',
  'src/guard/pipeline.mjs',
  'src/demo/guard-lab.mjs',
]

/**
 * Package entry points, resolved to the *production CJS* files.
 *
 * The browser gets the same code React ships for production; `react-dom` (the
 * umbrella entry) must resolve to `react-dom.production.js`, not the client
 * bundle — they are different modules and the client bundle expects the
 * umbrella's shared internals to exist.
 */
const NPM_ENTRY = {
  react: 'node_modules/react/cjs/react.production.js',
  'react-dom/client': 'node_modules/react-dom/cjs/react-dom-client.production.js',
  'react-dom': 'node_modules/react-dom/cjs/react-dom.production.js',
  scheduler: 'node_modules/scheduler/cjs/scheduler.production.js',
}

/**
 * Rewrites a module's ESM syntax into the CJS shape the registry executes.
 * The input is generated code plus small hand-written modules, so the patterns
 * are narrow on purpose — anything unexpected throws instead of silently
 * producing a bundle that differs from what the tests exercised.
 */
function toCommonJs(source, label) {
  let out = source

  // import { a, b as c } from './x.mjs'
  out = out.replace(/^import\s*\{([^}]+)\}\s*from\s*['"]([^'"]+)['"]\s*$/gm, (_match, names, from) => {
    const bindings = names.split(',').map(entry => entry.trim()).filter(Boolean).map(entry => {
      const [name, alias] = entry.split(/\s+as\s+/)
      return alias === undefined ? name : `${name}: ${alias}`
    })
    return `const { ${bindings.join(', ')} } = require(${JSON.stringify(from)})`
  })
  // import X from './x.mjs'  (default import)
  out = out.replace(/^import\s+([A-Za-z_$][\w$]*)\s+from\s*['"]([^'"]+)['"]\s*$/gm,
    (_match, name, from) => `const ${name} = require(${JSON.stringify(from)}).default ?? require(${JSON.stringify(from)})`)
  // export async function name(  ->  async function name(
  const named = []
  out = out.replace(/^export\s+(async\s+function|const|let|function|class)\s+([A-Za-z_$][\w$]*)/gm, (_match, kind, name) => {
    named.push(name)
    return `${kind} ${name}`
  })
  // export { a, b as c }   (a re-export of locals — the names must already exist)
  out = out.replace(/^export\s*\{([^}]+)\}\s*$/gm, (_match, names) => {
    for (const entry of names.split(',').map(value => value.trim()).filter(Boolean)) {
      const [local, alias] = entry.split(/\s+as\s+/)
      named.push(alias === undefined ? local : `${local}: ${alias}`)
    }
    return ''
  })
  if (/^\s*export\s/m.test(out)) throw new Error(`build-demo: unhandled export syntax in ${label}`)
  // de-duplicate: a name may be listed both as a declaration and in an export block
  const unique = [...new Set(named)]
  const exports = unique.map(entry => {
    const [local, alias] = entry.split(/:\s*/)
    return alias === undefined ? `${local}: ${local}` : `${alias}: ${local}`
  })
  return `${out}\nmodule.exports = { ${exports.join(', ')} }`
}

/* ── registry runtime ────────────────────────────────────────────────────── */

const registry = []

const moduleSources = []
for (const path of MODULES) {
  moduleSources.push(`  ${JSON.stringify(path)}: (require, module, exports) => {\n${toCommonJs(read(path), path)}\n  },`)
}
for (const [name, path] of Object.entries(NPM_ENTRY)) {
  moduleSources.push(`  ${JSON.stringify(`npm:${name}`)}: (require, module, exports) => {\n${read(path)}\n  },`)
}

const resolveFrom = (from, request) => {
  if (from.startsWith('npm:')) return `npm:${request}`
  const base = dirname(from)
  const joined = request.startsWith('.') ? join(base, request) : request
  const normalized = joined.replace(/\\/g, '/')
  if (MODULES.includes(normalized)) return normalized
  if (NPM_ENTRY[request] !== undefined) return `npm:${request}`
  // './page.mjs' from src/test/... style paths
  for (const candidate of MODULES) {
    if (candidate.endsWith(`/${request.replace(/^\.\//, '')}`) || candidate === request) return candidate
  }
  throw new Error(`build-demo: cannot resolve ${request} from ${from}`)
}

const bundle = `/* AUTO-GENERATED by scripts/build-demo.mjs — do not edit.
 *
 * The 白嫖模型接入 page, its extracted copy of the plugin's code, the mock
 * channel-pack backend and React 19, in one classic script so the demo runs
 * from file:// with no server and no network.
 */
(function () {
  'use strict'
  const factories = {
${moduleSources.join('\n')}
  }
  const cache = new Map()
  function load(id) {
    if (cache.has(id)) return cache.get(id).exports
    const factory = factories[id]
    if (factory === undefined) throw new Error('module not found: ' + id)
    const module = { exports: {} }
    cache.set(id, module)
    factory(request => load(resolve(id, request)), module, module.exports)
    return module.exports
  }
  const maps = ${JSON.stringify(Object.fromEntries(MODULES.map(path => [path, path])))}
  function resolve(from, request) {
    if (from.startsWith('npm:')) return 'npm:' + request
    const base = from.split('/').slice(0, -1)
    const parts = request.split('/')
    for (const part of parts) {
      if (part === '.' || part === '') continue
      if (part === '..') base.pop()
      else base.push(part)
    }
    const joined = base.join('/')
    if (factories[joined] !== undefined) return joined
    if (factories['npm:' + request] !== undefined) return 'npm:' + request
    for (const candidate of Object.keys(factories)) {
      if (candidate.endsWith('/' + joined) || candidate === joined) return candidate
    }
    throw new Error('cannot resolve ' + request + ' from ' + from)
  }
  window.__OFM_DEMO__ = {
    load,
    maps,
    React: load('npm:react'),
    ReactDOM: load('npm:react-dom/client'),
    page: load('src/demo/app.mjs'),
    mock: load('src/mock/pack.mjs'),
    guard: load('src/guard/tencent-gate.js'),
    pipeline: load('src/guard/pipeline.mjs'),
    guardLab: load('src/demo/guard-lab.mjs'),
  }
})()
`

/* ── stylesheet ──────────────────────────────────────────────────────────── */

const css = `/* AUTO-GENERATED by scripts/build-demo.mjs — page styles + harness tokens. */
${read('src/demo/demo.css')}

/* ── the harness's design tokens (extracted from the desktop app) ── */
${read('src/extracted/theme-tokens.css')}

/* ── the copied page's own stylesheet (extracted verbatim) ── */
${read('src/extracted/channels.css')}
`

const outDir = join(ROOT, 'dist')
mkdirSync(outDir, { recursive: true })

const outputs = {
  'demo.js': bundle,
  'demo.css': css,
  // copied as-is: the entry point it calls is what the demo smoke test asserts
  'index.html': read('src/demo/index.html'),
}

let stale = false
for (const [name, content] of Object.entries(outputs)) {
  const path = join(outDir, name)
  const previous = existsSync(path) ? readFileSync(path, 'utf8') : null
  if (previous === content) continue
  if (CHECK) {
    stale = true
    console.error(`build-demo --check: ${relative(ROOT, path)} is stale`)
    continue
  }
  writeFileSync(path, content)
  console.log(`build-demo: wrote ${relative(ROOT, path)} (${(content.length / 1024).toFixed(0)} KiB)`)
}

const digest = createHash('sha256').update(bundle).digest('hex').slice(0, 16)
console.log(`build-demo: ${MODULES.length} local modules + ${Object.keys(NPM_ENTRY).length} npm entries, sha256 ${digest}`)

if (CHECK && stale) process.exit(1)
