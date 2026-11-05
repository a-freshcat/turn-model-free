"""Python adaptation of free-m-clone src/guard/tencent-gate.js (see README).

Apply only to configured channels, once per account attempt from original
messages. Tool calls, user/tool text, and recognized paths retain their bytes.
"""

import re
import unicodedata
from typing import Any

_ZW = r"[\u200B-\u200D\uFEFF\u2060]"
_WS = r"[\s\uFEFF]+"
_BRAND = rf"DeepSeek{_ZW}*{_WS}{_ZW}*Harness"
_LOOSE = rf"DeepSeek{_ZW}*(?:{_WS})?{_ZW}*Harness"
_LEFT, _RIGHT = r"(?<![\\/A-Za-z0-9_])", r"(?![\\/A-Za-z0-9_])"
_POWER = "(?:powered by|built on|running on|based on)".replace(" ", _WS)
_OPTIONAL_THE = rf"(?:the{_WS})?"
_RULES = tuple(
    (level, leading, re.compile(pattern, flags), replacement)
    for level, leading, pattern, flags, replacement in [
        (1, False, r"You are an AI agent powered by DeepSeek Harness\.", 0,
         "You are an AI coding agent."),
        (1, False, "DeepSeek Harness Web GUI", 0, "the Web GUI"),
        (1, False, r"Main branch \(you will usually use this for PRs\)", 0,
         "Main branch (PRs usually go here)"),
        (1, False, _LEFT + _BRAND + _RIGHT, 0, "DSH"),
        (2, False, rf"You{_WS}are{_WS}(?:an?{_WS})?(?:AI{_WS})?"
         rf"(?:agent|assistant|model|coding{_WS}(?:agent|assistant))[^\n]{{0,120}}?"
         rf"(?:{_POWER}|from){_WS}{_OPTIONAL_THE}{_LOOSE}\.?", re.I,
         "You are an AI coding agent."),
        (2, False, rf"{_POWER}{_WS}{_OPTIONAL_THE}{_LOOSE}", re.I, "powered by DSH"),
        (2, False, _LEFT + _LOOSE + _RIGHT, 0, "DSH"),
        (2, False, _WS.join(r"Main branch \(you will usually use this for PRs\)".split()), re.I,
         "Main branch (PRs usually go here)"),
        (3, True, r"^\uFEFF?[ \t]*You[ \t]+are[^\n]{0,160}?"
         r"\b(?:deepseek|harness|dsh|codebuddy|workbuddy)\b[^\n]{0,160}?[.!?](?=[ \t]|$)",
         re.I, "You are a coding assistant."),
        (2, False, r"Tokens prefixed with @ are paths the user explicitly referenced\.", 0,
         "At-prefixed tokens denote files the user referenced explicitly."),
        (2, False, r"Relative paths resolve from the workspace root; absolute paths "
         r"identify files or directories on the host\.", 0,
         "Relative paths resolve against the project root; absolute ones identify entries on the host machine."),
        (3, False, r"Tokens\s+prefixed\s+with\s+@\s+are\s+paths?\s+the\s+user\s+"
         r"explicitly\s+referenced[^\n]{0,40}", re.I,
         "At-prefixed tokens denote files the user referenced explicitly."),
    ]
)
_PATH_START = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")
_PATHISH = re.compile(r"[\\/]|\.[A-Za-z0-9]{1,8}$")
_PLACEHOLDER = re.compile(r"\ue000[0-9]+\ue001")
_QUOTES = {'"': '"', "'": "'", chr(96): chr(96), "<": ">", "[": "]", "(": ")"}


def _mask_paths(text: str) -> tuple[str, dict[str, str]]:
    tokens = list(re.finditer(r"\S+", text))
    segments, paths = [], {}
    cursor = i = 0
    # If the text already includes the placeholder alphabet, leave it untouched.
    if "\ue000" in text or "\ue001" in text:
        return text, {}
    while i < len(tokens):
        token = tokens[i]
        body = token.group()
        closer = _QUOTES.get(body[0]) if _PATH_START.match(body[1:]) else None
        if not closer and not _PATH_START.match(body):
            i += 1
            continue
        start, end = token.start() + bool(closer), token.end()
        close = -1
        if closer:
            # Limit the search to the current line so an open quote cannot
            # protect unrelated prompt text on subsequent lines.
            end_line = text.find("\n", start)
            stop = len(text) if end_line < 0 else end_line
            candidate = text.find(closer, start, stop)
            while candidate >= 0:
                preceding = candidate - 1
                while preceding >= start and text[preceding] == "\\":
                    preceding -= 1
                if (candidate - 1 - preceding) % 2 == 0:
                    close = candidate
                    break
                candidate = text.find(closer, candidate + 1, stop)
        i += 1
        if close >= start:
            end = close
            while i < len(tokens) and tokens[i].start() <= close:
                i += 1
        else:
            while i < len(tokens) and re.fullmatch(r"[ \t]{1,64}", text[end:tokens[i].start()]):
                if not _PATHISH.search(tokens[i].group()):
                    break
                end = tokens[i].end()
                i += 1
        placeholder = f"\ue000{len(paths)}\ue001"
        paths[placeholder] = text[start:end]
        segments.extend((text[cursor:start], placeholder))
        cursor = end
    segments.append(text[cursor:])
    return "".join(segments), paths


def _rewrite(text: str, level: int, leading: bool) -> str:
    if not text or "\ue000" in text or "\ue001" in text:
        return text
    masked, paths = _mask_paths(text)

    def apply(source: str) -> str:
        for minimum, leading_only, pattern, replacement in _RULES:
            if level >= minimum and (leading or not leading_only):
                source = pattern.sub(replacement, source)
        return source

    result = apply(masked)
    if result == masked and level == 3:
        normalized = re.sub(_ZW, "", unicodedata.normalize("NFKC", masked))
        normalized = re.sub(r"[\u00A0\u1680\u2000-\u200A\u202F\u205F\u3000]", " ", normalized)
        normalized = re.sub(r"[ \t]{2,}", " ", normalized)
        result = apply(normalized)
        if result == normalized:
            return text
    holders = _PLACEHOLDER.findall(result)
    if len(holders) != len(paths) or set(holders) != paths.keys():
        return text
    return _PLACEHOLDER.sub(lambda m: paths[m.group()], result)


def scrub_messages(messages: list[dict[str, Any]], level: int) -> list[dict[str, Any]]:
    if level == 0:
        return messages
    result = []
    for message in messages:
        content = message.get("content")
        if message.get("role") in {"system", "assistant"}:
            if isinstance(content, str):
                message = {**message, "content": _rewrite(content, level, True)}
            elif isinstance(content, list):
                blocks, leading = [], True
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                        block = {**block, "text": _rewrite(block["text"], level, leading)}
                        leading = False
                    blocks.append(block)
                message = {**message, "content": blocks}
        result.append(message)
    return result
