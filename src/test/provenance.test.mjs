/**
 * Guards the copy against drift and missing pieces.
 *
 * The page is a byte-for-byte copy of the plugin's code, so the one thing that
 * can silently ruin it is the *extraction* going stale: someone updates the
 * plugin, or edits the generated file by hand. These tests re-derive the copy
 * from the installed `client.js` and compare.
 */

import { createHash } from 'node:crypto'
import { existsSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * 仓库根目录，锚到本文件位置。
 *
 * 早先这里用 `process.cwd()`，于是从别的目录运行
 * `node <repo>/scripts/test-all.mjs` 会去 `C:\Windows\src\...` 找文件并 ENOENT
 * —— "换个目录跑一遍"正是审查者会做的事，那样会得到满屏假红。
 */
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..')

const hash = text => createHash('sha256').update(text, 'utf8').digest('hex')

/**
 * `provenance.json` stores the source as `~/...` so the copy stays identical
 * wherever the folder sits (a relative path would change with the folder's
 * depth, and a backup that fails its own drift check after being unzipped
 * elsewhere is worse than no check). Expand it here.
 */
function resolveSource() {
  const explicit = process.env.OFM_CLIENT_PATH
  if (explicit !== undefined && explicit !== '') return explicit
  const provenance = JSON.parse(readFileSync(join(ROOT, 'src/extracted/provenance.json'), 'utf8'))
  const label = String(provenance.source).replace(/\\/g, '/')
  return label.startsWith('~/') ? join(homedir(), label.slice(2)) : label
}

export default {
  title: 'Provenance · 抄写没有漂移',
  tests: [
    {
      name: '生成物与已安装插件逐字节一致（extract 可重跑）',
      async run({ assert, readFile }) {
        const provenancePath = 'src/extracted/provenance.json'
        assert.ok(existsSync(join(ROOT, provenancePath)), 'provenance.json exists')
        const provenance = JSON.parse(readFileSync(join(ROOT, provenancePath), 'utf8'))
        assert.ok(provenance.sourceSha256.length === 64, 'source hash recorded')
        assert.ok(provenance.ranges.region.lines[0] > 0, 'source line range recorded')

        const sourcePath = resolveSource()
        if (!existsSync(sourcePath)) {
          // The installed plugin is the point of comparison; without it the
          // copy can still be checked for internal consistency (below).
          return
        }
        const source = readFileSync(sourcePath, 'utf8')
        assert.equal(hash(source), provenance.sourceSha256, 'installed client.js is the one the copy came from')

        const generated = readFile('src/extracted/channels-page.js')
        // Each recorded range, sliced out again by its exact character offsets,
        // must hash to the recorded value and appear verbatim in the generated
        // file — that is what "copy" means here.
        for (const [key, record] of Object.entries(provenance.ranges)) {
          const [start, end] = record.chars
          const slice = source.slice(start, end)
          assert.equal(hash(slice), record.sha256, `${key}: slice hash matches provenance`)
        }
        // the page region ships from its first component onward (the three tables
        // above it are copied from their own ranges)
        const [regionStart, regionEnd] = provenance.ranges.region.chars
        const region = source.slice(regionStart, regionEnd)
        const cut = region.indexOf('    function sameChannelTransport(')
        assert.ok(cut > 0, 'page region starts with the components')
        assert.ok(generated.includes(region.slice(cut).trimEnd()), 'page region copied verbatim')
        for (const key of ['icons', 'providers', 'credits']) {
          const [start, end] = provenance.ranges[key].chars
          assert.ok(generated.includes(source.slice(start, end).trimEnd()), `${key} table copied verbatim`)
        }
        // and the helper blocks carry their recorded hashes too
        for (const [key, record] of Object.entries(provenance.helpers)) {
          const [start, end] = record.chars
          assert.equal(hash(source.slice(start, end)), record.sha256, `${key}: helper hash matches provenance`)
        }
      },
    },
    {
      name: 'CSS 子集只包含原插件里真实存在的规则（无手写伪造）',
      async run({ assert, readFile }) {
        const provenance = JSON.parse(readFile('src/extracted/provenance.json'))
        const css = readFile('src/extracted/channels.css')
        assert.equal(provenance.css.keptRules, 162, 'rule count recorded')
        assert.ok(css.length > 28_000, 'stylesheet copied')
        assert.notIncludes(css, 'hand-written', 'no hand-written rules')
        // every selector must be one the plugin's own stylesheet has
        const source = resolveSource()
        if (existsSync(source)) {
          const client = readFileSync(source, 'utf8')
          const selectors = [...css.matchAll(/^([.#][^{}\n]+)\{/gm)].map(match => match[1].trim())
          assert.ok(selectors.length > 100, `selectors parsed (${selectors.length})`)
          const missing = selectors.filter(selector => !client.includes(selector))
          assert.deepEqual(missing.slice(0, 5), [], 'selectors all exist in client.js')
        }
      },
    },
    {
      name: '词条文件就是插件 DICT 的 chan.* 子集（70 × 2，无缺失）',
      async run({ assert, readFile }) {
        const provenance = JSON.parse(readFile('src/extracted/provenance.json'))
        // 用 provenance 自报的数字，避免把"当前有几句词条"写死成断言：
        // 插件每加一句 UI 文案，写死的数字就会变成假失败（上次 69→70 就踩到）。
        const zh = provenance.dict.zhKeys
        const en = provenance.dict.enKeys
        assert.ok(Number.isInteger(zh) && zh > 0, `zhKeys 是正整数 (${zh})`)
        assert.equal(en, zh, 'zh / en 数量相同')
        assert.deepEqual(provenance.dict.missingEn, [], 'no zh-only key')
        assert.deepEqual(provenance.dict.missingZh, [], 'no en-only key')
        const dict = readFile('src/extracted/channels-dict.js')
        assert.includes(dict, "export const CHANNEL_DICT")
        assert.equal((dict.match(/^\s{4}"chan\./gm) ?? []).length, zh * 2, 'both locales present')
      },
    },
    {
      name: '主题令牌来自安装包，不是手抄的颜色值',
      async run({ assert, readFile }) {
        const css = readFile('src/extracted/theme-tokens.css')
        assert.includes(css, 'AUTO-GENERATED by scripts/extract-theme.mjs')
        assert.includes(css, 'app.asar')
        assert.includes(css, 'body[data-ds-dark-theme]')
        const light = (css.match(/^\s{2}--dsw-/gm) ?? []).length
        assert.ok(light > 400, `token count (${light})`)
        // a missing token means an invisible card: spot-check the ones the page needs
        for (const token of [
          '--dsw-alias-bg-layer-1', '--dsw-alias-bg-layer-2', '--dsw-alias-bg-layer-3',
          '--dsw-alias-label-primary', '--dsw-alias-label-secondary', '--dsw-alias-label-tertiary',
          '--dsw-alias-border-l1', '--dsw-alias-border-l2', '--dsw-alias-bg-mask-1',
          '--dsw-alias-state-business-primary', '--dsw-alias-state-success-primary',
          '--dsw-alias-state-error-primary', '--dsw-alias-state-warning-primary',
          '--dsw-alias-label-on-accent', '--dsw-mask-blur',
        ]) {
          assert.includes(css, token + ':')
        }
        // and dark theme must actually redefine the layer colours
        const dark = css.slice(css.indexOf('body[data-ds-dark-theme]'))
        assert.includes(dark.slice(0, 6000), '--dsw-alias-bg-layer-1')
      },
    },
    {
      name: '页面用到的每个 CSS 变量都有定义（页面之外不背锅）',
      async run({ assert, readFile }) {
        const page = readFile('src/extracted/channels.css')
        const tokens = readFile('src/extracted/theme-tokens.css')
        const declaredInPage = new Set([...page.matchAll(/(--[A-Za-z0-9-]+)\s*:/g)].map(match => match[1]))
        const declaredInTheme = new Set([...tokens.matchAll(/(--[A-Za-z0-9-]+)\s*:/g)].map(match => match[1]))
        const consumed = new Set()
        for (const match of page.matchAll(/var\(\s*(--[A-Za-z0-9-]+)/g)) consumed.add(match[1])
        // `--chan-accent` is set inline by the component (`style={{'--chan-accent': accent}}`),
        // so it is defined per card at runtime rather than in the sheet.
        const runtimeOnly = new Set(['--chan-accent'])
        const undefinedVars = [...consumed].filter(name => !declaredInPage.has(name) && !declaredInTheme.has(name) && !runtimeOnly.has(name))
        assert.deepEqual(undefinedVars, [], 'every consumed variable is declared')
        const code = readFile('src/extracted/channels-page.js')
        for (const name of runtimeOnly) assert.includes(code, name, `${name} is set by the component`)
        // the easing tokens the animations depend on live on .ofm_root in the sheet
        for (const name of ['--ease-instrument', '--ease-settle', '--gap']) {
          assert.ok(declaredInPage.has(name), `${name} declared by the page stylesheet`)
        }
      },
    },
    {
      name: '迷你 DOM 提供页面用到的浏览器 API（测试环境不是摆设）',
      async run({ assert, readFile }) {
        const dom = readFile('src/runtime/mini-dom.mjs')
        for (const api of [
          'querySelector', 'querySelectorAll', 'addEventListener', 'removeEventListener',
          'dispatchEvent', 'appendChild', 'removeChild', 'insertBefore', 'setAttribute',
          'toggleAttribute', 'classList', 'dataset', 'textContent', 'isConnected',
        ]) {
          assert.includes(dom, api)
        }
        // and it refuses to pretend about what it does not implement
        assert.notIncludes(dom, 'innerHTML =')
      },
    },
    {
      name: '提取脚本与测试独立可跑：npm 脚本都指向真实文件',
      async run({ assert, readFile }) {
        const pkg = JSON.parse(readFile('package.json'))
        for (const [name, command] of Object.entries(pkg.scripts)) {
          const match = /node ([\w./-]+)/.exec(command)
          assert.ok(match, `${name} runs node`)
          assert.ok(existsSync(join(ROOT, match[1])), `${name}: ${match[1]} exists`)
        }
        assert.ok(pkg.scripts.test, 'test script')
        assert.ok(pkg.scripts.extract, 'extract script')
        assert.ok(pkg.scripts['check:drift'], 'drift check script')
        assert.ok(pkg.scripts.demo, 'demo build script')
      },
    },
    {
      name: '可搬迁：生成物里没有相对本目录的路径，解压到别处不会「误报漂移」',
      async run({ assert, readFile }) {
        // A relative path (`../../.dsh/profiles/...`) changes with the folder's
        // depth, so the same plugin at the same version would regenerate
        // different bytes after the copy moved — the drift check would then fail
        // on a perfectly good backup.
        const generated = [
          'src/extracted/channels-page.js',
          'src/extracted/channels-dict.js',
          'src/extracted/channels.css',
          'src/extracted/provenance.json',
          'src/extracted/theme-tokens.css',
        ]
        for (const path of generated) {
          const text = readFile(path)
          // a source label must be absolute or `~`-abbreviated
          for (const match of text.matchAll(/(?:Source: |"source": ")([^"\n]+)/g)) {
            const label = match[1].replace(/:\d+$/, '')
            assert.ok(
              label.startsWith('~') || /^[A-Za-z]:\//.test(label) || label.startsWith('/'),
              `${path}: source label must be absolute, got ${JSON.stringify(label)}`,
            )
            assert.notIncludes(label, '../', `${path}: no parent-relative label`)
          }
        }
        // and no absolute path may point *inside* this checkout, or moving the
        // folder would break the recorded provenance
        const root = ROOT.replace(/\\/g, '/')
        for (const path of generated) {
          assert.notIncludes(readFile(path).replace(/\\/g, '/'), root, `${path}: no self-referencing absolute path`)
        }
      },
    },
    {
      name: '可搬迁：演示页与所有资源都是相对路径（换目录、file:// 下都能开）',
      async run({ assert, readFile }) {
        const html = readFile('dist/index.html')
        // every src/href must be relative: an absolute path breaks on another machine
        for (const match of html.matchAll(/(?:src|href)="([^"]+)"/g)) {
          const url = match[1]
          assert.ok(!/^([A-Za-z]:|\/|\\\\|https?:)/.test(url), `absolute or remote resource: ${url}`)
          assert.ok(existsSync(join(ROOT, 'dist', url)), `dist/${url} exists`)
        }
        // and the bundle must not hard-code this checkout's location
        const bundle = readFile('dist/demo.js').replace(/\\/g, '/')
        assert.notIncludes(bundle, ROOT.replace(/\\/g, '/'), 'bundle has no absolute checkout path')
        // 演示页自包含：只需要 dist/，不需要 node_modules
        assert.ok(existsSync(join(ROOT, 'dist', 'demo.js')), 'demo bundle exists')
      },
    },
    {
      name: '依赖可复现：lock 文件锁死版本 + 校验和，仓库不提交 node_modules',
      async run({ assert, readFile }) {
        // 依赖曾经被塞进仓库（因为没有 registry），后来证实 registry 是通的，
        // 于是改成 lock + npm ci。这条测试守住"可复现"这件事。
        const lock = JSON.parse(readFile('package-lock.json'))
        assert.ok(lock.lockfileVersion >= 2, `lockfileVersion（${lock.lockfileVersion}）`)
        const pinned = {
          'node_modules/react': '19.2.3',
          'node_modules/react-dom': '19.2.3',
          'node_modules/scheduler': '0.27.0',
        }
        for (const [name, version] of Object.entries(pinned)) {
          const entry = lock.packages?.[name]
          assert.ok(entry !== undefined, `lock 里有 ${name}`)
          assert.equal(entry.version, version, `${name} 版本`)
          assert.match(entry.integrity ?? '', /^sha512-/, `${name} 有 integrity 校验和`)
        }
        // package.json 与 lock 不能各说各话
        const pkg = JSON.parse(readFile('package.json'))
        for (const [name, version] of Object.entries(pkg.dependencies ?? {})) {
          assert.equal(lock.packages[`node_modules/${name}`]?.version, version, `${name} 的 lock 与 package.json 一致`)
        }
        // node_modules 不进版本库
        assert.includes(readFile('.gitignore'), 'node_modules/', '.gitignore 排除了 node_modules')
      },
    },
    {
      name: '可独立复核：防护层基线随仓库走，不依赖工作区外部目录',
      async run({ assert, readFile }) {
        // merge-guard 曾经只能从 ../main_repai/ 取源，克隆到别处就跑不了，
        // "防护层有没有被改"也就无从复核。基线现在 vendored 进仓库。
        const baselinePath = 'vendor/upstream/tencent-gate-guard.js'
        assert.ok(existsSync(join(ROOT, baselinePath)), `${baselinePath} 在仓库里`)

        const baseline = readFile(baselinePath)
        const provenance = JSON.parse(readFile('src/guard/provenance.json'))
        // provenance 记录的就是这份基线，且 sha 对得上
        assert.equal(provenance.source, baselinePath, 'provenance 指向仓库内基线')
        assert.equal(
          hash(baseline),
          provenance.sourceSha256,
          '基线 sha256 与 provenance 一致（基线没被换过）',
        )
        // 基线本身确实是那个版本插件的抽取件
        assert.includes(baseline, 'TENCENT_GATE_RULES', '基线含规则表')
        assert.includes(baseline, 'TG_GATE_ERRORS', '基线含 WeakSet 登记表')
        assert.includes(baseline, 'ofm-tencent-gate', '基线含取证埋点前缀')
        // 合并产物记录了它是从哪来的
        const merged = readFile('src/guard/tencent-gate.js')
        assert.includes(merged, provenance.sourceSha256, '合并产物头部记录了源 sha256')
        assert.includes(provenance.upstreamOrigin ?? '', 'pack.js', '记录了上游来源是 pack.js')
      },
    },
    {
      name: '函数体逐字节：合并产物与基线的函数区完全一致（只有绑定与导出是新增的）',
      async run({ assert, readFile }) {
        const baseline = readFile('vendor/upstream/tencent-gate-guard.js')
        const merged = readFile('src/guard/tencent-gate.js')
        // 两边从 `var TG_ZW` 起是函数区；合并产物的函数区之后才是导出块
        const cut = text => {
          const start = text.indexOf('var TG_ZW')
          assert.ok(start > -1, '找到函数区起点 var TG_ZW')
          return text.slice(start)
        }
        const baselineBody = cut(baseline).trimEnd()
        const mergedRegion = cut(merged)
        const exportAt = mergedRegion.indexOf('/* ── 导出')
        assert.ok(exportAt > -1, '合并产物有导出块标记')
        const mergedBody = mergedRegion.slice(0, exportAt).trimEnd()

        // 这条断言是这份清单的核心：它不允许"顺手改一下函数体"。
        // 早先这里差 68 字节（我截掉了基线末尾一个未引用的常量），
        // 那种差异会让"逐字节"这句话不成立，于是改成保留原文。
        assert.equal(
          mergedBody.length,
          baselineBody.length,
          '函数区长度一致（不一致说明有增删）',
        )
        assert.equal(mergedBody, baselineBody, '函数区内容逐字节一致')

        // 差异只应出现在函数区之外：模块头的三个绑定 + 导出块
        const head = merged.slice(0, merged.indexOf('var TG_ZW'))
        for (const name of ['readFileSync', 'homedir', 'join']) {
          assert.includes(head, `let ${name}`, `头部把 ${name} 声明为可绑定`)
        }
        assert.includes(head, 'export function bindTencentGateEnv', '头部有绑定入口')
      },
    },
  ],
}
