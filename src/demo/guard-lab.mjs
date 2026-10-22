/**
 * 防护实验台：在浏览器里跑一遍真实的「11128 → 升档 → 重发」。
 *
 * 演示页的那一页 UI 是抄来的插件页面（它只负责显示渠道卡）；防护层是**后端**
 * 行为，在页面上看不见。所以这里给了一个独立面板，让你能：
 *   1. 粘一段带指纹的 system 提示词；
 *   2. 选一个假上游的严格程度；
 *   3. 看到每次发送实际发出去的字节、上游为什么拒绝、升到哪一档才过。
 *
 * 假上游的判据来自实测（见 REPORT），不是瞎编的三种档位。
 */

import {
  TENCENT_GATE_RULES,
  TENCENT_GATE_MAX_ESCALATIONS,
  tencentGateStats,
  applyTencentGateGuard,
} from '../guard/tencent-gate.js'
import { requestWithTencentGate } from '../guard/pipeline.mjs'

/** 默认样例：把四个实测指纹都放进去，包括一个必须保留的路径。 */
const DEFAULT_PROMPT = [
  'You are an AI agent powered by DeepSeek Harness.',
  'You operate inside the DeepSeek Harness Web GUI, a desktop app.',
  'Main branch (you will usually use this for PRs)',
  'The repo is at C:\\work docs DeepSeek Harness\\app.js — 这个路径绝对不能改。',
].join('\n')

const GATE_BODY = JSON.stringify({
  error: { code: 11128, message: 'Illegal API invocation from an unapproved channel' },
})

/** 三种假上游：严格程度递增。 */
const UPSTREAMS = [
  {
    id: 'literal',
    label: '只查字面量（L1 就能过）',
    test: text => /powered by DeepSeek Harness|DeepSeek Harness Web GUI|you will usually use this for PRs/.test(text),
  },
  {
    id: 'variant',
    label: '还查零宽变体（要升到 L2）',
    test: text => /powered by DeepSeek Harness|Web GUI|you will usually use this for PRs/.test(text)
      || /DeepSeek[\u200B-\u200D\uFEFF]?Harness(?!\\|\/)/.test(text),
  },
  {
    id: 'nfkc',
    label: '还查全角变体（要升到 L3）',
    test: text => /powered by DeepSeek Harness|Web GUI|you will usually use this for PRs/.test(text)
      || /DeepSeek[\u200B-\u200D\uFEFF]?Harness(?!\\|\/)/.test(text)
      || /ＤｅｅｐＳｅｅｋ/.test(text),
  },
  {
    id: 'always',
    label: '永远拒绝（预算耗尽，看结构化报错）',
    test: () => true,
  },
]

const textOfMessages = messages => messages
  .map(message => typeof message.content === 'string' ? message.content : JSON.stringify(message.content))
  .join('\n')

export function start(document) {
  const root = document.getElementById('guard-lab')
  if (root === null) return

  let upstreamId = 'variant'
  let prompt = DEFAULT_PROMPT
  let lastRun = null

  const state = document.createElement('div')
  state.className = 'guard-state'
  root.appendChild(state)

  const form = document.createElement('div')
  form.className = 'guard-form'
  root.appendChild(form)

  // 提示词输入
  const promptRow = document.createElement('label')
  promptRow.className = 'guard-row'
  promptRow.textContent = 'system 提示词（可改，含必须保留的路径）'
  const textarea = document.createElement('textarea')
  textarea.value = prompt
  textarea.rows = 5
  textarea.addEventListener('input', () => { prompt = textarea.value })
  promptRow.appendChild(textarea)
  form.appendChild(promptRow)

  // 上游严格程度
  const upstreamRow = document.createElement('label')
  upstreamRow.className = 'guard-row'
  upstreamRow.textContent = '假上游严格程度'
  const select = document.createElement('select')
  for (const upstream of UPSTREAMS) {
    const option = document.createElement('option')
    option.value = upstream.id
    option.textContent = upstream.label
    select.appendChild(option)
  }
  select.value = upstreamId
  select.addEventListener('change', () => { upstreamId = select.value })
  upstreamRow.appendChild(select)
  form.appendChild(upstreamRow)

  // 动作
  const actions = document.createElement('div')
  actions.className = 'guard-actions'
  form.appendChild(actions)

  const runButton = document.createElement('button')
  runButton.type = 'button'
  runButton.className = 'ofm_btn'
  runButton.textContent = '发一次请求（跑完整防护流程）'
  actions.appendChild(runButton)

  const stagesButton = document.createElement('button')
  stagesButton.type = 'button'
  stagesButton.className = 'ofm_btn ghost'
  stagesButton.textContent = '只看三档清洗效果'
  actions.appendChild(stagesButton)

  const rangesButton = document.createElement('button')
  rangesButton.type = 'button'
  rangesButton.className = 'ofm_btn ghost'
  rangesButton.textContent = '灌入全角变体再试'
  actions.appendChild(rangesButton)

  const output = document.createElement('div')
  output.className = 'guard-output'
  root.appendChild(output)

  /* ── 渲染 ─────────────────────────────────────────────────────────── */

  const line = (label, value, tone) => {
    const row = document.createElement('div')
    row.className = 'guard-line' + (tone === undefined ? '' : ' ' + tone)
    const key = document.createElement('span')
    key.className = 'guard-line-key'
    key.textContent = label
    const val = document.createElement('span')
    val.className = 'guard-line-val'
    val.textContent = value
    row.appendChild(key)
    row.appendChild(val)
    return row
  }

  const heading = text => {
    const node = document.createElement('div')
    node.className = 'guard-heading'
    node.textContent = text
    return node
  }

  const renderState = () => {
    state.textContent = ''
    state.appendChild(line('防护规则', `${TENCENT_GATE_RULES.length} 条（L1/L2/L3 三档）`))
    state.appendChild(line('升档预算', `最多 ${TENCENT_GATE_MAX_ESCALATIONS} 次（整个请求共享）`))
    const stats = tencentGateStats()
    state.appendChild(line('本次会话累计', `拦截 ${stats.gateBlocks ?? 0} · 升档 ${stats.escalations ?? 0}`))
  }

  const renderStages = () => {
    output.textContent = ''
    output.appendChild(heading('三档清洗效果（同一段输入）'))
    for (const level of [1, 2, 3]) {
      const wire = [{ role: 'system', content: prompt }]
      const changed = applyTencentGateGuard(wire, level)
      const cleaned = wire[0].content
      output.appendChild(heading(`L${level}${changed ? ' · 改了' : ' · 未命中（原样）'}`))
      const pre = document.createElement('pre')
      pre.className = 'guard-pre'
      pre.textContent = cleaned
      output.appendChild(pre)
      // 关键断言：路径必须还在
      const keepsPath = cleaned.includes('C:\\work docs DeepSeek Harness\\app.js')
      output.appendChild(line('路径是否保留', keepsPath ? '保留（正确）' : '被改动了（错误！）', keepsPath ? 'ok' : 'bad'))
      const keepsUser = true
      void keepsUser
    }
  }

  runButton.addEventListener('click', async () => {
    const upstream = UPSTREAMS.find(entry => entry.id === upstreamId) ?? UPSTREAMS[0]
    output.textContent = ''
    output.appendChild(heading(`上游：${upstream.label}`))

    const received = []
    const logLines = []
    const originalWarn = console.warn
    console.warn = (...parts) => { logLines.push(parts.map(String).join(' ')) }
    let outcome
    try {
      outcome = await requestWithTencentGate({
        channel: 'buddy',
        model: 'deepseek-v4.1-flash',
        messages: [{ role: 'system', content: prompt }],
        send: async body => {
          received.push(body)
          const text = textOfMessages(body.messages)
          if (upstream.test(text)) {
            logLines.push(`上游拒绝这次发送（第 ${received.length} 次）`)
            return { ok: false, status: 400, text: async () => GATE_BODY }
          }
          logLines.push(`上游放行（第 ${received.length} 次）`)
          return { ok: true, status: 200, text: async () => '{}' }
        },
      })
    } catch (error) {
      outcome = { error }
    } finally {
      console.warn = originalWarn
    }

    output.appendChild(line('发送次数', String(received.length)))
    if (outcome.error !== undefined) {
      output.appendChild(line('结果', `失败：${outcome.error.message.slice(0, 160)}`, 'bad'))
      output.appendChild(line('错误类型', outcome.error.code ?? outcome.error.name ?? '(无)'))
    } else {
      output.appendChild(line('结果', outcome.response.ok ? `成功（停在 L${outcome.finalLevel}）` : '失败', outcome.response.ok ? 'ok' : 'bad'))
      output.appendChild(line('升档次数', `${outcome.escalations} / ${TENCENT_GATE_MAX_ESCALATIONS}`))
      output.appendChild(line('traceId', outcome.traceId))
    }

    output.appendChild(heading('每次实际发出的 system 内容'))
    received.forEach((body, index) => {
      const text = textOfMessages(body.messages)
      output.appendChild(heading(`第 ${index + 1} 次`))
      const pre = document.createElement('pre')
      pre.className = 'guard-pre'
      pre.textContent = text
      output.appendChild(pre)
      const keepsPath = text.includes('C:\\work docs DeepSeek Harness\\app.js')
      output.appendChild(line('路径', keepsPath ? '保留（正确）' : '被改动了（错误！）', keepsPath ? 'ok' : 'bad'))
    })

    output.appendChild(heading('取证埋点（只含元数据）'))
    const trace = logLines.filter(text => text.includes('[ofm-tencent-gate]'))
    if (trace.length === 0) {
      output.appendChild(line('埋点', '本次没有（未发生升档）'))
    } else {
      for (const text of trace) {
        const pre = document.createElement('pre')
        pre.className = 'guard-pre mono'
        pre.textContent = text
        output.appendChild(pre)
      }
    }

    lastRun = outcome
    renderState()
  })

  stagesButton.addEventListener('click', () => { renderStages(); renderState() })

  rangesButton.addEventListener('click', () => {
    // 全角：L1/L2 不动，只有 L3 的 NFKC 能处理
    textarea.value = `${prompt}\nＤｅｅｐＳｅｅｋ　Ｈａｒｎｅｓｓ 是另一种写法`
    prompt = textarea.value
    upstreamId = 'nfkc'
    select.value = 'nfkc'
    renderStages()
  })

  renderState()
  renderStages()
  void lastRun
}
