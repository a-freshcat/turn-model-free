/**
 * 用**原生 Response** 复现审查报告指出的两个缺陷。
 *
 * 为什么要单独写：原有测试用的是 `{ text: async () => ... }` 这种可重复读取的
 * 假响应，而原生 `Response` 的 body 只能读一次。假对象把真实的读取限制掩盖掉了
 * —— 这正是 `main_repai` 交接文档里那条教训的翻版：
 * "mock 替真实依赖补了它不提供的属性，就掩盖了真实缺陷"。
 */

import { requestWithTencentGate } from '../src/guard/pipeline.mjs'

const GATE = JSON.stringify({
  error: { code: 11128, message: 'Illegal API invocation from an unapproved channel' },
})

const results = []
const record = (name, passed, detail) => { results.push({ name, passed, detail }) }

/* ── 1. 持续 11128：应抛出结构化错误 ─────────────────────────────────────── */
{
  const sent = []
  let outcome
  try {
    await requestWithTencentGate({
      channel: 'buddy',
      model: 'm',
      messages: [{ role: 'system', content: 'You are an AI agent powered by DeepSeek Harness.' }],
      send: async body => {
        sent.push(body)
        // 关键：原生 Response，不是假对象
        return new Response(GATE, { status: 400, headers: { 'content-type': 'application/json' } })
      },
    })
    outcome = 'no throw'
  } catch (error) {
    outcome = error
  }
  const threw = outcome !== 'no throw' && typeof outcome === 'object'
  record(
    '持续 400/11128 时抛出结构化错误（原生 Response）',
    threw,
    threw
      ? `抛出 ${outcome.constructor.name} code=${outcome.code}`
      : `未抛错，返回了响应对象（发送 ${sent.length} 次）`,
  )
}

/* ── 2. 普通 400：调用方应能读取错误详情 ─────────────────────────────────── */
{
  const body = JSON.stringify({ error: { code: 20001, message: 'model not found' } })
  let detail = ''
  let failure = ''
  try {
    const { response } = await requestWithTencentGate({
      channel: 'buddy',
      model: 'nope',
      messages: [{ role: 'system', content: 'clean text' }],
      send: async () => new Response(body, { status: 400 }),
    })
    try {
      detail = await response.text()
    } catch (error) {
      failure = String(error.message)
    }
  } catch (error) {
    failure = `管道自身抛出: ${error.message}`
  }
  const canRead = detail.includes('model not found')
  record(
    '普通 400 后调用方仍能读到错误正文',
    canRead,
    canRead ? `读到 ${detail.length} 字节` : `读不到：${failure || '(空)'}`,
  )
}

/* ── 3. 工具调用字段是否被保留 ───────────────────────────────────────────── */
{
  const toolMessage = {
    role: 'assistant',
    content: null,
    tool_calls: [{ id: 'call_1', type: 'function', function: { name: 'read_file', arguments: '{"path":"a.txt"}' } }],
  }
  const toolResult = { role: 'tool', tool_call_id: 'call_1', name: 'read_file', content: 'hello' }
  let captured = null
  for (const channel of ['buddy', 'codearts']) {
    await requestWithTencentGate({
      channel,
      model: 'm',
      messages: [toolMessage, toolResult],
      send: async body => {
        if (channel === 'buddy') captured = body.messages
        return new Response('{}', { status: 200 })
      },
    })
  }
  const assistant = captured.find(message => message.role === 'assistant')
  const tool = captured.find(message => message.role === 'tool')
  const keepsCalls = Array.isArray(assistant.tool_calls) && assistant.tool_calls[0]?.id === 'call_1'
  const keepsLink = tool.tool_call_id === 'call_1' && tool.name === 'read_file'
  record(
    '重建消息时保留 tool_calls / tool_call_id / name',
    keepsCalls && keepsLink,
    `assistant.tool_calls=${keepsCalls ? '保留' : '丢失'} · tool.tool_call_id=${keepsLink ? '保留' : '丢失'}`,
  )
}

/* ── 输出 ────────────────────────────────────────────────────────────────── */
let failed = 0
for (const { name, passed, detail } of results) {
  if (!passed) failed += 1
  console.log(`${passed ? '✅' : '❌'} ${name}`)
  console.log(`     ${detail}`)
}
console.log(`\n${failed === 0 ? 'PASS' : 'FAIL'} ${results.length - failed}/${results.length}`)
process.exit(failed === 0 ? 0 : 1)
