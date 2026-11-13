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
    # Antigravity 的 429 也不能当成「池子耗尽」。
    #
    # 早先这里刻意不收录，理由是「那是按池计配额已用尽，短退避只会把同一个
    # 耗尽了的池反复叫醒」。2026-11-13 的实测证明这个前提是错的，但**当时的
    # 结论也错了一次**，一并记在这里，避免下次再走同一条弯路：
    #
    #   · 撞 429 时 last_60s = 1（60 秒内只有 1 次请求），配额桶 0.96 / 0.99，
    #     details 为空、无 RetryInfo、无 quotaId；
    #   · 于是当时判定「上游拒绝的是一个速率窗口」——这个判断是错的。
    #     30 次间隔 0.5 秒的请求、8 路并发、442k 字符输出全部 200，密度不触发；
    #   · 真正的成因是 system prompt 里的一个 em dash（U+2014）：37 字符就能
    #     稳定复现 429，换成 ASCII 减号立刻恢复。详见
    #     antigravity_driver.normalize_outbound_text 的注释与实测数据。
    #
    # 也就是说上游用 RESOURCE_EXHAUSTED 表达了一个**与速率无关**的拒绝。既然
    # 短退避对真正的成因无效，这里的意义只剩一个：别再锁十分钟。一次内容匹配
    # 不该让账号停摆 600 秒，快速失败并把真实原因交回客户端更诚实。
    'antigravity_http_429',
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
