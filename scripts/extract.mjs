/**
 * Regenerates the copied page from the installed plugin.
 *
 * The copy is verbatim: the extractor slices byte ranges out of
 * `dsh-our-free-model/client.js` and wraps the slice in a factory that receives
 * the handful of helpers the page used to close over. Nothing inside the slice
 * is rewritten, so a diff against a future plugin version stays readable — the
 * provenance record carries the source hashes and the exact character offsets.
 *
 *   node scripts/extract.mjs                 # installed desktop profile plugin
 *   node scripts/extract.mjs --source PATH   # any unpacked client.js
 *   node scripts/extract.mjs --check         # fail if the copy drifted from source
 */

import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..')

const DEFAULT_SOURCE = join(
  homedir(),
  '.dsh/profiles/desktop/node_modules/dsh-our-free-model/client.js',
)

const args = process.argv.slice(2)
const argOf = name => {
  const index = args.indexOf(name)
  return index === -1 ? undefined : args[index + 1]
}
const SOURCE = resolve(argOf('--source') ?? DEFAULT_SOURCE)
const CHECK = args.includes('--check')

if (!existsSync(SOURCE)) {
  console.error(`extract: source not found: ${SOURCE}`)
  process.exit(2)
}

const source = readFileSync(SOURCE, 'utf8')
const sourceSha = createHash('sha256').update(source, 'utf8').digest('hex')

function offsets(anchor) {
  const index = source.indexOf(anchor)
  if (index === -1) throw new Error(`extract: anchor not found: ${JSON.stringify(anchor.slice(0, 60))}`)
  return index
}

function lineAt(index) {
  return source.slice(0, index).split('\n').length
}

/** Slice from `startAnchor` up to (excluding) `endAnchor`; both must be unique. */
function sliceBetween(startAnchor, endAnchor, label) {
  const start = offsets(startAnchor)
  const end = offsets(endAnchor)
  if (end <= start) throw new Error(`extract: ${label}: end anchor precedes start`)
  return { text: source.slice(start, end), start, end, label }
}

const readVersion = () => {
  const packagePath = join(dirname(SOURCE), 'package.json')
  try {
    return JSON.parse(readFileSync(packagePath, 'utf8')).version ?? 'unknown'
  } catch {
    return 'unknown'
  }
}

/**
 * How the source path is written into the generated files.
 *
 * It must be **absolute** (with `~` abbreviating the home directory), never
 * relative to this checkout: a relative path changes with the folder's depth, so
 * the same plugin at the same version would produce different bytes before and
 * after moving the copy — and a backup that fails its own drift check after
 * being unzipped somewhere else is worse than no check at all.
 */
function sourceLabel(path) {
  const posix = path.replace(/\\/g, '/')
  const home = homedir().replace(/\\/g, '/')
  return posix.startsWith(home + '/') ? `~${posix.slice(home.length)}` : posix
}

/** Expands the `~` abbreviation back to a real path (used by callers). */
function sourcePath(label) {
  const posix = String(label).replace(/\\/g, '/')
  return posix.startsWith('~/') ? join(homedir(), posix.slice(2)) : posix
}

/* ── slices ─────────────────────────────────────────────────────────────── */

const slices = {
  icons: sliceBetween(
    '    const CHANNEL_ICONS = {',
    '    /** The plugin\'s own brand mark',
    'CHANNEL_ICONS',
  ),
  providers: sliceBetween(
    '    const CHANNEL_PROVIDERS = [',
    '    /** 与现有 RPC 的能力对应',
    'CHANNEL_PROVIDERS',
  ),
  credits: sliceBetween(
    '    const CHANNEL_CREDITS = {',
    '    /**\n     * The host RPC the pack registered',
    'CHANNEL_CREDITS',
  ),
  region: sliceBetween(
    '    // ── 白嫖模型接入页',
    '    // ── 数据看板 / 运行日志 / 网关设置',
    'channels page',
  ),
}

/**
 * The page region opens by re-declaring the three tables above it
 * (`CHANNEL_ICONS` / `CHANNEL_PROVIDERS` / `CHANNEL_CREDITS`); in the plugin
 * those are the *only* declarations and the region anchor sits on the comment
 * above them. Copying both would declare them twice, so the region is kept from
 * the first React component onward and the tables come from their own slices.
 */
const FIRST_COMPONENT = '    function sameChannelTransport('
const regionCut = slices.region.text.indexOf(FIRST_COMPONENT)
if (regionCut === -1) throw new Error('extract: first component not found inside the page region')
const regionWithoutIcons = slices.region.text.slice(regionCut)

/** A top-level `function name(...) { ... }` block, including its leading comment. */
function functionBlock(name) {
  const start = source.indexOf(`    function ${name}(`)
  if (start === -1) throw new Error(`extract: function ${name} not found`)
  const close = source.indexOf('\n    }\n', start)
  if (close === -1) throw new Error(`extract: end of function ${name} not found`)
  const end = close + '\n    }\n'.length
  return { text: source.slice(start, end), start, end }
}

const helpers = {
  kilo: functionBlock('kilo'),
  ago: functionBlock('ago'),
  showToast: functionBlock('showToast'),
  // `toastHost` sits directly above showToast and is only used by it.
  toastHost: (() => {
    const start = source.indexOf('    function toastHost() {')
    const close = source.indexOf('\n    }\n', start)
    if (close === -1) throw new Error('extract: end of toastHost not found')
    const end = close + '\n    }\n'.length
    return { text: source.slice(start, end), start, end }
  })(),
}

/** `const X = (props) => h(...)` one-liners (Switch / Pill). */
function arrowComponent(name) {
  const start = source.indexOf(`    const ${name} = `)
  if (start === -1) throw new Error(`extract: const ${name} not found`)
  // the expression ends at the blank line that follows it
  const blank = source.indexOf('\n\n', start)
  if (blank === -1) throw new Error(`extract: end of const ${name} not found`)
  const end = blank + 1
  return { text: source.slice(start, end), start, end }
}
const components = { Switch: arrowComponent('Switch'), Pill: arrowComponent('Pill') }

/* ── css ────────────────────────────────────────────────────────────────── */

const cssStart = offsets('    const CSS = SPONSOR_CSS + `')
const cssOpen = source.indexOf('`', cssStart)
const cssClose = source.indexOf('\n`', cssOpen)
const fullCss = source.slice(cssOpen + 1, cssClose)

/**
 * Splits a stylesheet into top-level blocks, keeping at-rules that contain
 * nested blocks (@media, @supports, @keyframes) intact.
 */
function parseBlocks(css, offset = 0) {
  const blocks = []
  let index = 0
  while (index < css.length) {
    const open = css.indexOf('{', index)
    if (open === -1) break
    const prelude = css.slice(index, open)
    let depth = 0
    let cursor = open
    for (; cursor < css.length; cursor += 1) {
      if (css[cursor] === '{') depth += 1
      else if (css[cursor] === '}') { depth -= 1; if (depth === 0) break }
    }
    const body = css.slice(open + 1, cursor)
    // `start` is an absolute offset into the source sheet, so blocks from a
    // nested parse can be ordered against top-level ones
    blocks.push({ prelude: prelude.trim(), body, raw: css.slice(index, cursor + 1), start: offset + index })
    index = cursor + 1
  }
  return blocks
}

const allBlocks = parseBlocks(fullCss)
const flatten = (list, out = []) => {
  for (const block of list) {
    out.push(block)
    if (block.prelude.startsWith('@') && !block.prelude.startsWith('@keyframes')) {
      out.push(...parseBlocks(block.body, block.start + block.raw.indexOf('{') + 1))
    }
  }
  return out
}
const flatBlocks = flatten(allBlocks)

/* Class names the copied page actually renders. */
const pageSource = [
  slices.icons.text, slices.providers.text, slices.credits.text, slices.region.text,
  helpers.kilo.text, helpers.ago.text, helpers.showToast.text, helpers.toastHost.text,
  components.Switch.text, components.Pill.text,
].join('\n')
const usedClasses = [...new Set([...pageSource.matchAll(/ofm[_-][A-Za-z0-9_-]+/g)].map(match => match[0]))]
  .filter(name => !name.startsWith('ofm_') || !/^ofm_(toast|recogrid)/.test(name) || true)

const keepClass = name => usedClasses.includes(name)

function selectorMatches(selector) {
  const names = [...selector.matchAll(/\.(ofm[_-][A-Za-z0-9_-]+)/g)].map(match => match[1])
  if (names.length === 0) return false
  return names.some(keepClass)
}

const kept = []
const keptKeyframes = new Set()
for (const block of flatBlocks) {
  if (block.prelude.startsWith('@keyframes')) continue
  if (block.prelude.startsWith('@')) continue // handled through the flat list
  if (!selectorMatches(block.prelude)) continue
  kept.push(block)
  for (const match of block.body.matchAll(/animation(?:-name)?\s*:\s*([^;]+)/g)) {
    for (const token of match[1].split(/[\s,]+/)) if (/^[A-Za-z][\w-]*$/.test(token)) keptKeyframes.add(token)
  }
}

/**
 * Variable closure.
 *
 * The kept rules animate with `var(--ease-settle)` and size with `var(--gap)`,
 * and both are declared on `.ofm_root` — a selector the page itself never
 * renders (the plugin's Shell owns it). Filtering by class alone would drop the
 * declaration block and leave every transition with an invalid duration, so
 * repeat until no rule declares a variable that a kept rule consumes. Dropped
 * variables that the *host* provides (`--dsw-*`) are left alone.
 */
const declaredIn = body => new Set([...body.matchAll(/(--[A-Za-z0-9-]+)\s*:/g)].map(match => match[1]))
const usedIn = text => new Set([...text.matchAll(/var\(\s*(--[A-Za-z0-9-]+)/g)].map(match => match[1]))

let changed = true
while (changed) {
  changed = false
  const needed = new Set()
  for (const block of kept) {
    for (const name of usedIn(block.body)) needed.add(name)
    for (const name of usedIn(block.prelude)) needed.add(name)
  }
  for (const block of flatBlocks) {
    if (block.prelude.startsWith('@') || kept.includes(block)) continue
    const declares = declaredIn(block.body)
    const missing = [...declares].filter(name => needed.has(name))
    if (missing.length === 0) continue
    // keep the block in source order: it is a variable declaration the kept
    // rules depend on, whatever its selector says
    kept.push(block)
    changed = true
  }
}
kept.sort((left, right) => left.start - right.start)

/* Keep the enclosing @media / @supports shells for anything retained. */
const keptSet = new Set(kept)
const keptMedia = new Set()
for (const block of allBlocks) {
  if (!block.prelude.startsWith('@media') && !block.prelude.startsWith('@supports')) continue
  const offset = block.start + block.raw.indexOf('{') + 1
  const inner = parseBlocks(block.body, offset).filter(rule => keptSet.has(rule))
  if (inner.length > 0) keptMedia.add(block)
}

const keyframeBlocks = flatBlocks.filter(block => block.prelude.startsWith('@keyframes')
  && keptKeyframes.has(block.prelude.replace('@keyframes', '').trim()))

/**
 * Rebuild a stylesheet in source order, keeping only retained blocks.
 *
 * Retained blocks keep their original text *verbatim* (including a selector
 * list broken across lines and the body's own formatting): a pretty-printer
 * here would mean the "copy" no longer matches the source it claims to
 * reproduce, and the parity test could not prove provenance.
 */
function renderCss() {
  const lines = []
  for (const block of allBlocks) {
    if (block.prelude.startsWith('@media') || block.prelude.startsWith('@supports')) {
      if (!keptMedia.has(block)) continue
      const offset = block.start + block.raw.indexOf('{') + 1
      const inner = parseBlocks(block.body, offset).filter(rule => keptSet.has(rule))
      lines.push(block.prelude + '{')
      for (const rule of inner) lines.push(rule.prelude + '{' + rule.body + '}')
      lines.push('}')
      continue
    }
    if (block.prelude.startsWith('@keyframes')) {
      if (keyframeBlocks.includes(block)) lines.push(block.raw)
      continue
    }
    if (block.prelude.startsWith('@')) { lines.push(block.raw); continue }
    if (keptSet.has(block)) lines.push(block.raw)
  }
  return lines.join('\n')
}

const subsetCss = renderCss()

/* ── dictionary ─────────────────────────────────────────────────────────── */

/**
 * The page's `chan.*` strings, taken out of the plugin's own DICT.
 *
 * The dictionary is a JS object literal, so it is evaluated (in an empty
 * context) rather than pattern-matched — that keeps the copy faithful even if a
 * value contains an escaped quote or a nested placeholder.
 */
const dictStart = source.indexOf('    const DICT = {')
const dictOpen = source.indexOf('{', dictStart)
let dictClose = -1
for (let index = dictOpen, depth = 0; index < source.length; index += 1) {
  const char = source[index]
  if (char === '{') depth += 1
  else if (char === '}') {
    depth -= 1
    if (depth === 0) { dictClose = index; break }
  }
}
if (dictClose === -1) throw new Error('extract: could not find the end of DICT')
const dictLiteral = source.slice(dictOpen, dictClose + 1)
const dictHash = createHash('sha256').update(dictLiteral, 'utf8').digest('hex')
const DICT = (0, eval)(`(${dictLiteral})`)
const pick = locale => Object.fromEntries(
  Object.entries(DICT[locale] ?? {}).filter(([key]) => key.startsWith('chan.')),
)
const chanZh = pick('zh')
const chanEn = pick('en')
const missingEn = Object.keys(chanZh).filter(key => !(key in chanEn))
const missingZh = Object.keys(chanEn).filter(key => !(key in chanZh))

/* ── generated files ────────────────────────────────────────────────────── */

const header = `/* AUTO-GENERATED by scripts/extract.mjs — do not edit by hand.
 *
 * Source: ${sourceLabel(SOURCE)}
 * Plugin version: ${readVersion()}
 * Source sha256: ${sourceSha}
 * Regenerate: node scripts/extract.mjs
 *
 * The body below is a byte-for-byte copy of the plugin's own page module. Only
 * the closing \`return\` and the factory signature are generated.
 */

export const PROVENANCE = ${JSON.stringify({
    source: sourceLabel(SOURCE),
    version: readVersion(),
    sourceSha256: sourceSha,
    // No timestamp here: this export lives in the generated module so
    // `--check` can compare the file byte for byte. The run time is recorded in
    // provenance.json instead.
    ranges: Object.fromEntries(Object.entries(slices).map(([key, slice]) => [key, {
      chars: [slice.start, slice.end],
      lines: [lineAt(slice.start), lineAt(Math.max(slice.start, slice.end - 1))],
      sha256: createHash('sha256').update(slice.text, 'utf8').digest('hex'),
      bytes: slice.text.length,
    }])),
    helpers: Object.fromEntries(Object.entries({ ...helpers, ...components }).map(([key, slice]) => [key, {
      chars: [slice.start, slice.end],
      lines: [lineAt(slice.start), lineAt(Math.max(slice.start, slice.end - 1))],
      sha256: createHash('sha256').update(slice.text, 'utf8').digest('hex'),
      bytes: slice.text.length,
    }])),
  }, null, 2)}

/**
 * Wraps the copied page in a factory.
 *
 * The copied code closed over a dozen identifiers from the plugin's module
 * scope. Instead of editing the copy to import them, the factory takes them as
 * parameters — the copy inside stays exactly as it shipped.
 *
 * setTimeout/setInterval are passed in as well (they are the same globals in a
 * browser) so a headless host can drive the page's polling with virtual time
 * instead of waiting 3 seconds per poll.
 */
export function createChannelsPage(env) {
  const {
    h, Fragment,
    useState, useEffect, useMemo, useRef, useCallback,
    Pill, Switch, showToast, kilo, ago, post,
    setTimeout = globalThis.setTimeout,
    clearTimeout = globalThis.clearTimeout,
    setInterval = globalThis.setInterval,
    clearInterval = globalThis.clearInterval,
  } = env

${slices.icons.text}${slices.providers.text}${slices.credits.text}${regionWithoutIcons}
  return {
    ChannelsPage, ChannelCard, CHANNEL_PROVIDERS, CHANNEL_CREDITS, CHANNEL_ICONS,
    sameChannelTransport, channelTransport, useChannelRpc, expiryText, closeLoginPopup,
  }
}

/* ── helpers copied verbatim from the same client.js ─────────────────────── */

export function createHelpers(env) {
  const { h } = env
  const document = env.document ?? globalThis.document

${helpers.toastHost.text}${helpers.showToast.text}${components.Switch.text}${components.Pill.text}${helpers.kilo.text}${helpers.ago.text}
  return { toastHost, showToast, Switch, Pill, kilo, ago }
}
`

const outDir = join(ROOT, 'src', 'extracted')
mkdirSync(outDir, { recursive: true })

const dictFile = `/* AUTO-GENERATED by scripts/extract.mjs — the 白嫖模型接入 page's own strings.
 *
 * Source: ${sourceLabel(SOURCE)}:${lineAt(dictStart)}
 * DICT sha256: ${dictHash}
 * Picked: ${Object.keys(chanZh).length} zh keys, ${Object.keys(chanEn).length} en keys
 * (every other page's strings are left behind).
 */

export const CHANNEL_DICT = ${JSON.stringify({ zh: chanZh, en: chanEn }, null, 2)}
`

/**
 * Where each copied block came from, in a form a test can re-check without
 * trusting this script: character offsets into the source plus the sha256 of
 * the exact slice. `lines` are 1-based and inclusive; `chars` is `[start, end)`.
 */
function record(slice) {
  return {
    chars: [slice.start, slice.end],
    lines: [lineAt(slice.start), lineAt(Math.max(slice.start, slice.end - 1))],
    sha256: createHash('sha256').update(slice.text, 'utf8').digest('hex'),
    bytes: slice.text.length,
  }
}

const files = {
  'channels-dict.js': dictFile,
  'channels-page.js': header,
  'channels.css': `/* AUTO-GENERATED by scripts/extract.mjs — CSS subset for the copied page.
 *
 * Source: ${sourceLabel(SOURCE)}:${lineAt(cssOpen)}
 * Kept: every rule whose selector mentions a class the page renders, the
 * @media shells around those rules, and the @keyframes they animate.
 * Dropped: ${allBlocks.length - kept.length - keyframeBlocks.length} unrelated rules (other pages of the plugin).
 */

${subsetCss}
`,
  // Deliberately timestamp-free: `--check` compares every generated file byte
  // for byte, so anything that changes per run would make the guard useless.
  // The source hash below already identifies exactly which build this came from.
  'provenance.json': JSON.stringify({
    source: sourceLabel(SOURCE),
    version: readVersion(),
    sourceSha256: sourceSha,
    usedClasses,
    dict: {
      sourceLine: lineAt(dictStart),
      sha256: dictHash,
      zhKeys: Object.keys(chanZh).length,
      enKeys: Object.keys(chanEn).length,
      missingEn,
      missingZh,
    },
    css: {
      sourceChars: fullCss.length,
      keptRules: kept.length,
      keptKeyframes: [...keptKeyframes].filter(name => keyframeBlocks.some(block => block.prelude.includes(name))),
      droppedRules: allBlocks.length - kept.length - keyframeBlocks.length,
    },
    ranges: Object.fromEntries(Object.entries(slices).map(([key, slice]) => [key, record(slice)])),
    helpers: Object.fromEntries(Object.entries({ ...helpers, ...components }).map(([key, slice]) => [key, record(slice)])),
  }, null, 2) + '\n',
}

let drifted = false
for (const [name, content] of Object.entries(files)) {
  const path = join(outDir, name)
  const previous = existsSync(path) ? readFileSync(path, 'utf8') : null
  if (previous === content) continue
  if (CHECK) {
    // Everything generated here is deterministic — including provenance.json,
    // which carries offsets and hashes but no timestamp — so a difference means
    // the copy is stale (or was edited by hand).
    drifted = true
    console.error(`extract --check: ${relative(ROOT, path)} differs from the installed plugin`)
    continue
  }
  writeFileSync(path, content)
  console.log(`extract: wrote ${relative(ROOT, path)} (${content.length} chars)`)
}

console.log(`extract: source v${readVersion()} sha256 ${sourceSha.slice(0, 16)}…`)
console.log(`extract: page slice ${slices.region.text.length} chars, ${usedClasses.length} classes, ${kept.length} css rules`)

if (CHECK && drifted) process.exit(1)
