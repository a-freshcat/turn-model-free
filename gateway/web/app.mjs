// Reuse the original page factory, React runtime, icons and CSS without edits.
const upstream = window.__OFM_DEMO__
const { React, ReactDOM } = upstream
const { createPage, CHANNEL_DICT } = upstream.load('src/app/page.mjs')
const $ = id => document.getElementById(id)
const connecting = location.pathname === '/connect'
const initialHash = new URLSearchParams(location.hash.slice(1))
const bootstrap = initialHash.get('bootstrap') || ''
if (bootstrap) history.replaceState(null, '', location.pathname + location.search)
let locale = localStorage.getItem('gateway:locale') || 'zh'
let dark = (localStorage.getItem('gateway:theme') || 'light') === 'dark'
let page
let connected = false
let root
let modelSignature = ''
let chatController
let importInfo
let showAllModels = false
const dict = {
  zh: { ...CHANNEL_DICT.zh,
    'chan.sub': '把各家的账号与模型接进来：统一 OpenAI 接口，轮询调度，失败冷却后切换健康账号。凭据保存在本机。',
    'chan.act.refresh': '刷新状态',
    'chan.noAccount': '还没有账号——点「添加账号」开始登录。',
    'chan.expires.never': '本机管理',
    'chan.credits.unavailable': '未接入配额查询',
    'chan.balance.absent': '余额/签到：本机网关未接入该渠道的查询接口。账号本身可用，配额以你自己的上游账号为准。',
    // 12 家走厂商登录（插件 channel-pack 在后台轮询），不是填表。
    'chan.login.title': '登录 {name}',
    'chan.login.step1': '在打开的页面完成该厂商的登录（扫码、手机号或 SSO）。密码只输入在厂商页面。',
    'chan.login.step2': '登录成功后本页会自动完成，无需复制任何东西。',
    'chan.login.open': '打开登录页',
    'chan.login.waiting': '已打开登录页，等待授权…（最长等待 30 分钟）',
    'chan.login.failed': '登录未完成：{reason}',
    'chan.import.title': '导入 {name} 配置',
    'chan.import.step1': '在打开的窗口中填写已有账号与模型配置。',
    'chan.import.step2': '保存后此页会自动更新，无需重复添加。',
    'chan.import.open': '打开配置窗口',
    'chan.import.waiting': '等待账号配置保存…（最长等待 10 分钟）',
    'chan.oauth.title': '登录 {name}',
    'chan.oauth.step1': '在 Google 页面选择账号并确认授权。密码仅输入在 Google 页面。',
    'chan.oauth.step2': '完成后会返回本机，保存授权并自动加载账号模型与配额。',
    'chan.oauth.open': '打开 Google 授权页',
    'chan.oauth.waiting': '等待 Google 授权完成…（最长等待 10 分钟）',
  },
  en: { ...CHANNEL_DICT.en,
    'chan.sub': 'Bring your accounts and models into one OpenAI-compatible endpoint, with round-robin routing and cooldown failover. Credentials stay on this machine.',
    'chan.act.refresh': 'Refresh status',
    'chan.noAccount': 'No accounts yet — use Add account to sign in.',
    'chan.expires.never': 'Locally managed',
    'chan.credits.unavailable': 'Quota not connected',
    'chan.balance.absent': 'Balance / check-in: this gateway has no query API for this channel. The account itself works; your upstream account holds the quota.',
    'chan.login.title': 'Sign in to {name}',
    'chan.login.step1': 'Finish the vendor sign-in in the page that opened (QR, phone, or SSO). The password only goes to the vendor.',
    'chan.login.step2': 'This page completes by itself once the browser flow does — nothing to copy.',
    'chan.login.open': 'Open sign-in page',
    'chan.login.waiting': 'Sign-in page opened; waiting for authorization… (up to 30 minutes)',
    'chan.login.failed': 'Sign-in did not complete: {reason}',
    'chan.import.title': 'Import {name} configuration',
    'chan.import.step1': 'Fill in your account and model configuration in the new window.',
    'chan.import.step2': 'Save it and this page updates automatically.',
    'chan.import.open': 'Open configuration',
    'chan.import.waiting': 'Waiting for the configuration to be saved…',
    'chan.oauth.title': 'Sign in to {name}',
    'chan.oauth.step1': 'Choose your account on Google and approve the requested access.',
    'chan.oauth.step2': 'The browser returns locally to save authorization and load models and quota.',
    'chan.oauth.open': 'Open Google authorization',
    'chan.oauth.waiting': 'Waiting for Google authorization…',
  },
}
function setTheme() {
  document.body.toggleAttribute('data-ds-dark-theme', dark)
  $('theme').textContent = dark ? '切换浅色' : '切换深色'
}
setTheme()
$('locale').value = locale
function log(method, ok, elapsed) {
  const row = document.createElement('div')
  row.className = 'log-row' + (ok ? '' : ' bad')
  const name = document.createElement('code')
  name.textContent = method + '()'
  const timing = document.createElement('span')
  timing.textContent = Math.round(elapsed) + 'ms'
  row.append(name, timing)
  $('rpc-log').prepend(row)
  while ($('rpc-log').children.length > 100) $('rpc-log').lastElementChild.remove()
}
function requireLogin(message = '') {
  connected = false
  $('auth-error').textContent = message
  $('auth-scrim').hidden = false
  setTimeout(() => $('gateway-key').focus(), 0)
}
async function api(path, body, signal) {
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body), signal,
  })
  const data = await response.json()
  if (!response.ok || data.ok === false) {
    if (response.status === 401) requireLogin()
    throw new Error(data.error?.message || '请求失败')
  }
  return data
}
const connection = {
  rpc: {
    async call(_scope, _plugin, request, signal) {
      const started = performance.now()
      try {
        const result = await api('/ui/rpc', request, signal)
        log(request.method, true, performance.now() - started)
        return result
      } catch (error) {
        log(request.method, false, performance.now() - started)
        throw error
      }
    },
  },
}
function mount() {
  if (connecting) return
  if (!root) root = ReactDOM.createRoot($('page-root'))
  page = createPage({ React, h: React.createElement, Fragment: React.Fragment, locale, dict, timers: window })
  root.render(React.createElement(page.ChannelsPage, {
    t: page.t, ctx: { connection }, summary: { channels: { state: 'ready', error: '' } },
  }))
}
async function updateStatus() {
  const state = await api('/ui/state')
  const active = state.accounts.filter(a => a.status === 'active').length
  const cooling = state.accounts.filter(a => a.status === 'cooldown').length
  const inflight = state.accounts.reduce((n, a) => n + a.in_flight, 0)
  $('service-state').textContent = '可用 ' + active + ' · 冷却 ' + cooling + ' · 请求中 ' + inflight
  $('mode-note').textContent = state.demo
    ? '本机演示号池 · 修改仅在本次运行生效 · 账号状态来自当前网关'
    : '本机号池 · 账号配置保存在本机 · 统一 API 与渠道卡共享同一调度器'
  $('api-base').textContent = state.api_base
  const details = state.model_details || {}
  const listed = state.models.filter(model => showAllModels || details[model]?.pool !== 'gemini_shared')
  const signature = JSON.stringify([listed, details, showAllModels])
  if (signature !== modelSignature) {
    const current = $('chat-model').value
    const groups = [
      ['argon','Gemini 4 Argon · 独立池'], ['claude_gpt','Claude / GPT · 独立池'],
      ['gemini_shared','其他 Gemini / 内部型号 · 共享池'], [null,'其他渠道'],
    ].map(([pool,label]) => {
      const group = document.createElement('optgroup')
      group.label = label
      for (const model of listed.filter(model => (details[model]?.pool ?? null) === pool)) {
        group.append(new Option(details[model]?.name || model, model))
      }
      return group
    }).filter(group => group.children.length)
    $('chat-model').replaceChildren(...groups)
    if (listed.includes(current)) $('chat-model').value = current
    $('model-more').textContent = showAllModels ? '只显示常用型号' : '显示其他 Gemini / 内部型号'
    modelSignature = signature
  }
  return state
}
async function loadImport() {
  const requestId = new URLSearchParams(location.search).get('request') || ''
  importInfo = await api('/ui/import/' + encodeURIComponent(requestId))
  importInfo.requestId = requestId
  $('import-note').textContent = importInfo.demo
    ? '演示账号 · 不联网、不使用真实凭据 · 保存后返回主页面'
    : '导入已有的 OpenAI 兼容上游配置。此窗口不会发起厂商 OAuth 登录。'
  $('endpoint-field').hidden = importInfo.demo
  $('credential-fields').hidden = importInfo.demo
  $('import-form').elements.endpoint.required = !importInfo.demo
  if (importInfo.demo) $('import-form').elements.models.value = '{"demo-chat":"fixture-chat"}'
}
async function ready() {
  await updateStatus()
  connected = true
  $('auth-scrim').hidden = true
  $('gateway-key').value = ''
  if (connecting) await loadImport()
  else mount()
}
$('auth-form').addEventListener('submit', async event => {
  event.preventDefault()
  const button = event.currentTarget.querySelector('button')
  button.disabled = true
  try {
    await api('/ui/session', { key: $('gateway-key').value })
    await ready()
  } catch (error) { $('auth-error').textContent = error.message }
  finally { button.disabled = false }
})
$('theme').addEventListener('click', () => {
  dark = !dark
  localStorage.setItem('gateway:theme', dark ? 'dark' : 'light')
  setTheme()
})
$('locale').addEventListener('change', event => {
  locale = event.target.value
  localStorage.setItem('gateway:locale', locale)
  mount()
})
$('refresh').addEventListener('click', async () => {
  try {
    await updateStatus()
    window.dispatchEvent(new CustomEvent('ofm:channels'))
    mount()
  } catch (error) { requireLogin(error.message) }
})
$('reconnect').addEventListener('click', () => requireLogin())
$('copy-api').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText($('api-base').textContent)
    page?.showToast({ title: 'API 地址已复制', tone: 'ok' })
  } catch { page?.showToast({ title: '请手动复制 API 地址', tone: 'warn' }) }
})
$('import-close').addEventListener('click', () => window.close())
$('import-form').addEventListener('submit', async event => {
  event.preventDefault()
  $('import-error').textContent = ''
  $('import-submit').disabled = true
  try {
    if (!importInfo) throw new Error('请先连接本机网关')
    const data = new FormData(event.currentTarget)
    const models = JSON.parse(data.get('models'))
    if (!models || typeof models !== 'object' || Array.isArray(models)) throw new Error('模型映射需要是 JSON 对象')
    const account = {
      id: data.get('id'), nickname: data.get('nickname'), models,
      max_concurrency: Number(data.get('max_concurrency')), status: 'active',
    }
    if (!importInfo.demo) {
      account.endpoint = data.get('endpoint')
      const type = data.get('credential_type')
      if (type) account[type] = data.get('credential')
    }
    await api('/ui/import', { request_id: importInfo.requestId, account })
    $('import-form').elements.credential.value = ''
    $('import-note').textContent = '账号已保存，主页面会自动更新。现在可以关闭此窗口。'
    $('import-submit').textContent = '已保存'
  } catch (error) {
    $('import-error').textContent = error instanceof SyntaxError ? '模型映射不是有效的 JSON 对象' : error.message
    $('import-submit').disabled = false
  }
})
function addChatEvent(data) {
  if (data === '[DONE]') return
  const value = JSON.parse(data)
  if (value.error) throw new Error(value.error.message)
  for (const choice of value.choices || []) {
    const delta = choice.delta || {}
    $('chat-output').textContent += delta.content || delta.refusal || ''
    if (delta.tool_calls) $('chat-output').textContent += JSON.stringify(delta.tool_calls)
  }
  if ($('chat-output').textContent.length > 1024 * 1024) throw new Error('显示内容超过上限')
}
$('chat-form').addEventListener('submit', async event => {
  event.preventDefault()
  chatController = new AbortController()
  $('chat-submit').disabled = true
  $('chat-stop').disabled = false
  $('chat-output').textContent = ''
  try {
    const response = await fetch('/ui/chat', {
      method: 'POST', credentials: 'same-origin', signal: chatController.signal,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: $('chat-model').value,
        messages: [{ role: 'user', content: $('chat-message').value }], stream: true }),
    })
    if (!response.ok) {
      const value = await response.json()
      if (response.status === 401) requireLogin()
      throw new Error(value.error?.message || '测试请求失败')
    }
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let completed = false
    try {
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        let boundary
        while ((boundary = buffer.indexOf('\n\n')) >= 0) {
          const frame = buffer.slice(0, boundary)
          buffer = buffer.slice(boundary + 2)
          const data = frame.split('\n').filter(line => line.startsWith('data: ')).map(line => line.slice(6)).join('\n')
          if (data === '[DONE]') completed = true
          if (data) addChatEvent(data)
        }
      }
      if (!completed) throw new Error('连接在完整结束前断开')
    } finally {
      await reader.cancel().catch(() => {})
      reader.releaseLock()
    }
    if (!$('chat-output').textContent) $('chat-output').textContent = '请求已完成。'
  } catch (error) {
    $('chat-output').textContent += '\n' + (error.name === 'AbortError' ? '已停止' : '请求失败：' + error.message)
  } finally {
    $('chat-submit').disabled = false
    $('chat-stop').disabled = true
    chatController = undefined
    if (connected) updateStatus().catch(() => {})
  }
})
$('chat-stop').addEventListener('click', () => chatController?.abort())
$('model-more').addEventListener('click', async () => {
  showAllModels = !showAllModels
  if (connected) await updateStatus()
})
if (connecting) {
  $('dashboard-shell').hidden = true
  $('connect-shell').hidden = false
}
try {
  if (bootstrap) await api('/ui/session', { bootstrap })
  await ready()
} catch (error) { requireLogin(error.message) }
setInterval(() => { if (connected && !connecting) updateStatus().catch(() => {}) }, 5000)
