/**
 * 腾讯渠道请求管道 —— 把防护层接进真实的请求路径。
 *
 * 这不是"演示防护层能洗字符串"，而是把它按 pack.js 里的接线方式装进一条
 * 真的会发 HTTP 请求、真的会收到 11128、真的会升档重发的管道：
 *
 *   构建消息（按档位） → 发送 → 400 + 11128？ → 升一档 → 从原始输入重建 → 重发
 *                              ↓ 不是 11128
 *                          普通错误照常抛
 *
 * 与 pack.js 的对应关系（行号为 pack.js v2.5.4）：
 *   · `buildMessages(level)`          ← 35944：每档都从 options.messages 重建
 *   · `tencentGuarded`                ← 35942：只对 buddy / workbuddy 生效
 *   · `tencentEscalate()` 预算共享    ← 35972：HTTP 与 SSE 共用一份
 *   · HTTP 400/11128 升档循环         ← 36055：只在失败响应上探测
 *   · 仍失败则 markTencentGateError   ← 36062：结构化登记，不靠 error 自定义属性
 *
 * 刻意保留的边界：
 *   · 成功响应一个字节都不读（不 clone().text() 去嗅探）
 *   · 普通 400 不升档、不改内容（免得掩盖参数错误）
 *   · 每档都从原始输入重建，不在已改过的文本上二次改写（可复现、不叠加）
 */

import {
  applyTencentGateGuard,
  isTencentGateBlock,
  markTencentGateError,
  isTencentGateBlockError,
  tencentBaseGuardLevel,
  tencentNextGuardLevel,
  TENCENT_GATE_MAX_ESCALATIONS,
  tencentGateStats,
  tgTrace,
} from '../guard/tencent-gate.js'

/** 腾讯两车道。其它渠道完全不走这条管道。 */
export const TENCENT_CHANNELS = new Set(['buddy', 'workbuddy'])

/** 与宿主 LlmError 同形状的错误，足够表达"渠道校验拒绝"。 */
export class ChannelGateError extends Error {
  constructor(message, code, options = {}) {
    super(message)
    this.name = 'ChannelGateError'
    this.code = code
    if (options.status !== undefined) this.status = options.status
    this.failure = { status: options.status, code }
  }
}

/**
 * 把一条消息数组按当前档位洗一遍（不修改入参）。
 *
 * 只做「深拷贝 + 交给防护层」，不自己再实现一遍清洗：`applyTencentGateGuard`
 * 本身就会遍历 system / assistant 并处理字符串与文本块两种形状，重复处理
 * 会让已经变成 `DSH` 的文本再被当成待洗内容（实测会二次改写）。
 *
 * @param {Array<{role: string, content: unknown}>} messages
 * @param {number} level
 */
export function scrubForLevel(messages, level) {
  const wire = messages.map(message => ({
    role: message.role,
    content: typeof message.content === 'string'
      ? message.content
      : Array.isArray(message.content)
        ? message.content.map(part => (part?.type === 'text' ? { ...part } : part))
        : message.content,
  }))
  applyTencentGateGuard(wire, level)
  return wire
}

/**
 * 一条渠道请求的完整生命周期。
 *
 * @param {object} options
 * @param {string} options.channel          渠道 id（buddy / workbuddy / 其它）
 * @param {Array} options.messages          原始消息（每次重发都从这里重建）
 * @param {string} [options.system]         额外 system 提示
 * @param {(body: object) => Promise<{ok: boolean, status: number, text: () => Promise<string>}>} options.send
 *        真实发送函数。由调用方注入，便于测试与真实 HTTP 共用同一条管道。
 * @returns {Promise<{response: object, sends: number, escalations: number, finalLevel: number, traceId: string}>}
 */
export async function requestWithTencentGate(options) {
  const { channel, messages, system, send } = options
  const guarded = TENCENT_CHANNELS.has(channel)
  const traceId = `${channel}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`

  /** 与 pack.js 一致：每档都从**原始输入**重建，不在已改文本上叠加。 */
  const buildMessages = level => {
    const base = messages.map(message => ({ role: message.role, content: message.content }))
    if (typeof system === 'string' && system.length > 0) {
      base.unshift({ role: 'system', content: system })
    }
    return guarded ? scrubForLevel(base, level) : base
  }

  let level = guarded ? tencentBaseGuardLevel() : 0
  let escalations = 0
  let sends = 0
  const startedAt = Date.now()

  const sendOnce = async () => {
    sends += 1
    const body = { model: options.model, messages: buildMessages(level), stream: options.stream === true }
    return send(body)
  }

  let response = await sendOnce()

  // ── HTTP 400 + 11128：升档重发（预算与 SSE 共享，这里只有 HTTP 一条路）
  for (;;) {
    if (!(guarded && !response.ok && response.status === 400)) break
    const probeBody = await response.text().catch(() => '')
    if (!isTencentGateBlock(probeBody)) break
    if (escalations >= TENCENT_GATE_MAX_ESCALATIONS) break
    const next = tencentNextGuardLevel(level)
    if (next === level) break
    level = next
    escalations += 1
    tencentGateStats().escalations += 1
    tencentGateStats().gateBlocks += 1
    tgTrace('escalate', { id: traceId, channel, fromLevel: level - 1, toLevel: level, attempt: escalations, budget: TENCENT_GATE_MAX_ESCALATIONS, sendsSoFar: sends })
    response = await sendOnce()
  }

  // ── 升档耗尽仍被拒：结构化抛错（不靠 error 自定义属性）
  if (guarded && !response.ok && response.status === 400) {
    const gateBody = await response.text().catch(() => '')
    if (isTencentGateBlock(gateBody)) {
      tgTrace('result', { id: traceId, result: 'blocked', sends, escalations, finalLevel: level, ms: Date.now() - startedAt })
      throw markTencentGateError(
        new ChannelGateError(
          `${channel}: 上游仍以渠道校验拒绝（code 11128），已升档 ${escalations} 次仍未通过。` +
          '请在 Channel Pack 里检查账号状态，或把起始档位调到 L3（OFM_TENCENT_GATE_LEVEL=3）。',
          'INVALID_REQUEST',
          { status: 400 },
        ),
        gateBody,
      )
    }
  }

  tgTrace('result', {
    id: traceId,
    result: response.ok ? 'ok' : `http-${response.status}`,
    sends,
    escalations,
    finalLevel: level,
    ms: Date.now() - startedAt,
  })

  return { response, sends, escalations, finalLevel: level, traceId }
}

export { isTencentGateBlock, isTencentGateBlockError, TENCENT_GATE_MAX_ESCALATIONS, tencentGateStats }
