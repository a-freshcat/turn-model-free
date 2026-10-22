/**
 * A very small DOM, just big enough to mount this page outside a browser.
 *
 * Why it exists: the harness ships no DOM library (no jsdom, no linkedom) and
 * this machine has no package registry, so the clone and the reference both
 * render into *this* DOM in tests. Keeping both sides on the same DOM is what
 * makes a tree diff between them meaningful.
 *
 * Supported on purpose, nothing more:
 *   - elements/text nodes, appendChild/insertBefore/removeChild/remove
 *   - attributes, className, style (object + setProperty), classList
 *   - addEventListener/removeEventListener/dispatchEvent with capture -> target
 *     -> bubble propagation, preventDefault/stopPropagation
 *   - querySelector(All) for `tag`, `.class`, `[attr]`, `[attr="v"]`, compounds
 *     (`div.a[data-x="1"]`) and descendant chains (`a b`)
 *   - `document.head/body/documentElement`, createElement/createTextNode
 *
 * Not supported (and not needed by the page): innerHTML, layout, CSS cascade.
 */

const ELEMENT_NODE = 1
const TEXT_NODE = 3
const DOCUMENT_NODE = 9
const FRAGMENT_NODE = 11

class MiniEvent {
  constructor(type, options = {}) {
    this.type = String(type)
    this.bubbles = options.bubbles !== false
    this.cancelable = options.cancelable === true
    this.detail = options.detail
    this.defaultPrevented = false
    this.propagationStopped = false
    this.immediatePropagationStopped = false
    this.target = null
    this.currentTarget = null
    this._path = []
  }
  preventDefault() {
    if (this.cancelable) this.defaultPrevented = true
  }
  stopPropagation() { this.propagationStopped = true }
  stopImmediatePropagation() { this.propagationStopped = true; this.immediatePropagationStopped = true }
}

/**
 * A CSSStyleDeclaration stand-in. React writes `style.minHeight = '44px'` and
 * `style.setProperty('--x', v)`; both land in the same declaration map through
 * a Proxy, so `cssText` always reflects everything that was set.
 */
function createStyle() {
  const props = new Map()
  const target = {
    _props: props,
    setProperty(name, value) { props.set(String(name), String(value)) },
    getPropertyValue(name) { return props.get(String(name)) ?? '' },
    removeProperty(name) { const previous = props.get(String(name)) ?? ''; props.delete(String(name)); return previous },
    get cssText() { return [...props.entries()].map(([key, value]) => `${key}:${value}`).join(';') },
    set cssText(value) {
      props.clear()
      for (const decl of String(value ?? '').split(';')) {
        const index = decl.indexOf(':')
        if (index > 0) props.set(decl.slice(0, index).trim(), decl.slice(index + 1).trim())
      }
    },
    get length() { return props.size },
    item(index) { return [...props.keys()][index] ?? '' },
  }
  return new Proxy(target, {
    set(object, key, value) {
      if (typeof key !== 'string' || key in object || key.startsWith('_')) { object[key] = value; return true }
      const name = key.startsWith('--') ? key : key.replace(/[A-Z]/g, match => '-' + match.toLowerCase())
      if (value === null || value === undefined || value === '') props.delete(name)
      else props.set(name, String(value))
      return true
    },
    get(object, key) {
      if (typeof key !== 'string' || key in object || key.startsWith('_')) return object[key]
      if (key.startsWith('--')) return props.get(key) ?? ''
      const name = key.replace(/[A-Z]/g, match => '-' + match.toLowerCase())
      return props.get(name) ?? ''
    },
  })
}

class MiniClassList {
  constructor(owner) { this._owner = owner }
  _list() { return String(this._owner.getAttribute('class') ?? '').split(/\s+/).filter(Boolean) }
  _write(list) { this._owner.setAttribute('class', list.join(' ')) }
  add(...names) { const list = this._list(); for (const name of names) if (!list.includes(name)) list.push(name); this._write(list) }
  remove(...names) { this._write(this._list().filter(name => !names.includes(name))) }
  contains(name) { return this._list().includes(name) }
  toggle(name, force) {
    const has = this.contains(name)
    const next = force === undefined ? !has : force === true
    if (next && !has) this.add(name)
    if (!next && has) this.remove(name)
    return next
  }
  get value() { return this._list().join(' ') }
}

class MiniNode {
  constructor(nodeType, nodeName, ownerDocument) {
    this.nodeType = nodeType
    this.nodeName = nodeName
    this.ownerDocument = ownerDocument ?? null
    this.childNodes = []
    this.parentNode = null
    this._listeners = new Map()
    this._attrs = new Map()
    this._className = ''
    this.style = createStyle()
    this.classList = new MiniClassList(this)
    this.namespaceURI = 'http://www.w3.org/1999/xhtml'
  }

  /* DOM reflects `className` onto the `class` attribute; React reads it back. */
  get className() { return this._className }
  set className(value) { this._className = String(value ?? ''); this._attrs.set('class', this._className) }

  /* GlobalEventHandlers: React DOM assigns these while looking for a
   * `defaultPrevented` policy on the error handler, and reads `window.event`. */
  get onerror() { return this._onerror }
  set onerror(value) { this._onerror = value }
  get onclick() { return this._onclick }
  set onclick(value) { this._onclick = value }
  get nodeValue() { return null }
  set nodeValue(_value) { /* elements have no value; text nodes override this */ }
  get isConnected() {
    for (let node = this; node !== null; node = node.parentNode) {
      if (node.nodeType === DOCUMENT_NODE) return true
    }
    return false
  }
  getAttributeNS(_namespace, name) { return this.getAttribute(name) }
  setAttributeNS(_namespace, name, value) { this.setAttribute(name, value) }
  removeAttributeNS(_namespace, name) { this.removeAttribute(name) }
  hasAttributeNS(_namespace, name) { return this.hasAttribute(name) }
  getElementsByTagName(tag) { return this.querySelectorAll(tag) }
  getElementsByClassName(name) { return this.querySelectorAll('.' + name) }
  getRootNode() {
    let node = this
    while (node.parentNode !== null) node = node.parentNode
    return node
  }
  get children() { return this.childNodes.filter(node => node.nodeType === ELEMENT_NODE) }
  get firstChild() { return this.childNodes[0] ?? null }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] ?? null }
  get firstElementChild() { return this.children[0] ?? null }
  get nextSibling() {
    const siblings = this.parentNode?.childNodes ?? []
    const index = siblings.indexOf(this)
    return index === -1 ? null : siblings[index + 1] ?? null
  }
  appendChild(child) {
    if (child.nodeType === FRAGMENT_NODE) { for (const node of [...child.childNodes]) this.appendChild(node); child.childNodes = []; return child }
    if (child.parentNode) child.parentNode.removeChild(child)
    child.parentNode = this
    this.childNodes.push(child)
    return child
  }
  insertBefore(child, reference) {
    if (reference === null || reference === undefined) return this.appendChild(child)
    if (child.parentNode) child.parentNode.removeChild(child)
    const index = this.childNodes.indexOf(reference)
    if (index === -1) return this.appendChild(child)
    child.parentNode = this
    this.childNodes.splice(index, 0, child)
    return child
  }
  removeChild(child) {
    const index = this.childNodes.indexOf(child)
    if (index !== -1) { this.childNodes.splice(index, 1); child.parentNode = null }
    return child
  }
  replaceChild(next, previous) { this.insertBefore(next, previous); this.removeChild(previous); return previous }
  remove() { this.parentNode?.removeChild(this) }
  get textContent() {
    if (this.nodeType === TEXT_NODE) return this._text ?? ''
    return this.childNodes.map(node => node.textContent).join('')
  }
  set textContent(value) {
    if (this.nodeType === TEXT_NODE) { const parent = this.parentNode; this._text = String(value ?? ''); if (parent) parent._replaceText(this, this._text); return }
    for (const node of this.childNodes) node.parentNode = null
    this.childNodes = []
    if (value !== undefined && value !== null && String(value) !== '') this.appendChild(this.ownerDocument.createTextNode(String(value)))
  }
  _replaceText(node, text) { node._text = text }
  setAttribute(name, value) {
    const key = String(name).toLowerCase()
    if (key === 'class') { this.className = String(value); return }
    if (key === 'style') { this._attrs.set('style', String(value)); return }
    this._attrs.set(key, String(value))
    if (key.startsWith('data-')) this.dataset[dataKey(key)] = String(value)
  }
  getAttribute(name) {
    const key = String(name).toLowerCase()
    if (key === 'class') return this._attrs.get('class') ?? null
    return this._attrs.has(key) ? this._attrs.get(key) : null
  }
  hasAttribute(name) { return this._attrs.has(String(name).toLowerCase()) }
  /** DOMTokenList-style toggle for a plain attribute (`toggleAttribute('x', force)`). */
  toggleAttribute(name, force) {
    const key = String(name).toLowerCase()
    const has = this.hasAttribute(key)
    const next = force === undefined ? !has : force === true
    if (next && !has) this.setAttribute(key, '')
    if (!next && has) this.removeAttribute(key)
    return next
  }
  removeAttribute(name) {
    const key = String(name).toLowerCase()
    if (key === 'class') { this._className = ''; this._attrs.delete('class'); return }
    this._attrs.delete(key)
    if (key.startsWith('data-')) delete this.dataset[dataKey(key)]
  }
  get id() { return this.getAttribute('id') ?? '' }
  set id(value) { this.setAttribute('id', value) }
  get dataset() { if (this._dataset === undefined) this._dataset = {}; return this._dataset }
  get src() { return this.getAttribute('src') ?? '' }
  set src(value) { this.setAttribute('src', value) }
  get srcSet() { return this.getAttribute('srcset') ?? '' }
  set srcSet(value) { this.setAttribute('srcset', value) }
  get href() { return this.getAttribute('href') ?? '' }
  set href(value) { this.setAttribute('href', value) }
  get title() { return this.getAttribute('title') ?? '' }
  set title(value) { this.setAttribute('title', value) }
  get name() { return this.getAttribute('name') ?? '' }
  set name(value) { this.setAttribute('name', value) }
  get type() { return this.getAttribute('type') ?? '' }
  set type(value) { this.setAttribute('type', value) }
  get value() { return this.getAttribute('value') ?? this._value ?? '' }
  set value(next) { this._value = next; this.setAttribute('value', next) }
  get checked() { return this._checked === true }
  set checked(next) { this._checked = next === true }
  get disabled() { return this.hasAttribute('disabled') }
  set disabled(next) { if (next === true || next === '') this.setAttribute('disabled', ''); else this.removeAttribute('disabled') }
  get multiple() { return this.hasAttribute('multiple') }
  set multiple(next) { if (next === true || next === '') this.setAttribute('multiple', ''); else this.removeAttribute('multiple') }
  get selected() { return this.hasAttribute('selected') }
  set selected(next) { if (next === true || next === '') this.setAttribute('selected', ''); else this.removeAttribute('selected') }
  get rel() { return this.getAttribute('rel') ?? '' }
  set rel(value) { this.setAttribute('rel', value) }
  get target() { return this.getAttribute('target') ?? '' }
  set target(value) { this.setAttribute('target', value) }
  get tabIndex() { return Number(this.getAttribute('tabindex') ?? -1) }
  set tabIndex(value) { this.setAttribute('tabindex', String(value)) }
  addEventListener(type, listener, options) {
    if (typeof listener !== 'function') return
    const capture = options === true || (typeof options === 'object' && options !== null && options.capture === true)
    const list = this._listeners.get(String(type)) ?? []
    list.push({ listener, capture, once: typeof options === 'object' && options?.once === true })
    this._listeners.set(String(type), list)
  }
  removeEventListener(type, listener, options) {
    const capture = options === true || (typeof options === 'object' && options !== null && options.capture === true)
    const list = this._listeners.get(String(type))
    if (list === undefined) return
    this._listeners.set(String(type), list.filter(entry => !(entry.listener === listener && entry.capture === capture)))
  }
  dispatchEvent(event) {
    const path = []
    for (let node = this; node !== null; node = node.parentNode) path.push(node)
    // document is the root of every path
    event.target = this
    event._path = path
    const run = (node, phase) => {
      if (event.immediatePropagationStopped) return
      const list = (node._listeners?.get(event.type) ?? []).filter(entry => phase === 'capture' ? entry.capture : true)
      for (const entry of [...list]) {
        if (event.immediatePropagationStopped) break
        if (phase !== 'capture' && entry.capture && node !== this) continue
        event.currentTarget = node
        entry.listener.call(node, event)
        if (entry.once) node.removeEventListener(event.type, entry.listener, entry.capture)
      }
    }
    if (event.bubbles) for (let i = path.length - 1; i > 0; i -= 1) run(path[i], 'capture')
    run(this, 'target')
    if (event.bubbles && !event.propagationStopped) for (let i = 1; i < path.length; i += 1) run(path[i], 'bubble')
    return !event.defaultPrevented
  }
  closest(selector) {
    for (let node = this; node !== null && node.nodeType === ELEMENT_NODE; node = node.parentNode) {
      if (matches(node, parseSelector(selector))) return node
    }
    return null
  }
  querySelector(selector) { return querySelectorAll(this, selector)[0] ?? null }
  querySelectorAll(selector) { return querySelectorAll(this, selector) }
  contains(node) {
    for (let current = node; current !== null; current = current.parentNode) if (current === this) return true
    return false
  }
  get offsetLeft() { return 0 }
  get offsetTop() { return 0 }
  get offsetWidth() { return 0 }
  get offsetHeight() { return 0 }
  focus() { this.ownerDocument?.defaultView?.document && (this.ownerDocument.activeElement = this) }
  click() { this.dispatchEvent(new MiniEvent('click', { bubbles: true, cancelable: true })) }
}

class MiniText extends MiniNode {
  constructor(text, ownerDocument) { super(TEXT_NODE, '#text', ownerDocument); this._text = String(text ?? '') }
  get nodeValue() { return this._text }
  set nodeValue(next) { this._text = String(next) }
  get data() { return this._text }
  set data(next) { this._text = String(next) }
}

class MiniElement extends MiniNode {
  constructor(tagName, ownerDocument) {
    super(ELEMENT_NODE, String(tagName).toUpperCase(), ownerDocument)
    this.tagName = this.nodeName
    this.localName = String(tagName).toLowerCase()
  }
}

class MiniFragment extends MiniNode {
  constructor(ownerDocument) { super(FRAGMENT_NODE, '#fragment', ownerDocument) }
}

const dataKey = key => String(key).slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())

class MiniDocument extends MiniNode {
  constructor() {
    super(DOCUMENT_NODE, '#document', null)
    this.ownerDocument = this
    this.documentElement = this.createElement('html')
    this.head = this.createElement('head')
    this.body = this.createElement('body')
    this.documentElement.appendChild(this.head)
    this.documentElement.appendChild(this.body)
    this.appendChild(this.documentElement)
    this.defaultView = { document: this }
    this.activeElement = null
  }
  createElement(tag) {
    if (String(tag).toLowerCase() === 'template') return new MiniElement('template', this)
    return new MiniElement(tag, this)
  }
  createElementNS(_ns, tag) { return this.createElement(tag) }
  createTextNode(text) { return new MiniText(text, this) }
  createDocumentFragment() { return new MiniFragment(this) }
  createEvent() { return new MiniEvent('event') }
  getElementById(id) { return this.querySelector(`#${id}`) }
  execCommand() { return false }
}

/* ── selectors ──────────────────────────────────────────────────────────── */

function parseSelector(selector) {
  return String(selector).trim().split(/\s+/).map(part => {
    // `div.a[b="c"]:hover` -> tag=div, classes=[a], attrs=[[b,c]]
    const tag = /^[a-zA-Z][\w-]*/.exec(part)?.[0]?.toLowerCase() ?? null
    const classes = [...part.matchAll(/\.([\w-]+)/g)].map(match => match[1])
    const attrs = [...part.matchAll(/\[([\w-]+)(?:\s*=\s*"([^"]*)"|\s*=\s*'([^']*)')?\]/g)]
      .map(match => [match[1].toLowerCase(), match[2] ?? match[3] ?? null])
    const id = /#([\w-]+)/.exec(part)?.[1] ?? null
    return { tag, classes, attrs, id }
  })
}

function matches(node, parts) {
  if (node.nodeType !== ELEMENT_NODE) return false
  const simple = parts[parts.length - 1]
  if (simple.tag !== null && node.tagName.toLowerCase() !== simple.tag) return false
  if (simple.id !== null && node.getAttribute('id') !== simple.id) return false
  for (const name of simple.classes) if (!node.classList.contains(name)) return false
  for (const [name, value] of simple.attrs) {
    const actual = node.getAttribute(name)
    if (actual === null) return false
    if (value !== null && actual !== value) return false
  }
  return true
}

function querySelectorAll(root, selector) {
  const groups = String(selector).split(',').map(parseSelector)
  const found = new Set()
  const walk = node => {
    for (const child of node.childNodes) {
      if (child.nodeType === ELEMENT_NODE) {
        for (const parts of groups) {
          if (matches(child, parts)) {
            // verify the ancestor chain for descendant selectors
            let cursor = child.parentNode
            let index = parts.length - 2
            while (index >= 0 && cursor !== null) {
              if (matches(cursor, [parts[index]])) index -= 1
              cursor = cursor.parentNode
            }
            if (index < 0) found.add(child)
          }
        }
        walk(child)
      }
    }
  }
  walk(root)
  return [...found]
}

/* ── public API ─────────────────────────────────────────────────────────── */

export function createDocument() { return new MiniDocument() }

/**
 * Installs `document`/`window`/`navigator`/timers as globals on `target`
 * (usually `globalThis`, or a `vm` sandbox). Returns a teardown function.
 */
export function installGlobals(target, options = {}) {
  const doc = options.document ?? createDocument()
  const storage = new Map()
  const localStorage = {
    getItem: key => storage.has(String(key)) ? storage.get(String(key)) : null,
    setItem: (key, value) => { storage.set(String(key), String(value)) },
    removeItem: key => { storage.delete(String(key)) },
    clear: () => storage.clear(),
    get length() { return storage.size },
    key: index => [...storage.keys()][index] ?? null,
  }
  const listeners = new Map()
  const windowObject = {
    document: doc,
    localStorage,
    navigator: { clipboard: undefined, userAgent: 'mini-dom' },
    location: { href: 'http://localhost/', origin: 'http://localhost', protocol: 'http:', pathname: '/' },
    setTimeout: (...args) => globalThis.setTimeout(...args),
    clearTimeout: (...args) => globalThis.clearTimeout(...args),
    setInterval: (...args) => globalThis.setInterval(...args),
    clearInterval: (...args) => globalThis.clearInterval(...args),
    addEventListener: (type, listener) => {
      const list = listeners.get(type) ?? []
      list.push(listener)
      listeners.set(type, list)
    },
    removeEventListener: (type, listener) => listeners.set(type, (listeners.get(type) ?? []).filter(entry => entry !== listener)),
    dispatchEvent: event => {
      for (const listener of listeners.get(event.type) ?? []) listener.call(windowObject, event)
      return !event.defaultPrevented
    },
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    open: () => null,
    confirm: () => true,
    alert: () => {},
    Notification: undefined,
    EventSource: undefined,
    fetch: (...args) => globalThis.fetch(...args),
    URL: globalThis.URL,
    CustomEvent: class MiniCustomEvent extends MiniEvent { constructor(type, options = {}) { super(type, options); this.detail = options.detail } },
    Event: MiniEvent,
    MiniEvent,
  }
  windowObject.window = windowObject
  windowObject.self = windowObject
  windowObject.globalThis = target
  doc.defaultView = windowObject
  // React DOM reads these constructors off `document.defaultView` (and every
  // document defaults to globalThis) for its `instanceof` guards.
  for (const name of ['Node', 'Element', 'HTMLElement', 'Text', 'Comment', 'Document', 'DocumentFragment', 'HTMLIFrameElement', 'HTMLUnknownElement', 'SVGElement']) {
    windowObject[name] = MiniNode
  }
  windowObject.Node = MiniNode
  windowObject.Element = MiniNode

  const keys = ['window', 'document', 'navigator', 'localStorage', 'Event', 'CustomEvent', 'Node',
    'Element', 'HTMLElement', 'Text', 'Comment', 'Document', 'DocumentFragment',
    'HTMLIFrameElement', 'HTMLUnknownElement', 'SVGElement']
  const previous = new Map()
  const define = (key, value) => {
    // `navigator` is a getter-only accessor on recent Node globals; define over it.
    Object.defineProperty(target, key, { value, configurable: true, writable: true, enumerable: true })
  }
  for (const key of keys) if (key in target) previous.set(key, { value: target[key], descriptor: Object.getOwnPropertyDescriptor(target, key) })
  define('window', windowObject)
  define('document', doc)
  define('navigator', windowObject.navigator)
  define('localStorage', localStorage)
  define('CustomEvent', windowObject.CustomEvent)
  define('Event', MiniEvent)
  define('Node', MiniNode)
  define('Element', MiniNode)
  define('HTMLElement', MiniNode)
  define('Text', MiniText)
  define('Comment', MiniNode)
  define('Document', MiniDocument)
  define('DocumentFragment', MiniFragment)
  define('HTMLIFrameElement', MiniNode)
  define('HTMLUnknownElement', MiniNode)
  define('SVGElement', MiniNode)

  return () => {
    for (const key of keys) {
      const saved = previous.get(key)
      if (saved === undefined) { delete target[key]; continue }
      if (saved.descriptor?.get !== undefined) Object.defineProperty(target, key, saved.descriptor)
      else define(key, saved.value)
    }
  }
}

export { MiniEvent, MiniNode, MiniElement, MiniDocument, MiniText, ELEMENT_NODE, TEXT_NODE, DOCUMENT_NODE, FRAGMENT_NODE }
