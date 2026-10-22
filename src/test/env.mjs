/**
 * A browser-ish environment for mounting the page headlessly.
 *
 * One module owns the globals so the two implementations under comparison can
 * never end up on *different* DOM implementations — a tree diff across two
 * different DOMs would prove nothing.
 */

import { createRequire } from 'node:module'
import { installGlobals } from '../runtime/mini-dom.mjs'

const require = createRequire(import.meta.url)

export function createEnvironment(options = {}) {
  const restore = installGlobals(globalThis, options)
  const React = require('react')
  const ReactDOMClient = require('react-dom/client')
  const ReactDOM = require('react-dom')
  const server = require('react-dom/server')
  const document = globalThis.document
  const window = globalThis.window

  const container = document.createElement('div')
  document.body.appendChild(container)

  /**
   * Virtual timers.
   *
   * The page polls (login.poll every 3s, balances every 90s) and the tests must
   * not wait in real time. Everything the page schedules through
   * `window.setTimeout` / `window.setInterval` lands here instead, and
   * `advance(ms)` runs whatever is due. `settle()` keeps working for promise
   * microtasks, which are not timers.
   */
  const timers = new Map()
  let timerId = 0
  /** Virtual clock: only this collection moves it, so `advance` is deterministic. */
  let clock = 0
  const REAL_TIMERS = {
    setTimeout: globalThis.setTimeout.bind(globalThis),
    clearTimeout: globalThis.clearTimeout.bind(globalThis),
    setInterval: globalThis.setInterval.bind(globalThis),
    clearInterval: globalThis.clearInterval.bind(globalThis),
  }
  const schedule = (kind, handler, delay, args) => {
    const id = ++timerId
    const wait = Math.max(0, Number(delay) || 0)
    timers.set(id, { kind, handler, delay: wait, due: clock + wait, args: args ?? [], seq: id })
    return id
  }
  const cancel = id => { timers.delete(id) }
  // Both halves run in this realm, and the reference bundle's timers resolve to
  // the globals — so the virtual clock has to be installed on globalThis too,
  // not only on `window`, or one side would poll in real time.
  const virtualTimers = {
    setTimeout: (handler, delay, ...args) => schedule('timeout', handler, delay, args),
    clearTimeout: id => cancel(id),
    setInterval: (handler, delay, ...args) => schedule('interval', handler, delay, args),
    clearInterval: id => cancel(id),
  }
  window.setTimeout = virtualTimers.setTimeout
  window.clearTimeout = virtualTimers.clearTimeout
  window.setInterval = virtualTimers.setInterval
  window.clearInterval = virtualTimers.clearInterval
  window.requestAnimationFrame = handler => schedule('timeout', () => handler(clock), 0)
  const savedGlobals = { ...REAL_TIMERS }
  for (const [name, fn] of Object.entries(virtualTimers)) globalThis[name] = fn

  const runTimer = entry => {
    if (entry.kind === 'timeout') timers.delete(entry.id)
    else entry.due = clock + entry.delay
    if (typeof entry.handler === 'function') entry.handler(...entry.args)
  }
  const pendingWith = predicate => [...timers.entries()].filter(([, entry]) => predicate(entry))

  let root = null

  return {
    React,
    ReactDOM,
    server,
    document,
    window,
    container,
    h: React.createElement,
    Fragment: React.Fragment,
    timers,
    /** Mounts an element and returns once React has flushed. */
    mount(element) {
      root = ReactDOMClient.createRoot(container)
      ReactDOM.flushSync(() => root.render(element))
      return container
    },
    /** Mounts into a caller-owned container (side-by-side comparisons). */
    mountInto(target, element) {
      const local = ReactDOMClient.createRoot(target)
      ReactDOM.flushSync(() => local.render(element))
      return local
    },
    /** Runs an arbitrary React update synchronously. */
    act(fn) {
      let result
      ReactDOM.flushSync(() => { result = fn() })
      return result
    },
    /** Lets queued promise callbacks and 0ms timers run. */
    async settle(rounds = 6) {
      for (let index = 0; index < rounds; index += 1) await new Promise(resolve => REAL_TIMERS.setTimeout(resolve, 0))
    },
    /**
     * Advances virtual time by `ms`, firing every timer that comes due (and the
     * promise work each one queues) in order.
     */
    async advance(ms, step = 250) {
      const end = clock + ms
      while (clock < end) {
        const next = pendingWith(entry => entry.due <= end)
          .sort((left, right) => left[1].due - right[1].due || left[1].seq - right[1].seq)[0]
        if (next === undefined) { clock = end; break }
        clock = Math.max(clock, next[1].due)
        next[1].id = next[0]
        if (timers.has(next[0])) runTimer(next[1])
        await this.settle(2)
        if (step !== undefined && clock >= end) break
      }
      clock = Math.max(clock, end)
      await this.settle(4)
    },
    /** Current virtual time (ms since the environment was created). */
    now() { return clock },
    /** Number of timers still scheduled (a leak check). */
    timerCount() { return timers.size },
    /** Timers whose delay matches, e.g. `timerCountFor(3000)`. */
    timerCountFor(delay) { return pendingWith(entry => entry.delay === delay).length },
    unmount() {
      if (root !== null) root.unmount()
      root = null
      container.remove()
      timers.clear()
    },
    restore() {
      for (const [name, fn] of Object.entries(savedGlobals)) globalThis[name] = fn
      restore()
    },
  }
}

/** Serializes a mini-DOM tree into a stable, comparable string. */
export function serialize(node, options = {}) {
  const skipStyle = options.skipStyle === true
  const lines = []
  const walk = (current, depth) => {
    const pad = '  '.repeat(depth)
    if (current.nodeType === 3) {
      const text = current.textContent
      if (text.trim() !== '') lines.push(`${pad}#text ${JSON.stringify(text)}`)
      return
    }
    if (current.nodeType !== 1) {
      for (const child of current.childNodes) walk(child, depth)
      return
    }
    const attrs = [...current._attrs.entries()]
      .filter(([key]) => key !== 'style' || !skipStyle)
      .map(([key, value]) => `${key}=${JSON.stringify(value.length > 120 ? value.slice(0, 120) + '…' : value)}`)
      .sort()
    lines.push(`${pad}<${current.localName}${attrs.length === 0 ? '' : ' ' + attrs.join(' ')}>`)
    for (const child of current.childNodes) walk(child, depth + 1)
  }
  walk(node, 0)
  return lines.join('\n')
}

/** Collects every element under `node` (document order). */
export function elements(node) {
  const out = []
  const walk = current => {
    for (const child of current.childNodes) {
      if (child.nodeType === 1) { out.push(child); walk(child) }
    }
  }
  walk(node)
  return out
}

/** Text of the first element whose class list contains `className`. */
export function textOf(node, className) {
  const found = elements(node).find(element => element.classList.contains(className))
  return found === undefined ? undefined : found.textContent.trim()
}

/** All matches for `.className`, in document order. */
export function allByClass(node, className) {
  return elements(node).filter(element => element.classList.contains(className))
}

/** Clicks the first button whose text matches `label` (trimmed equality or substring). */
export function clickButton(node, label) {
  const button = elements(node).find(element => element.tagName === 'BUTTON'
    && (label === undefined || element.textContent.trim() === label || element.textContent.includes(label)))
  if (button === undefined) throw new Error(`no button matching ${JSON.stringify(label)}`)
  button.dispatchEvent(new window.Event('click', { bubbles: true, cancelable: true }))
  return button
}
