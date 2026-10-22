/**
 * Loads the *installed* plugin bundle headlessly and hands back the pieces the
 * parity tests compare against.
 *
 * This is the reference side of the comparison: nothing here is a copy — it is
 * the very `client.js` the DSH desktop profile loads today, evaluated in this
 * process with the globals a browser half expects (a document, a window, real
 * React). The bundle is a plain script that calls
 * `window.__ModuleLoader__.load({ id, factory })`, and the factory returns
 * `module.exports`, whose `__test` export is the seam the plugin's own headless
 * tests use — so this is the plugin's supported entry point, not a hack.
 *
 * The same realm matters: the reference component must mount into the *same*
 * DOM and React as the copy, otherwise a tree diff compares runtimes instead of
 * page behaviour.
 */

import { createRequire } from 'node:module'
import { existsSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import vm from 'node:vm'

export const DEFAULT_CLIENT = join(
  homedir(),
  '.dsh/profiles/desktop/node_modules/dsh-our-free-model/client.js',
)

export function clientPath() {
  return process.env.OFM_CLIENT_PATH ?? DEFAULT_CLIENT
}

/**
 * @param {object} [options]
 * @param {string} [options.path]  client.js to load (defaults to the installed one)
 * @param {object} [options.globals]  extra globals for the bundle
 * @returns {{ id: string, exports: object, test: object, source: string, path: string }}
 */
export function loadClient(options = {}) {
  const path = options.path ?? clientPath()
  if (!existsSync(path)) throw new Error(`client.js not found: ${path}`)
  const source = readFileSync(path, 'utf8')
  const requireFromHere = createRequire(import.meta.url)

  const previousLoader = globalThis.window?.__ModuleLoader__
  let loaded = null
  if (globalThis.window === undefined) throw new Error('loadClient needs the browser globals installed first (see src/test/env.mjs)')
  globalThis.window.__ModuleLoader__ = {
    load(definition) {
      const module = { exports: {} }
      module.exports = definition.factory(requireFromHere)
      loaded = { id: definition.id, exports: module.exports }
    },
  }
  try {
    vm.runInThisContext(source, { filename: path })
  } finally {
    if (previousLoader === undefined) delete globalThis.window.__ModuleLoader__
    else globalThis.window.__ModuleLoader__ = previousLoader
  }
  if (loaded === null) throw new Error('client.js did not call window.__ModuleLoader__.load')
  const test = loaded.exports.__test ?? {}
  return { ...loaded, test, source, path }
}
