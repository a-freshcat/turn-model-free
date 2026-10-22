/**
 * Smoke test for the deliverable as shipped: `dist/demo.js`, not the sources.
 *
 * The bundle is the classic script the browser loads, so this evaluates that
 * exact file, builds the DOM `dist/index.html` provides, calls the same entry
 * point the page calls, and then exercises the demo's controls. It catches what
 * the source tests cannot: a broken ESM→CJS rewrite, a module missing from the
 * registry, a control that throws on mount, or a bundle that stopped publishing
 * what `index.html` expects.
 *
 * A real headless browser is not available here — Chrome/Edge's mojo channel is
 * blocked by the sandbox — so `--dump-dom` cannot be used. This executes the
 * shipped artifact end to end instead of proxying for it.
 */

import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'
import { installGlobals } from '../runtime/mini-dom.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..', '..')

/** The element ids `dist/index.html` ships with. */
function createDemoDocument(document) {
  document.body.childNodes = []
  for (const id of ['page-root', 'controls', 'rpc-log', 'build-info']) {
    const element = document.createElement('div')
    element.setAttribute('id', id)
    document.body.appendChild(element)
  }
  return document
}

export function bootDemo(options = {}) {
  const restore = installGlobals(globalThis)
  const document = createDemoDocument(globalThis.document)
  globalThis.window.location.hash = options.hash ?? ''
  const source = readFileSync(join(ROOT, 'dist', 'demo.js'), 'utf8')
  vm.runInThisContext(source, { filename: 'dist/demo.js' })
  const demo = globalThis.window.__OFM_DEMO__
  if (demo === undefined) throw new Error('demo bundle did not publish window.__OFM_DEMO__')
  return { demo, document, window: globalThis.window, React: demo.React, ReactDOM: demo.ReactDOM, restore }
}
