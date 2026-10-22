/**
 * Assembles everything the copied page needs, in one place, so the app, the
 * tests and the parity harness all mount it with identical wiring.
 *
 * The wiring mirrors the plugin's own module scope:
 *   - React + its six hooks, and the `h` shorthand the page is written in;
 *   - the plugin's shared primitives (`Pill`, `Switch`, `showToast`, `kilo`,
 *     `ago`) — imported from the extracted copy of *that* code, not re-invented;
 *   - `post`, the plugin's JSON API helper, kept for the `/open-external` call
 *     the login flow makes on hosts that cannot open a popup;
 *   - the dictionary: the plugin's 69 `chan.*` strings, copied out of its zh/en
 *     DICT, with a `t()` binder that does the same `{placeholder}` substitution
 *     and falls back to the key like the shell's locale service.
 */

import { createChannelsPage, createHelpers, PROVENANCE } from '../extracted/channels-page.js'
import { CHANNEL_DICT } from '../extracted/channels-dict.js'

export { PROVENANCE, CHANNEL_DICT }

/** Binder with the shell's semantics: `{name}` substitution, key as fallback. */
export function createTranslator(dict, locale = 'zh') {
  const table = dict[locale] ?? dict.zh
  const t = (key, vars) => {
    const value = table[key] ?? key
    if (vars === undefined) return value
    return String(value).replace(/\{(\w+)\}/g, (match, name) => vars[name] === undefined ? match : String(vars[name]))
  }
  t.locale = locale
  t.dict = table
  t.keys = () => Object.keys(table)
  return t
}

/**
 * @param {object} options
 * @param {object} options.channelDict  `{ zh: {...}, en: {...} }` — the plugin's chan.* strings
 * @param {Function} options.h  React.createElement
 * @param {Function} options.Fragment
 * @param {string} [options.locale]
 * @param {(path: string, body?: object) => Promise<object>} [options.post]
 */
export function createPage(deps) {
  const {
    React, h, Fragment, dict = CHANNEL_DICT, locale = 'zh', post, timers,
  } = deps
  const t = createTranslator(dict, locale)
  const document = deps.document ?? globalThis.document
  const helpers = createHelpers({ h, document })
  const clock = timers ?? (typeof window === 'undefined' ? {} : window)
  const page = createChannelsPage({
    h,
    Fragment,
    useState: React.useState,
    useEffect: React.useEffect,
    useMemo: React.useMemo,
    useRef: React.useRef,
    useCallback: React.useCallback,
    Pill: helpers.Pill,
    Switch: helpers.Switch,
    showToast: helpers.showToast,
    kilo: helpers.kilo,
    ago: helpers.ago,
    post: post ?? (async () => ({ opened: false })),
    setTimeout: clock.setTimeout ?? globalThis.setTimeout,
    clearTimeout: clock.clearTimeout ?? globalThis.clearTimeout,
    setInterval: clock.setInterval ?? globalThis.setInterval,
    clearInterval: clock.clearInterval ?? globalThis.clearInterval,
  })
  return { ...page, ...helpers, t, dict }
}

/** Renders just the page (what the plugin's settings shell does for the tab). */
export function channelsSection(props) {
  const { ChannelsPage } = props.page
  return props.h(ChannelsPage, { t: props.page.t, ctx: props.ctx, summary: props.summary })
}
