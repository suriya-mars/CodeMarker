"""LLM-generated one-line summaries for Symbols and Communities.

Summaries are the semantic bridge: `find_symbol` searches the full-text index
over [summary, name], so a query like "verify the user is allowed in" can match
a function named `_authz_gate`. Without summaries, lexical search over raw code
is weak — so don't skip this step. We use the cheapest model (Haiku) for it.
"""

from __future__ import annotations

import llm
from parsing import Symbol

_MAX_SOURCE_CHARS = 2000


def summarize_symbol(symbol: Symbol) -> str | None:
    """One natural-language line describing what a Symbol does.

    Returns None on failure rather than falling back to the symbol name. The
    fallback looked harmless but silently poisoned the search index: a
    rate-limited symbol would get `summary = name` forever, and — because the
    resume query looks for symbols WITHOUT a summary — it would never be retried.
    None leaves it pending so the next pass picks it up.
    """
    if symbol.kind == "file":
        prompt = (
            f"This is a {symbol.language} file named `{symbol.name}`.\n"
            "Describe in ONE concise sentence what this file/module is responsible for. "
            "No preamble, just the sentence.\n\n"
            f"```\n{symbol.source[:_MAX_SOURCE_CHARS]}\n```"
        )
    else:
        header = symbol.signature or symbol.name
        prompt = (
            f"This is a {symbol.language} {symbol.kind} `{header}`.\n"
            "Describe in ONE concise sentence what it does — focus on behavior and "
            "intent, not syntax. No preamble, just the sentence.\n\n"
            f"```\n{symbol.source[:_MAX_SOURCE_CHARS]}\n```"
        )
    try:
        return llm.complete(prompt, model=llm.SUMMARY_MODEL, max_tokens=120)
    except Exception as exc:  # noqa: BLE001 — one failure must not stop the run
        print(f"[summarize] FAILED {symbol.id}: {exc}")
        return None


def summarize_community(name: str, member_summaries: list[str]) -> str:
    """One line describing an architectural community from its members' summaries."""
    joined = "\n".join(f"- {s}" for s in member_summaries[:40])
    prompt = (
        "The following one-line summaries belong to one cluster of related code "
        "(functions, classes, files) in a codebase:\n\n"
        f"{joined}\n\n"
        "In ONE concise sentence, describe the architectural responsibility this "
        "cluster represents. No preamble, just the sentence."
    )
    try:
        return llm.complete(prompt, model=llm.SUMMARY_MODEL, max_tokens=120)
    except Exception as exc:  # noqa: BLE001
        print(f"[summarize community] {name}: {exc}")
        return name
