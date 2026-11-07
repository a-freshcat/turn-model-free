/* 导入窗口逻辑。
 *
 * 两种模式共用同一张表单，但字段完全不同：
 *   - gemini_api：只问一个 API Key。账号 ID、模型清单、并发数全部由本机
 *     网关于保存时决定（读官方 /v1beta/models 得到真实可用模型）。
 *   - 其他渠道：老的自有兼容上游导入，字段照旧。
 *
 * 之所以把「不该问用户的」都收起来：要求手写模型映射 JSON 等于让用户猜
 * 上游的模型 id，猜错就是一次 404，而这条链路上本机网关本来就能查到。
 */

const $ = id => document.getElementById(id)
const params = new URLSearchParams(location.search)
const requestId = params.get('request') || ''
let info = null

async function api(path, options = {}) {
  const { body, headers, ...rest } = options
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...rest,
    headers: { ...(body === undefined ? {} : { 'Content-Type': 'application/json' }), ...(headers || {}) },
    ...(body === undefined ? {} : { body }),
  })
  const text = await response.text()
  let data = null
  try { data = text ? JSON.parse(text) : null } catch { data = null }
  if (!response.ok || (data && data.ok === false)) {
    throw new Error(data?.error?.message || data?.detail || `HTTP ${response.status}`)
  }
  return data
}

function showKeyMode() {
  $('import-title').textContent = '添加 Gemini API Key'
  $('import-note').textContent = '粘贴一个 Google AI Studio 的 API Key 即可，其余由网关处理。'
  $('gemini-fields').hidden = false
  $('generic-fields').hidden = true
}

function showGenericMode() {
  $('import-title').textContent = info.demo ? '导入演示账号' : '导入已有上游'
  $('generic-fields').hidden = false
  $('gemini-fields').hidden = true
}

async function boot() {
  try {
    info = await api('/ui/import/' + encodeURIComponent(requestId))
  } catch (error) {
    $('import-note').textContent = '添加窗口不可用'
    $('import-error').textContent = error.message
    $('import-submit').disabled = true
    return
  }
  if (info.provider === 'gemini_api') {
    showKeyMode()
  } else {
    showGenericMode()
    $('import-note').textContent = info.demo
      ? '演示账号 · 不联网、不使用真实凭据 · 保存后返回控制台'
      : '导入已有的 OpenAI 兼容上游配置。'
    if (info.demo) {
      $('import-form').elements.id_visible.value = 'fixture-chat'
      $('import-form').elements.models_visible.value = '{"demo-chat":"fixture-chat"}'
      $('credential-fields').hidden = true
      $('import-form').elements.endpoint.required = false
    } else {
      $('import-form').elements.endpoint.required = true
    }
  }
}

$('import-form').addEventListener('submit', async event => {
  event.preventDefault()
  $('import-error').textContent = ''
  $('import-ok').textContent = ''
  const button = $('import-submit')
  button.disabled = true
  try {
    if (!info) throw new Error('请先连接本机网关')
    const data = new FormData(event.currentTarget)
    let account

    if (info.provider === 'gemini_api') {
      const key = String(data.get('gemini_key') || '').trim()
      if (!key) throw new Error('请先粘贴 Gemini API Key')
      account = {
        // ID 由本机生成：用户不需要为「哪把钥匙」想一个标识符。
        id: 'gemini-key-' + Date.now().toString(36),
        nickname: String(data.get('gemini_nickname') || '').trim(),
        token: key,
        models: {},
        max_concurrency: 1,
        status: 'active',
      }
    } else {
      const models = JSON.parse(data.get('models_visible') || '{}')
      if (!models || typeof models !== 'object' || Array.isArray(models)) {
        throw new Error('模型映射需要是 JSON 对象')
      }
      account = {
        id: data.get('id_visible'),
        nickname: data.get('nickname'),
        models,
        max_concurrency: Number(data.get('max_concurrency_visible') || 1),
        status: 'active',
      }
      if (!info.demo) {
        account.endpoint = data.get('endpoint')
        const type = data.get('credential_type')
        if (type) account[type] = data.get('credential')
      }
    }

    const saved = await api('/ui/import', {
      method: 'POST',
      headers: { Origin: location.origin },
      body: JSON.stringify({ request_id: requestId, account }),
    })
    const count = saved?.models
    $('import-form').elements.gemini_key.value = ''
    $('import-ok').textContent = info.provider === 'gemini_api'
      ? `已保存${count ? `，识别到 ${count} 个可用模型` : ''}。回到控制台点「刷新」即可使用。`
      : '账号已保存。回到控制台点「刷新」即可看到它。'
    button.textContent = '已保存'
  } catch (error) {
    $('import-error').textContent = error instanceof SyntaxError
      ? '模型映射不是有效的 JSON 对象'
      : error.message
    button.disabled = false
  }
})

$('import-close').addEventListener('click', () => window.close())

const theme = localStorage.getItem('gateway:theme') || 'paper'
document.body.dataset.theme = theme

await boot()
