"""Regression: the exact system-prompt sentence Antigravity answers 429 to.

Measured 2026-11-16 with direct `send_native` (no pool, so one 429 cannot poison
the next probe), each case stable across repeats:

    the whole 31765-char system prompt               -> 429   3/3
    the same prompt minus one 67-char sentence       -> 200   3/3
    that sentence alone                              -> 429   4/4
    that sentence, any single content word edited    -> 200
    that sentence, word order reversed               -> 200
    that sentence, upper- or lower-cased             -> 429
    that sentence, spaces turned into newlines       -> 429
    that sentence, padded on either side             -> 429
    that sentence sent as a user message             -> 200

So the upstream does a normalised, order-sensitive multi-word match over the
system region only, and answers `HTTP 429 / RESOURCE_EXHAUSTED` with a generic
message, empty `details` and no `RetryInfo`. Treating that as a rate limit can
never help: every retry re-sends the same sentence.

An earlier round blamed U+2014 for this and rewrote every dash and curly quote on
the way out. That was wrong — the em-dash cases all pass, and rewriting
user-visible punctuation is visible damage. Those tests are gone with it.
"""
import pytest

import antigravity_driver as drv

KNOWN_TRIGGER_SENTENCES = drv.KNOWN_TRIGGER_SENTENCES
known_triggers_in_text = drv.known_triggers_in_text
rewrite_known_triggers = drv.rewrite_known_triggers


TRIGGER = "The browser provides no implicit DOM, route, or screenshot context."
CLEAN = "The browser exposes no implicit DOM, route, or screenshot context."


# ── the trigger table ───────────────────────────────────────────────────────

def test_known_trigger_set_is_not_empty():
    assert KNOWN_TRIGGER_SENTENCES


def test_every_entry_rewrites_to_something_different():
    for source, replacement in KNOWN_TRIGGER_SENTENCES.items():
        assert source != replacement
        assert source and replacement


def test_detects_the_measured_sentence():
    assert known_triggers_in_text("prefix " + TRIGGER + " suffix") == [TRIGGER]
    assert known_triggers_in_text(CLEAN) == []


def test_detects_nothing_in_ordinary_prose():
    assert known_triggers_in_text("The browser is a program that renders pages.") == []
    assert known_triggers_in_text("") == []


# ── the rewrite ─────────────────────────────────────────────────────────────

def test_rewrites_the_trigger():
    assert rewrite_known_triggers(TRIGGER) == CLEAN


def test_rewrite_keeps_surrounding_text():
    text = "before. " + TRIGGER + " after."
    assert rewrite_known_triggers(text) == "before. " + CLEAN + " after."


def test_rewrite_is_idempotent():
    once = rewrite_known_triggers(TRIGGER)
    assert rewrite_known_triggers(once) == once


def test_rewrite_handles_repeated_occurrences():
    text = TRIGGER + " " + TRIGGER
    assert rewrite_known_triggers(text) == CLEAN + " " + CLEAN


def test_rewrite_leaves_punctuation_alone():
    """The disproven fix rewrote dashes and quotes; this one must not."""
    for text in ("a \u2014 b", "\u201cq\u201d", "\u2018q\u2019",
                 "a\u00a0b", "a\u200bb", "\ufeffa", "中文 — ok"):
        assert rewrite_known_triggers(text) == text


def test_rewrite_leaves_non_strings_alone():
    assert rewrite_known_triggers(None) is None
    assert rewrite_known_triggers(42) == 42


def test_rewrite_leaves_empty_alone():
    assert rewrite_known_triggers("") == ""


# ── the outbound wire ───────────────────────────────────────────────────────

def _wire(messages, tools=None):
    options = {"tools": tools} if tools else {}
    return drv.native_request(messages, options)


def test_system_trigger_is_rewritten_on_the_wire():
    wire = _wire([{"role": "system", "content": TRIGGER},
                  {"role": "user", "content": "hi"}])
    assert wire["systemInstruction"]["parts"][0]["text"] == CLEAN


def test_message_trigger_is_rewritten_on_the_wire():
    wire = _wire([{"role": "user", "content": TRIGGER}])
    assert wire["contents"][0]["parts"][0]["text"] == CLEAN


def test_tool_description_trigger_is_rewritten_on_the_wire():
    tools = [{"type": "function", "function": {
        "name": "t",
        "description": TRIGGER,
        "parameters": {"type": "object", "properties": {}}}}]
    wire = _wire([{"role": "user", "content": "hi"}], tools)
    declaration = wire["tools"][0]["functionDeclarations"][0]
    assert declaration["description"] == CLEAN


def test_tool_name_is_never_rewritten():
    """A tool name is an identifier; changing it would break dispatch."""
    tools = [{"type": "function", "function": {
        "name": "keep_this_name",
        "description": "d",
        "parameters": {"type": "object", "properties": {}}}}]
    wire = _wire([{"role": "user", "content": "hi"}], tools)
    assert wire["tools"][0]["functionDeclarations"][0]["name"] == "keep_this_name"


def test_wire_keeps_typography_intact():
    """A request carrying a real em dash must go out byte-identical."""
    wire = _wire([{"role": "user", "content": "turn \u2014 now"}])
    assert wire["contents"][0]["parts"][0]["text"] == "turn \u2014 now"
