// The three-pool policy was explicitly supplied by the user.
export const POOLS = [
  { id: 'argon', name: 'Gemini 4 Argon', note: '独立配额池' },
  { id: 'claude_gpt', name: 'Claude / GPT', note: 'Claude 与 GPT 共用，独立于 Argon' },
  { id: 'gemini_shared', name: '其他 Gemini / 内部型号', note: '其余 Gemini 和内部型号共用' },
]
export function poolFor(model) {
  if (POOLS.some(pool => pool.id === model.pool)) return model.pool
  const identity = (String(model.name || '') + ' ' + String(model.id || '')).toLowerCase()
  if (identity.includes('argon')) return 'argon'
  if (identity.includes('claude') || identity.includes('gpt')) return 'claude_gpt'
  return 'gemini_shared'
}
export function summarize(models) {
  const known = models.map(model => model.remaining).filter(value => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100)
  return { remaining: known.length ? Math.min(...known) : null,
    maximum: known.length ? Math.max(...known) : null, known: known.length, count: models.length }
}
export function deduplicate(models) {
  const result = new Map()
  for (const model of models) {
    const key = JSON.stringify([model.name || model.id, model.remaining, model.reset_time || ''])
    if (!result.has(key)) result.set(key, { ...model, aliases: [] })
    result.get(key).aliases.push(model.id)
  }
  return [...result.values()].sort((a,b) => String(a.name).localeCompare(String(b.name)))
}
