/* 启动探针：模块里的任何异常都要可见，否则页面只会停在静态占位文案上。 */
window.addEventListener('error', event => {
  document.title = 'ERR: ' + (event.message || 'unknown')
  const host = document.getElementById('roster-host')
  if (host) host.innerHTML = '<div class="notice bad"><b>脚本错误</b>' + (event.message || '') + '</div>'
})
window.addEventListener('unhandledrejection', event => {
  const reason = event.reason && (event.reason.stack || event.reason.message) || String(event.reason)
  document.title = 'REJ: ' + reason.split('\n')[0]
  const host = document.getElementById('roster-host')
  if (host) host.innerHTML = '<div class="notice bad"><b>启动失败</b>' + reason.split('\n')[0] + '</div>'
})
/* turn-model-free 控制台前端
 *
 * 全部数值都来自本机网关：`/ui/quota` 给配额与重置时刻，`/ui/state` 给运行态
 * （并发、失败计数、冷却），`/ui/rpc provider.status` 给各渠道账号与模型统计，
 * `/ui/protection` 给调用保护状态。
 *
 * 一条硬规矩：**没有读数就写「未知」，不写 0、不写「正常」**。
 * 状态徽章只认 `/ui/state` 的运行态；配额条只认 `remaining` 为有限数的模型。
 */

const POOLS = [
  { id: 'argon', label: 'Argon' },
  { id: 'claude_gpt', label: 'Claude' },
  { id: 'gemini_shared', label: 'Gemini' },
]

const CHANNELS = {
  antigravity: { label: 'Antigravity', icon: 'antigravity.png', hint: '本机 OAuth 目录 · 三个配额池' },
  codearts:    { label: '华为 CodeArts', icon: 'codearts.png', hint: '华为云 · CodeArts' },
  buddy:       { label: '腾讯 WorkBuddy', icon: 'buddy.png', note: '国内', hint: '腾讯 · copilot.tencent.com（CodeBuddy 同一端点，国内版账号）' },
  workbuddy:   { label: 'WorkBuddy', icon: 'workbuddy.png', note: '国际', hint: '腾讯 · workbuddy.ai（国际版端点，独立账号与积分体系）' },
  qoder:       { label: '阿里 Qoder', icon: 'qoder.png', hint: '阿里 · qoder.com' },
  qodercn:     { label: '阿里 Qoder CN', icon: 'qodercn.png', hint: '阿里 · qoder.com.cn' },
  cline:       { label: 'Cline 官方', icon: 'cline.png', hint: 'Cline 官方端点' },
  raccoon:     { label: '商汤 Raccoon', icon: 'raccoon.png', hint: '商汤 · Raccoon' },
  zcode:       { label: '智谱 ZCode', icon: 'zcode.png', hint: '智谱 · bigmodel.cn / z.ai' },
  minimax:     { label: 'MiniMax Code', icon: null, hint: 'MiniMax · Code，设备码 + PKCE' },
  lobsterai:   { label: '有道 LobsterAI', icon: null, hint: '有道 · LobsterAI' },
}

// 已下线的渠道：插件包里仍然认得这两个 id，但它们的登录与模型早已不可用，
// 页面不再给入口。列在这里是为了在数据里遇到它们时能说清「为什么看不见」，
// 而不是默默渲染出一张点不通的卡片。
const RETIRED_CHANNELS = new Set(['trae', 'loomy'])

const $ = id => document.getElementById(id)

/* ---------------------------------------------------------------- 主题 */
const themeBtn = $('theme-toggle')
function applyTheme(mode) {
  document.body.dataset.theme = mode
  themeBtn.textContent = mode === 'night' ? '◑ 日间' : '◐ 夜间'
}
applyTheme(localStorage.getItem('gateway:theme') || 'paper')
themeBtn.addEventListener('click', () => {
  const next = document.body.dataset.theme === 'night' ? 'paper' : 'night'
  localStorage.setItem('gateway:theme', next)
  applyTheme(next)
})

/* ---------------------------------------------------------------- 请求 */
let connected = false

async function api(path, options = {}) {
  const { body, headers, ...rest } = options
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...rest,
    headers: {
      ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
      ...(headers || {}),
    },
    ...(body === undefined ? {} : { body }),
  })
  const text = await response.text()
  let data = null
  try { data = text ? JSON.parse(text) : null } catch { data = null }
  if (!response.ok || (data && data.ok === false)) {
    const message = data?.error?.message || data?.detail || `HTTP ${response.status}`
    const error = new Error(message)
    error.status = response.status
    throw error
  }
  return data
}

const rpc = (method, payload = {}) => api('/ui/rpc', {
  method: 'POST',
  body: JSON.stringify({ method, payload }),
})

/* ---------------------------------------------------------------- 工具 */
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
))

const fmtPct = value => (
  typeof value === 'number' && Number.isFinite(value)
    ? (value >= 10 ? value.toFixed(1).replace(/\.0$/, '') : value.toFixed(2)) + '%'
    : '未知'
)

const shortId = id => {
  const text = String(id || '')
  return text.length <= 14 ? text : text.slice(0, 6) + '…' + text.slice(-5)
}

const barClass = remaining => {
  if (typeof remaining !== 'number' || !Number.isFinite(remaining)) return 'none'
  if (remaining <= 10) return 'low'
  if (remaining <= 35) return 'warn'
  return ''
}

function fmtClock(ts) {
  if (!Number.isFinite(ts)) return '—'
  const date = new Date(ts * 1000)
  const now = new Date()
  const sameDay = date.getFullYear() === now.getFullYear()
    && date.getMonth() === now.getMonth()
    && date.getDate() === now.getDate()
  const hm = `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
  if (sameDay) return hm
  return `${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')} ${hm}`
}

/** 把毫秒差写成「1 天 3 小时」「2 小时 11 分」「6 分 20 秒」这种能直接读的形式。 */
function fmtCountdown(ms) {
  if (!Number.isFinite(ms)) return '未知'
  if (ms <= 0) return '已到时间'
  const totalSec = Math.floor(ms / 1000)
  const day = Math.floor(totalSec / 86400)
  const hour = Math.floor((totalSec % 86400) / 3600)
  const minute = Math.floor((totalSec % 3600) / 60)
  const second = totalSec % 60
  if (day > 0) return `${day} 天 ${hour} 小时`
  if (hour > 0) return `${hour} 小时 ${minute} 分`
  if (minute > 0) return `${minute} 分 ${second} 秒`
  return `${second} 秒`
}

/** 到点提醒分三档：已到点（红）／一小时内（红）／一天内（琥珀）。 */
function countdownClass(ms) {
  if (!Number.isFinite(ms)) return 'none'
  if (ms <= 0) return 'soon'
  if (ms <= 3600_000) return 'soon'
  if (ms <= 86400_000) return 'near'
  return ''
}

/* ---------------------------------------------------------------- 数据 */
let model = { accounts: [], models: [], api_base: '', api_key: '', statuses: {}, protection: null }
let filterText = ''
let modelFilterText = ''
let currentView = 'pool'
const dismissedDue = new Set()

function poolSummary(models, poolId) {
  const rows = models.filter(item => item.pool === poolId)
  const known = rows.filter(item => typeof item.remaining === 'number' && Number.isFinite(item.remaining))
  if (!known.length) return { known: 0, total: rows.length, remaining: null, resetAt: null, resetRaw: '' }
  let lowest = Infinity
  let resetAt = null
  let resetRaw = ''
  for (const item of known) {
    if (item.remaining < lowest) lowest = item.remaining
    // 只信后端算好的 epoch 秒（reset_at）。reset_time 是本地化文本，月/日顺序
    // 随区域设置变化，前端自己 parse 会得出「已到时间」这种假读数。
    const at = typeof item.reset_at === 'number' && Number.isFinite(item.reset_at)
      ? item.reset_at * 1000
      : null
    if (at !== null && (resetAt === null || at < resetAt)) {
      resetAt = at
      resetRaw = item.reset_time || ''
    }
  }
  return { known: known.length, total: rows.length, remaining: lowest, resetAt, resetRaw }
}

/** 插件账号的积分包读数：把 `balance.packages` 折算成剩余比例与最早过期时刻。 */
function creditSummary(account) {
  const packages = Array.isArray(account.balance?.packages) ? account.balance.packages : []
  if (!packages.length) return null
  let total = 0
  let remaining = 0
  let counted = false
  let earliest = null
  for (const pack of packages) {
    const t = Number(pack.total)
    const r = Number(pack.remaining)
    if (Number.isFinite(t) && t > 0 && Number.isFinite(r)) {
      total += t
      remaining += r
      counted = true
    }
    const expiry = pack.expiredTime || pack.cycleEndTime || ''
    if (expiry) {
      const at = new Date(expiry).getTime()
      if (Number.isFinite(at) && (earliest === null || at < earliest)) earliest = at
    }
  }
  const pct = counted && total > 0 ? Math.max(0, Math.min(100, (remaining / total) * 100)) : null
  return { pct, remaining, total, unit: packages.find(item => item.unit)?.unit || '积分', earliest, count: packages.length }
}

/** 整个号池在每个池子上的最早重置时刻。 */
function poolResetAt(poolId) {
  let earliest = null
  let owner = ''
  for (const account of model.accounts) {
    // 只有本机 Antigravity 账号有这套三池配额；插件账号没有 models 数组
    // （它是 null，不是空数组），不能当成本机池的一部分去算重置时间。
    if (account.driver !== 'antigravity' || !Array.isArray(account.models)) continue
    const summary = poolSummary(account.models, poolId)
    if (summary.resetAt === null) continue
    if (earliest === null || summary.resetAt < earliest) {
      earliest = summary.resetAt
      owner = account.name || account.id
    }
  }
  return { at: earliest, owner }
}

function stateOf(account) {
  const status = account.status || 'active'
  if (status === 'active') return { cls: 'ok', text: '正常' }
  if (status === 'disabled') return { cls: 'stop', text: '已暂停' }
  if (status === 'reauth') return { cls: 'err', text: '需重登' }
  if (status === 'cooldown') {
    const sec = account.cooldown_remaining_seconds || 0
    // 冷却读数是「请求那一刻还剩多久」。10 秒一跳的倒计时把它往下扣，
    // 扣到 0 就写「即将就绪」而不是继续显示「冷却中」——否则用户会以为
    // 账号一直卡在冷却里，实际它早就该被重新调度了。
    if (sec <= 0) return { cls: 'cool', text: '即将就绪' }
    const text = sec >= 120 ? `冷却 ${Math.ceil(sec / 60)} 分` : `冷却 ${Math.ceil(sec)}s`
    return { cls: 'cool', text }
  }
  return { cls: 'stop', text: status || '未知' }
}

/* ---------------------------------------------------------------- 渲染：KPI */
function renderKpi() {
  const accounts = model.accounts
  const online = accounts.filter(item => item.enabled !== false && item.status === 'active').length
  const inFlight = accounts.reduce((sum, item) => sum + (item.in_flight ?? 0), 0)
  const maxConc = accounts.reduce((sum, item) => sum + (item.max_concurrency ?? 1), 0)
  const fail = accounts.reduce((sum, item) => sum + (item.fail_count ?? 0), 0)

  $('kpi-online').innerHTML = `${online}<small>/${accounts.length}</small>`

  // 模型可用 = 所有**有账号的渠道**声明的模型数之和。只看本机 Antigravity 会
  // 少报（本机 26 个，插件渠道还有 64 个），那会让人以为只有 antigravity 能用。
  let models = null
  for (const [id, row] of Object.entries(model.statuses || {})) {
    if (RETIRED_CHANNELS.has(id)) continue
    const live = (row?.accounts?.total ?? 0) > 0
    const count = row?.models?.total
    if (!live || !Number.isFinite(count)) continue
    models = (models ?? 0) + count
  }
  $('kpi-models').textContent = models === null ? '—' : models

  $('kpi-conc').innerHTML = `${inFlight}<small>/${maxConc}</small>`
  $('kpi-fail').textContent = fail

  // KPI 上的「最近重置」取所有池子里最近的那个时刻
  let nearest = null
  for (const pool of POOLS) {
    const { at } = poolResetAt(pool.id)
    if (at !== null && (nearest === null || at < nearest)) nearest = at
  }
  $('kpi-reset').textContent = nearest === null
    ? '未知'
    : (nearest > Date.now() ? fmtCountdown(nearest - Date.now()) : '已到时间')

  $('rail-accts').textContent = accounts.length
  $('rail-models').textContent = models === null ? '—' : models
  const activeChannels = Object.values(model.statuses).length
  const liveChannels = Object.entries(model.statuses)
    .filter(([id, item]) => !RETIRED_CHANNELS.has(id) && item.accounts?.total > 0).length
  $('rail-channels').textContent = liveChannels || '—'
  $('foot-conc').textContent = maxConc
  $('foot-guard').textContent = model.protection?.paused
    ? '已暂停'
    : `${model.protection?.max_dispatch_per_minute ?? '—'} 次/分`
  $('foot-listen').textContent = (model.api_base || '').replace(/^https?:\/\//, '').replace(/\/v1$/, '') || '127.0.0.1:8199'
}

/* ---------------------------------------------------------------- 渲染：名册 */
function renderRoster() {
  const host = $('roster-host')
  const needle = filterText.trim().toLowerCase()
  const visible = needle
    ? model.accounts.filter(item => (item.name || '').toLowerCase().includes(needle))
    : model.accounts

  if (!model.accounts.length) {
    host.innerHTML = `<div class="notice"><b>号池里还没有账号</b>点右上角「＋ 添加账号」登录 Antigravity，或导入已有兼容上游配置。</div>`
    return
  }
  if (!visible.length) {
    host.innerHTML = `<div class="notice">没有匹配「${esc(filterText)}」的账号。</div>`
    return
  }

  const rows = visible.map(account => {
    const meta = CHANNELS[account.provider] || { label: account.provider || '未知渠道', icon: null }
    const state = stateOf(account)
    const enabled = account.enabled !== false
    const isLocal = account.driver === 'antigravity'

    let pools
    if (isLocal) {
      // 本机 Antigravity：三个独立配额池，条长 = 池内最低剩余
      pools = POOLS.map(pool => {
        const summary = poolSummary(account.models || [], pool.id)
        const cls = barClass(summary.remaining)
        const width = summary.remaining === null ? 0 : Math.max(0, Math.min(100, summary.remaining))
        const trackCls = summary.remaining === null ? 'track none' : 'track'
        const pctCls = 'pool-pct' + (summary.remaining === null ? ' none' : (cls === 'low' ? ' low' : ''))
        const countdown = summary.resetAt === null ? '' : fmtCountdown(summary.resetAt - Date.now())
        const title = summary.resetAt === null
          ? `${pool.label}：池内没有读数，无法判断重置时间`
          : `${pool.label}：最早重置 ${fmtClock(summary.resetAt / 1000)}（${countdown}）`
        return `<div class="pool" title="${esc(title)}">
          <span class="pool-name">${esc(pool.label)}</span>
          <div class="${trackCls}"><div class="fill ${cls}" style="width:${width}%"></div></div>
          <span class="${pctCls}">${summary.remaining === null ? '未知' : fmtPct(summary.remaining)}</span>
        </div>`
      }).join('')
    } else {
      // 插件渠道：显示积分包剩余与到期；没有读数就写「无读数」，不冒充 0
      const credit = creditSummary(account)
      const lines = []
      if (credit) {
        const cls = barClass(credit.pct)
        const width = credit.pct === null ? 0 : credit.pct
        const amount = `${Math.round(credit.remaining)} / ${Math.round(credit.total)} ${credit.unit}`
        lines.push(`<div class="pool" title="${esc(amount)} · ${credit.count} 个套餐">
          <span class="pool-name">积分</span>
          <div class="${credit.pct === null ? 'track none' : 'track'}"><div class="fill ${cls}" style="width:${width}%"></div></div>
          <span class="pool-pct${credit.pct !== null && cls === 'low' ? ' low' : ''}">${credit.pct === null ? '未知' : fmtPct(credit.pct)}</span>
        </div>`)
        lines.push(`<div class="pool note-line">
          <span class="pool-name">剩余</span>
          <span class="note-text" title="${esc(amount)}">${esc(amount)}</span>
        </div>`)
        if (credit.earliest) {
          lines.push(`<div class="pool note-line">
            <span class="pool-name">到期</span>
            <span class="note-text muted">${esc(fmtClock(credit.earliest / 1000))}</span>
          </div>`)
        }
        // 积分是真实上游读数，必须说清它是几点的：5 分钟内可能是沿用上一轮的值。
        if (account.balanceAt) {
          lines.push(`<div class="pool note-line">
            <span class="pool-name">读数</span>
            <span class="note-text muted">${esc(clockText(account.balanceAt))}</span>
          </div>`)
        }
      } else {
        lines.push(`<div class="pool">
          <span class="pool-name">积分</span>
          <div class="track none"><div class="fill" style="width:0%"></div></div>
          <span class="pool-pct none">无读数</span>
        </div>`)
        if (account.balanceError) {
          lines.push(`<div class="pool note-line">
            <span class="pool-name">原因</span>
            <span class="note-text bad" title="${esc(account.balanceError)}">${esc(account.balanceError)}</span>
          </div>`)
        }
      }
      pools = lines.join('')
    }

    const inFlight = account.in_flight ?? 0
    const maxConc = account.max_concurrency ?? 1
    const failCount = account.fail_count ?? 0
    const failNote = failCount > 0 ? `<em>失败 ${failCount}</em>` : ''
    // 本机账号的模型数从配额行数得出；插件账号用渠道级计数；都没有就写 —
    const modelCount = isLocal
      ? (account.models || []).length
      : (Number.isFinite(account.modelCount) ? account.modelCount : null)
    const modelsTitle = modelCount === null ? '模型数未知' : `模型 ${modelCount} 个`
    const modelsAttr = modelCount === null ? '—' : String(modelCount)
    const icon = meta.icon
      ? `<img src="/assets/brand/${meta.icon}" alt="${esc(meta.label)}">`
      : `<span class="initial">${esc((meta.label || '?').slice(0, 1))}</span>`
    const note = meta.note ? ` <span class="tag">${esc(meta.note)}</span>` : ''
    const channelTag = `<span class="tag channel" title="${esc(meta.hint || '')}">${esc(meta.label)}</span>${note}`
    const driverTag = isLocal ? 'OAuth' : (account.driver === 'channel-pack' ? '插件' : (account.driver || 'token'))

    return `<div class="acct${enabled ? '' : ' off'}" data-id="${esc(account.id)}">
      <div class="acct-id">
        <div class="acct-avatar">${icon}</div>
        <div class="acct-meta">
          <div class="acct-name" title="${esc(account.name || account.id)}">${esc(account.name || account.id)}</div>
          <div class="acct-tags">
            ${channelTag}
            <span class="tag"><span class="mono">${esc(shortId(account.id))}</span></span>
            <span class="tag">${esc(driverTag)}</span>
          </div>
        </div>
      </div>

      <div class="pools">${pools}</div>

      <div class="state-cell">
        <span class="state ${state.cls}"><i></i>${esc(state.text)}</span>
        <span class="conc"><b>${inFlight}</b><span>/${maxConc}</span>${failNote}</span>
      </div>

      <div class="row-actions">
        <button class="mini" data-act="test" title="探活测速">
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round">
            <path d="M2 8h3l2-4 2 8 2-4h3"></path>
          </svg>
        </button>
        <button class="mini" data-act="models" title="${esc(modelsTitle)}">
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round">
            <path d="M2.5 4h11M2.5 8h11M2.5 12h7"></path>
          </svg>
        </button>
        <button class="mini" data-act="refresh" title="${isLocal ? '刷新配额' : '刷新令牌'}">
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round">
            <path d="M13 8a5 5 0 1 1-1.6-3.7M13 2.5V5h-2.5"></path>
          </svg>
        </button>
        <button class="mini danger" data-act="delete" title="移除账号">
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round">
            <path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.7 8.5h5.6l.7-8.5"></path>
          </svg>
        </button>
        <button class="switch" data-act="toggle" aria-checked="${enabled}" title="${enabled ? '参与调度' : '已停用调度'}"></button>
      </div>
    </div>`
  }).join('')

  host.innerHTML = `<div class="roster">
    <div class="roster-head">
      <div>账号</div>
      <div>配额池（条长 = 剩余）</div>
      <div>状态 · 并发</div>
      <div style="text-align:right">操作</div>
    </div>
    ${rows}
  </div>`

  host.querySelectorAll('.acct').forEach(row => {
    const id = row.dataset.id
    row.querySelectorAll('[data-act]').forEach(btn => {
      btn.addEventListener('click', () => handleAction(id, btn.dataset.act, row))
    })
  })
}

/* ---------------------------------------------------------------- 渲染：重置提醒 */
function renderResetStrip() {
  const grid = $('reset-grid')
  const now = Date.now()
  let dueSoon = null

  grid.innerHTML = POOLS.map(pool => {
    const { at, owner } = poolResetAt(pool.id)
    if (at === null) {
      return `<div class="reset-cell">
        <div class="reset-cell-top"><b>${esc(pool.label)}</b> 池</div>
        <div class="reset-count none">无读数</div>
        <div class="reset-cell-sub">池内模型未返回重置时间</div>
      </div>`
    }
    const ms = at - now
    const cls = countdownClass(ms)
    if (ms <= 3600_000 && dueSoon === null) dueSoon = { pool: pool.label, at, ms }

    const sub = ms <= 86400_000
      ? `${owner ? esc(owner) + ' · ' : ''}<code>${esc(fmtClock(at / 1000))}</code>`
      : `${owner ? esc(owner) + ' · ' : ''}<code>${esc(fmtClock(at / 1000))}</code>`

    return `<div class="reset-cell">
      <div class="reset-cell-top"><b>${esc(pool.label)}</b> 池</div>
      <div class="reset-count ${cls}">${esc(fmtCountdown(ms))}</div>
      <div class="reset-cell-sub">${sub}</div>
    </div>`
  }).join('')

  const stamps = model.accounts
    .map(item => item.updated_at)
    .filter(value => typeof value === 'number' && Number.isFinite(value))
  $('reset-note').textContent = stamps.length
    ? `配额查询于 ${new Date(Math.max(...stamps) * 1000).toLocaleTimeString('zh-CN', { hour12: false })}`
    : ''

  const banner = $('due-banner')
  const key = dueSoon ? `${dueSoon.pool}:${Math.floor(dueSoon.at / 60000)}` : ''
  if (dueSoon && !dismissedDue.has(key)) {
    banner.hidden = false
    $('due-text').textContent = dueSoon.ms <= 0
      ? `${dueSoon.pool} 池已到重置时间，配额应已刷新`
      : `${dueSoon.pool} 池将在 ${fmtCountdown(dueSoon.ms)} 后重置`
  } else {
    banner.hidden = true
  }
}

/* ---------------------------------------------------------------- 渲染：右侧 */
function renderSide() {
  const water = $('water-card')
  if (!model.accounts.length) {
    water.innerHTML = `<div class="water-row"><span class="water-name">无账号</span><div></div><span class="water-pct">—</span></div>`
  } else {
    water.innerHTML = POOLS.map(pool => {
      let lowest = null
      let missing = 0
      for (const account of model.accounts) {
        const summary = poolSummary(account.models || [], pool.id)
        if (summary.remaining === null) { missing += 1; continue }
        if (lowest === null || summary.remaining < lowest) lowest = summary.remaining
      }
      const cls = barClass(lowest)
      const width = lowest === null ? 0 : Math.max(0, Math.min(100, lowest))
      const track = lowest === null ? 'track none' : 'track'
      const note = missing ? ` title="${missing} 个账号该池无读数"` : ''
      return `<div class="water-row"${note}>
        <span class="water-name">${esc(pool.label)}</span>
        <div class="${track}"><div class="fill ${cls}" style="width:${width}%"></div></div>
        <span class="water-pct${lowest !== null && cls === 'low' ? ' low' : ''}">${lowest === null ? '未知' : fmtPct(lowest)}</span>
      </div>`
    }).join('')
  }

  $('reset-card').innerHTML = POOLS.map(pool => {
    const { at } = poolResetAt(pool.id)
    if (at === null) return `<div class="guard-row"><span>${esc(pool.label)}</span><b>未知</b></div>`
    return `<div class="guard-row"><span>${esc(pool.label)}</span><b>${esc(fmtCountdown(at - Date.now()))}</b></div>`
  }).join('')

  const guard = model.protection
  $('guard-card').innerHTML = guard
    ? `<div class="guard-row"><span>状态</span><b class="${guard.paused ? 'warn' : ''}">${guard.paused ? '已暂停' : '正常'}</b></div>
       <div class="guard-row"><span>在途请求</span><b>${guard.active_requests ?? 0} / ${guard.max_active_requests ?? '—'}</b></div>
       <div class="guard-row"><span>调度上限</span><b>${guard.max_dispatch_per_minute ?? '—'} 次/分</b></div>`
    : `<div class="guard-row"><span>状态</span><b>不可用</b></div>`

  $('api-base').textContent = model.api_base || '—'
  $('api-key').textContent = model.api_key
    ? model.api_key.slice(0, 3) + '••••' + model.api_key.slice(-4)
    : '—'
}

/* ---------------------------------------------------------------- 渲染：桌面启动 */
function renderLauncher() {
  const portEl = $('launch-port')
  const kindEl = $('launch-kind')
  if (!portEl || !kindEl) return
  const base = (model.api_base || '').replace(/\/v1$/, '')
  portEl.textContent = base ? base.replace(/^https?:\/\//, '') : '未知'
  // pywebview 只在独立窗口里注入 window.pywebview；系统浏览器里没有它。
  // 这里只说「现在这扇窗户是什么」，不猜用户当初双击的是 exe 还是 cmd ——
  // 两者打开的都是同一个窗口，猜错反而误导。
  kindEl.textContent = window.pywebview
    ? '独立窗口（桌面端）'
    : '系统浏览器（网页端）'
}

/* ---------------------------------------------------------------- 渲染：渠道 */
function renderChannels() {
  const host = $('channels-host')
  const entries = Object.entries(CHANNELS).map(([id, meta]) => {
    const status = model.statuses[id] || null
    return { id, meta, status }
  })

  // 有账号的排前面，其余按名称
  entries.sort((a, b) => {
    const ca = a.status?.accounts?.total ?? 0
    const cb = b.status?.accounts?.total ?? 0
    if (ca !== cb) return cb - ca
    return a.meta.label.localeCompare(b.meta.label, 'zh-CN')
  })

  const rows = entries.map(({ id, meta, status }) => {
    const accounts = status?.accounts || { total: 0, enabled: 0 }
    const models = status?.models || { total: 0, disabled: 0 }
    const icon = meta.icon
      ? `<img src="/assets/brand/${meta.icon}" alt="">`
      : `<span class="initial">${esc(meta.label.slice(0, 1))}</span>`
    const state = accounts.total === 0
      ? '<span class="state stop"><i></i>未接入</span>'
      : accounts.enabled === accounts.total
        ? '<span class="state ok"><i></i>正常</span>'
        : `<span class="state cool"><i></i>${accounts.enabled}/${accounts.total} 可用</span>`
    const note = meta.note ? ` <em>${esc(meta.note)}</em>` : ''
    return `<div class="acct">
      <div class="acct-id">
        <div class="acct-avatar">${icon}</div>
        <div class="acct-meta">
          <div class="acct-name">${esc(meta.label)}${note}</div>
          <div class="acct-tags">
            <span class="tag"><span class="mono">${esc(id)}</span></span>
          </div>
        </div>
      </div>
      <div class="pools">
        <div class="pool" style="grid-template-columns:60px minmax(0,1fr) 60px">
          <span class="pool-name">账号</span>
          <div class="track"><div class="fill" style="width:${accounts.total ? (accounts.enabled / accounts.total) * 100 : 0}%"></div></div>
          <span class="pool-pct">${accounts.enabled}/${accounts.total}</span>
        </div>
        <div class="pool" style="grid-template-columns:60px minmax(0,1fr) 60px">
          <span class="pool-name">模型</span>
          <div class="track"><div class="fill" style="width:${models.total ? 100 : 0}%"></div></div>
          <span class="pool-pct">${models.total}</span>
        </div>
      </div>
      <div class="state-cell">${state}</div>
      <div class="row-actions">
        <button class="mini" data-channel="${esc(id)}" title="添加账号">
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round">
            <path d="M8 3v10M3 8h10"></path>
          </svg>
        </button>
      </div>
    </div>`
  }).join('')

  host.innerHTML = `<div class="roster">
    <div class="roster-head">
      <div>渠道</div>
      <div>账号 / 模型</div>
      <div>状态</div>
      <div style="text-align:right">操作</div>
    </div>
    ${rows}
  </div>`

  host.querySelectorAll('[data-channel]').forEach(btn => {
    btn.addEventListener('click', () => startLogin(btn.dataset.channel))
  })
}

/* ---------------------------------------------------------------- 渲染：模型目录 */
function renderModels() {
  const host = $('models-host')
  const needle = modelFilterText.trim().toLowerCase()
  const all = model.models.slice().sort((a, b) => a.localeCompare(b))
  const visible = needle ? all.filter(name => name.toLowerCase().includes(needle)) : all

  $('models-note').textContent = `${all.length} 个模型 · 统一走 /v1/chat/completions`

  if (!visible.length) {
    host.innerHTML = `<div class="notice">${all.length ? `没有匹配「${esc(modelFilterText)}」的模型。` : '本机号池当前没有可用模型。'}</div>`
    return
  }

  // 按渠道前缀分组，一眼看出哪些来自哪个渠道
  const groups = new Map()
  for (const name of visible) {
    const slash = name.indexOf('/')
    const provider = slash > 0 ? name.slice(0, slash) : '(本机)'
    if (!groups.has(provider)) groups.set(provider, [])
    groups.get(provider).push(name)
  }

  host.innerHTML = [...groups.entries()].map(([provider, names]) => {
    const meta = CHANNELS[provider] || { label: provider, icon: null }
    const icon = meta.icon
      ? `<img src="/assets/brand/${meta.icon}" alt="">`
      : `<span class="initial">${esc(meta.label.slice(0, 1))}</span>`
    const items = names.map(name => {
      // 该模型被哪些账号支持、其中几个健康
      const owners = model.accounts.filter(account =>
        (account.models || []).some(item => item.id === name || item.name === name))
      const healthy = owners.filter(account => account.status === 'active').length
      const chip = owners.length
        ? `<span class="pool-pct${healthy ? '' : ' low'}">${healthy}/${owners.length}</span>`
        : `<span class="pool-pct none">—</span>`
      return `<div class="pool" style="grid-template-columns:minmax(0,1fr) 44px; gap:12px; padding:4px 0">
        <span class="mono" style="font-size:10.5px; color:var(--ink-2); overflow:hidden; text-overflow:ellipsis">${esc(name)}</span>
        ${chip}
      </div>`
    }).join('')

    return `<div class="reset-strip" style="margin-top:0; margin-bottom:12px">
      <div class="reset-strip-head">
        <span class="acct-avatar" style="width:20px;height:20px;flex:0 0 20px;border-radius:5px">${icon}</span>
        ${esc(meta.label)}
        <span>${names.length} 个模型</span>
      </div>
      <div style="padding:8px 16px">${items}</div>
    </div>`
  }).join('')
}

/* ---------------------------------------------------------------- 渲染：调用保护 */
function renderGuard() {
  const host = $('guard-host')
  const guard = model.protection
  const resumeBtn = $('guard-resume')
  if (!guard) {
    host.innerHTML = `<div class="notice">调用保护状态不可用。</div>`
    resumeBtn.hidden = true
    return
  }
  resumeBtn.hidden = !guard.paused

  const rows = [
    ['状态', guard.paused ? '已暂停' : '正常', guard.paused],
    ['原因', guard.reason || '—', false],
    ['说明', guard.message || '—', false],
    ['在途请求', `${guard.active_requests ?? 0} / ${guard.max_active_requests ?? '—'}`, false],
    ['调度上限', `${guard.max_dispatch_per_minute ?? '—'} 次/分`, false],
    ['近期上游拒绝', String(guard.recent_failure_count ?? 0), false],
    ['近期网络故障', String(guard.recent_network_failure_count ?? 0), false],
    ['上次异常', guard.occurred_at ? new Date(guard.occurred_at * 1000).toLocaleString('zh-CN', { hour12: false }) : '—', false],
  ]

  host.innerHTML = `<div class="card">
    ${rows.map(([label, value, warn]) => `<div class="guard-row" style="padding:9px 14px; font-size:11.5px">
      <span>${esc(label)}</span><b class="${warn ? 'warn' : ''}" style="max-width:60%; text-align:right">${esc(value)}</b>
    </div>`).join('')}
  </div>
  <div class="notice" style="margin-top:12px; text-align:left">${esc(guard.help || '')}</div>`
}

/* ---------------------------------------------------------------- 渲染：接入设置 */
function renderSettings() {
  const host = $('settings-host')
  const base = model.api_base || 'http://127.0.0.1:8199/v1'
  const key = model.api_key || '<网关密钥>'
  // PowerShell 的续行符是反引号，不能直接写进模板字符串（会提前结束字面量），
  // 这里用 \x60 转义拼出来，复制出去仍是可直接粘贴的命令。
  const cont = '\x60'
  const script = [
    `$env:GATEWAY_API_KEY = "${key}"`,
    `curl.exe --noproxy "*" --fail-with-body -N ${base}/chat/completions ${cont}`,
    `  -H "Authorization: Bearer $env:GATEWAY_API_KEY" ${cont}`,
    `  -H "Content-Type: application/json" ${cont}`,
    `  --data-binary "@gateway/examples/stream.json"`,
  ].join('\n')

  host.innerHTML = `<div class="card">
    <div class="guard-row" style="padding:10px 14px; font-size:11.5px"><span>Base URL</span><b>${esc(base)}</b></div>
    <div class="guard-row" style="padding:10px 14px; font-size:11.5px"><span>API Key</span><b>${esc(key.slice(0, 3))}••••</b></div>
    <div class="guard-row" style="padding:10px 14px; font-size:11.5px"><span>模型</span><b>见「模型目录」或 GET /v1/models</b></div>
  </div>
  <div class="notice" style="margin-top:12px; text-align:left">
    <b>curl 自测</b>
    <code style="display:block; margin-top:8px; padding:10px; white-space:pre-wrap; line-height:1.7">${esc(script)}</code>
  </div>`
}

/* ---------------------------------------------------------------- 渲染总入口 */
function renderStatus() {
  const dot = document.querySelector('#tb-status .dot')
  const label = document.querySelector('#tb-status span')
  if (!connected) {
    dot.className = 'dot err'
    label.textContent = '未连接'
    return
  }
  const accounts = model.accounts
  const cooling = accounts.filter(item => item.status === 'cooldown').length
  const paused = accounts.filter(item => item.status === 'disabled').length
  const reauth = accounts.filter(item => item.status === 'reauth').length
  const guardPaused = model.protection?.paused
  dot.className = 'dot' + (guardPaused ? ' paused' : (cooling || reauth) ? ' paused' : '')
  const bits = [`${accounts.length} 个账号`]
  if (cooling) bits.push(`${cooling} 冷却`)
  if (paused) bits.push(`${paused} 暂停`)
  if (reauth) bits.push(`${reauth} 待重登`)
  if (guardPaused) bits.push('保护已暂停')
  label.textContent = `运行中 · ${(model.api_base || '').replace(/\/v1$/, '').replace(/^https?:\/\//, '') || '127.0.0.1:8199'} · ${bits.join(' · ')}`
}

function renderAll() {
  renderKpi()
  renderRoster()
  renderResetStrip()
  renderSide()
  renderChannels()
  renderModels()
  renderGuard()
  renderSettings()
  renderLauncher()
  renderStatus()
  renderHeadNote()
}

/**
 * 标题下面那行说明。以前它永远停在 HTML 里的「读取中…」——数都读回来了，
 * 页面还在说自己在读，用户会以为卡住了。
 */
function renderHeadNote() {
  const note = $('main-note')
  if (!note) return
  if (!connected) { note.textContent = '未连接本机网关'; return }
  const online = model.accounts.filter(item => item.enabled !== false && item.status === 'active').length
  const cooling = model.accounts.filter(item => item.status === 'cooldown').length
  const bits = [`${model.accounts.length} 个账号 · ${online} 在线`]
  if (cooling) bits.push(`${cooling} 冷却`)
  if (lastColdAt) bits.push(`账号读数 ${clockText(lastColdAt)}`)
  note.textContent = bits.join(' · ')
}

/* ---------------------------------------------------------------- 视图切换 */
function switchView(view) {
  currentView = view
  document.querySelectorAll('.rail-item').forEach(item => {
    item.classList.toggle('on', item.dataset.view === view)
  })
  document.querySelectorAll('.view').forEach(section => {
    section.hidden = section.id !== `view-${view}`
  })
  if (view === 'pool') renderResetStrip()
}

document.querySelectorAll('.rail-item').forEach(item => {
  item.addEventListener('click', () => switchView(item.dataset.view || 'pool'))
})

/* ---------------------------------------------------------------- 动作 */
async function handleAction(id, act, row) {
  const account = model.accounts.find(item => item.id === id)
  if (!account) return

  if (act === 'test') {
    const btn = row.querySelector('[data-act="test"]')
    btn.disabled = true
    try {
      const reply = await rpc('account.test', { accountId: id })
      const value = reply.value || {}
      window.alert(value.ok ? `探活成功：${value.ms} ms` : `探活失败：${value.error || '未知原因'}`)
    } catch (error) {
      window.alert(`探活请求失败：${error.message}`)
    } finally {
      btn.disabled = false
    }
    return
  }

  if (act === 'toggle') {
    const next = account.status === 'disabled'
    try {
      await rpc('account.update', { accountId: id, patch: { enabled: next } })
      await load({ cold: true, credits: 'skip' })
    } catch (error) {
      window.alert(`切换失败：${error.message}`)
    }
    return
  }

  if (act === 'refresh') {
    const btn = row.querySelector('[data-act="refresh"]')
    btn.disabled = true
    try {
      if (account.driver === 'antigravity') {
        const reply = await api('/ui/quota/refresh', {
          method: 'POST',
          body: JSON.stringify({ method: 'quota.refresh', payload: { accountId: id } }),
        })
        await load({ cold: true, credits: 'skip' })
        const errors = reply?.errors || []
        if (errors.length) window.alert(`部分账号刷新失败：\n${errors.join('\n')}`)
      } else {
        // 插件账号的令牌续期是插件自己的事，交给 account.refresh。
        // 用户点了「刷新」就是要看这个账号的当前读数，所以这一行的积分强制重查。
        await rpc('account.refresh', { accountId: id })
        await load({ cold: true, credits: 'force' })
      }
    } catch (error) {
      window.alert(`刷新失败：${error.message}`)
    } finally {
      btn.disabled = false
    }
    return
  }

  if (act === 'models') {
    if (account.driver !== 'antigravity') {
      // 插件账号的模型表在插件里；用该渠道的模型清单并标注来源
      const reply = await rpc('model.list', { provider: account.provider }).catch(() => null)
      const names = (reply?.value?.models || []).map(item => item.id).sort()
      if (!reply || reply.error) {
        window.alert(`${account.name || id}\n未能读取该渠道的模型清单：${reply?.error?.message || '插件未响应'}`)
        return
      }
      window.alert(`${account.name || id}\n该渠道共 ${names.length} 个模型\n\n` + (names.join('\n') || '（插件返回空清单）'))
      return
    }
    const names = (account.models || []).map(item => item.name || item.id).sort()
    window.alert(`${account.name || id}\n共 ${names.length} 个模型\n\n` + names.join('\n'))
    return
  }

  if (act === 'delete') {
    if (!window.confirm(`确认从号池移除 ${account.name || id}？`)) return
    try {
      await rpc('account.delete', { accountId: id })
      await load({ cold: true, credits: 'skip' })
    } catch (error) {
      window.alert(`移除失败：${error.message}`)
    }
  }
}

async function startLogin(provider) {
  const meta = CHANNELS[provider] || { label: provider }
  try {
    if (provider === 'antigravity') {
      const reply = await rpc('account.create', { provider })
      const url = reply.value?.loginUrl
      if (!url) throw new Error('未返回授权地址')
      window.open(url, '_blank', 'noopener')
      window.alert('已在浏览器打开 Google 授权页。完成授权后回到本页点「刷新」。')
      return
    }
    const reply = await rpc('account.create', { provider })
    const url = reply.value?.loginUrl
    if (!url) throw new Error('未返回登录地址')
    window.open(url, '_blank', 'noopener')
    window.alert(`已打开 ${meta.label} 登录页。完成登录后回到本页点「刷新」。`)
  } catch (error) {
    window.alert(`${meta.label} 登录未能启动：${error.message}`)
  }
}

/* ---------------------------------------------------------------- 载入 */
/*
 * 冷热分离。这不是性能微调，是配额问题：`load()` 以前每 15 秒把所有渠道的
 * 账号与**积分**重查一遍，6 个渠道有账号时约 14 次调用 / 15 秒 ≈ 56 次/分，
 * 其中 `credits.balances` 对每个账号都是一次真实上游请求。既烧配额，也是
 * 上游 429 的放大器（Raccoon 尤其明显）。
 *
 * 现在的口径：
 *   热数据（本机、便宜）：在线状态、并发、冷却、保护状态 —— 15 秒一轮；
 *   冷数据（账号清单、模型目录、渠道统计）：5 分钟或手动刷新；
 *   积分（真实上游请求）：5 分钟一次，或在「积分」按钮里切成手动；
 *   窗口不可见时整体暂停，回到前台再补一轮。
 *
 * 一条硬规矩保持不变：拿不到读数就沿用**上次读数并标出时刻**，不写 0。
 */
const HOT_MS = 15_000
const COLD_MS = 5 * 60_000
const CREDITS_MS = 5 * 60_000
const TICK_MS = 10_000

let creditsMode = localStorage.getItem('gateway:credits-mode') === 'manual' ? 'manual' : 'auto'
let lastColdAt = 0
let lastCreditsAt = 0
let lastHotAt = 0
let loading = null
let hotTimer = null
let coldTimer = null
// 插件渠道的账号行（含积分与模型计数）。只在冷刷新时重建；热刷新只覆盖运行态，
// 否则每次心跳都要重查一遍积分。
let externalRows = []

const clockText = ts => (ts
  ? new Date(ts).toLocaleTimeString('zh-CN', { hour12: false })
  : '')

function creditsDue(now) {
  // 首次加载总要查一次：否则切到「仅手动」后重开页面，积分永远是「无读数」，
  // 而用户并没有要求「不显示」，只是不想被定时打上游。
  if (lastCreditsAt === 0) return true
  return creditsMode === 'auto' && now - lastCreditsAt >= CREDITS_MS
}

/** 把实时运行态叠到已有账号行上；不改动积分等冷数据。 */
function withRuntime(rows, stateById) {
  return rows.map(row => {
    const runtime = stateById.get(row.id) || {}
    return {
      ...row,
      status: runtime.status || row.status,
      enabled: runtime.status ? runtime.status !== 'disabled' : row.enabled !== false,
      in_flight: runtime.in_flight ?? row.in_flight ?? 0,
      max_concurrency: runtime.max_concurrency ?? row.max_concurrency ?? 1,
      fail_count: runtime.fail_count ?? row.fail_count ?? 0,
      cooldown_remaining_seconds: runtime.cooldown_remaining_seconds ?? row.cooldown_remaining_seconds ?? 0,
    }
  })
}

/**
 * @param {{cold?: boolean, credits?: 'auto'|'force'|'skip'}} options
 *   cold=false 只拉热数据（本机接口，不碰上游）；
 *   credits='force' 立刻重查插件积分，'skip' 沿用上次读数。
 */
async function load(options = {}) {
  const { cold = true, credits = 'auto' } = options
  // 手动刷新与定时器撞在一起时只跑一轮：并发重入会让下游看到重复的积分查询。
  if (loading) return loading
  loading = loadInner({ cold, credits }).finally(() => { loading = null })
  return loading
}

async function loadInner({ cold, credits }) {
  try {
    const [quotaReply, stateReply, guardReply] = await Promise.all([
      api('/ui/quota'),
      api('/ui/state').catch(() => null),
      api('/ui/protection').catch(() => null),
    ])

    const stateById = new Map()
    if (stateReply && Array.isArray(stateReply.accounts)) {
      for (const row of stateReply.accounts) stateById.set(row.id, row)
    }

    const local = (quotaReply.accounts || []).map(item => {
      const runtime = stateById.get(item.id) || {}
      return {
        id: item.id,
        name: item.name,
        provider: 'antigravity',
        driver: 'antigravity',
        models: item.models || [],
        updated_at: item.updated_at,
        error: item.error || '',
        // 状态以运行态为准；运行态缺失时退回配额接口报告的状态
        status: runtime.status || item.status || 'active',
        enabled: runtime.status ? runtime.status !== 'disabled' : item.status !== 'disabled',
        in_flight: runtime.in_flight ?? 0,
        max_concurrency: runtime.max_concurrency ?? 1,
        fail_count: runtime.fail_count ?? 0,
        cooldown_remaining_seconds: runtime.cooldown_remaining_seconds ?? 0,
      }
    })

    if (cold) {
      const statusReply = await rpc('provider.status').catch(() => null)
      model.statuses = statusReply?.value?.statuses || {}

      // 插件渠道的账号不在 /ui/quota 里（那是本机 OAuth 目录）。它们由插件持有：
      // 账号、令牌、积分都在那边。以前只画 antigravity，号池里有 10 个账号却只
      // 显示 2 个——这正是「号池怎么只看到两个」的原因。
      const statuses = model.statuses
      const pluginProviders = Object.keys(statuses).filter(
        id => id !== 'antigravity'
          && !RETIRED_CHANNELS.has(id)
          && (statuses[id]?.accounts?.total ?? 0) > 0,
      )
      const pluginLists = await Promise.all(pluginProviders.map(provider =>
        rpc('account.list', { provider }).catch(() => null)))

      // 积分对每个账号是一次上游请求，所以和账号列表分开取、按模式与到期判断。
      const wantCredits = credits === 'force' || (credits !== 'skip' && creditsDue(Date.now()))
      const creditLists = wantCredits
        ? await Promise.all(pluginProviders.map(provider =>
            rpc('credits.balances', { provider }).catch(() => null)))
        : []
      if (wantCredits) lastCreditsAt = Date.now()
      // 这一轮没查积分时沿用上一轮读数：把旧值按账号取回来，别让它变成「无读数」。
      const previousCredits = new Map()
      if (!wantCredits) {
        for (const row of externalRows) {
          if (row.balance || row.balanceError) previousCredits.set(row.id, row)
        }
      }

      const creditById = new Map()
      for (const reply of creditLists) {
        for (const row of reply?.value?.accounts || []) {
          if (row?.accountId) creditById.set(row.accountId, row)
        }
      }

      const seen = new Set(local.map(item => item.id))
      const external = []
      pluginLists.forEach((reply, index) => {
        const provider = pluginProviders[index]
        // 渠道级模型计数来自 provider.status；账号行自己没有这个数字。
        // 拿不到就保持 null（显示「—」），绝不能默认成 0——0 会被读成
        // 「这个账号一个模型都没有」，而事实是我们没查。
        const channelModels = statuses[provider]?.models?.total
        for (const row of reply?.value?.accounts || []) {
          if (!row?.id || seen.has(row.id)) continue
          seen.add(row.id)
          const runtime = stateById.get(row.id) || {}
          const cached = previousCredits.get(row.id) || null
          const credit = creditById.get(row.id) || null
          external.push({
            id: row.id,
            name: (row.nickname || '未命名账号').replace(/\s*·\s*冷却.*$/, ''),
            provider: row.provider || provider,
            driver: row.driver || 'channel-pack',
            models: null,
            modelCount: Number.isFinite(channelModels) ? channelModels : null,
            // 插件行自带 status；运行态有则以运行态为准
            status: runtime.status || row.status || 'active',
            enabled: runtime.status
              ? runtime.status !== 'disabled'
              : row.enabled !== false,
            in_flight: runtime.in_flight ?? row.in_flight ?? 0,
            max_concurrency: runtime.max_concurrency ?? row.max_concurrency ?? 1,
            fail_count: runtime.fail_count ?? row.fail_count ?? 0,
            cooldown_remaining_seconds: runtime.cooldown_remaining_seconds ?? row.cooldown_remaining_seconds ?? 0,
            rate_limited_models: row.rate_limited_models || [],
            // 这一轮没查积分时沿用上次读数，并带上它的时刻（页面会标出来）。
            balance: credit?.balance ?? cached?.balance ?? null,
            balanceError: credit?.error ?? cached?.balanceError ?? '',
            balanceAt: credit ? lastCreditsAt : (cached?.balanceAt ?? 0),
          })
        }
      })
      externalRows = external
      lastColdAt = Date.now()
    }

    model = {
      accounts: mergeAccounts(local, externalRows, stateById),
      models: stateReply?.models || model.models || [],
      api_base: quotaReply.api_base || stateReply?.api_base || model.api_base || '',
      api_key: quotaReply.api_key || stateReply?.api_key || model.api_key || '',
      statuses: model.statuses,
      protection: guardReply && guardReply.ok !== false ? guardReply : null,
    }
    connected = true
    lastHotAt = Date.now()
    renderAll()
    renderPollNote()
  } catch (error) {
    connected = false
    renderStatus()
    renderPollNote()
    const host = $('roster-host')
    if (error.status === 401) {
      host.innerHTML = `<div class="notice bad"><b>未连接本机网关</b>当前窗口没有有效会话，输入网关密钥后即可读取真实号池。</div>`
      requireConnect()
    } else {
      host.innerHTML = `<div class="notice bad"><b>读取号池失败</b>${esc(error.message)}</div>`
    }
  }
}

/**
 * 本机行每次重建，插件行沿用冷刷新缓存 —— 但插件账号如果在本机号池里也有
 * 同 id 的记录（导入过的），以本机行优先，避免同一个账号出现两次。
 */
function mergeAccounts(local, external, stateById) {
  const seen = new Set(local.map(item => item.id))
  const extra = withRuntime(external, stateById).filter(row => !seen.has(row.id))
  return [...local, ...extra]
}

/** 右下角那行小字：说清数据是几点的、下一轮什么时候来、为什么停了。 */
function renderPollNote() {
  const note = $('poll-note')
  if (!note) return
  const bits = []
  if (lastColdAt) bits.push(`数据 ${clockText(lastColdAt)}`)
  if (!connected) {
    note.dataset.paused = '1'
    note.textContent = bits.length ? `${bits.join(' · ')} · 已断开` : '已断开'
    return
  }
  if (document.hidden) {
    note.dataset.paused = '1'
    note.textContent = `${bits.join(' · ')} · 窗口不在前台，已暂停刷新`
    return
  }
  delete note.dataset.paused
  const credits = lastCreditsAt ? `积分 ${clockText(lastCreditsAt)}` : '积分未查'
  const mode = creditsMode === 'auto' ? '自动' : '仅手动'
  const hot = lastHotAt ? `热数据 ${clockText(lastHotAt)}` : ''
  note.textContent = [...bits, credits, hot, `积分${mode}`, `${Math.round(HOT_MS / 1000)} 秒/轮`]
    .filter(Boolean)
    .join(' · ')
}

/* ---------------------------------------------------------------- 连接 */
const connectScrim = $('connect-scrim')
const connectError = $('connect-error')

/*
 * 桌面端（launch_desktop.py）用 `#bootstrap=<一次性令牌>` 打开本页：令牌由
 * 启动器生成、只在启动时有效，换完会话就作废。旧界面（app.mjs）认这个 hash，
 * 重写控制台时漏了它 —— 于是双击 exe 开出来的窗口照样弹「粘贴网关密钥」，
 * 用户以为快捷方式坏了。
 *
 * 这里把它补回来：有 bootstrap 就静默换会话，并立刻把 hash 抹掉（刷新/截图
 * 都不该把它带出去）。换失败才退回手动输入框，并把原因写在框里。
 */
const bootstrapToken = new URLSearchParams(location.hash.slice(1)).get('bootstrap') || ''
if (bootstrapToken) history.replaceState(null, '', location.pathname + location.search)

$('origin-hint').textContent = location.origin

async function exchangeBootstrap() {
  if (!bootstrapToken) return false
  try {
    await api('/ui/session', {
      method: 'POST',
      headers: { Origin: location.origin },
      body: JSON.stringify({ bootstrap: bootstrapToken }),
    })
    connectScrim.hidden = true
    await load({ cold: true, credits: 'auto' })
    startTimers()
    return true
  } catch (error) {
    requireConnect(`启动令牌未能换取会话（${error.message}），请粘贴网关密钥。`)
    return false
  }
}

$('copy-key-path').addEventListener('click', async event => {
  event.preventDefault()
  const btn = event.currentTarget
  try {
    await navigator.clipboard.writeText('gateway\\.gateway-api-key')
    btn.textContent = '已复制'
  } catch {
    btn.textContent = '请手动复制'
  }
  setTimeout(() => { btn.textContent = '复制路径' }, 1400)
})

function requireConnect(message = '') {
  connectError.textContent = message
  connectScrim.hidden = false
  setTimeout(() => $('connect-key').focus(), 0)
}

$('connect-form').addEventListener('submit', async event => {
  event.preventDefault()
  const button = $('connect-submit')
  button.disabled = true
  connectError.textContent = ''
  try {
    await api('/ui/session', {
      method: 'POST',
      headers: { Origin: location.origin },
      body: JSON.stringify({ key: $('connect-key').value }),
    })
    $('connect-key').value = ''
    connectScrim.hidden = true
    await load({ cold: true, credits: 'auto' })
  } catch (error) {
    connectError.textContent = error.message
  } finally {
    button.disabled = false
  }
})

/* ---------------------------------------------------------------- 顶部与侧栏动作 */
// 手动刷新是用户明确要「现在就查」，所以冷热与积分都强制走一遍。
$('reload').addEventListener('click', () => load({ cold: true, credits: 'force' }))

$('add-account').addEventListener('click', () => {
  switchView('channels')
})

$('probe-all').addEventListener('click', async event => {
  const btn = event.currentTarget
  btn.disabled = true
  const results = []
  for (const account of model.accounts) {
    try {
      const reply = await rpc('account.test', { accountId: account.id })
      const value = reply.value || {}
      results.push(`${account.name || account.id}: ${value.ok ? `正常 ${value.ms} ms` : `失败 ${value.error || ''}`}`)
    } catch (error) {
      results.push(`${account.name || account.id}: 失败 ${error.message}`)
    }
  }
  btn.disabled = false
  window.alert(results.length ? results.join('\n') : '号池里没有账号')
})

$('filter').addEventListener('input', event => {
  filterText = event.target.value
  renderRoster()
})

$('model-filter').addEventListener('input', event => {
  modelFilterText = event.target.value
  renderModels()
})

$('due-refresh').addEventListener('click', async () => {
  try {
    await api('/ui/quota/refresh', {
      method: 'POST',
      body: JSON.stringify({ method: 'quota.refresh', payload: { accountId: '' } }),
    })
    await load({ cold: false })
    $('due-banner').hidden = true
  } catch (error) {
    window.alert(`刷新失败：${error.message}`)
  }
})

$('due-dismiss').addEventListener('click', () => {
  let nearest = null
  for (const pool of POOLS) {
    const { at } = poolResetAt(pool.id)
    if (at !== null && at - Date.now() <= 3600_000 && (nearest === null || at < nearest.at)) {
      nearest = { pool: pool.label, at }
    }
  }
  if (nearest) dismissedDue.add(`${nearest.pool}:${Math.floor(nearest.at / 60000)}`)
  $('due-banner').hidden = true
})

$('guard-resume').addEventListener('click', async () => {
  if (!window.confirm('确认已检查异常原因，恢复模型调度？恢复不会解除上游限制。')) return
  try {
    await api('/ui/protection', {
      method: 'POST',
      body: JSON.stringify({ method: 'protection.resume', payload: { acknowledge: true } }),
    })
    await load({ cold: false })
  } catch (error) {
    window.alert(`恢复失败：${error.message}`)
  }
})

document.querySelectorAll('[data-copy]').forEach(btn => {
  btn.addEventListener('click', async () => {
    const kind = btn.dataset.copy
    const raw = kind === 'api-key' ? (model.api_key || '') : ($(kind).textContent || '')
    try {
      await navigator.clipboard.writeText(raw)
      const old = btn.textContent
      btn.textContent = '已复制'
      setTimeout(() => { btn.textContent = old }, 1200)
    } catch {
      window.alert('复制失败，请手动选中复制')
    }
  })
})

$('open-docs').addEventListener('click', () => {
  window.open('/docs', '_blank', 'noopener')
})

/* 桌面端窗口控制：pywebview 暴露 window.pywebview.api 时才生效 */
const shell = window.pywebview?.api
for (const [id, action] of [
  ['win-min', 'minimize'],
  ['win-max', 'toggle_fullscreen'],
  ['win-close', 'destroy'],
]) {
  $(id).addEventListener('click', () => {
    try {
      shell?.[action]?.()
    } catch {
      /* 浏览器里打开时没有窗口可控制，忽略 */
    }
  })
}

/* ---------------------------------------------------------------- 启动 */
// 桌面端带一次性令牌打开时先换会话：省掉一次必然 401 的读盘，
// 也避免页面上先闪一下「未连接」再变正常。
if (!(await exchangeBootstrap())) {
  await load({ cold: true, credits: 'auto' })
}

/**
 * 定时器只在窗口前台跑。`document.hidden` 时把定时器停掉、把状态写清楚：
 * 一个最小化了一夜的窗口不该继续每分钟去问上游要积分。
 */
function startTimers() {
  stopTimers()
  if (document.hidden) { renderPollNote(); return }
  // 重置倒计时每 10 秒走一格（不重新发请求）
  hotTimer = setInterval(() => {
    renderResetStrip()
    if (currentView === 'pool') renderKpi()
    renderSide()
  }, TICK_MS)
  coldTimer = setInterval(() => {
    if (!connected) return
    const now = Date.now()
    if (now - lastColdAt >= COLD_MS) {
      void load({ cold: true, credits: creditsMode === 'auto' && creditsDue(now) ? 'auto' : 'skip' })
    } else {
      void load({ cold: false })
    }
  }, HOT_MS)
}

function stopTimers() {
  if (hotTimer !== null) { clearInterval(hotTimer); hotTimer = null }
  if (coldTimer !== null) { clearInterval(coldTimer); coldTimer = null }
}

startTimers()

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    stopTimers()
    renderPollNote()
    return
  }
  // 回到前台补一轮：这段时间里上游可能已经被别人打满了。
  void load({ cold: Date.now() - lastColdAt >= COLD_MS, credits: 'skip' })
  startTimers()
})

/* 积分刷新开关：自动（5 分钟）／手动。手动模式下只在用户点的时候查上游。 */
const creditsBtn = $('credits-mode')
function paintCreditsMode() {
  creditsBtn.textContent = creditsMode === 'auto' ? '积分 自动 5 分' : '积分 仅手动'
  creditsBtn.title = creditsMode === 'auto'
    ? '积分对每个账号都是一次真实上游请求，自动模式 5 分钟查一次。点一下改成只手动查。'
    : '积分只在你点的时候查：点「刷新」或本按钮立刻查一次。点一下恢复自动。'
}
paintCreditsMode()
creditsBtn.addEventListener('click', async () => {
  creditsMode = creditsMode === 'auto' ? 'manual' : 'auto'
  localStorage.setItem('gateway:credits-mode', creditsMode)
  paintCreditsMode()
  if (creditsMode === 'auto') await load({ cold: true, credits: 'force' })
  else renderPollNote()
})
