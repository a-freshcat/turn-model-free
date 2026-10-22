/**
 * The headless harness used by every test: it mounts the *reference* page (the
 * installed plugin's own `ChannelsPage`) and the *copy* (`src/extracted`) next
 * to each other, on the same mock backend, in the same DOM, in the same process.
 *
 * Two things make the comparison meaningful:
 *   - the reference is loaded from the installed `client.js`, not from this
 *     repository, so a copy that drifted would show up as a tree diff;
 *   - both sides get their own container *and* their own mock pack, so RPC call
 *     logs can be compared per side without one side's refresh waking the other.
 */

import { createEnvironment, serialize } from './env.mjs'
import { createMockPack } from '../mock/pack.mjs'
import { createPage } from '../app/page.mjs'

/** Console noise React prints for a data: URI image in a non-browser DOM. */
export function silenceReactWarnings() {
  const original = console.error
  const originalWarn = console.warn
  const isNoise = args => args.some(value => typeof value === 'string'
    && (value.includes('error occurred in the <img>') || value.includes('error-boundaries')
      || value.includes('An error occurred in the')))
  console.error = (...args) => { if (!isNoise(args)) original(...args) }
  console.warn = (...args) => { if (!isNoise(args)) originalWarn(...args) }
  return () => { console.error = original; console.warn = originalWarn }
}

/**
 * @param {object} [options]
 * @param {boolean} [options.withReference]  load the installed plugin (default true)
 * @param {object}  [options.mock]  forwarded to createMockPack per side
 */
export async function createHarness(options = {}) {
  silenceReactWarnings()
  const env = createEnvironment()

  let reference = null
  if (options.withReference !== false) {
    const module = await import('../host/reference.mjs')
    reference = module.loadClient()
  }

  const sides = {}
  for (const side of ['reference', 'copy']) {
    const pack = createMockPack(options.mock ?? {})
    const container = env.document.createElement('div')
    env.document.body.appendChild(container)
    let page = null
    let root = null
    if (side === 'reference') {
      if (reference === null) continue
      const dict = reference.test.DICT
      page = {
        ChannelsPage: reference.test.ChannelsPage,
        t: Object.assign(key => dict.zh[key] ?? key, { locale: 'zh', dict: dict.zh }),
        dict,
      }
    } else {
      page = createPage({ React: env.React, h: env.h, Fragment: env.Fragment, timers: env.window })
    }
    sides[side] = {
      side,
      pack,
      container,
      page,
      /** Mounts the page with the pack's RPC on the context. */
      mount(extra = {}) {
        root = env.mountInto(container, env.h(page.ChannelsPage, {
          t: page.t,
          ctx: extra.ctx ?? { connection: pack.connection },
          summary: extra.summary ?? { channels: { state: 'ready', error: '' } },
        }))
        return container
      },
      html() { return serialize(container, options.serialize) },
      unmount() { root?.unmount(); container.remove() },
    }
  }

  return {
    env,
    sides,
    reference,
    async settle(rounds = 12) { await env.settle(rounds) },
    /** Advances virtual time on both sides (poll timers, login loops). */
    async advance(ms, step = 250) { await env.advance(ms, step) },
    /** Mounts both sides and waits for their first RPC round to land. */
    async mountBoth(extra = {}) {
      for (const side of Object.values(sides)) side.mount(extra)
      await env.settle(14)
      return sides
    },
    teardown() {
      for (const side of Object.values(sides)) side.unmount()
      env.unmount()
      env.restore()
    },
  }
}

/** Line-by-line diff; returns the first `limit` differing lines. */
export function diffTrees(left, right, limit = 12) {
  if (left === right) return []
  const a = left.split('\n')
  const b = right.split('\n')
  const out = []
  for (let index = 0; index < Math.max(a.length, b.length) && out.length < limit; index += 1) {
    if (a[index] !== b[index]) out.push({ line: index + 1, reference: a[index] ?? '<missing>', copy: b[index] ?? '<missing>' })
  }
  return out
}
