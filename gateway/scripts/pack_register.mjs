// 注册解析钩子（`--import` 只是加载模块，钩子必须显式 register）。
import { register } from 'node:module'
import { pathToFileURL } from 'node:url'

register('./pack_loader.mjs', pathToFileURL(import.meta.filename))
