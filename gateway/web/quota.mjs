import { POOLS, poolFor, summarize, deduplicate } from './quota-models.mjs'

const $ = id => document.getElementById(id)
let accounts = []
let selected = new URLSearchParams(location.search).get('account') || ''
let activeGroup = new URLSearchParams(location.search).get('group') || 'all'
let dark = localStorage.getItem('gateway:theme') === 'dark'

const CHANNEL_LABELS = {
  antigravity: 'Gemini 自用',
  gemini: 'Gemini 独立池',
  workbuddy: 'WorkBuddy 国内版',
  buddy: 'CodeBuddy 国际版',
  codearts: '华为云 CodeArts',
  lobsterai: '有道 LobsterAI',
  qoder: '阿里 Qoder',
  qodercn: '阿里 Qoder CN',
  trae: '字节 TRAE',
  cline: 'Cline 官方',
  loomy: '讯飞 Loomy',
  raccoon: '商汤 Raccoon',
  minimax: 'MiniMax Code',
  zcode: '智谱 ZCode',
}

const el = (tag, className, textContent) => {
  const node = document.createElement(tag)
  if (className) node.className = className
  if (textContent !== undefined && textContent !== null) node.textContent = textContent
  return node
}

const fmtNumber = value => new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(value)
const percentRemaining = value =>
  typeof value !== 'number' || !Number.isFinite(value) ? '未知' : fmtNumber(value) + '%'

const percentUsed = remaining => {
  if (typeof remaining !== 'number' || !Number.isFinite(remaining)) return null
  return Math.max(0, Math.min(100, Number((100 - remaining).toFixed(2))))
}

function theme() {
  document.body.toggleAttribute('data-ds-dark-theme', dark)
}
theme()

async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    credentials: 'same-origin',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const result = await response.json()
  if (response.status === 401) $('quota-auth').hidden = false
  if (!response.ok || result.ok === false) throw new Error(result.error?.message || '请求失败')
  return result
}

function formatResetTime(rawValue) {
  if (!rawValue) return ''
  const date = new Date(rawValue)
  if (Number.isNaN(date.getTime())) return ''
  const now = new Date()
  const sameDay =
    date.getFullYear() === now.getFullYear() &&
    date.getMonth() === now.getMonth() &&
    date.getDate() === now.getDate()
  const hh = String(date.getHours()).padStart(2, '0')
  const mm = String(date.getMinutes()).padStart(2, '0')
  if (sameDay) return `重置时间: ${hh}:${mm}`
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `重置时间: ${month}-${day} ${hh}:${mm}`
}

function earliestReset(models) {
  let bestTime = Infinity
  let bestRaw = ''
  for (const m of models) {
    if (!m.reset_time) continue
    const t = new Date(m.reset_time).getTime()
    if (Number.isFinite(t) && t < bestTime) {
      bestTime = t
      bestRaw = m.reset_time
    }
  }
  return formatResetTime(bestRaw)
}

function createUsageBar(remainingValue, label) {
  const valid =
    typeof remainingValue === 'number' &&
    Number.isFinite(remainingValue) &&
    remainingValue >= 0 &&
    remainingValue <= 100
  const used = valid ? percentUsed(remainingValue) : null
  const track = el('div', 'quota-track')
  track.setAttribute('role', 'progressbar')
  track.setAttribute('aria-label', label)
  track.setAttribute('aria-valuemin', '0')
  track.setAttribute('aria-valuemax', '100')
  if (used !== null) {
    track.setAttribute('aria-valuenow', String(used))
    track.setAttribute('aria-valuetext', `已使用 ${fmtNumber(used)}%，剩余 ${fmtNumber(remainingValue)}%`)
  } else {
    track.setAttribute('aria-valuetext', '配额未知')
  }

  const fill = el('div', 'quota-fill')
  if (valid) {
    if (remainingValue <= 5) fill.classList.add('low')
    else if (remainingValue <= 20) fill.classList.add('warning')
    // Ensure a subtle visible sliver even at 0% used so the bar reads clearly
    fill.style.width = (used === 0 ? 2 : used) + '%'
  } else {
    fill.classList.add('unknown')
    fill.style.width = '0%'
  }
  track.append(fill)
  return track
}

function buildUsagePoolCard(pool, models) {
  const card = el('section', 'usage-card')
  const state = summarize(models)
  const used = percentUsed(state.remaining)

  // Top row (Image 3 style): Left = Pool Name + info icon; Right = 已使用 X% · 剩余 Y%
  const head = el('div', 'usage-card-head')
  const titleWrap = el('div', 'usage-card-title')
  titleWrap.append(el('strong', 'usage-pool-name', pool.name))
  const infoBadge = el('span', 'usage-info-icon', 'i')
  infoBadge.setAttribute('aria-hidden', 'true')
  titleWrap.append(infoBadge)

  const metricWrap = el('div', 'usage-card-metrics')
  if (used !== null) {
    metricWrap.append(
      el('span', 'usage-used-label', `已使用 ${fmtNumber(used)}%`),
      el('span', 'usage-remain-pill', `剩余 ${percentRemaining(state.remaining)}`)
    )
  } else {
    metricWrap.append(el('span', 'usage-used-label muted', '用量未知'))
  }
  head.append(titleWrap, metricWrap)
  card.append(head)

  // Middle row (Image 3 style): Capsule progress bar
  card.append(createUsageBar(state.remaining, `${pool.name} 当前用量`))

  // Bottom row (Image 3 style): Reset time on left, model count / range note on right
  const foot = el('div', 'usage-card-foot')
  const resetText = earliestReset(models) || `规则: ${pool.note}`
  foot.append(el('span', 'usage-reset-text', resetText))

  let rangeNote = `${models.length} 个型号`
  if (state.remaining !== null && state.remaining !== state.maximum) {
    rangeNote = `剩余 ${percentRemaining(state.remaining)}–${percentRemaining(state.maximum)}（取最低）`
  } else if (state.known !== state.count && state.count) {
    rangeNote += ' · 部分未知'
  }
  foot.append(el('span', 'usage-range-note', rangeNote))
  card.append(foot)

  // Collapsible per-model details
  const unique = deduplicate(models)
  if (unique.length > 0) {
    const details = el('details', 'usage-details')
    details.append(el('summary', '', `型号明细（${unique.length}）`))
    const rows = el('div', 'quota-models')
    for (const model of unique) {
      const mUsed = percentUsed(model.remaining)
      const row = el('div', 'quota-model')
      const mHead = el('div', 'quota-model-head')
      mHead.append(
        el('span', 'quota-model-name', model.name || model.id),
        el(
          'strong',
          '',
          mUsed !== null
            ? `已用 ${fmtNumber(mUsed)}% · 剩余 ${percentRemaining(model.remaining)}`
            : '未知'
        )
      )
      row.append(mHead, createUsageBar(model.remaining, `${model.name || model.id} 当前用量`))
      const mReset = formatResetTime(model.reset_time)
      if (mReset) row.append(el('div', 'quota-small', mReset))

      const aliases = el('details', 'quota-alias-details')
      aliases.append(
        el('summary', '', model.aliases.length > 1 ? `${model.aliases.length} 个同名同配额别名` : '模型 ID'),
        el('div', 'quota-aliases', model.aliases.join(' · '))
      )
      row.append(aliases)
      rows.append(row)
    }
    details.append(rows)
    card.append(details)
  }
  return card
}

function buildExternalUsageCard(account) {
  const card = el('section', 'usage-card external-usage-card')
  const maxC = Math.max(1, account.max_concurrency || 1)
  const inFlight = account.in_flight || 0
  const loadPct = Math.min(100, Math.round((inFlight / maxC) * 100))
  const remainingLoad = Math.max(0, 100 - loadPct)

  const head = el('div', 'usage-card-head')
  const titleWrap = el('div', 'usage-card-title')
  titleWrap.append(el('strong', 'usage-pool-name', '当前并发负载'))
  const infoBadge = el('span', 'usage-info-icon', 'i')
  infoBadge.setAttribute('aria-hidden', 'true')
  titleWrap.append(infoBadge)

  const metricWrap = el('div', 'usage-card-metrics')
  metricWrap.append(
    el('span', 'usage-used-label', `已使用 ${loadPct}%`),
    el('span', 'usage-remain-pill', `空闲 ${maxC - inFlight}/${maxC}`)
  )
  head.append(titleWrap, metricWrap)
  card.append(head)
  card.append(createUsageBar(remainingLoad, `${account.name} 并发负载`))

  const foot = el('div', 'usage-card-foot')
  foot.append(
    el('span', 'usage-reset-text', '重置时间: 请求结束后实时释放'),
    el('span', 'usage-range-note', `失败计数 ${account.fail_count || 0}`)
  )
  card.append(foot)
  return card
}

function buildPlatformCell(account) {
  const wrap = el('div', 'acct-platform-cell')
  const isAg = account.driver === 'antigravity'
  const isDemo = account.driver === 'demo'

  const topBadges = el('div', 'acct-badge-row')
  if (isAg) {
    topBadges.append(
      el('span', 'acct-chip purple', '☁ Antigravity'),
      el('span', 'acct-chip purple-outline', '🔑 OAuth')
    )
  } else if (isDemo) {
    topBadges.append(
      el('span', 'acct-chip blue', '✦ Demo'),
      el('span', 'acct-chip blue-outline', 'Local')
    )
  } else {
    topBadges.append(
      el('span', 'acct-chip green', '◎ OpenAI'),
      el('span', 'acct-chip green-outline', '🔑 Key')
    )
  }

  const subBadges = el('div', 'acct-badge-row')
  if (isAg) {
    const hasArgon = (account.models || []).some(m => poolFor(m) === 'argon' && m.remaining !== null)
    subBadges.append(
      el('span', 'acct-chip tier-pro', hasArgon ? 'Pro' : 'Free'),
      el('span', 'acct-chip shield-private', '🛡 Private')
    )
  } else {
    subBadges.append(el('span', 'acct-subtext', '• Compact Auto'))
  }

  wrap.append(topBadges, subBadges)
  return wrap
}

function displayAccountName(account, idx = 0) {
  const raw = String(account.name || '').trim()
  if (!raw || raw === account.id || /^ag-[0-9a-f]{12,}$/i.test(raw)) {
    return `账号 #${idx + 1}`
  }
  return raw
}

function buildStatusBadge(account) {
  const st = account.status || 'active'
  if (st === 'active') return el('span', 'status-pill ok', '正常')
  if (st === 'disabled') return el('span', 'status-pill paused', '暂停')
  if (st === 'reauth') return el('span', 'status-pill err', '需重登')
  const sec = account.cooldown_remaining_seconds
  return el('span', 'status-pill warn', sec ? `冷却 ${sec}s` : '冷却中')
}

function buildScheduleSwitch(account) {
  const enabled = account.status !== 'disabled'
  const btn = el('button', 'schedule-switch')
  btn.type = 'button'
  btn.setAttribute('role', 'switch')
  btn.setAttribute('aria-checked', String(enabled))
  btn.setAttribute('aria-label', `调度 ${account.name || '账号'}`)
  const thumb = el('i', '')
  btn.append(thumb)
  btn.addEventListener('click', async () => {
    btn.disabled = true
    $('quota-error').textContent = ''
    try {
      await api('/ui/rpc', {
        method: 'account.update',
        payload: { accountId: account.id, patch: { enabled: !enabled } },
      })
      await load()
    } catch (error) {
      $('quota-error').textContent = error.message
      btn.disabled = false
    }
  })
  return btn
}

function buildGroupBadge(account) {
  const provider = account.provider || (account.driver === 'antigravity' ? 'antigravity' : 'buddy')
  const label = CHANNEL_LABELS[provider] || provider
  if (provider === 'antigravity' || provider === 'gemini') {
    return el('span', 'group-pill gemini-group', `☁ ${label}`)
  }
  return el('span', 'group-pill ext-group', `◎ ${label}`)
}

function renderSummaryPills(visible) {
  const container = $('quota-summary')
  if (!container) return
  const total = visible.length
  const activeCount = visible.filter(a => a.status === 'active').length
  const pausedCount = visible.filter(a => a.status === 'disabled').length
  const coolingCount = visible.filter(a => a.status === 'cooldown').length

  container.replaceChildren(
    el('span', 'ofm_pill strong', `共 ${total} 个账号`),
    el('span', 'ofm_pill', `正常 ${activeCount}`),
    el('span', 'ofm_pill', `冷却 ${coolingCount}`),
    el('span', 'ofm_pill', `暂停 ${pausedCount}`)
  )
}

function render() {
  const byGroup = accounts.filter(account => {
    if (activeGroup === 'antigravity') return account.driver === 'antigravity'
    if (activeGroup === 'external') return account.driver !== 'antigravity'
    return true
  })
  const visible = byGroup.filter(account => !selected || account.id === selected)
  renderSummaryPills(visible)

  const host = $('quota-accounts')
  if (!visible.length) {
    host.replaceChildren()
    $('quota-empty').hidden = false
  } else {
    $('quota-empty').hidden = true
    const wrapper = el('div', 'quota-table-wrapper')
    wrapper.setAttribute('tabindex', '0')
    wrapper.setAttribute('role', 'region')
    wrapper.setAttribute('aria-labelledby', 'quota-table-caption')

    const table = el('table', 'quota-table')
    const caption = el('caption', 'visually-hidden', '号池账号调度与配额用量表')
    caption.id = 'quota-table-caption'
    table.append(caption)

    const thead = el('thead')
    const headRow = el('tr')
    for (const col of ['ID', '平台/类型', '接口配额池 · 当前用量', '容量', '状态', '调度', '分组']) {
      const th = el('th', '', col)
      th.setAttribute('scope', 'col')
      headRow.append(th)
    }
    thead.append(headRow)
    table.append(thead)

    const tbody = el('tbody')
    visible.forEach((account, idx) => {
      const tr = el('tr', account.status === 'disabled' ? 'row-paused' : '')

      // 1. ID Column (Row header - sequence number followed by account name, never raw internal ID)
      const thId = el('th', 'col-id')
      thId.setAttribute('scope', 'row')
      const idRow = el('div', 'acct-id-row')
      const acctName = displayAccountName(account, idx)
      const nameSpan = el('span', 'acct-id-name', acctName)
      nameSpan.title = acctName
      idRow.append(el('span', 'acct-index', `#${idx + 1}`), nameSpan)
      thId.append(idRow)

      // 2. 平台/类型 Column
      const tdPlatform = el('td', 'col-platform')
      tdPlatform.append(buildPlatformCell(account))

      // 3. 接口配额池 · 当前用量 Column (Image 3 style bars)
      const tdUsage = el('td', 'col-usage')
      if (account.error) {
        tdUsage.append(el('p', 'ofm_balanceerror quota-account-error', account.error))
      }
      if (account.driver === 'antigravity') {
        const poolsGrid = el('div', 'usage-pools-grid')
        for (const pool of POOLS) {
          const poolModels = (account.models || []).filter(model => poolFor(model) === pool.id)
          poolsGrid.append(buildUsagePoolCard(pool, poolModels))
        }
        tdUsage.append(poolsGrid)
      } else {
        tdUsage.append(buildExternalUsageCard(account))
      }

      // 4. 容量 Column
      const tdCap = el('td', 'col-capacity')
      const inFlight = account.in_flight ?? 0
      const maxC = account.max_concurrency ?? 1
      tdCap.append(el('span', 'capacity-pill', `⊞ ${inFlight} / ${maxC}`))

      // 5. 状态 Column
      const tdStatus = el('td', 'col-status')
      tdStatus.append(buildStatusBadge(account))

      // 6. 调度 Column
      const tdSchedule = el('td', 'col-schedule')
      tdSchedule.append(buildScheduleSwitch(account))

      // 7. 分组 Column
      const tdGroup = el('td', 'col-group')
      tdGroup.append(buildGroupBadge(account))

      tr.append(thId, tdPlatform, tdUsage, tdCap, tdStatus, tdSchedule, tdGroup)
      tbody.append(tr)
    })

    table.append(tbody)
    wrapper.append(table)
    host.replaceChildren(wrapper)
  }

  const timestamps = visible
    .map(account => account.updated_at)
    .filter(value => typeof value === 'number' && Number.isFinite(value))
  $('quota-updated').textContent = timestamps.length
    ? '最近查询：' + new Date(Math.max(...timestamps) * 1000).toLocaleString('zh-CN')
    : '实时连接本机号池'
}

async function load() {
  const [quotaReply, stateReply] = await Promise.all([
    api('/ui/quota'),
    api('/ui/state').catch(() => null),
  ])
  $('quota-error').textContent = ''
  $('quota-auth-error').textContent = ''

  const stateById = new Map()
  if (stateReply && Array.isArray(stateReply.accounts)) {
    for (const s of stateReply.accounts) stateById.set(s.id, s)
  }

  // Merge Antigravity quota accounts with live runtime state (concurrency, cooldown)
  const merged = (quotaReply.accounts || []).map(ag => {
    const rt = stateById.get(ag.id) || {}
    return {
      ...ag,
      driver: 'antigravity',
      provider: 'antigravity',
      status: ag.status === 'reauth' ? 'reauth' : rt.status || ag.status || 'active',
      in_flight: rt.in_flight ?? 0,
      max_concurrency: rt.max_concurrency ?? 1,
      fail_count: rt.fail_count ?? 0,
      cooldown_remaining_seconds: rt.cooldown_remaining_seconds ?? 0,
    }
  })

  // Also fetch any non-Antigravity accounts so the pool table shows all grouped accounts like Image 2
  const seenIds = new Set(merged.map(a => a.id))
  if (stateById.size > seenIds.size) {
    try {
      const statusRpc = await api('/ui/rpc', { method: 'provider.status', payload: {} })
      const statuses = statusRpc.value?.statuses || {}
      const activeProviders = Object.keys(statuses).filter(
        ch => ch !== 'antigravity' && statuses[ch]?.accounts?.total > 0
      )
      const lists = await Promise.all(
        activeProviders.map(provider =>
          api('/ui/rpc', { method: 'account.list', payload: { provider } }).catch(() => null)
        )
      )
      for (const item of lists) {
        for (const row of item?.value?.accounts || []) {
          if (seenIds.has(row.id)) continue
          seenIds.add(row.id)
          const rt = stateById.get(row.id) || {}
          merged.push({
            id: row.id,
            name: (row.nickname || '未命名账号').replace(/\s*·\s*冷却.*$/, ''),
            provider: row.provider || 'buddy',
            driver: row.driver || 'openai',
            status: rt.status || row.status || 'active',
            in_flight: rt.in_flight ?? row.in_flight ?? 0,
            max_concurrency: rt.max_concurrency ?? row.max_concurrency ?? 1,
            fail_count: rt.fail_count ?? row.fail_count ?? 0,
            cooldown_remaining_seconds: rt.cooldown_remaining_seconds ?? 0,
            models: [],
          })
        }
      }
    } catch {
      // Fallback gracefully if RPC is not mocked in unit tests
    }
  }

  accounts = merged
  if (selected && !accounts.some(account => account.id === selected)) selected = ''
  $('quota-account').replaceChildren(
    new Option('全部账号', ''),
    ...accounts.map((account, idx) => new Option(`#${idx + 1} · ${displayAccountName(account, idx)}`, account.id))
  )
  $('quota-account').value = selected
  render()
}

$('quota-account').addEventListener('change', event => {
  selected = event.target.value
  const params = new URLSearchParams()
  if (selected) params.set('account', selected)
  if (activeGroup !== 'all') params.set('group', activeGroup)
  const qs = params.toString()
  history.replaceState(null, '', qs ? `/quota?${qs}` : '/quota')
  render()
})

for (const btn of document.querySelectorAll('#quota-group-tabs button')) {
  if (btn.dataset.group === activeGroup) {
    btn.setAttribute('aria-pressed', 'true')
  } else {
    btn.setAttribute('aria-pressed', 'false')
  }
  btn.addEventListener('click', () => {
    activeGroup = btn.dataset.group || 'all'
    for (const b of document.querySelectorAll('#quota-group-tabs button')) {
      b.setAttribute('aria-pressed', String(b === btn))
    }
    render()
  })
}

$('quota-back')?.addEventListener('click', event => {
  event.preventDefault()
  // If opened as a new tab/window from the channel page, focus opener and close this window
  if (window.opener && !window.opener.closed) {
    try {
      window.opener.focus()
    } catch {}
    window.close()
    return
  }
  // Try closing even if opener was stripped (allowed in Chromium when history.length === 1)
  window.close()
  // Fallback if navigated within the same tab
  if (window.history.length > 1 && document.referrer.startsWith(location.origin)) {
    window.history.back()
  } else {
    window.location.replace('/')
  }
})

$('quota-theme').addEventListener('click', () => {
  dark = !dark
  localStorage.setItem('gateway:theme', dark ? 'dark' : 'light')
  theme()
})

$('quota-refresh').addEventListener('click', async event => {
  event.currentTarget.disabled = true
  $('quota-error').textContent = ''
  try {
    const reply = await api('/ui/quota/refresh', {
      method: 'quota.refresh',
      payload: { accountId: selected },
    })
    await load()
    $('quota-error').textContent = (reply.errors || []).join('；')
  } catch (error) {
    $('quota-error').textContent = error.message
  } finally {
    $('quota-refresh').disabled = false
  }
})

$('quota-auth-form').addEventListener('submit', async event => {
  event.preventDefault()
  try {
    await api('/ui/session', { key: $('quota-key').value })
    $('quota-key').value = ''
    $('quota-auth').hidden = true
    await load()
  } catch (error) {
    $('quota-auth-error').textContent = error.message
  }
})

try {
  await load()
} catch (error) {
  $('quota-error').textContent = error.message
}
