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
      const match = block.match(/InstallLocation\s+REG_\w+\s+(.+)/)
      if (match) found.push(match[1].trim())
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

export function findDshModules() {
  if (process.env.DSH_NODE_MODULES) return process.env.DSH_NODE_MODULES

  const candidates = []

  // 1. 常见安装根 + 常见产品名
  const roots = [
    process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, 'Programs'),
    process.env.ProgramFiles,
    process.env['ProgramFiles(x86)'],
    '/usr/lib', '/usr/local/lib', '/Applications',
  ].filter(Boolean)
  for (const root of roots) {
    for (const name of ['dsh', 'DSH', 'DeepSeek Harness', 'deepseek-harness']) {
      for (const rel of RELATIVE_LAYOUTS) {
        candidates.push(path.join(root, name, ...rel))
      }
    }
  }

  // 2. 从插件目录往上逐级找
  let cursor = process.env.DSH_PACK_HOME
    || path.join(homedir(), '.dsh', 'profiles', 'desktop', 'node_modules', 'dsh-our-free-model')
  for (let i = 0; i < 6; i += 1) {
    cursor = path.dirname(cursor)
    candidates.push(path.join(cursor, 'node_modules'))
    candidates.push(cursor)
  }

  // 3. 注册表（Windows 上最准，不要求进程在跑）
  for (const location of registryLocations()) {
    for (const rel of RELATIVE_LAYOUTS) {
      candidates.push(path.join(location, ...rel))
    }
  }

  for (const dir of candidates) if (has(dir)) return dir
  return ''
}

/** 指向 DSH node_modules 的 require；找不到时退回自身，让 require 报出真实缺失。 */
export function dshRequire() {
  const modules = findDshModules()
  const base = modules
    ? pathToFileURL(modules.endsWith(path.sep) ? modules : modules + path.sep).href
    : import.meta.url
  return createRequire(base)
}
