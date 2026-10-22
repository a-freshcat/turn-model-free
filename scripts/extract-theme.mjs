/**
 * Builds the CSS-variable layer the copied page needs to look like the plugin.
 *
 * The page is styled entirely with the harness's design tokens
 * (`--dsw-alias-*`, `--dsw-static-*`, `--dsw-mask-blur`). Those live in the DSH
 * web shell, not in the plugin, so a standalone copy renders colourless without
 * them. This script reads them out of the installed desktop app — the asar
 * `dsh-client-ui-theme` module — and writes the two token blocks the shell uses
 * for its light and dark themes, plus the handful of easing fallbacks.
 *
 *   node scripts/extract-theme.mjs
 *   node scripts/extract-theme.mjs --check
 */

import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..')

const CANDIDATE_ASARS = [
  process.env.DSH_ASAR,
  'D:/ai_ds/resources/app.asar',
  join(process.env.LOCALAPPDATA ?? homedir(), 'Programs', 'dsh', 'resources', 'app.asar'),
].filter(Boolean)

const THEME_ENTRY = '/dsh/node_modules/@deepseek-ai/dsh-client-ui-theme/lib/client.js'
const CHECK = process.argv.includes('--check')

/* ── minimal asar reader (header is JSON, entries are offset+size) ─────────── */

function readAsarEntry(asarPath, entryPath) {
  const fd = readFileSync(asarPath) // asar files are read whole here: ~180 MiB
  const headerSize = fd.readUInt32LE(12)
  const header = JSON.parse(fd.subarray(16, 16 + headerSize).toString('utf8').replace(/\0+$/, ''))
  const base = 8 + headerSize + 4
  let node = { files: header.files }
  for (const part of entryPath.split('/').filter(Boolean)) {
    node = node?.files?.[part]
    if (node === undefined) return undefined
  }
  if (node === undefined || node.files !== undefined) return undefined
  const start = base + Number(node.offset)
  return fd.subarray(start, start + node.size)
}

function findAsar() {
  for (const candidate of CANDIDATE_ASARS) {
    if (candidate !== undefined && existsSync(candidate)) return candidate
  }
  return undefined
}

/* ── token extraction ────────────────────────────────────────────────────── */

const asarPath = findAsar()
if (asarPath === undefined) {
  console.error('extract-theme: no app.asar found (set DSH_ASAR=/path/to/app.asar)')
  process.exit(2)
}

const bundle = readAsarEntry(asarPath, THEME_ENTRY)
if (bundle === undefined) {
  console.error(`extract-theme: ${THEME_ENTRY} not found inside ${asarPath}`)
  process.exit(2)
}
const source = bundle.toString('utf8')
const sourceSha = createHash('sha256').update(source, 'utf8').digest('hex')

/**
 * The theme module embeds its stylesheets as JS strings
 * (`var design_platform_css_default = "body{--dsw-static-…}"`). Pull those
 * bodies out and keep the ones that actually declare tokens.
 */
function cssStrings(text) {
  const found = []
  for (const match of text.matchAll(/=\s*("(?:[^"\\]|\\.)*")/g)) {
    let value
    try { value = JSON.parse(match[1]) } catch { continue }
    if (value.includes('--dsw-')) found.push(value)
  }
  return found
}

const sheets = cssStrings(source)
if (sheets.length === 0) {
  console.error('extract-theme: no token stylesheets found in the theme module')
  process.exit(2)
}

/** All `selector{--token:value;…}` blocks in a sheet, selector kept verbatim. */
function tokenBlocks(css) {
  const blocks = []
  const re = /([^{}]+)\{([^{}]*)\}/g
  let match
  while ((match = re.exec(css)) !== null) {
    const selector = match[1].trim()
    const body = match[2]
    if (!body.includes('--dsw-')) continue
    if (!selector.startsWith('body') && !selector.startsWith(':root') && !selector.startsWith('html')) continue
    blocks.push({ selector, declarations: decls(body) })
  }
  return blocks
}

function decls(body) {
  const out = []
  for (const raw of body.split(';')) {
    const index = raw.indexOf(':')
    if (index <= 0) continue
    const name = raw.slice(0, index).trim()
    const value = raw.slice(index + 1).trim()
    if (!name.startsWith('--')) continue
    out.push([name, value])
  }
  return out
}

const light = new Map()
const dark = new Map()
for (const sheet of sheets) {
  for (const block of tokenBlocks(sheet)) {
    const target = block.selector.includes('data-ds-dark-theme') || block.selector.includes('data-theme=dark')
      ? dark
      : block.selector === 'body' || block.selector === ':root' ? light : null
    if (target === null) continue
    for (const [name, value] of block.declarations) target.set(name, value)
  }
}

if (light.size === 0) {
  console.error('extract-theme: no light-theme tokens found')
  process.exit(2)
}

/**
 * Tokens the plugin's own CSS consumes but the theme does not define under that
 * exact name (`--dsw-alias-state-warning-primary` vs the theme's `state-warn-*`
 * family). Mapped explicitly rather than guessed from a prefix rule.
 */
const ALIASES = {
  '--dsw-alias-state-warning-primary': '--dsw-alias-state-warn-primary',
  '--dsw-alias-state-warning-secondary': '--dsw-alias-state-warn-secondary',
  '--dsw-alias-label-on-accent': null, // no theme equivalent: literal white below
}
const LITERALS = {
  '--dsw-alias-label-on-accent': '#ffffff',
}

for (const [name, from] of Object.entries(ALIASES)) {
  if (light.has(name)) continue
  if (from === null) continue
  if (light.has(from)) light.set(name, `var(${from})`)
  if (dark.has(from)) dark.set(name, `var(${from})`)
}
for (const [name, value] of Object.entries(LITERALS)) {
  if (!light.has(name)) light.set(name, value)
  if (!dark.has(name)) dark.set(name, value)
}

/** The plugin's easing tokens are declared on `.ofm_root` itself; listed here for the report. */
const EASINGS = {
  '--ease-instrument': 'cubic-bezier(.16,1,.3,1)',
  '--ease-settle': 'cubic-bezier(.34,1.4,.4,1)',
}

const render = map => [...map.entries()].map(([name, value]) => `  ${name}: ${value};`).join('\n')

/**
 * Absolute, `~`-abbreviated label. A path relative to this checkout would change
 * with the folder's depth, and then the same app version would generate
 * different bytes — a moved copy would fail its own `--check`.
 */
function sourceLabel(path) {
  const posix = String(path).replace(/\\/g, '/')
  const home = homedir().replace(/\\/g, '/')
  if (posix.startsWith(home + '/')) return `~${posix.slice(home.length)}`
  return posix
}

const output = `/* AUTO-GENERATED by scripts/extract-theme.mjs — DSH design tokens for the copied page.
 *
 * Source: ${sourceLabel(asarPath)} → ${THEME_ENTRY}
 * Bundle sha256: ${sourceSha.slice(0, 32)}…
 * Light tokens: ${light.size}, dark tokens: ${dark.size}
 *
 * The copied page styles itself with the harness's tokens, so a standalone copy
 * needs them. They are reproduced here rather than approximated: a missing
 * token means an invisible card, not a slightly different shade.
 */

:root,
body {
${render(light)}
}

body[data-ds-dark-theme] {
${render(dark)}
}

/* Easing tokens normally declared on the plugin's own \`.ofm_root\` wrapper. */
:root {
${Object.entries(EASINGS).map(([name, value]) => `  ${name}: ${value};`).join('\n')}
}
`

const outDir = join(ROOT, 'src', 'extracted')
mkdirSync(outDir, { recursive: true })
const path = join(outDir, 'theme-tokens.css')
const previous = existsSync(path) ? readFileSync(path, 'utf8') : null

// The bundle hash is part of the header, so compare after stripping it.
const normalize = text => text.replace(/Bundle sha256: [0-9a-f]+…/, '')
if (CHECK && previous !== null && normalize(previous) !== normalize(output)) {
  console.error(`extract-theme --check: ${relative(ROOT, path)} drifted from ${THEME_ENTRY}`)
  process.exit(1)
}
if (previous !== output) {
  writeFileSync(path, output)
  console.log(`extract-theme: wrote ${relative(ROOT, path)} (${light.size} light + ${dark.size} dark tokens)`)
} else {
  console.log(`extract-theme: ${relative(ROOT, path)} is up to date`)
}
