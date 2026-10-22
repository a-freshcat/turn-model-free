/**
 * The demo application: the copied page, a fake channel pack, and a control
 * strip that lets you watch it respond.
 *
 * Everything here is scaffolding *around* the copy — the page itself is the
 * plugin's own code, extracted verbatim. The strip on the right drives the mock
 * backend so each state the page can be in (connected, idle, closed, pack
 * missing, balance errors, login in flight) can be seen and compared against
 * the real plugin.
 */

import { createPage } from '../app/page.mjs'
import { createMockPack } from '../mock/pack.mjs'
import { CHANNEL_DICT } from '../extracted/channels-dict.js'
import { PROVENANCE } from '../extracted/channels-page.js'

const PROVIDER_NAMES = {
  codearts: 'CodeArts',
  buddy: 'WorkBuddy 国内版',
  workbuddy: 'WorkBuddy 国际版',
  lobsterai: 'LobsterAI',
  qoder: 'Qoder',
  qodercn: 'Qoder 中国版',
  trae: 'TRAE',
  cline: 'Cline',
  loomy: 'Loomy',
  raccoon: 'Raccoon',
  minimax: 'MiniMax Code',
  zcode: 'ZCode',
  gemini: 'Gemini',
}

const SCENARIOS = [
  { id: 'normal', label: '正常（13 个渠道，12 个已接入）', mock: {} },
  { id: 'fresh', label: '全新安装（没有任何账号）', mock: { empty: true } },
  { id: 'closed', label: '插件关闭（渠道全部「已关闭」）', mock: { closed: true } },
  { id: 'balance', label: '余额查询失败', mock: { failBalances: true } },
  { id: 'claimFailed', label: '领取部分失败', mock: { claimFails: true } },
  { id: 'noTransport', label: '宿主无 connection 服务', mock: {}, ctx: false },
  { id: 'packMissing', label: '渠道包挂载失败', mock: { fail: ['provider.status'] }, summary: { state: 'failed', error: 'import failed: ENOENT vendor/channel-pack/pack.js' } },
]

export function start(document, React, ReactDOM) {
  const h = React.createElement
  const state = {
    scenario: 'normal',
    locale: 'zh',
    dark: false,
    slowLogin: false,
    /** `#open=accounts|models|both` — expand a fold on the cards after mount. */
    open: undefined,
    /** `#only=CodeArts` — restrict the expansion to one card. */
    only: undefined,
  }

  // Deep links (`#scenario=fresh&lang=en&theme=dark`) are read before the first
  // mount so a shared link opens directly on the state it names.
  const hash = new URLSearchParams(String(window.location?.hash ?? '').replace(/^#/, ''))
  if (hash.get('scenario') !== null) state.scenario = hash.get('scenario')
  if (hash.get('lang') !== null) state.locale = hash.get('lang')
  if (hash.get('theme') === 'dark') state.dark = true
  if (hash.get('open') !== null) state.open = hash.get('open')
  if (hash.get('only') !== null) state.only = hash.get('only')

  const rootNode = document.getElementById('page-root')
  const logNode = document.getElementById('rpc-log')
  const buildNode = document.getElementById('build-info')
  const root = ReactDOM.createRoot(rootNode)

  const log = []
  let pack = null
  let page = null

  const describeCall = call => {
    const payload = call.payload ?? {}
    const bits = []
    if (payload.provider !== undefined) bits.push(payload.provider)
    if (payload.accountId !== undefined) bits.push(payload.accountId.slice(0, 22))
    if (payload.modelId !== undefined) bits.push(payload.modelId)
    if (payload.disabled !== undefined) bits.push(`disabled=${payload.disabled}`)
    if (payload.enabled !== undefined) bits.push(`enabled=${payload.enabled}`)
    if (payload.force === true) bits.push('force')
    return `${call.method}(${bits.join(', ')})`
  }

  const wrapConnection = connection => ({
    rpc: {
      async call(scope, plugin, request, signal) {
        const started = performance.now()
        try {
          const result = await connection.rpc.call(scope, plugin, request, signal)
          log.unshift({ ok: true, text: describeCall(request), ms: Math.round(performance.now() - started) })
          renderLog()
          return result
        } catch (error) {
          log.unshift({ ok: false, text: describeCall(request), error: String(error?.message ?? error), ms: Math.round(performance.now() - started) })
          renderLog()
          throw error
        }
      },
    },
  })

  const renderLog = () => {
    if (logNode === null) return
    logNode.textContent = ''
    for (const entry of log.slice(0, 120)) {
      const row = document.createElement('div')
      row.className = 'log-row' + (entry.ok ? '' : ' bad')
      const method = document.createElement('code')
      method.textContent = entry.text
      const timing = document.createElement('span')
      timing.textContent = entry.ok ? `${entry.ms}ms` : (entry.error ?? '').slice(0, 60)
      row.appendChild(method)
      row.appendChild(timing)
      logNode.appendChild(row)
    }
  }

  /**
   * Opens a fold on every card whose name matches `only` (or on all of them).
   * Used by `#open=accounts` / `#open=models` deep links so a screenshot or a
   * shared URL can show the expanded state without clicking 13 buttons.
   */
  const openEveryFold = (label, only) => {
    const cards = [...document.querySelectorAll('#page-root .ofm_chan')]
    for (const card of cards) {
      if (only !== undefined && !card.textContent.includes(only)) continue
      const toggle = [...card.querySelectorAll('button.ofm_foldtoggle')].find(button => button.textContent.includes(label))
      toggle?.dispatchEvent(new window.Event('click', { bubbles: true }))
    }
  }

  const mount = () => {
    const scenario = SCENARIOS.find(entry => entry.id === state.scenario) ?? SCENARIOS[0]
    log.length = 0
    pack = createMockPack({ ...scenario.mock, loginPolls: state.slowLogin ? 3 : 1 })
    page = createPage({ React, h, Fragment: React.Fragment, locale: state.locale, timers: window })
    const ctx = scenario.ctx === false ? {} : { connection: wrapConnection(pack.connection) }
    const summary = { channels: scenario.summary ?? { state: 'ready', error: '' } }
    root.render(h(page.ChannelsPage, { t: page.t, ctx, summary }))
    renderLog()
    if (state.open !== undefined) {
      // the fold buttons only exist once the readiness probe has answered
      setTimeout(() => {
        if (state.open === 'accounts' || state.open === 'both') openEveryFold('账号', state.only)
        if (state.open === 'models' || state.open === 'both') openEveryFold('模型', state.only)
      }, 260)
    }
  }

  document.body.toggleAttribute('data-ds-dark-theme', state.dark)
  if (buildNode !== null) {
    buildNode.textContent = `plugin v${PROVENANCE.version} · client.js ${PROVENANCE.sourceSha256.slice(0, 12)}…`
  }

  const controls = document.getElementById('controls')
  if (controls !== null) {
    // scenario
    const scenarioLabel = document.createElement('label')
    scenarioLabel.textContent = '场景'
    const select = document.createElement('select')
    for (const scenario of SCENARIOS) {
      const option = document.createElement('option')
      option.value = scenario.id
      option.textContent = scenario.label
      select.appendChild(option)
    }
    select.value = state.scenario
    select.addEventListener('change', () => { state.scenario = select.value; mount() })
    scenarioLabel.appendChild(select)
    controls.appendChild(scenarioLabel)

    // locale
    const localeLabel = document.createElement('label')
    localeLabel.textContent = '语言'
    const localeSelect = document.createElement('select')
    for (const [value, text] of [['zh', '中文'], ['en', 'English']]) {
      const option = document.createElement('option')
      option.value = value
      option.textContent = text
      localeSelect.appendChild(option)
    }
    localeSelect.value = state.locale
    localeSelect.addEventListener('change', () => { state.locale = localeSelect.value; mount() })
    localeLabel.appendChild(localeSelect)
    controls.appendChild(localeLabel)

    const themeButton = document.createElement('button')
    themeButton.type = 'button'
    themeButton.textContent = state.dark ? '切换浅色' : '切换深色'
    themeButton.addEventListener('click', () => {
      state.dark = !state.dark
      document.body.toggleAttribute('data-ds-dark-theme', state.dark)
      themeButton.textContent = state.dark ? '切换浅色' : '切换深色'
    })
    controls.appendChild(themeButton)

    const slowButton = document.createElement('button')
    slowButton.type = 'button'
    slowButton.textContent = '模拟慢登录（3 次轮询）'
    slowButton.addEventListener('click', () => {
      state.slowLogin = !state.slowLogin
      slowButton.textContent = state.slowLogin ? '模拟慢登录（开）' : '模拟慢登录（3 次轮询）'
      mount()
    })
    controls.appendChild(slowButton)

    const pushButton = document.createElement('button')
    pushButton.type = 'button'
    pushButton.textContent = '推送 ofm:channels 事件'
    pushButton.addEventListener('click', () => {
      window.dispatchEvent(new CustomEvent('ofm:channels'))
      log.unshift({ ok: true, text: 'window.dispatchEvent(ofm:channels)', ms: 0 })
      renderLog()
    })
    controls.appendChild(pushButton)

    const resetButton = document.createElement('button')
    resetButton.type = 'button'
    resetButton.textContent = '重置 mock 数据'
    resetButton.addEventListener('click', () => {
      mount()
      log.unshift({ ok: true, text: 'mock 数据已重置', ms: 0 })
      renderLog()
    })
    controls.appendChild(resetButton)
  }

  mount()

  // expose for the smoke test the build runs headlessly
  window.__OFM_DEMO_STATE__ = {
    get page() { return page },
    get pack() { return pack },
    dict: CHANNEL_DICT,
  }
}
