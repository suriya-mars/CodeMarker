"""Handling for untrusted repository text on its way into an LLM prompt.

Repo content — source, summaries derived from it, diffs — is written by whoever
authored the repo, so it is treated like any untrusted input. Three tools:

  wrap()             fence text in a RANDOM per-call boundary. A fixed tag like
                     <code> can be closed early by the text itself ("</CODE>",
                     "</ code>"); a boundary the author cannot predict cannot.
  strip_invisible()  remove characters a reviewer cannot see but a model reads:
                     zero-width spaces, bidi overrides (Trojan Source), Unicode
                     tag characters that smuggle whole hidden sentences.
  hidden_texts()     the text an obfuscated payload decodes to (tag characters,
                     base64, hex), so injection detection can scan it too.

None of this makes injection impossible; it removes the cheap tricks. The real
guarantee is structural: facts in this system come from the parsed graph, and
the model has no write capability.
"""

from __future__ import annotations

import base64
import binascii
import re
import secrets

# Characters with no visible glyph. Cf (format) ranges, plus variation
# selectors and Hangul fillers, which are used for the same smuggling.
_INVISIBLE_RANGES = (
    "­؜᠎​-‏‪-‮⁠-⁤⁦-⁯"
    "ᅟᅠㅤﾠ︀-️﻿￹-￻"
    "\U000e0000-\U000e007f\U000e0100-\U000e01ef"
)
_INVISIBLE = re.compile(f"[{_INVISIBLE_RANGES}]")
_TAG_CHARS = re.compile("[\U000e0020-\U000e007e]+")
_BIDI = re.compile("[‪-‮⁦-⁩]")
# a zero-width character splitting a word: "ig​nore" hides "ignore" from filters
_SPLIT_WORD = re.compile(r"\w[​-‍⁠﻿]+\w")
_B64 = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")
_HEX = re.compile(r"\b(?:[0-9a-fA-F]{2}){12,}\b")
_MAX_CANDIDATES = 50


def wrap(text: str, label: str = "data") -> tuple[str, str]:
    """Return (tag, text fenced as <tag>...</tag>) with an unguessable tag."""
    while True:
        tag = f"{label}-{secrets.token_hex(8)}"
        if tag not in text:  # 64 random bits: effectively always first try
            return tag, f"<{tag}>\n{text}\n</{tag}>"


def strip_invisible(text: str) -> str:
    return _INVISIBLE.sub("", text) if text else text


def has_smuggling(text: str) -> bool:
    """Invisible characters used the way an attack uses them. A leading BOM, or
    a zero-width joiner inside an emoji, is ordinary and not counted."""
    if not text:
        return False
    return bool(_TAG_CHARS.search(text) or _BIDI.search(text) or _SPLIT_WORD.search(text))


def _printable(raw: bytes) -> str | None:
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if len(s) < 8 or sum(c.isprintable() or c.isspace() for c in s) < 0.9 * len(s):
        return None
    return s if any(c.isalpha() for c in s) else None


def hidden_texts(text: str) -> list[str]:
    """Plain text an obfuscated payload in `text` decodes to."""
    if not text:
        return []
    out = [strip_invisible(text)]
    for run in _TAG_CHARS.findall(text):
        out.append("".join(chr(ord(c) - 0xE0000) for c in run))
    for m in _B64.findall(text)[:_MAX_CANDIDATES]:
        try:
            decoded = _printable(base64.b64decode(m + "=" * (-len(m) % 4), validate=True))
        except (binascii.Error, ValueError):
            continue
        if decoded:
            out.append(decoded)
    for m in _HEX.findall(text)[:_MAX_CANDIDATES]:
        decoded = _printable(bytes.fromhex(m))
        if decoded:
            out.append(decoded)
    return out
