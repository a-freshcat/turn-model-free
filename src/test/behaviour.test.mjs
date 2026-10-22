/**
 * Behaviour: the copied page actually *works* — every button on the card does
 * what its label says, against the mock pack, with no login and no network.
 *
 * Each test drives the DOM the way a user would (click the real `<button>`),
 * then asserts on what the page shows. Where a flow exists on both sides, the
 * reference is driven through the same steps so "works" also means "behaves
 * like the original".
 */

import { createHarness } from './harness.mjs'
import { allByClass, clickButton, elements, serialize } from './env.mjs'

const cardFor = (container, name) => {
  const card = allByClass(container, 'ofm_chan').find(entry => entry.textContent.includes(name))
  if (card === undefined) throw new Error(`card not found: ${name}`)
  return card
}

const buttonIn = (node, label) => {
  const button = elements(node).find(element => element.tagName === 'BUTTON'
    && element.textContent.trim() === label)
  if (button === undefined) throw new Error(`button not found: ${label}`)
  return button
}

const click = (env, node) => {
  node.dispatchEvent(new env.window.Event('click', { bubbles: true, cancelable: true }))
  env.ReactDOM.flushSync(() => {})
}

export default {
  title: 'Behaviour · 功能是否真的能用',
  tests: [
    {
      name: '添加账号：登录 URL 打开、轮询落地、账号与余额刷新（两侧一致）',
      async run({ assert }) {
        const harness = await createHarness()
        const { env, sides } = harness
        try {
          await harness.mountBoth()
          const opened = []
          const originalOpen = env.window.open
          env.window.open = url => { opened.push(url); return null }
          for (const side of Object.values(sides)) click(env, buttonIn(cardFor(side.container, 'LobsterAI'), '添加账号'))
          await harness.settle(16)
          // login.poll runs on a 3s interval: advance the virtual clock past one tick
          await harness.advance(4000)
          env.window.open = originalOpen
          for (const side of Object.values(sides)) {
            const created = side.pack.calls.find(call => call.method === 'account.create')
            assert.ok(created, `${side.side}: account.create was called`)
            assert.equal(created.payload.provider, 'lobsterai', `${side.side}: provider passed through`)
            assert.ok(side.pack.calls.some(call => call.method === 'login.poll'), `${side.side}: login.poll polled`)
            const html = side.html()
            assert.includes(html, '已接入：新账号', `${side.side}: login success notice`)
            assert.includes(html, 'LobsterAI')
            assert.equal(side.pack.state.get('lobsterai').accounts.length, 1, `${side.side}: account landed in the pool`)
          }
          assert.ok(opened.some(url => String(url).includes('https://example.invalid/login')), `login url opened (${opened.length} opens)`)
          // both sides ended on the same notice text
          assert.equal(
            sides.copy.html().split('\n').filter(line => line.includes('已接入：')).join('\n'),
            sides.reference.html().split('\n').filter(line => line.includes('已接入：')).join('\n'),
            'same success notice',
          )
        } finally { harness.teardown() }
      },
    },
    {
      name: '登录链接不是 http(s) 时拒绝执行（不在页面里跳转任意 scheme）',
      async run({ assert }) {
        const harness = await createHarness()
        const { env, sides } = harness
        try {
          await harness.mountBoth()
          const side = sides.copy
          // hijack account.create to answer a javascript: URL
          const original = side.pack.connection.rpc.call
          side.pack.connection.rpc.call = async (_s, plugin, request, signal) => {
            if (request?.method === 'account.create') {
              side.pack.calls.push({ plugin, method: request.method, payload: request.payload })
              return { ok: true, value: { accountId: 'bad-1', loginUrl: 'javascript:alert(1)' } }
            }
            return original.call(side.pack.connection.rpc, _s, plugin, request, signal)
          }
          const popups = []
          env.window.open = url => { popups.push(url); return null }
          click(env, buttonIn(cardFor(side.container, 'TRAE'), '添加账号'))
          await harness.settle(10)
          assert.notOk(popups.some(url => String(url).startsWith('javascript:')), `no javascript: navigation (${popups.join(',')})`)
          assert.notIncludes(side.html(), 'javascript:alert', 'bad URL never rendered')
        } finally { harness.teardown() }
      },
    },
    {
      name: '测试连接：成功显示毫秒数，失败显示原因',
      async run({ assert }) {
        const harness = await createHarness()
        const { env, sides } = harness
        try {
          await harness.mountBoth()
          const side = sides.copy
          // open the account fold for CodeArts, then hit 测试
          harness.env.ReactDOM.flushSync(() => {})
          const card = cardFor(side.container, 'CodeArts')
          click(env, buttonIn(card, '账号'))
          await harness.settle(10)
          click(env, buttonIn(cardFor(side.container, 'CodeArts'), '测试'))
          await harness.settle(10)
          assert.includes(side.html(), '连接正常（128 ms）')
          assert.ok(side.pack.calls.some(call => call.method === 'account.test'), 'account.test called')
        } finally { harness.teardown() }
      },
    },
    {
      name: '测试连接失败时把上游原因显示出来',
      async run({ assert }) {
        const harness = await createHarness({ mock: { testFails: true } })
        const { env, sides } = harness
        try {
          await harness.mountBoth()
          const side = sides.copy
          click(env, buttonIn(cardFor(side.container, 'CodeArts'), '账号'))
          await harness.settle(10)
          click(env, buttonIn(cardFor(side.container, 'CodeArts'), '测试'))
          await harness.settle(10)
          assert.includes(side.html(), '测试失败：401 凭证失效')
        } finally { harness.teardown() }
      },
    },
    {
      name: '移除账号：确认框取消时不删，确认后才删',
      async run({ assert }) {
        const harness = await createHarness()
        const { env, sides } = harness
        try {
          await harness.mountBoth()
          const side = sides.copy
          click(env, buttonIn(cardFor(side.container, 'CodeArts'), '账号'))
          await harness.settle(10)
          const asks = []
          env.window.confirm = message => { asks.push(message); return false }
          click(env, buttonIn(cardFor(side.container, 'CodeArts'), '移除'))
          await harness.settle(6)
          assert.equal(asks.length, 1, 'confirm asked once')
          assert.includes(asks[0], '移除账号 codearts-b2a98891？')
          assert.notOk(side.pack.calls.some(call => call.method === 'account.delete'), 'cancel keeps the account')
          env.window.confirm = () => true
          click(env, buttonIn(cardFor(side.container, 'CodeArts'), '移除'))
          await harness.settle(10)
          assert.ok(side.pack.calls.some(call => call.method === 'account.delete'), 'confirmed delete ran')
          assert.equal(side.pack.state.get('codearts').accounts.length, 0, 'account removed from the pool')
          assert.includes(side.html(), '还没有账号——点「添加账号」开始登录。')
        } finally { harness.teardown() }
      },
    },
    {
      name: '停用 / 启用账号走 account.update，卡片状态随之翻转',
      async run({ assert }) {
        const harness = await createHarness()
        const { env, sides } = harness
        try {
          await harness.mountBoth()
          const side = sides.copy
          click(env, buttonIn(cardFor(side.container, 'CodeArts'), '账号'))
          await harness.settle(10)
          click(env, buttonIn(cardFor(side.container, 'CodeArts'), '停用'))
          await harness.settle(10)
          const update = side.pack.calls.find(call => call.method === 'account.update')
          assert.ok(update, 'account.update called')
          assert.deepEqual(update.payload.patch, { enabled: false }, 'patch flips enabled')
          assert.equal(side.pack.state.get('codearts').accounts[0].enabled, false, 'pool updated')
          assert.includes(serialize(cardFor(side.container, 'CodeArts')), '已停用')
          click(env, buttonIn(cardFor(side.container, 'CodeArts'), '启用'))
          await harness.settle(10)
          assert.equal(side.pack.state.get('codearts').accounts[0].enabled, true, 'enabled again')
        } finally { harness.teardown() }
      },
    },
    {
      name: '一键领取：显示领取数量与积分，并刷新余额',
      async run({ assert }) {
        const harness = await createHarness()
        const { env, sides } = harness
        try {
          await harness.mountBoth()
          const side = sides.copy
          const before = side.pack.state.get('codearts').balance[0].total
          click(env, buttonIn(cardFor(side.container, 'CodeArts'), '一键领取'))
          await harness.settle(12)
          assert.ok(side.pack.calls.some(call => call.method === 'credits.claimAll'), 'credits.claimAll called')
          assert.includes(side.html(), '已领取 1 个账号，共 1000 积分。')
          assert.ok(side.pack.state.get('codearts').balance[0].total > before, 'balance grew')
          // a claim must be followed by a forced balance refresh
          const claims = side.pack.calls.filter(call => call.method === 'credits.balances')
          assert.ok(claims.length >= 2, `balance refreshed after the claim (${claims.length} reads)`)
        } finally { harness.teardown() }
      },
    },
    {
      name: '领取结果部分失败时，成功与失败原因一起显示',
      async run({ assert }) {
        const harness = await createHarness()
        const { env, sides } = harness
        try {
          await harness.mountBoth()
          const side = sides.copy
          click(env, buttonIn(cardFor(side.container, 'TRAE'), '一键领取'))
          await harness.settle(12)
          const html = serialize(cardFor(side.container, 'TRAE'))
          assert.includes(html, '已领取 1 个账号，共 50 积分。')
          assert.includes(html, '领取失败：凭证过期')
        } finally { harness.teardown() }
      },
    },
    {
      name: '余额查询失败：卡片汇总显示失败提示，账号行显示上游原因',
      async run({ assert }) {
        const harness = await createHarness()
        const { env, sides } = harness
        try {
          await harness.mountBoth()
          const side = sides.copy
          click(env, buttonIn(cardFor(side.container, 'TRAE'), '账号'))
          await harness.settle(10)
          const html = serialize(cardFor(side.container, 'TRAE'))
          assert.includes(html, '个账号未能查询余额', 'card-level partial notice')
          assert.includes(html, '凭证过期', 'per-row reason')
          // and a healthy channel shows the number instead
          assert.includes(serialize(cardFor(side.container, 'CodeArts')), '剩余积分')
        } finally { harness.teardown() }
      },
    },
    {
      name: '刷新余额按钮强制重查（force: true）',
      async run({ assert }) {
        const harness = await createHarness()
        const { env, sides } = harness
        try {
          await harness.mountBoth()
          const side = sides.copy
          click(env, buttonIn(cardFor(side.container, 'CodeArts'), '刷新余额'))
          await harness.settle(10)
          const forced = side.pack.calls.filter(call => call.method === 'credits.balances' && call.payload.force === true)
          assert.equal(forced.length, 1, 'exactly one forced read')
          assert.equal(forced[0].payload.provider, 'codearts')
        } finally { harness.teardown() }
      },
    },
    {
      name: '模型开关：单条关闭走 model.setDisabled，全部关闭走 model.setAllDisabled',
      async run({ assert }) {
        const harness = await createHarness()
        const { env, sides } = harness
        try {
          await harness.mountBoth()
          const side = sides.copy
          click(env, buttonIn(cardFor(side.container, 'CodeArts'), '模型'))
          await harness.settle(10)
          click(env, buttonIn(cardFor(side.container, 'CodeArts'), '关闭'))
          await harness.settle(10)
          const single = side.pack.calls.find(call => call.method === 'model.setDisabled')
          assert.ok(single, 'model.setDisabled called')
          assert.deepEqual(single.payload, { provider: 'codearts', modelId: 'deepseek-v4', disabled: true })
          click(env, buttonIn(cardFor(side.container, 'CodeArts'), '全部关闭'))
          await harness.settle(10)
          const all = side.pack.calls.find(call => call.method === 'model.setAllDisabled')
          assert.ok(all, 'model.setAllDisabled called')
          assert.deepEqual(all.payload, { provider: 'codearts', disabled: true })
          assert.ok(side.pack.state.get('codearts').models.every(model => model.disabled === true), 'all hidden in the pool')
          assert.includes(serialize(cardFor(side.container, 'CodeArts')), '已开启 0/4')
        } finally { harness.teardown() }
      },
    },
    {
      name: '已下架模型：点「恢复显示」走 model.clearDead 并清掉标记',
      async run({ assert }) {
        const harness = await createHarness()
        const { env, sides } = harness
        try {
          await harness.mountBoth()
          const side = sides.copy
          click(env, buttonIn(cardFor(side.container, 'WorkBuddy 国内版'), '模型'))
          await harness.settle(10)
          assert.includes(serialize(cardFor(side.container, 'WorkBuddy 国内版')), '已下架')
          click(env, buttonIn(cardFor(side.container, 'WorkBuddy 国内版'), '恢复显示'))
          await harness.settle(10)
          const clear = side.pack.calls.find(call => call.method === 'model.clearDead')
          assert.ok(clear, 'model.clearDead called')
          assert.deepEqual(clear.payload, { provider: 'buddy', modelId: 'hunyuan-t1' })
          assert.notIncludes(serialize(cardFor(side.container, 'WorkBuddy 国内版')), '已下架')
        } finally { harness.teardown() }
      },
    },
    {
      name: '每日自动签到开关：切换即写回 pack，状态文字跟着变',
      async run({ assert }) {
        const harness = await createHarness()
        const { env, sides } = harness
        try {
          await harness.mountBoth()
          const side = sides.copy
          const switchButton = elements(side.container).find(element => element.getAttribute('role') === 'switch')
          assert.ok(switchButton, 'auto check-in switch rendered')
          assert.equal(switchButton.getAttribute('aria-checked'), 'true')
          click(env, switchButton)
          await harness.settle(10)
          const writes = side.pack.calls.filter(call => call.method === 'usage.autoCheckin' && call.payload.enabled !== undefined)
          assert.equal(writes.length, 1, 'one write')
          assert.equal(writes[0].payload.enabled, false, 'turned off')
          const after = elements(side.container).find(element => element.getAttribute('role') === 'switch')
          assert.equal(after.getAttribute('aria-checked'), 'false', 'switch reflects the write')
        } finally { harness.teardown() }
      },
    },
    {
      name: '宿主没有 connection 服务时给出说明，卡片按钮全部禁用',
      async run({ assert }) {
        const harness = await createHarness()
        const { sides } = harness
        try {
          sides.copy.mount({ ctx: {} })
          await harness.settle(8)
          const html = sides.copy.html()
          assert.includes(html, '宿主未提供 connection 服务，渠道操作暂不可用。')
          // Identity (the 13 channels) is static, so the grid still renders — but
          // every action button must be disabled because there is no transport.
          const cards = allByClass(sides.copy.container, 'ofm_chan')
          assert.equal(cards.length, 13, 'static channel identity still renders')
          const actionButtons = elements(cards[0]).filter(element => element.tagName === 'BUTTON' && element.classList.contains('ofm_btn'))
          assert.ok(actionButtons.length > 0, 'card has action buttons')
          for (const button of actionButtons) assert.ok(button.hasAttribute('disabled'), `disabled without a transport: ${button.textContent.trim()}`)
          // and no RPC was attempted at all
          assert.equal(sides.copy.pack.calls.length, 0, 'no RPC without a transport')
        } finally { harness.teardown() }
      },
    },
    {
      name: '渠道包挂载失败时显示原因与提示',
      async run({ assert }) {
        const harness = await createHarness()
        const { sides } = harness
        try {
          // `failed` alone is computed as `!ready && pack.state === 'failed'`, so
          // the probe has to still be outstanding when the page paints.
          sides.copy.mount({ ctx: {}, summary: { channels: { state: 'failed', error: 'import failed: ENOENT pack.js' } } })
          await harness.settle(10)
          const html = sides.copy.html()
          assert.includes(html, '渠道包未挂载：import failed: ENOENT pack.js')
          assert.includes(html, '渠道包由本插件内置（vendor/channel-pack）')
          // no grid while the readiness probe has not answered
          assert.equal(allByClass(sides.copy.container, 'ofm_chan').length, 0, 'no cards before the probe lands')
          assert.equal(allByClass(sides.copy.container, 'ofm_skel').length, 3, 'skeleton placeholders')
        } finally { harness.teardown() }
      },
    },
    {
      name: 'probe 失败重试 4 次（每次 4s），耗尽后把 RPC 错误显示出来',
      async run({ assert }) {
        const harness = await createHarness({ mock: { fail: ['provider.status'] } })
        const { sides } = harness
        try {
          // pack state reports ready, so the retry note is the *only* explanation
          sides.copy.mount()
          await harness.settle(10)
          assert.equal(sides.copy.pack.calls.filter(call => call.method === 'provider.status').length, 1, 'first probe')
          for (let round = 0; round < 5; round += 1) await harness.advance(4200)
          assert.ok(sides.copy.pack.calls.filter(call => call.method === 'provider.status').length >= 5, 'retried')
          assert.includes(sides.copy.html(), '操作失败：mock-pack: provider.status failed (forced)')
        } finally { harness.teardown() }
      },
    },
    {
      name: 'ZCode：渠道选择写进 localStorage，并带进 account.create',
      async run({ assert }) {
        const harness = await createHarness()
        const { env, sides } = harness
        try {
          await harness.mountBoth()
          const side = sides.copy
          const zcard = cardFor(side.container, 'ZCode')
          click(env, buttonIn(zcard, 'z.ai 国际版'))
          assert.equal(env.window.localStorage.getItem('ofm:zcode:login-provider'), 'zai', 'preference persisted')
          env.window.open = () => null
          click(env, buttonIn(cardFor(side.container, 'ZCode'), '添加账号'))
          await harness.settle(12)
          const created = side.pack.calls.find(call => call.method === 'account.create')
          assert.ok(created, 'account.create called')
          assert.equal(created.payload.zcodeProvider, 'zai', 'route passed to the pack')
        } finally { harness.teardown() }
      },
    },
    {
      name: 'RPC 失败：toast 报出渠道名，页面不崩',
      async run({ assert }) {
        const harness = await createHarness({ mock: { fail: ['account.test'] } })
        const { env, sides } = harness
        try {
          await harness.mountBoth()
          const side = sides.copy
          click(env, buttonIn(cardFor(side.container, 'CodeArts'), '账号'))
          await harness.settle(10)
          click(env, buttonIn(cardFor(side.container, 'CodeArts'), '测试'))
          await harness.settle(10)
          const toasts = allByClass(env.document.body, 'ofm_toast')
          assert.ok(toasts.length > 0, 'toast rendered')
          assert.includes(toasts[0].textContent, 'CodeArts')
          assert.includes(toasts[0].textContent, 'account.test failed')
          // the page is still usable
          assert.equal(allByClass(side.container, 'ofm_chan').length, 13, 'cards still rendered')
        } finally { harness.teardown() }
      },
    },
    {
      name: '阶段状态：只开了一半的渠道显示未接入，卡片有 off/idle 区分',
      async run({ assert }) {
        const harness = await createHarness()
        const { sides } = harness
        try {
          await harness.mountBoth()
          const html = serialize(sides.copy.container)
          assert.includes(html, 'data-state="on"', 'connected cards')
          assert.includes(html, 'data-state="idle"', 'channels with no enabled account')
          const lob = serialize(cardFor(sides.copy.container, 'LobsterAI'))
          assert.includes(lob, '未接入')
          assert.includes(lob, '0/0')
        } finally { harness.teardown() }
      },
    },
    {
      name: '整页关闭（closed）时卡片显示「turned off」且按钮不受阻',
      async run({ assert }) {
        const harness = await createHarness({ mock: { closed: true } })
        const { sides } = harness
        try {
          await harness.mountBoth()
          const card = serialize(cardFor(sides.copy.container, 'CodeArts'))
          assert.includes(card, '已关闭')
          assert.includes(card, 'data-state="off"')
          assert.includes(card, 'off')
          // the reference agrees
          assert.deepEqual(sides.copy.html(), sides.reference.html(), 'closed state renders identically')
        } finally { harness.teardown() }
      },
    },
    {
      name: '英文界面：同样 13 张卡，文案走 en 词条',
      async run({ assert }) {
        const harness = await createHarness()
        const { env } = harness
        try {
          const page = harness.sides.copy.page
          const dict = page.dict
          const t = Object.assign(key => dict.en[key] ?? key, { locale: 'en', dict: dict.en })
          const box = env.document.createElement('div')
          env.document.body.appendChild(box)
          env.mountInto(box, env.h(page.ChannelsPage, {
            t, ctx: { connection: harness.sides.copy.pack.connection }, summary: { channels: { state: 'ready' } },
          }))
          await harness.settle(12)
          env.ReactDOM.flushSync(() => {})
          const html = serialize(box)
          assert.includes(html, 'Free channel access')
          assert.includes(html, 'Accounts')
          assert.includes(html, 'Add account')
          assert.equal(allByClass(box, 'ofm_chan').length, 13)
        } finally { harness.teardown() }
      },
    },
  ],
}
