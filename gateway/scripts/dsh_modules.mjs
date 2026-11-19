// 定位宿主 DSH 的 node_modules（`@deepseek-ai/*` 在这里）。
//
// pack.js 用裸包名 import 这些包，但它们只随 DSH 应用本体安装，不在用户目录的
// 插件树里。bridge 与 loader 都要用它，逻辑只能有一份——两处各写一份的结果是
// 「bridge 找得到、loader 找不到」，实测已经踩过一次。
import { createRequire } from 'node:module'
import { execFileSync } from 'node:child_process'
import { pathToFileURL } from 'node:url'
import { homedir } from 'node:os'
import path from 'node:path'
import fs from 'node:fs'

const PROBE = path.join('@deepseek-ai', 'cordis')

function has(dir) {
  try { return fs.existsSync(path.join(dir, PROBE)) } catch { return false }
}

function registryLocations() {
  if (process.platform !== 'win32') return []
  // `reg query /f X /d` 的 /d 只搜「数据」，且不理解反斜杠路径，实测返回空。
  // 改成全量 dump 再按「同一个键块里既有 DeepSeek Harness 又有 InstallLocation」解析。
  try {
    const out = execFileSync('reg', ['query',
      'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall', '/s'],
      { encoding: 'utf8', timeout: 8000, windowsHide: true })
    const found = []
    for (const block of out.split(/\r?\n\r?\n/)) {
      if (!/DeepSeek\s+Harness/i.test(block)) continue
      const location = block.match(/InstallLocation\s+REG_\w+\s+(.+)/)?.[1].trim()
      const icon = block.match(/DisplayIcon\s+REG_\w+\s+"?([^"\r\n]+?\.exe)(?:"|,|\s*$)/im)?.[1]
      if (location) found.push(location)
      if (icon && path.isAbsolute(icon)) found.push(path.dirname(icon))
    }
    return found
  } catch {
    return []
  }
}

const RELATIVE_LAYOUTS = [
  ['resources', 'app', 'dsh', 'node_modules'],  // Electron 打包布局
  ['resources', 'app', 'node_modules'],
  ['node_modules'],                              // 未打包的安装
]

function installationLocations() {
  const roots = [
    process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, 'Programs'),
    process.env.ProgramFiles,
    process.env['ProgramFiles(x86)'],
    '/usr/lib', '/usr/local/lib', '/Applications',
  ].filter(Boolean)
  const names = ['dsh', 'DSH', 'DeepSeek Harness', 'deepseek-harness']
  return [...roots.flatMap(root => names.map(name => path.join(root, name))),
    ...registryLocations()]
}

export function findDshElectron() {
  if (process.platform !== 'win32' || process.env.DSH_NODE_MODULES) return ''
  for (const location of installationLocations()) {
    const executable = path.join(location, 'DeepSeek Harness.exe')
    if (fs.existsSync(executable) && fs.existsSync(path.join(location, 'resources', 'app.asar'))) {
      return executable
    }
  }
  return ''
}

let detectedModules

export function findDshModules() {
  if (process.env.DSH_NODE_MODULES) return process.env.DSH_NODE_MODULES
  if (detectedModules !== undefined) return detectedModules

  const candidates = []
  if (process.versions.electron) {
    const resources = path.join(path.dirname(process.execPath), 'resources')
    candidates.push(path.join(resources, 'app.asar', 'dsh', 'node_modules'))
    candidates.push(path.join(resources, 'app.asar', 'node_modules'))
  }
  for (const location of installationLocations()) {
    for (const rel of RELATIVE_LAYOUTS) candidates.push(path.join(location, ...rel))
  }

  // 2. 从插件目录往上逐级找
  let cursor = process.env.DSH_PACK_HOME
    || path.join(homedir(), '.dsh', 'profiles', 'desktop', 'node_modules', 'dsh-our-free-model')
  for (let i = 0; i < 6; i += 1) {
    cursor = path.dirname(cursor)
    candidates.push(path.join(cursor, 'node_modules'))
    candidates.push(cursor)
  }

  detectedModules = candidates.find(has) || ''
  return detectedModules
}

/** 指向 DSH node_modules 的 require；找不到时退回自身，让 require 报出真实缺失。 */
export function dshRequire() {
  const modules = findDshModules()
  const base = modules
    ? pathToFileURL(modules.endsWith(path.sep) ? modules : modules + path.sep).href
    : import.meta.url
  return createRequire(base)
}
