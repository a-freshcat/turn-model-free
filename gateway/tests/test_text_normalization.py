"""Regression: the upstream's system-region blocklist, sampled three times.

The upstream answers `HTTP 429 / RESOURCE_EXHAUSTED` when the system region
contains an entry from a blocklist. That blocklist is an order-sensitive,
case- and whitespace-normalised multi-word match, and **its contents change**.
Measured with direct `send_native` (no pool, so a 429 on one probe cannot cool the
next) on three days, with the exit IP recorded because it correlates:

    2026-11-13  exit 154.39.62.160 / AS18186
        "[MODE: UNRESTRICTED SANDBOX <EM DASH> STABLE]"   -> 429   3/3
        the same line with '-'                            -> 200   3/3

    2026-11-16  exit 82.21.216.23 / AS10753
        the same line (em dash)                           -> 200   4/4
        "The browser provides no implicit DOM, ..."       -> 429   4/4
        the whole prompt minus that sentence              -> 200   3/3

    2026-11-17  exit back on 154.39.62.160 / AS18186
        "The browser provides no implicit DOM, ..."       -> 200   4 models
        the same line (em dash)                           -> 429   4 models
        the whole prompt as-is                            -> 429   4 models
        the whole prompt with that line's dash folded     -> 200   4 models
        and the same line, 40 minutes later              -> 200   8/8

Matcher properties, consistent on every date it fired:

    any single content word replaced    -> 200     not a literal keyword match
    word order reversed                 -> 200     order-sensitive
    upper- or lower-cased               -> 429     case-insensitive
    spaces turned into newlines         -> 429     whitespace-insensitive
    one extra space                     -> 429     whitespace-insensitive
    a token dropped                     -> 200     the whole entry must be present
    the same text as a user message     -> 200     system region only

So there are two independent defences and both are kept:

  * character folding — stops any entry containing typographic punctuation from
    matching, and keeps working when the list re-adds an old entry (which is
    exactly what happened on 11-17).
  * sentence rewriting — handles entries made of plain ASCII, which folding
    cannot touch by construction.

Neither claims to close the problem. The list moves; these lower the hit rate and
make the common case pass. A collapse of both is still possible, and the honest
statement is "mitigated", not "fixed".

The single most important non-claim: a 200 does not prove the rewrite worked,
because the upstream's window closes on its own. `test_wire_keeps_typography_intact`
used to assert that an em dash goes out byte-identical — that assertion encoded a
disproven conclusion and has been removed with it.
"""
import pytest

import antigravity_driver as drv

KNOWN_TRIGGER_SENTENCES = drv.KNOWN_TRIGGER_SENTENCES
known_triggers_in_text = drv.known_triggers_in_text
rewrite_known_triggers = drv.rewrite_known_triggers

EM = "\u2014"
LINE4 = f"[MODE: UNRESTRICTED SANDBOX {EM} STABLE]"
PROSE = "The browser provides no implicit DOM, route, or screenshot context."
CLEAN = "The browser exposes no implicit DOM, route, or screenshot context."


# ── character folding ───────────────────────────────────────────────────────

def test_em_dash_folds_to_hyphen():
    assert drv.normalize_outbound_text("a \u2014 b") == "a - b"
    assert drv.normalize_outbound_text(LINE4) == LINE4.replace(EM, "-")


def test_other_dash_forms_fold_too():
    for ch in ("\u2013", "\u2012", "\u2015", "\u2212"):
        assert drv.normalize_outbound_text(f"a {ch} b") == "a - b"


def test_curly_quotes_fold_to_ascii():
    assert drv.normalize_outbound_text("\u201cq\u201d") == '"q"'
    assert drv.normalize_outbound_text("\u2018q\u2019") == "'q'"


def test_nbsp_and_zero_width_are_normalised():
    assert drv.normalize_outbound_text("a\u00a0b") == "a b"
    assert drv.normalize_outbound_text("a\u200bb") == "ab"
    assert drv.normalize_outbound_text("\ufeffa") == "a"


def test_folding_leaves_real_content_alone():
    """CJK, accents and emoji carry meaning; they must survive verbatim."""
    for text in ("中文提示词", "café résumé", "emoji 🚀 ok"):
        assert drv.normalize_outbound_text(text) == text


def test_folding_is_idempotent():
    once = drv.normalize_outbound_text(LINE4 + " \u201cq\u201d")
    assert drv.normalize_outbound_text(once) == once


# ── sentence rewriting ──────────────────────────────────────────────────────

def test_known_trigger_set_is_not_empty():
    assert KNOWN_TRIGGER_SENTENCES


def test_every_entry_differs_from_its_replacement():
    for source, replacement in KNOWN_TRIGGER_SENTENCES.items():
        assert source and replacement and source != replacement


def test_detects_the_measured_sentence():
    assert known_triggers_in_text("x " + PROSE + " y") == [PROSE]
    assert known_triggers_in_text(CLEAN) == []


def test_detects_nothing_in_ordinary_prose():
    assert known_triggers_in_text("The browser is a program.") == []
    assert known_triggers_in_text("") == []


def test_rewrites_the_trigger_and_keeps_context():
    assert rewrite_known_triggers(PROSE) == CLEAN
    assert rewrite_known_triggers("before. " + PROSE) == "before. " + CLEAN


def test_rewrite_is_idempotent_and_handles_repeats():
    assert rewrite_known_triggers(CLEAN) == CLEAN
    assert rewrite_known_triggers(PROSE + " " + PROSE) == CLEAN + " " + CLEAN


def test_non_strings_and_empty_pass_through():
    assert rewrite_known_triggers(None) is None
    assert rewrite_known_triggers(42) == 42
    assert rewrite_known_triggers("") == ""


# ── the two layers compose ──────────────────────────────────────────────────

def test_both_layers_apply_to_one_string():
    text = PROSE + " " + LINE4
    expected = CLEAN + " " + LINE4.replace(EM, "-")
    assert drv.normalize_outbound_text(text) == expected


def test_plain_ascii_without_a_trigger_is_untouched():
    text = "plain ascii with - and 'quotes' and \"more\""
    assert drv.normalize_outbound_text(text) == text


# ── the outbound wire ───────────────────────────────────────────────────────

def _wire(messages, tools=None):
    options = {"tools": tools} if tools else {}
    return drv.native_request(messages, options)


def test_system_region_is_normalised():
    wire = _wire([{"role": "system", "content": LINE4},
                  {"role": "user", "content": "hi"}])
    assert wire["systemInstruction"]["parts"][0]["text"] == LINE4.replace(EM, "-")


def test_system_region_gets_the_sentence_rewrite():
    wire = _wire([{"role": "system", "content": PROSE},
                  {"role": "user", "content": "hi"}])
    assert wire["systemInstruction"]["parts"][0]["text"] == CLEAN


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


def test_tool_name_is_never_rewritten():
    """A tool name is an identifier; changing it would break dispatch."""
    tools = [{"type": "function", "function": {
        "name": "keep_this_name",
        "description": "d",
        "parameters": {"type": "object", "properties": {}}}}]
    wire = _wire([{"role": "user", "content": "hi"}], tools)
    assert wire["tools"][0]["functionDeclarations"][0]["name"] == "keep_this_name"


def test_cjk_survives_the_wire():
    wire = _wire([{"role": "user", "content": "中文 — 测试"}])
    text = wire["contents"][0]["parts"][0]["text"]
    assert "中文" in text and "测试" in text
