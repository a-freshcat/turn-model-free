import assert from 'node:assert/strict'
import { POOLS, poolFor, summarize, deduplicate } from '../web/quota-models.mjs'

assert.equal(POOLS.length,3)
const models = [
  {id:'argon-high',name:'Gemini 4 Argon (High)',remaining:100},
  {id:'claude',name:'Claude Opus 5.5 (High)',remaining:80},
  {id:'gpt',name:'GPT-OSS 120B (Medium)',remaining:80},
  {id:'flash-a',name:'Gemini 3.5 Flash Lite',remaining:99.61},
  {id:'flash-b',name:'Gemini 3.5 Flash Lite',remaining:99.61},
  {id:'chat_20706',name:'chat_20706',remaining:99.61},
]
assert.deepEqual(models.map(poolFor),['argon','claude_gpt','claude_gpt','gemini_shared','gemini_shared','gemini_shared'])
assert.equal(deduplicate(models).find(row=>row.name==='Gemini 3.5 Flash Lite').aliases.length,2)
assert.equal(summarize(models.filter(row=>poolFor(row)==='gemini_shared')).remaining,99.61)
assert.equal(summarize([{remaining:null}]).remaining,null)
assert.equal(summarize([{remaining:100},{remaining:80}]).remaining,80)
assert.equal(summarize([{remaining:100},{remaining:80}]).maximum,100)
console.log('Three independent pools, alias collapse, exact percentages and unknown quota PASS')
