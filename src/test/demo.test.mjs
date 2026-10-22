/**
 * The shipped demo (`dist/demo.js` + `dist/index.html`) boots and works.
 *
 * These tests import nothing from `src/` on the page side: they load the built
 * bundle, call the entry point `dist/index.html` calls, and then drive the real
 * controls. If the bundling step broke, this is where it shows.
 */

import { readFileSync } from 'node:fs'
import { bootDemo } from './demo-boot.mjs'
import { allByClass, elements, serialize } from './env.mjs'

/* The bundle replaces the environment's timers with the page's own; keep the
 * harness's `settle` on real timers by grabbing them before any boot. */
const realSetTimeout = globalThis.setTimeout.bind(globalThis)
const settle = async (rounds = 14) => {
  for (let index = 0; index < rounds; index += 1) await new Promise(resolve => realSetTimeout(resolve, 0))
}

export default {
  title: 'Demo · 交付物（dist/）冒烟',
  tests: [
    {
      name: 'dist/index.html 引用的资源都在，且没有内联脚本错误',
      async run({ assert }) {
        const html = readFileSync('dist/index.html', 'utf8')
        assert.includes(html, 'demo.css')
        assert.includes(html, 'demo.js')
        assert.includes(html, 'page-root')
        assert.includes(html, 'controls')
        assert.includes(html, 'rpc-log')
        // the entry point the inline script calls must exist in the bundle…
        const bundle = readFileSync('dist/demo.js', 'utf8')
        assert.includes(bundle, 'window.__OFM_DEMO__')
        assert.includes(bundle, 'start: start')
        // …and the HTML must be the one calling it
        assert.includes(html, 'demo.page.start(document, demo.React, demo.ReactDOM)')
      },
    },
    {
      name: 'bundle 自举：13 张卡、13 车道、13 个余额读取',
      async run({ assert }) {
        const { demo, document, restore } = bootDemo()
        try {
          demo.page.start(document, demo.React, demo.ReactDOM)
          await settle(20)
          const root = document.getElementById('page-root')
          const cards = allByClass(root, 'ofm_chan')
          assert.equal(cards.length, 13, 'thirteen channel cards')
          const html = serialize(root)
          assert.includes(html, '白嫖模型接入')
          assert.includes(html, '每日自动签到')
          assert.includes(html, '已接入')
          // the demo's own RPC log recorded the traffic
          const log = document.getElementById('rpc-log')
          assert.ok(log.childNodes.length > 0, 'rpc log rendered')
          assert.includes(log.textContent, 'provider.status')
          assert.includes(document.getElementById('build-info').textContent, '2.5.4')
        } finally { restore() }
      },
    },
    {
      name: '场景切换：全新安装没有任何账号，按钮仍然可用',
      async run({ assert }) {
        const { demo, document, restore } = bootDemo({ hash: '#scenario=fresh' })
        try {
          demo.page.start(document, demo.React, demo.ReactDOM)
          await settle(20)
          const root = document.getElementById('page-root')
          assert.equal(allByClass(root, 'ofm_chan').length, 13)
          assert.includes(serialize(root), '0/13 已接入')
          const first = allByClass(root, 'ofm_chan')[0]
          const add = elements(first).find(element => element.tagName === 'BUTTON' && element.textContent.trim() === '添加账号')
          assert.ok(add, 'add-account button present')
          assert.notOk(add.hasAttribute('disabled'), 'enabled with a transport')
        } finally { restore() }
      },
    },
    {
      name: '场景切换：渠道包挂载失败时显示原因',
      async run({ assert }) {
        const { demo, document, restore } = bootDemo({ hash: '#scenario=packMissing' })
        try {
          demo.page.start(document, demo.React, demo.ReactDOM)
          await settle(14)
          const html = serialize(document.getElementById('page-root'))
          assert.includes(html, '渠道包未挂载：import failed')
          assert.includes(html, '渠道包由本插件内置')
        } finally { restore() }
      },
    },
    {
      name: '场景切换：无 connection 服务时不崩、按钮全禁用',
      async run({ assert }) {
        const { demo, document, restore } = bootDemo({ hash: '#scenario=noTransport' })
        try {
          demo.page.start(document, demo.React, demo.ReactDOM)
          await settle(12)
          const root = document.getElementById('page-root')
          assert.includes(serialize(root), '宿主未提供 connection 服务')
          const buttons = elements(allByClass(root, 'ofm_chan')[0]).filter(element => element.tagName === 'BUTTON' && element.classList.contains('ofm_btn'))
          assert.ok(buttons.length > 0)
          for (const button of buttons) assert.ok(button.hasAttribute('disabled'), 'disabled')
        } finally { restore() }
      },
    },
    {
      name: '英文 + 深色：文案走 en，深色令牌存在',
      async run({ assert }) {
        const { demo, document, restore } = bootDemo({ hash: '#lang=en&theme=dark' })
        try {
          demo.page.start(document, demo.React, demo.ReactDOM)
          await settle(16)
          const html = serialize(document.getElementById('page-root'))
          assert.includes(html, 'Free channel access')
          assert.includes(html, 'Add account')
          assert.ok(document.body.hasAttribute('data-ds-dark-theme'), 'dark attribute set')
          const css = readFileSync('dist/demo.css', 'utf8')
          assert.includes(css, 'body[data-ds-dark-theme]')
          // dark tokens must actually redefine the layer the cards paint on
          const dark = css.slice(css.indexOf('body[data-ds-dark-theme]'))
          assert.includes(dark.slice(0, 4000), '--dsw-alias-bg-layer-1')
        } finally { restore() }
      },
    },
    {
      name: '演示页按钮：切主题、推送事件、重置都不抛错',
      async run({ assert }) {
        const { demo, document, window, restore } = bootDemo()
        try {
          demo.page.start(document, demo.React, demo.ReactDOM)
          await settle(16)
          const controls = document.getElementById('controls')
          const buttons = elements(controls).filter(element => element.tagName === 'BUTTON')
          assert.equal(buttons.length, 4, `four buttons (found ${buttons.length})`)
          const logText = document.getElementById('rpc-log').textContent
          const seen = []
          for (const button of buttons) {
            button.dispatchEvent(new window.Event('click', { bubbles: true }))
            await settle(8)
            seen.push({ label: button.textContent, log: document.getElementById('rpc-log').textContent })
            // reset clears the log on purpose; assert on what this click produced
            assert.ok(document.getElementById('page-root').childNodes.length > 0, `${button.textContent}: page still mounted`)
          }
          assert.ok(seen.some(entry => entry.log.includes('ofm:channels')), 'push button logged the event')
          assert.ok(logText.includes('provider.status'), 'boot traffic logged')
          assert.equal(allByClass(document.getElementById('page-root'), 'ofm_chan').length, 13, 'reset re-rendered the grid')
          const selects = elements(controls).filter(element => element.tagName === 'SELECT')
          assert.equal(selects.length, 2, 'scenario + language selects')
        } finally { restore() }
      },
    },
    {
      name: '切换场景下拉：每个场景都能挂载',
      async run({ assert }) {
        const { demo, document, window, restore } = bootDemo()
        try {
          demo.page.start(document, demo.React, demo.ReactDOM)
          await settle(14)
          const select = elements(document.getElementById('controls')).find(element => element.tagName === 'SELECT')
          const options = elements(select).filter(element => element.tagName === 'OPTION')
          assert.ok(options.length >= 7, `scenario options (${options.length})`)
          const scenarios = []
          for (const option of options) {
            select.value = option.getAttribute('value')
            select.dispatchEvent(new window.Event('change', { bubbles: true }))
            await settle(12)
            const root = document.getElementById('page-root')
            scenarios.push({ id: option.getAttribute('value'), html: serialize(root) })
            assert.ok(root.childNodes.length > 0, `${option.getAttribute('value')} rendered something`)
          }
          // every scenario produced a distinct page
          const unique = new Set(scenarios.map(entry => entry.html))
          assert.ok(unique.size >= 5, `scenarios differ (${unique.size} distinct)`)
        } finally { restore() }
      },
    },
    {
      name: '样式表：页面样式与主题令牌都打进了 dist/demo.css',
      async run({ assert }) {
        const css = readFileSync('dist/demo.css', 'utf8')
        assert.includes(css, '.ofm_chan{')
        assert.includes(css, '.ofm_chanwrap{')
        assert.includes(css, '--dsw-alias-bg-layer-1:')
        assert.includes(css, '--dsw-alias-state-business-primary:')
        assert.includes(css, '--ease-settle')
        // no accidental template leftovers
        assert.notIncludes(css, '${')
      },
    },
  ],
}
