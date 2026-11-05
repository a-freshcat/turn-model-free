// ESM 解析钩子：把 pack.js 里的裸包名 @deepseek-ai/* 指向宿主 DSH 的实现。
//
// 为什么不直接改 pack.js 或往插件目录塞 node_modules：
//   pack.js 装在 ~/.dsh/profiles/desktop/node_modules/ 下，是 dsh-our-free-model
//   这个已安装插件的产物。改动它会让「插件到底跑的是哪份代码」失去确定性，
//   也会被下次插件更新覆盖。解析钩子只影响本次进程，不改任何磁盘上的既有文件。
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'
import { findDshModules } from './dsh_modules.mjs'

const DSH_MODULES = findDshModules()
const require = createRequire(
  DSH_MODULES
    ? pathToFileURL(DSH_MODULES.endsWith('/') ? DSH_MODULES : DSH_MODULES + '/')
    : import.meta.url)

const PEERS = new Set([
  '@deepseek-ai/cordis',
  '@deepseek-ai/dsh-credentials',
  '@deepseek-ai/dsh-llm',
  '@deepseek-ai/schemastery',
])

const resolved = new Map()

export async function resolve(specifier, context, next) {
  if (!PEERS.has(specifier)) return next(specifier, context)
  let url = resolved.get(specifier)
  if (url === undefined) {
    try {
      url = pathToFileURL(require.resolve(specifier)).href
    } catch (error) {
      const where = DSH_MODULES || '(未找到宿主 node_modules)'
      throw new Error(
        `宿主依赖 ${specifier} 不可用（查找位置：${where}）。`
        + '请设置 DSH_NODE_MODULES 指向 DSH 应用的 node_modules 目录。'
        + `原始错误：${error.message}`)
    }
    resolved.set(specifier, url)
  }
  return { url, shortCircuit: true }
}
