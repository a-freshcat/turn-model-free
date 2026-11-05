"""Transport/temporary availability failures are not account-security evidence."""

TRANSIENT_FAILURES = frozenset({
    'upstream_transport', 'upstream_stream_transport',
    'antigravity_transport', 'antigravity_stream_transport', 'antigravity_auth_transport',
    'plugin_transport', 'plugin_stream_transport',
    'first_output_timeout', 'completion_timeout', 'stream_timeout', 'test_timeout',
    'http_408', 'http_500', 'http_502', 'http_503', 'http_504',
})


def is_transient_failure(reason: str) -> bool:
    if reason.startswith('antigravity_http_'):
        reason = reason.removeprefix('antigravity_')
    return reason in TRANSIENT_FAILURES
