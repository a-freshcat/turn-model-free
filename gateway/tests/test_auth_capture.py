"""Tests for the auth/quota RPC 429 capture.

Before this, a 429 on `loadCodeAssist` or `retrieveUserQuotaSummary` produced no
log line and no evidence record at all — it was raised straight out as
`OAuthFailure(antigravity_http_429)`. So the one hypothesis left for "two
accounts rate-limited within 3 s while every quota bucket is healthy" could be
neither confirmed nor excluded.

The capture must obey the same redaction boundary as the generation-path
record: fixed enums, numbers, booleans and key *names* only.
"""
import json

import httpx
import pytest

import antigravity_auth as auth


ARTIFACT = auth.Path(auth.__file__).resolve().parent / ".gateway-rate-limit.jsonl"


@pytest.fixture(autouse=True)
def clean():
    ARTIFACT.unlink(missing_ok=True)
    yield
    ARTIFACT.unlink(missing_ok=True)


def last_record():
    lines = ARTIFACT.read_text(encoding="utf-8").strip().splitlines()
    return json.loads(lines[-1])


def test_rpc_label_is_a_fixed_enum_not_the_url():
    assert auth._auth_rpc_label("https://x/v1internal:loadCodeAssist") == "loadCodeAssist"
    assert auth._auth_rpc_label("https://x/v1internal:retrieveUserQuotaSummary") == "retrieveUserQuotaSummary"
    assert auth._auth_rpc_label("https://x/oauth2/v4/token") == "oauth2_token"
    assert auth._auth_rpc_label("https://x/some/other/path") == "other"


def test_capture_writes_a_redacted_record():
    body = json.dumps({"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                 "message": "Resource has been exhausted (e.g. check quota)."}}).encode()
    auth._capture_auth_rate_limit(429, body, httpx.Headers({}),
                                  "https://x/v1internal:loadCodeAssist")
    record = last_record()
    assert record["source"] == "auth_rpc"
    assert record["rpc"] == "loadCodeAssist"
    assert record["http_status"] == 429
    assert record["error_status"] == "RESOURCE_EXHAUSTED"
    assert record["message_labels"] == ["resource_exhausted", "quota"]


def test_capture_keeps_no_url_path_project_or_token():
    body = json.dumps({"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}).encode()
    auth._capture_auth_rate_limit(
        429, body,
        httpx.Headers({"x-goog-request-id": "secret-request-id-123"}),
        "https://x/v1internal:loadCodeAssist?project=my-private-project&token=abc")
    blob = ARTIFACT.read_text(encoding="utf-8")
    assert "my-private-project" not in blob
    assert "secret-request-id" not in blob
    assert "abc" not in blob
    assert "loadCodeAssist?project" not in blob
    # Request ids are hashed, never stored raw.
    assert last_record()["request_id_fingerprints"]


def test_capture_handles_a_non_json_body():
    """A 429 with an HTML or truncated body must still produce a record."""
    auth._capture_auth_rate_limit(429, b"<html>oops</html>", httpx.Headers({}),
                                  "https://x/v1internal:loadCodeAssist")
    record = last_record()
    assert record["http_status"] == 429
    assert record["error_status"] is None


def test_capture_handles_an_empty_body():
    auth._capture_auth_rate_limit(429, b"", httpx.Headers({}),
                                  "https://x/v1internal:retrieveUserQuotaSummary")
    assert last_record()["rpc"] == "retrieveUserQuotaSummary"


def test_capture_failure_never_raises():
    """The invariant: diagnostics must never break the request path."""
    auth._capture_auth_rate_limit(429, b"x", httpx.Headers({}), None)


def test_capture_reuses_the_generation_path_record_shape():
    """One file, one schema — otherwise post-hoc reading needs two parsers."""
    body = json.dumps({"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}).encode()
    auth._capture_auth_rate_limit(429, body, httpx.Headers({}),
                                  "https://x/v1internal:loadCodeAssist")
    record = last_record()
    for key in ("at", "http_status", "rate_window", "error_keys", "reason",
                "reason_filtered", "retry_delay_seconds", "request_id_fingerprints"):
        assert key in record, key
