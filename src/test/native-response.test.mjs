/**
 * 回归：**用原生 `Response`** 跑管道。
 *
 * 背景：这套管道最初只被 `{ text: async () => … }` 这种假响应测过，而假响应
 * 可以被反复读取，于是掩盖了原生 `Response` body 只能消费一次的事实。后果是
 * 「持续 11128 不抛错」和「调用方读不到错误详情」两个真实缺陷一直没被发现。
 *
 * 这正是交接文档里那条教训的具体案例：
 *   "mock 替真实依赖补了它不提供的属性，就掩盖了真实缺陷。"
 *
 * 所以这一套一律用 `new Response(...)`，不再用假对象。
 */

import { requestWithTencentGate, scrubForLevel } from '../guard/pipeline.mjs'
import { isTencentGateBlockError, tencentGateErrorBody } from '../guard/tencent-gate.js'

const GATE_BODY = JSON.stringify({
  error: { code: 11128, message: 'Illegal API invocation from an unapproved channel' },
})

const SYSTEM = 'You are an AI agent powered by DeepSeek Harness.'

const baseOptions = extra => ({
  channel: 'buddy',
  model: 'deepseek-v4.1-flash',
  messages: [{ role: 'system', content: SYSTEM }],
  ...extra,
})

export default {
  title: 'Guard · 原生 Response 回归（假响应曾掩盖的缺陷）',
  tests: [
    {
      name: '持续 400/11128：预算耗尽后必须抛出结构化错误（不能静默返回 400）',
      async run({ assert }) {
        let sends = 0
        let caught
        try {
          await requestWithTencentGate(baseOptions({
            send: async () => {
              sends += 1
              // 原生 Response：body 只能读一次
              return new Response(GATE_BODY, { status: 400, headers: { 'content-type': 'application/json' } })
            },
          }))
        } catch (error) { caught = error }

        assert.ok(caught !== undefined, '必须抛错，而不是返回一个 400 响应')
        assert.equal(caught.code, 'INVALID_REQUEST', '错误码')
        assert.equal(caught.status, 400, '状态码')
        assert.includes(caught.message, '11128', '错误信息说明原因')
        assert.equal(sends, 3, '1 次原始 + 2 次升档')
        // 结构化登记仍然有效，且原始响应体被保留下来供排障
        assert.ok(isTencentGateBlockError(caught), 'WeakSet 登记生效')
        const body = tencentGateErrorBody(caught)
        assert.includes(body, '11128', '错误里带着上游响应体')
      },
    },
    {
      name: '普通 400：调用方仍能读取错误详情（body 未被消耗）',
      async run({ assert }) {
        const payload = JSON.stringify({ error: { code: 20001, message: 'model not found' } })
        const { response, escalations } = await requestWithTencentGate(baseOptions({
          model: 'not-a-model',
          send: async () => new Response(payload, { status: 400 }),
        }))
        assert.equal(escalations, 0, '普通 400 不升档')
        assert.notOk(response.ok, '仍然失败')
        // 关键：原生 Response 的 body 已经不能被消费，管道必须替调用方留住它
        let text = ''
        let failure = ''
        try { text = await response.text() } catch (error) { failure = String(error.message) }
        assert.equal(failure, '', `读取不应失败（实际：${failure}）`)
        assert.includes(text, 'model not found', '错误正文完整可读')
      },
    },
    {
      name: '成功响应：不读 body，也不破坏调用方读取',
      async run({ assert }) {
        const { response, sends } = await requestWithTencentGate(baseOptions({
          send: async () => new Response(JSON.stringify({ ok: true }), { status: 200 }),
        }))
        assert.equal(sends, 1, '一次就成')
        assert.ok(response.ok, '成功')
        // while 循环里的条件在成功时直接 break，不读 body；调用方仍可读
        const text = await response.text()
        assert.includes(text, '"ok":true', '成功响应体仍可读')
      },
    },
    {
      name: '工具调用消息：tool_calls / tool_call_id / name 必须保留',
      async run({ assert }) {
        const assistant = {
          role: 'assistant',
          content: null,
          tool_calls: [{
            id: 'call_1',
            type: 'function',
            function: { name: 'read_file', arguments: '{"path":"a.txt"}' },
          }],
        }
        const toolResult = { role: 'tool', tool_call_id: 'call_1', name: 'read_file', content: 'hello world' }
        const user = { role: 'user', content: 'read a.txt' }

        for (const channel of ['buddy', 'codearts']) {
          let captured = null
          await requestWithTencentGate({
            channel,
            model: 'm',
            messages: [assistant, toolResult, user],
            send: async body => {
              captured = body.messages
              return new Response('{}', { status: 200 })
            },
          })
          assert.ok(captured !== null, `${channel}: 捕获到请求体`)
          const a = captured.find(message => message.role === 'assistant')
          const t = captured.find(message => message.role === 'tool')
          const u = captured.find(message => message.role === 'user')

          assert.ok(Array.isArray(a?.tool_calls), `${channel}: assistant.tool_calls 被保留`)
          assert.equal(a.tool_calls[0].id, 'call_1', `${channel}: tool_call id 原样`)
          assert.equal(a.tool_calls[0].function.name, 'read_file', `${channel}: 函数名原样`)
          assert.equal(a.tool_calls[0].function.arguments, '{"path":"a.txt"}', `${channel}: 参数原样`)

          assert.equal(t?.tool_call_id, 'call_1', `${channel}: tool_call_id 被保留（调用与结果的关联）`)
          assert.equal(t?.name, 'read_file', `${channel}: tool 的 name 被保留`)
          assert.equal(t?.content, 'hello world', `${channel}: tool 内容原样`)

          assert.equal(u?.content, 'read a.txt', `${channel}: user 内容原样`)
        }
      },
    },
    {
      name: '所有消息字段都经过重建（不静默丢字段）',
      async run({ assert }) {
        // 除 role/content 外的字段，一个都不该丢
        const message = {
          role: 'assistant',
          content: 'DeepSeek Harness',
          name: 'assistant-name',
          tool_call_id: 'x',
          reasoning: 'chain of thought',
          refusal: null,
        }
        const wire = scrubForLevel([message], 1)
        assert.equal(wire.length, 1)
        const out = wire[0]
        for (const key of Object.keys(message)) {
          assert.ok(key in out, `字段 ${key} 没丢`)
        }
        assert.equal(out.name, 'assistant-name', 'name 原样')
        assert.equal(out.reasoning, 'chain of thought', 'reasoning 原样')
        assert.notIncludes(out.content, 'DeepSeek Harness', 'content 被清洗')
        // 且不改入参
        assert.equal(message.content, 'DeepSeek Harness', '原始消息对象未被修改')
      },
    },
    {
      name: '同一条响应被读两次时不会报 "Body is unusable"',
      async run({ assert }) {
        // 直接压测缓存：管道读了 body 之后，调用方还能读
        let sends = 0
        const { response } = await requestWithTencentGate(baseOptions({
          send: async () => {
            sends += 1
            // 第一次 11128 触发升档，第二次成功
            if (sends === 1) return new Response(GATE_BODY, { status: 400 })
            return new Response('{"done":true}', { status: 200 })
          },
        }))
        assert.equal(sends, 2, '升了一次档')
        const text = await response.text()
        assert.includes(text, 'done', '最终响应的 body 没被读坏')
      },
    },
  ],
}
