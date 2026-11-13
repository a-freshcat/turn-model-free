"""Regression: outbound text normalisation for the upstream's dash sensitivity.

Measured 2026-11-13, direct `send_native` so pool cooling could not confound it:

    3 lines of the real system prompt    43 chars  ->  200
    the 37-char 4th line alone                    ->  429   3/3
    the same line with the em dash as '-'         ->  200   3/3
    the line embedded in 40 lines                 ->  429

One U+2014 em dash made the upstream answer `HTTP 429 / RESOURCE_EXHAUSTED`
while every quota bucket read healthy. The gateway treated that as a rate limit,
cooled the account, and retried — none of which could ever help, because the
request was being rejected for its text, not its rate.

`normalize_outbound_text` maps typographic characters to ASCII equivalents at
the three outbound sites: system text, message text, tool descriptions.
"""
import pytest

import antigravity_driver as drv


# ── the translation itself ──────────────────────────────────────────────────

def test_em_dash_becomes_hyphen():
    assert drv.normalize_outbound_text("a \u2014 b") == "a - b"


def test_en_dash_and_minus_sign_also_become_hyphen():
    assert drv.normalize_outbound_text("a \u2013 b") == "a - b"
    assert drv.normalize_outbound_text("a \u2212 b") == "a - b"


def test_curly_quotes_become_ascii_quotes():
    assert drv.normalize_outbound_text("\u201cq\u201d") == '"q"'
    assert drv.normalize_outbound_text("\u2018q\u2019") == "'q'"


def test_no_break_space_becomes_plain_space():
    assert drv.normalize_outbound_text("a\u00a0b") == "a b"


def test_zero_width_characters_are_dropped():
    assert drv.normalize_outbound_text("a\u200bb") == "ab"
    assert drv.normalize_outbound_text("\ufeffa") == "a"


def test_ascii_text_is_untouched():
    text = "plain ascii with - and 'quotes' and \"more\""
    assert drv.normalize_outbound_text(text) == text


def test_other_scripts_survive():
    """Normalisation must not damage CJK, accents or emoji."""
    for text in ("中文提示词", "café résumé", "emoji 🚀 ok"):
        assert drv.normalize_outbound_text(text) == text


def test_is_idempotent():
    once = drv.normalize_outbound_text("a \u2014 b \u201cq\u201d")
    assert drv.normalize_outbound_text(once) == once


def test_non_string_values_pass_through():
    assert drv.normalize_outbound_text(None) is None
    assert drv.normalize_outbound_text(42) == 42


def test_result_is_ascii_when_input_is_only_typographic():
    assert drv.normalize_outbound_text("a \u2014 b \u2013 c").isascii()


# ── the three outbound sites ────────────────────────────────────────────────

def _wire(messages, tools=None):
    options = {"tools": tools} if tools else {}
    return drv.native_request(messages, options)


def test_system_text_is_normalised():
    wire = _wire([{"role": "system", "content": "rule \u2014 one"},
                  {"role": "user", "content": "hi"}])
    parts = wire["systemInstruction"]["parts"]
    assert parts[0]["text"] == "rule - one"
    assert parts[0]["text"].isascii()


def test_message_text_is_normalised():
    wire = _wire([{"role": "user", "content": "turn \u2014 now"}])
    assert wire["contents"][0]["parts"][0]["text"] == "turn - now"


def test_tool_description_is_normalised():
    tools = [{"type": "function", "function": {
        "name": "t",
        "description": "does \u2014 things",
        "parameters": {"type": "object", "properties": {}}}}]
    wire = _wire([{"role": "user", "content": "hi"}], tools)
    declaration = wire["tools"][0]["functionDeclarations"][0]
    assert declaration["description"] == "does - things"
    assert declaration["description"].isascii()


def test_tool_name_is_never_rewritten():
    """A tool name is an identifier; changing it would break dispatch."""
    tools = [{"type": "function", "function": {
        "name": "keep_this_name",
        "description": "d",
        "parameters": {"type": "object", "properties": {}}}}]
    wire = _wire([{"role": "user", "content": "hi"}], tools)
    assert wire["tools"][0]["functionDeclarations"][0]["name"] == "keep_this_name"


def test_normalisation_applies_to_marked_text_too():
    """Text carrying a legacy marker still gets its typography folded.

    `strip_legacy_markers` may discard the marked span entirely — that is the
    pre-existing replay behaviour and not what this file is about. What matters
    here is the ordering: whatever survives the strip is normalised, so a marker
    cannot smuggle an em dash through.
    """
    wire = _wire([{"role": "user",
                   "content": "keep this \u2014 part"}])
    text = wire["contents"][0]["parts"][0]["text"]
    assert text == "keep this - part"
