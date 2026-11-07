"""Transport/temporary availability failures are not account-security evidence."""

TRANSIENT_FAILURES = frozenset({
    'upstream_transport', 'upstream_stream_transport',
    'antigravity_transport', 'antigravity_stream_transport', 'antigravity_auth_transport',
    'gemini_transport',
    # 官方 Gemini API Key 的 429 是**每分钟令牌桶**，不是一个账号坏了。给 20 秒
    # 起步的短退避（`network_cooldown_seconds` 起的指数），号池会立刻换下一把
    # 钥匙；用 600 秒整轮冷却等于把一把好钥匙锁十分钟。
    # Antigravity 的 `antigravity_http_429` 刻意不在此列：那是按池计的配额已经
    # 用尽（Argon/Claude 池），短退避只会把同一个耗尽了的池反复叫醒。
    'gemini_http_429',
    'plugin_transport', 'plugin_stream_transport',
    'first_output_timeout', 'completion_timeout', 'stream_timeout', 'test_timeout',
    'http_408', 'http_500', 'http_502', 'http_503', 'http_504',
})


def is_transient_failure(reason: str) -> bool:
    """这条失败是「暂时性」还是「账号本身有问题」。

    两条原生驱动都把 HTTP 状态编进 reason（`antigravity_http_503` /
    `gemini_http_503`），所以先按原样查一次（个别驱动有专属条目，例如
    `gemini_http_429`），再剥掉 `*_http_` 前缀按 `http_503` 查一次 —— 否则每加
    一个驱动都要回到这里补一遍前缀规则，漏一次就是「网络抖动把账号锁 10 分钟」。
    """
    if reason in TRANSIENT_FAILURES:
        return True
    for prefix in ('antigravity_', 'gemini_'):
        if reason.startswith(prefix + 'http_'):
            return reason.removeprefix(prefix) in TRANSIENT_FAILURES
    return False
