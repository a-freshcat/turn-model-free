/**
 * A fake channel-pack backend: the same RPC method names, payload shapes and
 * reply shapes the real pack answers on `/api/channel-pack`.
 *
 * Written from the RPC calls the page makes (`account.list`, `model.list`,
 * `credits.balances`, `provider.status`, `credits.claimAll`, …) and from what
 * the installed pack's own state files show for each provider. Nothing here
 * talks to a vendor: it exists so the copied page can be exercised end to end
 * without a login, a credential, or a network call.
 *
 * Shapes were cross-checked against `~/.dsh/channel-pack/state.json` (accounts,
 * providers, expiry fields) and `auto-checkin.json` (check-in summary text).
 */

const DAY = 86_400_000
const now = () => Date.now()

/** Per-provider canned data. `accounts` mirrors real rows from state.json. */
const SEED = {
  codearts: {
    accounts: [{ id: 'codearts-b2a98891', nickname: 'codearts-b2a98891', enabled: true, refreshable: true, expiresIn: 5 * DAY }],
    models: [
      { id: 'deepseek-v4', name: 'DeepSeek V4', isFree: true },
      { id: 'glm-5', name: 'GLM-5', isFree: true },
      { id: 'openpangu-2', name: 'openPangu 2', isFree: false },
      { id: 'qwen3-coder', name: 'Qwen3 Coder', isFree: false, disabled: true },
    ],
    balance: [{ accountId: 'codearts-b2a98891', total: 1000 }],
    claim: { claimed: 1, totalCredit: 1000 },
  },
  buddy: {
    accounts: [
      { id: 'buddy-b0bf3b35', nickname: '153****9148', enabled: true, refreshable: true, expiresIn: 9 * DAY },
      { id: 'buddy-472071ad', nickname: 'buddy-472071ad', enabled: true, refreshable: true, expiresIn: 9 * DAY },
    ],
    models: [
      { id: 'deepseek-v4.1-flash', name: 'DeepSeek V4.1 Flash', isFree: true },
      { id: 'kimi-k3', name: 'Kimi K3', isFree: true },
      { id: 'glm-5-air', name: 'GLM-5 Air', isFree: true, disabled: true },
      { id: 'hunyuan-t1', name: 'Hunyuan T1', isFree: false, dead: true },
    ],
    balance: [
      { accountId: 'buddy-b0bf3b35', total: 400 },
      { accountId: 'buddy-472071ad', total: 200 },
    ],
    claim: { claimed: 2, totalCredit: 400 },
  },
  workbuddy: {
    accounts: [{ id: 'workbuddy-2b3bbc37', nickname: 'yuanfuhandsome@gmail.com', enabled: true, refreshable: true, expiresIn: 365 * DAY }],
    models: [
      { id: 'deepseek-v4.1-flash', name: 'DeepSeek V4.1 Flash', isFree: true },
      { id: 'kimi-k3', name: 'Kimi K3', isFree: false },
    ],
    balance: [],
  },
  lobsterai: {
    accounts: [],
    models: [],
    balance: [],
    claim: { claimed: 0, totalCredit: 0 },
  },
  qoder: {
    accounts: [{ id: 'qoder-1f00aa11', nickname: 'qoder-1f00aa11', enabled: true, refreshable: true, expiresIn: 2 * DAY }],
    models: [
      { id: 'qoder-lite', name: 'Qoder Lite', isFree: true },
      { id: 'qoder-pro', name: 'Qoder Pro', isFree: false },
    ],
    balance: [{ accountId: 'qoder-1f00aa11', total: 100 }],
    claim: { claimed: 1, totalCredit: 100 },
  },
  qodercn: {
    accounts: [{ id: 'qodercn-bdc2b290', nickname: 'nick4849922841', enabled: true, refreshable: true, expiresIn: 30 * DAY }],
    models: [{ id: 'qodercn-lite', name: 'Qoder CN Lite', isFree: true }],
    balance: [{ accountId: 'qodercn-bdc2b290', total: 100 }],
    claim: { claimed: 0, totalCredit: 0 },
  },
  trae: {
    accounts: [
      { id: 'trae-1', nickname: '138****0001', enabled: true, refreshable: true, expiresIn: 3 * DAY },
      { id: 'trae-2', nickname: '138****0002', enabled: false, refreshable: false, expiresIn: null },
    ],
    models: [{ id: 'trae-free', name: 'TRAE Free', isFree: true }],
    balance: [{ accountId: 'trae-1', total: 50 }, { accountId: 'trae-2', error: '凭证过期' }],
    claim: { claimed: 1, totalCredit: 50, failed: ['凭证过期'] },
  },
  cline: {
    accounts: [{ id: 'cline-1', nickname: 'cline-1', enabled: true, refreshable: true, expiresIn: 20 * DAY }],
    models: [{ id: 'cline-free', name: 'Cline Free', isFree: true }],
    balance: [{ accountId: 'cline-1', total: 4.5, unit: 'dollars' }],
  },
  loomy: {
    accounts: [{ id: 'loomy-1', nickname: '微信用户', enabled: true, refreshable: true, expiresIn: 6 * DAY }],
    models: [{ id: 'loomy-free', name: 'Loomy Free', isFree: true }],
    balance: [{ accountId: 'loomy-1', total: 300 }],
    claim: { claimed: 1, totalCredit: 300 },
  },
  raccoon: {
    accounts: [{ id: 'raccoon-1', nickname: '186****0001', enabled: true, refreshable: true, expiresIn: 12 * DAY }],
    models: [{ id: 'raccoon-free', name: 'Raccoon Free', isFree: true }],
    balance: [],
  },
  minimax: {
    accounts: [{ id: 'minimax-1', nickname: 'MiniMax 设备码', enabled: true, refreshable: true, expiresIn: 60 * DAY }],
    models: [{ id: 'minimax-m2', name: 'MiniMax M2', isFree: true }],
    balance: [{ accountId: 'minimax-1', total: 1200 }],
    claim: { claimed: 1, totalCredit: 1200 },
  },
  zcode: {
    accounts: [{ id: 'zcode-1', nickname: 'zcode-1', enabled: true, refreshable: true, expiresIn: 14 * DAY }],
    models: [{ id: 'glm-5', name: 'GLM-5', isFree: true }],
    balance: [{ accountId: 'zcode-1', total: 1_000_000, unit: 'tokens' }],
    claim: { claimed: 1, totalCredit: 1_000_000 },
  },
  gemini: {
    accounts: [{ id: 'gemini-1', nickname: 'me@example.com', enabled: true, refreshable: true, expiresIn: 1 * DAY }],
    models: [
      { id: 'gemini-3-flash', name: 'Gemini 3 Flash', isFree: true },
      { id: 'gemini-3-pro', name: 'Gemini 3 Pro', isFree: false },
    ],
    balance: [{
      accountId: 'gemini-1',
      packages: [{ name: 'Flash 免费额度', remaining: 82 }, { name: 'Pro 免费额度', remaining: 41 }],
      total: 61.5,
    }],
  },
}

function stateFor(provider, options = {}) {
  const seed = SEED[provider]
  if (seed === undefined) return { accounts: [], models: [], balance: [], claim: { claimed: 0, totalCredit: 0 } }
  if (options.empty === true) return { accounts: [], models: seed.models.map(row => ({ id: row.id, name: row.name, isFree: row.isFree === true })), balance: [], claim: { claimed: 0, totalCredit: 0 } }
  return {
    accounts: seed.accounts.map(row => ({
      id: row.id,
      provider,
      nickname: row.nickname,
      enabled: row.enabled !== false,
      refreshable: row.refreshable !== false,
      expiresAt: row.expiresIn === null ? 0 : now() + row.expiresIn,
      createdAt: now() - 40 * DAY,
    })),
    models: seed.models.map(row => ({ id: row.id, name: row.name, isFree: row.isFree === true, disabled: row.disabled === true, dead: row.dead === true })),
    balance: seed.balance.map(row => ({ ...row })),
    claim: seed.claim ?? { claimed: 0, totalCredit: 0 },
  }
}

/**
 * @param {object} [options]
 * @param {string[]} [options.fail]  RPC methods that should throw
 * @param {number} [options.loginPolls]  polls before a started login completes
 * @param {boolean} [options.closed]  report every provider as "turned off"
 * @param {boolean} [options.empty]  start with no accounts (a fresh install)
 * @param {boolean} [options.failBalances]  every balance read fails
 * @param {boolean} [options.claimFails]  claim reports a per-account failure
 */
export function createMockPack(options = {}) {
  const state = new Map()
  const calls = []
  const listeners = new Set()
  const fail = new Set(options.fail ?? [])
  /** Account ids created by a login, with the poll count they are on. */
  const pending = new Map()
  const autoCheckin = {
    enabled: true,
    running: false,
    ranToday: true,
    lastResult: '3 个渠道：3 个账号领取成功（+1200积分），2 个渠道不支持签到',
    lastAt: now() - 3 * 3600_000,
  }

  const get = provider => {
    if (!state.has(provider)) state.set(provider, stateFor(provider, options))
    return state.get(provider)
  }

  /**
   * Some methods carry only an `accountId` (`account.delete`, `account.update`,
   * `account.test`, `account.refresh`) because the pack keys accounts globally —
   * so the mock resolves the owning provider the same way.
   */
  const providerOfAccount = accountId => {
    for (const provider of Object.keys(SEED)) {
      if (get(provider).accounts.some(row => row.id === accountId)) return provider
    }
    return undefined
  }
  const withProvider = payload => payload.provider === undefined && payload.accountId !== undefined
    ? { ...payload, provider: providerOfAccount(payload.accountId) }
    : payload

  const notify = () => { for (const listener of listeners) listener() }

  const reply = (method, payload = {}) => {
    const provider = payload.provider
    switch (method) {
      case 'provider.status': {
        const statuses = {}
        for (const id of payload.providers ?? []) {
          const row = get(id)
          statuses[id] = {
            accounts: { total: row.accounts.length, enabled: row.accounts.filter(entry => entry.enabled).length },
            models: { total: row.models.length, disabled: row.models.filter(entry => entry.disabled).length },
            closed: options.closed === true,
          }
        }
        return { statuses }
      }
      case 'account.list': return { accounts: get(provider).accounts.map(row => ({ ...row })) }
      case 'model.list': return { models: get(provider).models.map(row => ({ ...row })) }
      case 'credits.status': {
        return {
          accounts: get(provider).accounts.map(row => ({
            accountId: row.id,
            status: {
              todayCheckedIn: options.noCheckin !== true,
              todayCredit: options.noCheckin === true ? 0 : 200,
            },
          })),
        }
      }
      case 'credits.balances': {
        const row = get(provider)
        if (options.failBalances === true) throw new Error('余额接口 502 Bad Gateway')
        return {
          accounts: row.balance.map(entry => ({
            accountId: entry.accountId,
            balance: entry.error !== undefined
              ? null
              : {
                total: entry.total ?? null,
                ...entry.packages === undefined ? {} : { packages: entry.packages },
              },
            ...entry.error === undefined ? {} : { error: entry.error },
          })),
        }
      }
      case 'account.create': {
        const id = `${provider}-login${pending.size + 1}`
        pending.set(id, 0)
        return { accountId: id, loginUrl: `https://example.invalid/login?account=${encodeURIComponent(id)}&provider=${provider}` }
      }
      case 'login.poll': {
        const id = payload.accountId
        if (!pending.has(id)) return { done: false }
        const polls = pending.get(id) + 1
        pending.set(id, polls)
        if (polls < (options.loginPolls ?? 1)) return { done: false }
        pending.delete(id)
        const row = get(provider)
        row.accounts.push({ id, provider, nickname: `新账号 ${pending.size + 1}`, enabled: true, refreshable: true, expiresAt: now() + 30 * DAY, createdAt: now() })
        notify()
        return { done: true, success: true, login: `新账号 ${row.accounts.length}` }
      }
      case 'account.delete': {
        const row = get(provider)
        const index = row.accounts.findIndex(entry => entry.id === payload.accountId)
        if (index !== -1) row.accounts.splice(index, 1)
        notify()
        return { ok: true }
      }
      case 'account.update': {
        const row = get(provider)
        const entry = row.accounts.find(item => item.id === payload.accountId)
        if (entry !== undefined && payload.patch !== undefined) Object.assign(entry, payload.patch)
        notify()
        return { ok: true }
      }
      case 'account.test': return options.testFails === true ? { ok: false, error: '401 凭证失效' } : { ok: true, ms: 128 }
      case 'account.refresh': return { ok: true, expiresAt: now() + 30 * DAY }
      case 'model.setDisabled':
      case 'model.setAllDisabled': {
        const row = get(provider)
        const disabled = payload.disabled === true
        for (const model of row.models) {
          if (method === 'model.setAllDisabled' || model.id === payload.modelId) model.disabled = disabled
        }
        notify()
        return { ok: true }
      }
      case 'model.clearDead': {
        const row = get(provider)
        const entry = row.models.find(model => model.id === payload.modelId)
        if (entry !== undefined) delete entry.dead
        notify()
        return { ok: true }
      }
      case 'credits.claimAll': {
        const row = get(provider)
        const claim = row.claim
        if (claim.totalCredit > 0 && !options.claimAlreadyDone) {
          for (const entry of row.balance) if (entry.error === undefined && typeof entry.total === 'number') entry.total += claim.totalCredit / Math.max(1, row.balance.length)
        }
        notify()
        const failures = options.claimFails === true
          ? ['今日额度已被另一个窗口领取']
          : claim.failed ?? []
        return {
          summary: { claimed: claim.claimed, totalCredit: claim.totalCredit },
          results: failures.map(message => ({ accountId: row.accounts[0]?.id, outcome: { kind: 'failed', message } })),
        }
      }
      case 'usage.autoCheckin': {
        if (payload.enabled !== undefined) autoCheckin.enabled = payload.enabled === true
        return { autoCheckin: { ...autoCheckin } }
      }
      default: throw new Error(`mock-pack: unknown method ${method}`)
    }
  }

  return {
    calls,
    state,
    /** `connection` stub shaped like the shell's RPC service. */
    connection: {
      rpc: {
        async call(_scope, plugin, request, signal) {
          const { method, payload } = request ?? {}
          calls.push({ plugin, method, payload })
          if (fail.has(method)) throw new Error(`mock-pack: ${method} failed (forced)`)
          if (signal?.aborted === true) throw new Error('aborted')
          return { ok: true, value: reply(method, withProvider(payload ?? {})) }
        },
      },
    },
    /** Simulates the pack pushing a change from another window. */
    emitChange() { notify() },
    onCheckinChange(listener) { listeners.add(listener); return () => listeners.delete(listener) },
  }
}
