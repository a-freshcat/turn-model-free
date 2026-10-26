/**
 * Parity: the copy renders the same tree as the installed plugin.
 *
 * Every test here mounts both sides through the harness (same DOM, same React,
 * same mock backend) and compares the committed tree line by line. A single
 * differing line is a failure with the two lines printed, so a regression is
 * readable rather than "trees differ".
 */

import { createHarness, diffTrees } from './harness.mjs'
import { allByClass, elements, serialize } from './env.mjs'

/** Opens one fold (`账号` / `模型`) on every channel card. */
function openEveryFold(harness, side, label) {
  const buttons = elements(side.container).filter(element => element.tagName === 'BUTTON'
    && element.classList.contains('ofm_foldtoggle') && element.textContent.includes(label))
  if (buttons.length === 0) throw new Error(`${side.side}: no ${label} fold buttons`)
  for (const button of buttons) button.dispatchEvent(new harness.env.window.Event('click', { bubbles: true }))
  harness.env.ReactDOM.flushSync(() => {})
  return buttons.length
}

export default {
  title: 'Parity · 抄写结果 vs 已安装插件',
  tests: [
    {
      name: '默认状态（13 张渠道卡 + 自动签到条）渲染树逐行一致',
      async run({ assert }) {
        const harness = await createHarness()
        try {
          const sides = await harness.mountBoth()
          const reference = sides.reference.html()
          const copy = sides.copy.html()
          const diff = diffTrees(reference, copy)
          assert.deepEqual(diff, [], 'trees differ')
          assert.includes(copy, '白嫖模型接入')
          assert.equal(allByClass(sides.copy.container, 'ofm_chan').length, 13, 'card count')
        } finally { harness.teardown() }
      },
    },
    {
      name: '首屏 RPC 调用序列与参数完全一致',
      async run({ assert }) {
        const harness = await createHarness()
        try {
          const sides = await harness.mountBoth()
          const summarize = side => side.pack.calls.map(call => `${call.plugin}|${call.method}|${JSON.stringify(call.payload)}`)
          assert.deepEqual(summarize(sides.copy), summarize(sides.reference), 'RPC sequence differs')
          // and the sequence is the one the page is supposed to make
          const methods = sides.copy.pack.calls.map(call => call.method)
          assert.ok(methods.includes('provider.status'), 'provider.status is the readiness probe')
          assert.ok(methods.includes('usage.autoCheckin'), 'auto check-in switch state')
          assert.equal(methods.filter(method => method === 'credits.balances').length, 13, 'every channel with a balance capability asks once')
        } finally { harness.teardown() }
      },
    },
    {
      name: '展开「账号」折叠区后渲染树一致（含余额、到期、今日已领）',
      async run({ assert }) {
        const harness = await createHarness()
        try {
          const sides = await harness.mountBoth()
          for (const side of Object.values(sides)) {
            const count = openEveryFold(harness, side, '账号')
            assert.equal(count, 13, `${side.side}: one account fold per channel`)
          }
          await harness.settle(14)
          const diff = diffTrees(sides.reference.html(), sides.copy.html())
          assert.deepEqual(diff, [], 'account folds differ')
          // The fold is the page's own code path: it must have fetched the list.
          assert.ok(sides.copy.pack.calls.some(call => call.method === 'account.list'), 'account.list was fetched')
          const html = sides.copy.html()
          assert.includes(html, 'ofm_acctlist')
          assert.includes(html, '剩余积分')
          assert.includes(html, '已启用')
          assert.includes(html, '已停用')
          // buddy rows carry a today credit; codearts rows do not
          assert.includes(html, '今日已领 200')
          // 2026-10-25 的修复：凭据还在但续期材料已死的账号必须标成「需重新登录」。
          // 旧判据 `!refreshable && !expiresAt` 在 CodeArts 身上恒为 false
          // （它的 expiresAt 永远有值），于是账号明明用不了、面板还显示「已启用」。
          // trae-2 正是 refreshable:false 的行，这条断言把新判据钉住。
          assert.includes(html, '需重新登录', '失效凭据的账号被标出来')
          assert.match(html, /ofm_acct pending/, 'pending 修饰类渲染出来了')
        } finally { harness.teardown() }
      },
    },
    {
      name: '展开「模型」折叠区后渲染树一致（含免费/隐藏/下架标记）',
      async run({ assert }) {
        const harness = await createHarness()
        try {
          const sides = await harness.mountBoth()
          for (const side of Object.values(sides)) {
            const count = openEveryFold(harness, side, '模型')
            assert.equal(count, 13, `${side.side}: one model fold per channel`)
          }
          await harness.settle(14)
          const diff = diffTrees(sides.reference.html(), sides.copy.html())
          assert.deepEqual(diff, [], 'model folds differ')
          assert.ok(sides.copy.pack.calls.some(call => call.method === 'model.list'), 'model.list was fetched')
          // the buddy card is the one that carries hidden + dead rows
          const cards = allByClass(sides.copy.container, 'ofm_chan')
          const buddy = cards.find(card => card.textContent.includes('WorkBuddy 国内版'))
          assert.ok(buddy, 'buddy card missing')
          const buddyHtml = serialize(buddy)
          assert.includes(buddyHtml, '免费额度')
          assert.includes(buddyHtml, '已隐藏')
          assert.includes(buddyHtml, '已下架')
          // buddy: 4 models, 1 hidden -> 3/4 on display
          assert.includes(buddyHtml, '已开启 3/4')
          const codearts = cards.find(card => card.textContent.includes('CodeArts'))
          assert.includes(serialize(codearts), '已开启 3/4')
        } finally { harness.teardown() }
      },
    },
    {
      name: 'CSS 子集覆盖页面渲染出的每一个类名（无样式缺失）',
      async run({ assert, readFile }) {
        const css = readFile('src/extracted/channels.css')
        const defined = new Set([...css.matchAll(/\.(ofm[_-][A-Za-z0-9_-]+)/g)].map(match => match[1]))
        const harness = await createHarness()
        try {
          const sides = await harness.mountBoth()
          // open both folds, then collect every class the page can render
          for (const side of Object.values(sides)) {
            for (const label of ['账号', '模型']) {
              const button = elements(side.container).find(element => element.tagName === 'BUTTON'
                && element.classList.contains('ofm_foldtoggle') && element.textContent.includes(label))
              button?.dispatchEvent(new harness.env.window.Event('click', { bubbles: true }))
            }
          }
          harness.env.ReactDOM.flushSync(() => {})
          await harness.settle(8)
          const used = new Set()
          for (const element of elements(sides.copy.container)) {
            for (const name of String(element.getAttribute('class') ?? '').split(/\s+/)) if (name.startsWith('ofm')) used.add(name)
          }
          // classes rendered only by transient states the mock does not reach
          const transient = new Set([
            'ofm_skel', 'ofm_callout', 'ofm_error', 'ofm_scrim', 'ofm_modal', 'ofm_modalhead',
            'ofm_modalbody', 'ofm_modalfoot', 'ofm_loginbody', 'ofm_loginstep', 'ofm_scanline',
            'ofm_balanceerror', 'ofm_toasts', 'ofm_toast', 'ofm_toasttitle', 'ofm_toastbody',
            'ofm_toastactions', 'ofm_toastclose', 'ofm_mono', 'ofm_acctlist', 'ofm_acct',
            'ofm_acctname', 'ofm_acctmeta', 'ofm_acctacts', 'ofm_modellist', 'ofm_modelrow',
            'ofm_tagpill', 'ofm_chanfold', 'ofm_creditbadge', 'ofm_credittotal', 'ofm_id',
            'ofm_minibtn', 'ofm_foldtoggle', 'ofm_chanacts',
          ])
          const missing = [...used].filter(name => !defined.has(name) && !transient.has(name))
          assert.deepEqual(missing, [], 'rendered classes with no CSS rule')
          // and the subset must still carry the rules for the card grid itself
          for (const required of ['ofm_chanwrap', 'ofm_chan', 'ofm_chanhead', 'ofm_chanlogo', 'ofm_chanid', 'ofm_chanstate', 'ofm_chanmeta', 'ofm_chanacts']) {
            assert.ok(defined.has(required), `CSS is missing .${required}`)
          }
        } finally { harness.teardown() }
      },
    },
    {
      name: '词条一致：chan.* 的 zh / en 两份与插件 DICT 逐条相同',
      async run({ assert, readFile }) {
        const harness = await createHarness()
        try {
          const dict = harness.reference.test.DICT
          const { CHANNEL_DICT } = await import('../extracted/channels-dict.js')
          for (const locale of ['zh', 'en']) {
            const expected = Object.fromEntries(Object.entries(dict[locale]).filter(([key]) => key.startsWith('chan.')))
            const actual = CHANNEL_DICT[locale]
            assert.equal(Object.keys(actual).length, Object.keys(expected).length, `${locale} key count`)
            for (const [key, value] of Object.entries(expected)) {
              assert.equal(actual[key], value, `${locale} ${key}`)
            }
          }
          // and the file on disk is the export (no hand edits after generation)
          const onDisk = readFile('src/extracted/channels-dict.js')
          assert.includes(onDisk, '"白嫖模型接入"')
          assert.includes(onDisk, '"Free channel access"')
        } finally { harness.teardown() }
      },
    },
    {
      name: '样式来源可追溯：子集里的每条规则都能在原插件 CSS 中找到原文',
      async run({ assert, readFile }) {
        const harness = await createHarness()
        try {
          const full = harness.reference.test.CSS.replace(/\s+/g, '')
          // Rules keep their source formatting (a selector list may wrap), so
          // parse blocks out of the sheet instead of splitting on newlines.
          const subset = readFile('src/extracted/channels.css')
          const rules = []
          let index = 0
          while (index < subset.length) {
            const open = subset.indexOf('{', index)
            if (open === -1) break
            const selector = subset.slice(index, open)
            let depth = 0
            let cursor = open
            for (; cursor < subset.length; cursor += 1) {
              if (subset[cursor] === '{') depth += 1
              else if (subset[cursor] === '}') { depth -= 1; if (depth === 0) break }
            }
            const body = subset.slice(open + 1, cursor)
            if (selector.trim().startsWith('.')) rules.push({ selector, body })
            index = cursor + 1
          }
          assert.ok(rules.length > 100, `expected >100 rules, got ${rules.length}`)
          const missing = rules.filter(rule => !full.includes((rule.selector + '{' + rule.body + '}').replace(/\s+/g, '')))
          assert.equal(missing.length, 0, `rules not found verbatim in the source: ${missing.slice(0, 3).map(rule => rule.selector.trim().slice(0, 60)).join(' | ')}`)
          const selectors = rules.map(rule => rule.selector.trim())
          for (const required of ['.ofm_chan', '.ofm_chanlogo', '.ofm_chanacts', '.ofm_foldtoggle', '.ofm_creditbadge']) {
            assert.ok(selectors.includes(required), `missing ${required}`)
          }
        } finally { harness.teardown() }
      },
    },
  ],
}
