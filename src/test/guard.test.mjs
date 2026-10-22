/**
 * 「腾讯 11128 防护」在抄写版里的端到端测试。
 *
 * 与 `main_repai/` 下那套离线测试的区别：那套测的是 `pack.js` 里的函数；
 * 这套测的是**移植进本仓库、并接进请求管道**之后的行为——一个假的腾讯
 * 上游会真的按内容指纹决定放不放行，管道要自己升档直到通过或耗尽。
 *
 * 假上游的行为来自实测（见 TENCENT-11128-REAL-CAUSE.md）：
 *   · 命中 L1 指纹 → 400 + `code 11128` + `Illegal API invocation from an unapproved channel`
 *   · 命中 L2 变体（零宽、多空格、大小写）→ 只有升到 L2 才通过
 *   · 命中 L3 变体（全角 / NFKC 才等价）→ 只有升到 L3 才通过
 *   · 普通参数错误 → 400 但响应体不是 11128，**必须不升档**
 */

import {
  applyTencentGateGuard,
  isTencentGateBlock,
  TENCENT_GATE_RULES,
  TENCENT_GATE_MAX_ESCALATIONS,
  tencentBaseGuardLevel,
  tencentNextGuardLevel,
  tencentGateStats,
  tencentGateRules,
  tencentGateUserConfig,
  scrubTencentFingerprintMessage,
  bindTencentGateEnv,
} from '../guard/tencent-gate.js'
import {
  requestWithTencentGate,
  scrubForLevel,
  ChannelGateError,
  TENCENT_CHANNELS,
} from '../guard/pipeline.mjs'

const GATE_BLOCK_BODY = JSON.stringify({
  error: {
    code: 11128,
    message: 'Illegal API invocation from an unapproved channel',
    type: 'invalid_request_error',
  },
})

const textOf = body => body.messages
  .filter(message => message.role === 'system' || message.role === 'assistant')
  .map(message => typeof message.content === 'string' ? message.content : JSON.stringify(message.content))
  .join('\n')

/**
 * 一个会按内容判定的假腾讯上游。
 *
 * 判据刻意只认「洗过之后就不存在」的东西 —— 早期版本用 `/DeepSeek Harness/`
 * 当判据，可路径里的 `C:\...\DeepSeek Harness\app.js` 是**必须保留**的，
 * 于是永远命中、预算被烧光。真实上游扫的是 system/assistant 的措辞，不是
 * 路径，所以判据必须避开路径内容。
 *
 * @param {object} options
 * @param {boolean} [options.alwaysBlock] 无论怎么洗都拒绝（模拟上游新增未知指纹）
 */
function createFakeTencentUpstream(options = {}) {
  const received = []
  const upsteam = {
    received,
    detectors: [],
    send: async body => {
      received.push(structuredClone(body))
      const text = textOf(body)
      const hit = upsteam.detectors.find(detector => detector.test(text))
      if (hit !== undefined) {
        return { ok: false, status: 400, text: async () => hit.body ?? GATE_BLOCK_BODY }
      }
      return { ok: true, status: 200, text: async () => JSON.stringify({ id: 'chatcmpl-ok', choices: [] }) }
    },
  }

  if (options.alwaysBlock === true) {
    upsteam.detectors.push({ test: () => true })
  } else if (options.requireLevel === 2) {
    // 只有 L2 才清的变体（零宽 / 无分隔）
    upsteam.detectors.push({ test: text => /DeepSeek[\u200B-\u200D\uFEFF]?Harness(?!\\|\/)/.test(text) && !/DeepSeek Harness/.test(text) })
    upsteam.detectors.push({ test: text => /powered by DeepSeek Harness/.test(text) })
  } else {
    // 默认：L1 就能清的实测指纹（身份句 / Web GUI / git 模板）
    upsteam.detectors.push({
      test: text => /powered by DeepSeek Harness|DeepSeek Harness Web GUI|you will usually use this for PRs/.test(text),
    })
  }
  return upsteam
}

const SYSTEM_WITH_FINGERPRINTS = [
  'You are an AI agent powered by DeepSeek Harness.',
  'You operate inside the DeepSeek Harness Web GUI, a desktop app.',
  'Main branch (you will usually use this for PRs)',
  'The project lives at C:\\work docs DeepSeek Harness\\app.js on this machine.',
].join('\n')

const MESSAGES = [
  { role: 'system', content: SYSTEM_WITH_FINGERPRINTS },
  { role: 'assistant', content: 'DeepSeek Harness is ready.' },
  { role: 'user', content: 'DeepSeek Harness 这个词出现在用户消息里，不许改我。' },
]

/** 上游收到的那次请求里，system/assistant 的全文（用于判断指纹是否被洗）。 */
const fingerprintText = body => body.messages
  .filter(message => message.role === 'system' || message.role === 'assistant')
  .map(message => typeof message.content === 'string' ? message.content : JSON.stringify(message.content))
  .join('\n')

/** 上游收到的那次请求的全部文本（含 user，用于验证 user 未被改）。 */
const allText = body => body.messages
  .map(message => typeof message.content === 'string' ? message.content : JSON.stringify(message.content))
  .join('\n')

export default {
  title: 'Guard · 腾讯 11128 防护层（移植版）',
  tests: [
    {
      name: '移植完整：10 条规则、三档语义、预算默认 2',
      async run({ assert }) {
        assert.ok(Array.isArray(TENCENT_GATE_RULES), '规则表存在')
        assert.equal(TENCENT_GATE_RULES.length, 10, '规则条数')
        const levels = new Set(TENCENT_GATE_RULES.map(rule => rule.level))
        assert.deepEqual([...levels].sort(), [1, 2, 3], '三档齐全')
        // 每条规则都必须有 id / find / replace
        for (const rule of TENCENT_GATE_RULES) {
          assert.ok(typeof rule.id === 'string' && rule.id.length > 0, `规则 ${rule.id} 有 id`)
          assert.ok(rule.find instanceof RegExp, `规则 ${rule.id} 的 find 是正则`)
          assert.ok(typeof rule.replace === 'string', `规则 ${rule.id} 有 replace`)
        }
        assert.equal(TENCENT_GATE_MAX_ESCALATIONS, 2, '升档预算默认 2')
        assert.equal(tencentBaseGuardLevel(), 1, '起始档位默认 L1')
        assert.equal(tencentNextGuardLevel(1), 2, 'L1 → L2')
        assert.equal(tencentNextGuardLevel(2), 3, 'L2 → L3')
        assert.equal(tencentNextGuardLevel(3), 3, 'L3 到顶不再升')
      },
    },
    {
      name: 'L1 洗掉实测指纹，且不动 user / tool，路径逐字节保留',
      async run({ assert }) {
        const wire = MESSAGES.map(message => ({ ...message }))
        const changed = applyTencentGateGuard(wire, 1)
        assert.ok(changed, '发生了改写')
        const systemText = wire[0].content
        // 身份句整句被替换
        assert.notIncludes(systemText, 'You are an AI agent powered by DeepSeek Harness.', '身份句被替换')
        assert.includes(systemText, 'You are an AI coding agent.', '换成中性身份句')
        // Web GUI 短语被缩写
        assert.notIncludes(systemText, 'DeepSeek Harness Web GUI', 'Web GUI 前缀被去掉')
        assert.includes(systemText, 'the Web GUI', '保留 the Web GUI')
        // git 模板
        assert.notIncludes(systemText, 'you will usually use this for PRs', 'git 模板被改写')
        assert.includes(systemText, 'PRs usually go here', '换成等价表达')
        // 路径必须逐字节保留 —— 注意路径里本来就含 "DeepSeek Harness"，
        // 这正是路径保护存在的理由：不能因为洗指纹把目录名改了。
        assert.includes(systemText, 'C:\\work docs DeepSeek Harness\\app.js', '路径原样保留')
        // assistant 也被处理
        assert.notIncludes(wire[1].content, 'DeepSeek Harness is ready', 'assistant 也被洗')
        // user 内容逐字节不变
        assert.equal(wire[2].content, MESSAGES[2].content, 'user 消息一字未改')
        assert.includes(wire[2].content, 'DeepSeek Harness', 'user 里的品牌词保留')
      },
    },
    {
      name: '路径保护：四种边界都不吞正文、也不切断路径',
      async run({ assert }) {
        const scrub = (content, level) => {
          const message = { role: 'assistant', content }
          scrubTencentFingerprintMessage(message, level)
          return message.content
        }

        // 1) 带空格、带引号的路径：路径内逐字节保住
        const case1 = '"C:\\work docs DeepSeek Harness\\app.js" 放在这里'
        const out1 = scrub(case1, 1)
        assert.includes(out1, '"C:\\work docs DeepSeek Harness\\app.js"', '带空格路径原样')
        // 路径后面的**中文正文**没被一起保护（若过度保护，整行都会被遮住）
        assert.includes(out1, '放在这里', '路径后的正文仍在')

        // 2) 双空格是文件名的一部分，L3 规范化不能折
        const case2 = 'see my  file.txt 然后 DeepSeek Harness'
        const out2 = scrub(case2, 3)
        assert.includes(out2, 'my  file.txt', '双空格文件名保留')

        // 3) UNC 路径
        const case3 = '\\\\server\\share\\DeepSeek Harness\\x.js 在上面'
        const out3 = scrub(case3, 1)
        assert.includes(out3, '\\\\server\\share\\DeepSeek Harness\\x.js', 'UNC 路径原样')

        // 4) 路径后紧跟另一个文件名，不能被吞掉
        const case4 = 'C:\\a\\b.js and other.txt 后面'
        const out4 = scrub(case4, 1)
        assert.includes(out4, 'other.txt', '后一个文件名没被吞')

        // 5) 关键回归：路径里的品牌词不能被洗成 DSH（否则工具调用会失败）
        const case5 = 'C:\\work docs DeepSeek Harness\\app.js'
        assert.equal(scrub(case5, 3), case5, '纯路径在 L3 也逐字节不变')
      },
    },
    {
      name: '干净文本逐字节不变（保住 prompt 缓存）',
      async run({ assert }) {
        const clean = 'You are a coding assistant. Please refactor src/app.js.'
        const message = { role: 'system', content: clean }
        const wire = [message]
        const changed = applyTencentGateGuard(wire, 3)
        assert.notOk(changed, '没命中就不报告改写')
        assert.equal(wire[0].content, clean, '一个字节都没动')
      },
    },
    {
      name: '档位语义（按实测行为）：L1 只处理字面量，L2 处理容错变体，L3 才做 NFKC',
      async run({ assert }) {
        const scrub = (content, level) => {
          const message = { role: 'assistant', content }
          scrubTencentFingerprintMessage(message, level)
          return message.content
        }

        // 字面量品牌词：L1 就处理，且统一替换成短标记 DSH
        assert.equal(scrub('DeepSeek Harness', 1), 'DSH', 'L1 处理字面量品牌词')

        // 零宽穿插 / 无分隔：L1 不动，**L2** 才处理
        const zeroWidth = 'DeepSeek\u200BHarness'
        assert.equal(scrub(zeroWidth, 1), zeroWidth, 'L1 不动零宽变体')
        assert.equal(scrub(zeroWidth, 2), 'DSH', 'L2 处理零宽变体')
        assert.equal(scrub('DeepSeekHarness', 2), 'DSH', 'L2 处理无分隔变体')

        // 全角：L1 / L2 都不动，只有 L3 的 NFKC 规范���后处理
        const fullWidth = 'ＤｅｅｐＳｅｅｋ　Ｈａｒｎｅｓｓ'
        assert.equal(scrub(fullWidth, 1), fullWidth, 'L1 不动全角')
        assert.equal(scrub(fullWidth, 2), fullWidth, 'L2 不动全角')
        assert.equal(scrub(fullWidth, 3), 'DSH', 'L3 处理全角')

        // 更大的一段文本：L1 已足够清掉三个实测指纹
        const mixed = [
          'You are an AI agent powered by DeepSeek Harness.',
          'DeepSeek Harness Web GUI',
          'Main branch (you will usually use this for PRs)',
        ].join('\n')
        const out1 = scrub(mixed, 1)
        assert.notIncludes(out1, 'powered by DeepSeek Harness', '身份句被替换')
        assert.notIncludes(out1, 'DeepSeek Harness Web GUI', 'Web GUI 前缀去掉')
        assert.notIncludes(out1, 'you will usually use this for PRs', 'git 模板被改写')
      },
    },
    {
      name: '端到端：一次就过（起始档位 L1 已经洗过，上游放行 → sends=1）',
      async run({ assert }) {
        const upstream = createFakeTencentUpstream()
        const result = await requestWithTencentGate({
          channel: 'buddy',
          model: 'deepseek-v4.1-flash',
          messages: MESSAGES,
          send: upstream.send,
        })
        assert.ok(result.response.ok, '成功')
        assert.equal(result.sends, 1, '起始档位 L1 已清掉实测指纹，一次就过')
        assert.equal(result.escalations, 0, '不需要升档')
        assert.equal(result.finalLevel, 1, '停在 L1')
        // 发出去的那次已经洗过，但 user 原样、路径原样
        const sent = fingerprintText(upstream.received[0])
        assert.notIncludes(sent, 'powered by DeepSeek Harness', '指纹被洗')
        assert.includes(sent, 'C:\\work docs DeepSeek Harness\\app.js', '路径原样')
        assert.includes(allText(upstream.received[0]), 'DeepSeek Harness 这个词出现在用户消息里', 'user 原样')
      },
    },
    {
      name: '端到端：上游仍报 11128 → 升档 → 重发成功（sends=2）',
      async run({ assert }) {
        // 这个上游连洗过的文本都拒（模拟上游新增了 L1 覆盖不到的指纹）
        let calls = 0
        const received = []
        const result = await requestWithTencentGate({
          channel: 'buddy',
          model: 'deepseek-v4.1-flash',
          messages: MESSAGES,
          send: async body => {
            calls += 1
            received.push(structuredClone(body))
            if (calls === 1) return { ok: false, status: 400, text: async () => GATE_BLOCK_BODY }
            return { ok: true, status: 200, text: async () => '{}' }
          },
        })
        assert.ok(result.response.ok, '升档后成功')
        assert.equal(result.sends, 2, '第二次发送')
        assert.equal(result.escalations, 1, '升档一次')
        assert.equal(result.finalLevel, 2, '落到 L2')
        // 第二次是从原始输入按 L2 重建的，不是把第一次的产物再洗一遍
        const second = fingerprintText(received[1])
        assert.includes(second, 'C:\\work docs DeepSeek Harness\\app.js', '路径仍在（重建而非二次改写）')
        assert.includes(allText(received[1]), 'DeepSeek Harness 这个词出现在用户消息里', 'user 仍原样')
      },
    },
    {
      name: '端到端：L1 清不掉的变体，确实靠升到 L2 才过',
      async run({ assert }) {
        // 上游只认"洗过之后仍带零宽品牌词"这一种情况；L1 不动零宽、L2 才动
        const received = []
        const result = await requestWithTencentGate({
          channel: 'workbuddy',
          model: 'deepseek-v4.1-flash',
          messages: [{ role: 'system', content: 'DeepSeek\u200BHarness 是被零宽拆开的品牌词' }],
          send: async body => {
            received.push(structuredClone(body))
            const text = fingerprintText(body)
            if (text.includes('DeepSeek\u200BHarness')) {
              return { ok: false, status: 400, text: async () => GATE_BLOCK_BODY }
            }
            return { ok: true, status: 200, text: async () => '{}' }
          },
        })
        assert.ok(result.response.ok, '最终通过')
        assert.equal(result.sends, 2, 'L1 不过 → 升到 L2 重发')
        assert.equal(result.finalLevel, 2, '停在 L2')
        assert.notIncludes(fingerprintText(received[1]), 'DeepSeek\u200BHarness', 'L2 已清掉零宽变体')
      },
    },
    {
      name: '端到端：预算耗尽后抛结构化错误（不是裸 400）',
      async run({ assert }) {
        const upstream = createFakeTencentUpstream({ alwaysBlock: true })
        let caught
        try {
          await requestWithTencentGate({
            channel: 'workbuddy',
            model: 'deepseek-v4.1-flash',
            messages: MESSAGES,
            send: upstream.send,
          })
        } catch (error) { caught = error }
        assert.ok(caught, '必须抛错')
        assert.ok(caught instanceof ChannelGateError, '是 ChannelGateError')
        assert.equal(caught.code, 'INVALID_REQUEST', 'code')
        assert.equal(caught.status, 400, 'status')
        assert.includes(caught.message, '11128', '错误信息里说明是 11128')
        assert.includes(caught.message, 'L3', '给出补救建议')
        // 1 次原始 + 2 次升档 = 3 次发送
        assert.equal(upstream.received.length, 1 + TENCENT_GATE_MAX_ESCALATIONS, '预算用满')
        // 关键：结构化登记生效（不靠 error 自定义属性）
        const { isTencentGateBlockError } = await import('../guard/tencent-gate.js')
        assert.ok(isTencentGateBlockError(caught), 'WeakSet 登记生效')
      },
    },
    {
      name: '普通 400 不升档（不把参数错误当成指纹拦截）',
      async run({ assert }) {
        const received = []
        const result = await requestWithTencentGate({
          channel: 'buddy',
          model: 'not-a-model',
          messages: MESSAGES,
          send: async body => {
            received.push(body)
            return {
              ok: false,
              status: 400,
              text: async () => JSON.stringify({ error: { code: 20001, message: 'model not found' } }),
            }
          },
        })
        assert.equal(received.length, 1, '只发一次，不升档')
        assert.equal(result.escalations, 0, '没升档')
        assert.notOk(result.response.ok, '仍然失败（错误如实上报）')
        // 起始档位 L1 的清洗照常发生（那是常规防护），但不是"因为这次 400 才改"
        assert.notIncludes(fingerprintText(received[0]), 'powered by DeepSeek Harness', 'L1 常规清洗')
      },
    },
    {
      name: '非腾讯渠道完全不走防护（内容原样）',
      async run({ assert }) {
        const received = []
        await requestWithTencentGate({
          channel: 'codearts',
          model: 'deepseek-v4',
          messages: MESSAGES,
          send: async body => {
            received.push(body)
            return { ok: true, status: 200, text: async () => '{}' }
          },
        })
        assert.equal(received.length, 1)
        assert.includes(fingerprintText(received[0]), 'powered by DeepSeek Harness', 'codearts 的正文没被改')
        assert.notOk(TENCENT_CHANNELS.has('codearts'), 'codearts 不在腾讯车道')
      },
    },
    {
      name: '成功响应不被嗅探（不把整段输出缓冲一遍）',
      async run({ assert }) {
        let textCalls = 0
        await requestWithTencentGate({
          channel: 'buddy',
          model: 'deepseek-v4.1-flash',
          messages: MESSAGES,
          send: async () => ({
            ok: true,
            status: 200,
            text: async () => { textCalls += 1; return '{}' },
          }),
        })
        // 第一次发送就成功：只有"第一次带指纹被拒"时才读 body
        assert.ok(textCalls === 0, `成功路径读了 ${textCalls} 次 body，应为 0`)
      },
    },
    {
      name: '取证埋点：只写元数据，绝不写正文/凭据',
      async run({ assert }) {
        const lines = []
        const original = console.warn
        console.warn = (...parts) => { lines.push(parts.map(String).join(' ')) }
        try {
          // 强制升一次档，才能同时看到 escalate 与 result 两种事件
          let calls = 0
          await requestWithTencentGate({
            channel: 'buddy',
            model: 'deepseek-v4.1-flash',
            messages: MESSAGES,
            send: async () => {
              calls += 1
              if (calls === 1) return { ok: false, status: 400, text: async () => GATE_BLOCK_BODY }
              return { ok: true, status: 200, text: async () => '{}' }
            },
          })
        } finally { console.warn = original }
        const trace = lines.filter(line => line.includes('[ofm-tencent-gate]'))
        assert.ok(trace.length >= 2, `至少有 escalate 与 result 两条（实际 ${trace.length}）`)
        const joined = trace.join('\n')
        // 元数据在
        assert.includes(joined, '"evt":"escalate"')
        assert.includes(joined, '"evt":"result"')
        assert.includes(joined, '"finalLevel"')
        assert.includes(joined, '"budget"')
        // 正文与凭据绝不在
        assert.notIncludes(joined, 'powered by DeepSeek Harness', '埋点里没有正文')
        assert.notIncludes(joined, 'C:\\\\work docs', '埋点里没有路径')
        assert.notIncludes(joined, 'DeepSeek Harness 这个词', '埋点里没有用户消息')
        assert.notIncludes(joined, 'sk-', '埋点里没有 key')
        // 每行都是合法 JSON
        for (const line of trace) {
          const json = line.slice(line.indexOf('{'))
          assert.ok(json.length > 0, '取到 JSON 片段')
          let parsed
          try { parsed = JSON.parse(json) } catch (error) {
            assert.ok(false, `埋点不是合法 JSON: ${json.slice(0, 120)}`)
          }
          assert.equal(parsed.evt !== undefined, true, '有 evt 字段')
        }
      },
    },
    {
      name: '用户扩展规则：注入 fs 才读，缺失时静默退回内置',
      async run({ assert }) {
        // 没有 fs：不抛错，内置规则照常
        bindTencentGateEnv({})
        const wire = [{ role: 'system', content: 'You are an AI agent powered by DeepSeek Harness.' }]
        assert.ok(applyTencentGateGuard(wire, 1), '内置规则仍然生效')
        assert.notIncludes(wire[0].content, 'DeepSeek Harness')

        // 注入一个假 fs：能读到用户规则并生效
        let reads = 0
        bindTencentGateEnv({
          readFileSync: () => {
            reads += 1
            return JSON.stringify({
              baseLevel: 2,
              maxEscalations: 1,
              rules: [{ id: 'custom-probe', level: 1, find: 'CUSTOM_FINGERPRINT', flags: 'g', replace: 'CUSTOM_CLEAN' }],
            })
          },
          homedir: () => '/home/test',
        })
        // 文件真的被读了（回归：第一版改写把 join 丢了，异常被 catch 吞掉，
        // 用户规则永远静默失效且没有任何报错）
        assert.equal(tencentGateUserConfig().rules.length, 1, '用户规则被解析出来')
        assert.ok(reads > 0, `readFileSync 真的被调到（${reads} 次）`)
        assert.equal(tencentGateUserConfig().baseLevel, 2, 'baseLevel 来自用户配置')

        const wire2 = [{ role: 'system', content: 'CUSTOM_FINGERPRINT here' }]
        assert.ok(applyTencentGateGuard(wire2, 1), '用户规则生效')
        assert.includes(wire2[0].content, 'CUSTOM_CLEAN', '替换成了用户指定文本')
        // 内置规则同时还在
        assert.equal(tencentGateRules().length, TENCENT_GATE_RULES.length + 1, '内置 + 用户规则')

        // 清掉，别影响后面的测试
        bindTencentGateEnv({})
      },
    },
    {
      name: '移植完整性：函数体未被改写（只能有绑定差异）',
      async run({ assert, readFile }) {
        // 这是对「照描述改代码」的防线：防护层是原样搬过来的，除三个宿主
        // 名字的绑定外不该有任何逻辑差异。
        const merged = readFile('src/guard/tencent-gate.js')
        const provenance = JSON.parse(readFile('src/guard/provenance.json'))
        for (const marker of [
          'var TENCENT_GATE_RULES',
          'function tgPathSpans',
          'function tgMaskPaths',
          'function tgUnmaskPaths',
          'function tencentGateApplyRules',
          'function isTencentGateBlock',
          'var TG_GATE_ERRORS',
          'function markTencentGateError',
          'function tgTrace',
          'function tencentCanonicalizeText',
        ]) {
          assert.includes(merged, marker, `保留了 ${marker}`)
        }
        // 原文的规则过滤语义必须保留（第一版改写把它改严了）
        assert.includes(merged, 'typeof r.find === "string" && typeof r.replace === "string"', '规则过滤条件原样')
        assert.includes(merged, 'Number.isFinite(r.level) ? Number(r.level) : 2', 'level 缺省值原样（2）')
        // 不能出现我第一版写进去的那种改写痕迹
        assert.notIncludes(merged, 'TG_ENV.readFileSync', '没有引入 TG_ENV 改写')
        assert.notIncludes(merged, 'TG_ENV.homedir', '没有引入 TG_ENV 改写')
        // 记录源指纹，便于将来核对
        assert.ok(provenance.sourceSha256.length === 64, 'provenance 记录了源 sha256')
        assert.equal(provenance.rewrites.length, 1, '只有一处注入差异')
        assert.includes(provenance.rewrites[0].what, 'readFileSync', '差异说明写清了绑定哪三个名字')
      },
    },
  ],
}
