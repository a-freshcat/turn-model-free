/**
 * 驱动演示页里的「防护实验台」按钮，验证它在真实构建产物里能跑。
 *
 * 与 guard.test.mjs 的区别：那套测模块，这套测**打包后的 dist/demo.js** 里
 * 那个按钮点下去会发生什么——包括 DOM 渲染、假上游判定、升档、埋点展示。
 */

import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'
import { installGlobals } from '../runtime/mini-dom.mjs'
import { TENCENT_GATE_RULES } from '../guard/tencent-gate.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..', '..')

const realSetTimeout = globalThis.setTimeout.bind(globalThis)
const settle = async (rounds = 20) => {
  for (let index = 0; index < rounds; index += 1) await new Promise(done => realSetTimeout(done, 0))
}

function boot() {
  const restore = installGlobals(globalThis)
  const document = globalThis.document
  for (const id of ['page-root', 'controls', 'rpc-log', 'build-info', 'guard-lab']) {
    const element = document.createElement('div')
    element.setAttribute('id', id)
    document.body.appendChild(element)
  }
  vm.runInThisContext(readFileSync(join(ROOT, 'dist', 'demo.js'), 'utf8'), { filename: 'dist/demo.js' })
  const demo = globalThis.window.__OFM_DEMO__
  return { demo, document, restore }
}

const buttonsOf = root => [...root.querySelectorAll('button')]
const buttonByText = (root, text) => buttonsOf(root).find(button => button.textContent.includes(text))

export default {
  title: 'Demo · 防护实验台（dist 产物）',
  tests: [
    {
      name: '实验台挂载：状态栏、输入框、四个按钮都在',
      async run({ assert }) {
        const { demo, document, restore } = boot()
        try {
          demo.guardLab.start(document)
          const root = document.getElementById('guard-lab')
          assert.includes(root.textContent, '防护规则')
          // 不写死条数：上游 2026-10-28 把它从 10 条改成 12 条，
          // 写死就会在没有任何真实缺陷时变红。改为断言"显示的数字与规则表一致"。
          assert.match(root.textContent, new RegExp(`防护规则\\s*${TENCENT_GATE_RULES.length}\\s*条`), `状态栏显示 ${TENCENT_GATE_RULES.length} 条`)
          assert.includes(root.textContent, '升档预算')
          assert.includes(root.textContent, '最多 2 次')
          const textarea = root.querySelector('textarea')
          assert.ok(textarea, '提示词输入框存在')
          assert.includes(textarea.value, 'powered by DeepSeek Harness', '默认样例带指纹')
          assert.includes(textarea.value, 'C:\\work docs DeepSeek Harness\\app.js', '默认样例带必须保留的路径')
          const buttons = buttonsOf(root)
          assert.equal(buttons.length, 3, `三个动作按钮（实际 ${buttons.length}）`)
        } finally { restore() }
      },
    },
    {
      name: '只看三档清洗：L1/L2/L3 都显示，且路径每次都保留',
      async run({ assert }) {
        const { demo, document, restore } = boot()
        try {
          demo.guardLab.start(document)
          const root = document.getElementById('guard-lab')
          // 首屏就渲染了三档
          const text = root.textContent
          for (const level of ['L1', 'L2', 'L3']) assert.includes(text, level, `${level} 有结果块`)
          const keeps = (text.match(/保留（正确）/g) ?? []).length
          assert.equal(keeps, 3, `三档都判定路径保留（实际 ${keeps}）`)
          assert.notIncludes(text, '被改动了', '没有任何一档破坏路径')

          // 点一下按钮也不出错
          buttonByText(root, '只看三档清洗效果').dispatchEvent(new globalThis.window.Event('click', { bubbles: true }))
          assert.includes(root.textContent, '三档清洗效果')
        } finally { restore() }
      },
    },
    {
      name: '发一次请求（严格上游）：显示发送次数、升档次数、每次实际内容',
      async run({ assert }) {
        const { demo, document, restore } = boot()
        try {
          demo.guardLab.start(document)
          const root = document.getElementById('guard-lab')
          // 默认选的是「还查零宽变体」，但样例里没有零宽 → L1 就过
          buttonByText(root, '发一次请求').dispatchEvent(new globalThis.window.Event('click', { bubbles: true }))
          await settle(30)
          const text = root.textContent
          assert.includes(text, '发送次数', '显示了发送次数')
          assert.includes(text, '结果', '显示了结果')
          assert.includes(text, '每次实际发出的 system 内容', '显示了每次发送内容')
          assert.includes(text, '保留（正确）', '路径保留判定')
          assert.notIncludes(text, '被改动了', '路径没被破坏')
        } finally { restore() }
      },
    },
    {
      name: '三档分工：全角变体单独出现时，只有 L3 能清掉',
      async run({ assert }) {
        const { demo, document, restore } = boot()
        try {
          demo.guardLab.start(document)
          const root = document.getElementById('guard-lab')
          buttonByText(root, '灌入全角变体再试').dispatchEvent(new globalThis.window.Event('click', { bubbles: true }))
          await settle(10)
          const textarea = root.querySelector('textarea')
          assert.includes(textarea.value, 'ＤｅｅｐＳｅｅｋ', '全角变体写进了输入框')

          // 三档对比：L3 才清掉全角。这是「L3 只在 L1/L2 零命中时才触发」的
          // 可见证据 —— 注意默认样例里第一行是字面量指纹，L1 已命中并改动，
          // 所以 L3 对**整段文本**不会再跑；要看到 L3 生效，得用一段
          // 只含全角变体的输入（下面那半段就是这么测的）。
          const pres = [...root.querySelectorAll('pre')].map(node => node.textContent)
          assert.ok(pres.length >= 3, '三档结果块都在')
          assert.ok(pres.some(text => /ＤｅｅｐＳｅｅｋ/.test(text)), '前面的档位保留全角')

          // 换成只有全角变体的输入 → 必须升到 L3 才能过
          textarea.value = 'ＤｅｅｐＳｅｅｋ　Ｈａｒｎｅｓｓ 是唯一的内容'
          textarea.dispatchEvent(new globalThis.window.Event('input', { bubbles: true }))
          const select = root.querySelector('select')
          select.value = 'nfkc'
          select.dispatchEvent(new globalThis.window.Event('change', { bubbles: true }))
          buttonByText(root, '发一次请求').dispatchEvent(new globalThis.window.Event('click', { bubbles: true }))
          await settle(40)
          const text = root.textContent
          assert.includes(text, '发送次数', '有结果')
          const escalations = /升档次数\s*(\d)\s*\/\s*2/.exec(text.replace(/\s+/g, ' '))
          assert.ok(escalations !== null, `找到升档次数（片段：${text.replace(/\s+/g, ' ').slice(0, 160)}）`)
          assert.ok(Number(escalations[1]) >= 1, `确实升了档（${escalations?.[1]}）`)
          assert.includes(text, 'ＤｅｅｐＳｅｅｋ　Ｈａｒｎｅｓｓ 是唯一的内容'.replace('ＤｅｅｐＳｅｅｋ　Ｈａｒｎｅｓｓ', 'DSH'), 'L3 清成 DSH')
        } finally { restore() }
      },
    },
    {
      name: '永远拒绝：预算耗尽后显示失败与结构化错误',
      async run({ assert }) {
        const { demo, document, restore } = boot()
        try {
          demo.guardLab.start(document)
          const root = document.getElementById('guard-lab')
          // 把上游切到「永远拒绝」
          const select = root.querySelector('select')
          select.value = 'always'
          select.dispatchEvent(new globalThis.window.Event('change', { bubbles: true }))
          buttonByText(root, '发一次请求').dispatchEvent(new globalThis.window.Event('click', { bubbles: true }))
          await settle(40)
          const text = root.textContent
          // 1 次原始 + 2 次升档
          assert.includes(text, '发送次数3', `预算用满三次（实际片段：${text.slice(0, 200)}）`)
          assert.includes(text, '11128', '错误里说明是 11128')
          assert.includes(text, 'INVALID_REQUEST', '显示结构化错误码')
        } finally { restore() }
      },
    },
  ],
}
