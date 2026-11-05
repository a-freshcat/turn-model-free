import assert from 'node:assert/strict'
import { mergePluginRuntime } from '../web/quota-data.mjs'

const row = { status:'cooldown',cooldown_remaining_seconds:1800,rate_limited_models:['model-a'] }
assert.equal(mergePluginRuntime(row).cooldown_remaining_seconds,1800)
assert.deepEqual(mergePluginRuntime(row).rate_limited_models,['model-a'])
assert.equal(mergePluginRuntime(row,{cooldown_remaining_seconds:0}).cooldown_remaining_seconds,0)
assert.equal(mergePluginRuntime(row,{status:'active'}).status,'active')
console.log('Plugin cooldown and limited model metadata survive UI merging PASS')
