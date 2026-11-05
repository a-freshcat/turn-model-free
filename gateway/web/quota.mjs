import { POOLS, poolFor, summarize, deduplicate } from './quota-models.mjs'
const $ = id => document.getElementById(id)
let accounts = []
let selected = new URLSearchParams(location.search).get('account') || ''
let dark = localStorage.getItem('gateway:theme') === 'dark'
const text = (tag, value, className) => {
  const node = document.createElement(tag)
  node.textContent = value
  if (className) node.className = className
  return node
}
const percent = value => typeof value !== 'number' || !Number.isFinite(value) ? '未知' : new Intl.NumberFormat('zh-CN', {maximumFractionDigits:2}).format(value) + '%'
function theme() { document.body.toggleAttribute('data-ds-dark-theme', dark) }
theme()
async function api(path, body) {
  const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', credentials:'same-origin',
    headers: body === undefined ? {} : {'Content-Type':'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body) })
  const result = await response.json()
  if (response.status === 401) $('quota-auth').hidden = false
  if (!response.ok || result.ok === false) throw new Error(result.error?.message || '请求失败')
  return result
}
function progress(value, label) {
  value = typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100 ? value : null
  const track = document.createElement('div')
  track.className = 'quota-track'
  track.setAttribute('role','progressbar')
  track.setAttribute('aria-label',label)
  track.setAttribute('aria-valuemin','0')
  track.setAttribute('aria-valuemax','100')
  if (value !== null) track.setAttribute('aria-valuenow',String(value))
  else track.setAttribute('aria-valuetext','配额未知')
  const fill = document.createElement('div')
  fill.className = 'quota-fill' + (value !== null && value <= 5 ? ' low' : value !== null && value <= 20 ? ' warning' : '')
  fill.style.width = (value ?? 0) + '%'
  track.append(fill)
  return track
}
function resetLabel(value) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : '重置：' + date.toLocaleString('zh-CN')
}
function poolCard(pool, models) {
  const card = document.createElement('section')
  card.className = 'quota-pool'
  card.append(text('h4',pool.name),text('p',pool.note,'quota-subtitle'))
  const state = summarize(models)
  card.append(text('div',percent(state.remaining),'quota-percent'),progress(state.remaining,pool.name+'剩余配额'))
  let message = models.length ? models.length + ' 个型号' : '尚未返回该池型号'
  if (state.remaining !== null && state.remaining !== state.maximum) {
    message = '返回 ' + percent(state.remaining) + '–' + percent(state.maximum) + '，状态条取最低值'
  } else if (state.known !== state.count && state.count) message += ' · 部分配额未知'
  card.append(text('p',message,'quota-small'))
  const details = document.createElement('details')
  const unique = deduplicate(models)
  details.append(text('summary','型号明细（' + unique.length + '）'))
  const rows = document.createElement('div')
  rows.className = 'quota-models'
  for (const model of unique) {
    const row = document.createElement('div')
    row.className = 'quota-model'
    const head = document.createElement('div')
    head.className = 'quota-model-head'
    head.append(text('span',model.name || model.id),text('strong',percent(model.remaining)))
    row.append(head,progress(model.remaining,model.name+'剩余配额'))
    const reset = resetLabel(model.reset_time)
    if (reset) row.append(text('div',reset,'quota-small'))
    const aliases = document.createElement('details')
    aliases.append(text('summary',model.aliases.length > 1 ? model.aliases.length + ' 个同名同配额别名' : '模型 ID'),
      text('div',model.aliases.join(' · '),'quota-aliases'))
    row.append(aliases)
    rows.append(row)
  }
  details.append(rows)
  card.append(details)
  return card
}
function render() {
  const visible = accounts.filter(account => !selected || account.id === selected)
  const sections = []
  for (const account of visible) {
    const section = document.createElement('section')
    section.className = 'quota-account-section'
    const heading = document.createElement('div')
    heading.className = 'quota-account-title'
    heading.append(text('h3',account.name),text('span',account.status === 'reauth' ? '需重新登录' : account.status === 'cooldown' ? '冷却中' : account.status === 'disabled' ? '已停用' : '已接入','ofm_note'))
    section.append(heading)
    if (account.error) section.append(text('p',account.error,'ofm_balanceerror quota-account-error'))
    const grid = document.createElement('div')
    grid.className = 'quota-grid'
    for (const pool of POOLS) grid.append(poolCard(pool,account.models.filter(model => poolFor(model) === pool.id)))
    section.append(grid)
    sections.push(section)
  }
  $('quota-accounts').replaceChildren(...sections)
  $('quota-empty').hidden = visible.length !== 0
  const timestamps = visible.map(account => account.updated_at).filter(value => typeof value === 'number' && Number.isFinite(value))
  $('quota-updated').textContent = timestamps.length
    ? '最近查询：' + new Date(Math.max(...timestamps)*1000).toLocaleString('zh-CN') : '尚未查询到配额'
}
async function load() {
  const result = await api('/ui/quota')
  $('quota-error').textContent = ''
  $('quota-auth-error').textContent = ''
  accounts = result.accounts
  if (selected && !accounts.some(account => account.id === selected)) selected = ''
  $('quota-account').replaceChildren(new Option('全部账号',''),...accounts.map(account => new Option(account.name,account.id)))
  $('quota-account').value = selected
  render()
}
$('quota-account').addEventListener('change',event => {
  selected = event.target.value
  history.replaceState(null,'',selected ? '/quota?account='+encodeURIComponent(selected) : '/quota')
  render()
})
$('quota-theme').addEventListener('click',() => {
  dark = !dark
  localStorage.setItem('gateway:theme',dark?'dark':'light')
  theme()
})
$('quota-refresh').addEventListener('click',async event => {
  event.currentTarget.disabled = true
  $('quota-error').textContent = ''
  try {
    const reply = await api('/ui/quota/refresh',{method:'quota.refresh',payload:{accountId:selected}})
    await load()
    $('quota-error').textContent = reply.errors.join('；')
  } catch(error) { $('quota-error').textContent = error.message }
  finally { $('quota-refresh').disabled = false }
})
$('quota-auth-form').addEventListener('submit',async event => {
  event.preventDefault()
  try {
    await api('/ui/session',{key:$('quota-key').value})
    $('quota-key').value = ''
    $('quota-auth').hidden = true
    await load()
  } catch(error) { $('quota-auth-error').textContent = error.message }
})
try { await load() } catch(error) { $('quota-error').textContent = error.message }
