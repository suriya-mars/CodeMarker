"""Local ML guardrails, via LLM Guard (Protect AI, MIT).

Our own checks (summarize.py patterns, untrusted.py) are deterministic and
cheap, but keyword matching misses paraphrase and scrambled spellings
("ignroe all prevoius instructions"). LLM Guard adds a trained classifier
(a DeBERTa prompt-injection model) and detect-secrets' maintained secret rules.
Both run IN-PROCESS on this machine: nothing is sent to a guardrail service,
which matters because everything checked here contains customer source.

Measured on real code before adoption (see the prompt-injection report):
  * threshold 0.92 (library default) flagged 4.6% of ordinary functions;
    0.99 flagged 0.9% while still catching paraphrase and typoglycemia
  * MatchType.FULL truncates at 512 tokens and MISSED an injection placed at
    the end of a long function. MatchType.CHUNKS caught it but raised false
    positives from 0.9% to 11.9%: it cuts code into small fragments, and the
    model is unstable on short input (it flags even `ICON = ""`). So windowing
    is done here instead: large overlapping windows, the last one aligned to the
    end of the text, each scanned FULL — no tail is missed, no tiny fragments
  * its secret scanner missed `password = "..."`, which our regex catches —
    so it runs alongside ours, never instead
  * it flags long base64 blobs (data URIs, embedded images) as injections — a
    model trained on prose reads opaque tokens as adversarial. Such blobs are
    replaced by a placeholder before classification; a blob that decodes to
    text is still scanned, decoded, via untrusted.hidden_texts()
The classifier and our patterns are independent signals; either one flags.

GUARD_MODE:
  required  (default) refuse to start if LLM Guard is not installed
  off       skip ML checks — tests and local development only, logged loudly
"""

from __future__ import annotations

import os
import re
import sys
import threading
from typing import Any

_MODES = {"required", "off"}
_lock = threading.Lock()  # HF fast tokenizers are not safe to share across threads
_scanners: dict[str, Any] = {}
# Opaque tokens: base64 / base64url / long hex. Not language, so the classifier
# has nothing to judge in them except false positives.
_OPAQUE = re.compile(r"[A-Za-z0-9+/_-]{40,}={0,2}")


_WINDOW = 1200   # chars: comfortably inside the model's 512-token window
_OVERLAP = 300   # so an instruction split across a boundary is seen whole


def windows(text: str) -> list[str]:
    """Overlapping windows covering all of `text`. Every window is full-size
    except when the text itself is shorter than one window."""
    if len(text) <= _WINDOW:
        return [text]
    step = _WINDOW - _OVERLAP
    starts = list(range(0, len(text) - _WINDOW, step)) + [len(text) - _WINDOW]
    return [text[i:i + _WINDOW] for i in starts]


def _for_classifier(text: str) -> str:
    # "..." is deliberate: measured, an angle-bracket placeholder ("<blob>") was
    # itself flagged, and the model is unstable on near-empty strings; "..."
    # reads as ordinary elision.
    return _OPAQUE.sub("...", text)


def mode() -> str:
    return os.environ.get("GUARD_MODE", "required").strip().lower()


def enabled() -> bool:
    return mode() == "required"


def threshold() -> float:
    return float(os.environ.get("GUARD_THRESHOLD", "0.99"))


def check_config() -> None:
    """Fail at startup, not on the first request — and never fall back to
    'no guardrails' silently when they were asked for."""
    m = mode()
    if m not in _MODES:
        raise RuntimeError(f"GUARD_MODE must be one of {sorted(_MODES)}, got {m!r}")
    if m == "off":
        print("[guard] WARNING: GUARD_MODE=off — ML injection and secret checks are "
              "disabled. Pattern-based checks still run.", file=sys.stderr, flush=True)
        return
    try:
        import llm_guard  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "GUARD_MODE=required but llm-guard is not installed "
            "(pip install -r requirements-guard.txt), or set GUARD_MODE=off for "
            "local development"
        ) from exc


def preload() -> None:
    """Load the models now. Called at startup so the first request is not slow,
    and at image build so the weights are baked in and runtime is offline."""
    if enabled():
        _injection()
        _secrets()


def _injection():
    with _lock:
        if "injection" not in _scanners:
            from llm_guard.input_scanners import PromptInjection
            from llm_guard.input_scanners.prompt_injection import MatchType

            _scanners["injection"] = PromptInjection(threshold=threshold(),
                                                     match_type=MatchType.FULL)
        return _scanners["injection"]


def _secrets():
    with _lock:
        if "secrets" not in _scanners:
            from llm_guard.input_scanners import Secrets

            _scanners["secrets"] = Secrets(redact_mode="all")
        return _scanners["secrets"]


def is_injection(text: str) -> bool:
    """True if the classifier judges `text` to contain instructions aimed at an
    LLM. False when disabled — callers keep their pattern checks regardless."""
    if not enabled() or not text or not text.strip():
        return False
    text = _for_classifier(text)
    if not text.replace("...", "").strip():
        return False
    scanner = _injection()
    for window in windows(text):
        with _lock:
            _, valid, _ = scanner.scan(window)
        if not valid:
            return True
    return False


def mask_secrets(text: str) -> str:
    """detect-secrets' rules (cloud keys, tokens, high-entropy strings)."""
    if not enabled() or not text:
        return text
    scanner = _secrets()
    with _lock:
        masked, _, _ = scanner.scan(text)
    return masked
