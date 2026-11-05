// Preserve plugin metadata when the local-only /ui/state has no matching row.
export function mergePluginRuntime(row, runtime = {}) {
  return {
    status: runtime.status ?? row.status ?? 'active',
    in_flight: runtime.in_flight ?? row.in_flight ?? 0,
    max_concurrency: runtime.max_concurrency ?? row.max_concurrency ?? 1,
    fail_count: runtime.fail_count ?? row.fail_count ?? 0,
    cooldown_remaining_seconds: runtime.cooldown_remaining_seconds ?? row.cooldown_remaining_seconds ?? 0,
    rate_limited_models: runtime.rate_limited_models ?? row.rate_limited_models ?? [],
  }
}
