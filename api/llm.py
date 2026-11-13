"""Provider abstraction for LLM access. THE ONLY place `anthropic` / `openai`
are imported.

Provider is chosen by the `LLM_PROVIDER` env var (`anthropic` | `openai`).

Two surfaces:
  - `complete(...)`       → plain text. Used for index-time summaries.
  - `create_message(...)` → a tool-use turn, normalized to Anthropic's
                            content-block shape so the agent loop (agent.py) is
                            written once against that protocol regardless of
                            provider.

Model roles (see CLAUDE.md §6):
  - NAV_MODEL     : graph navigation (cheap, agentic)
  - SYNTH_MODEL   : forced final synthesis when the step cap is hit
  - SUMMARY_MODEL : index-time one-line summaries (cheapest)
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

import guard
import telemetry
import untrusted

# --- Secret redaction at the egress point -------------------------------------
# Every byte that leaves the machine passes through this module (CLAUDE.md §2.3),
# so this is the one place that can guarantee credentials committed into a repo
# are never sent to the provider — whether they arrive in a summary prompt, a
# get_definition tool result, or an impact diff. Patterns are deliberately
# specific (known key formats, quoted secret assignments) so ordinary code is
# left alone; the graph itself keeps the original source.
_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),                      # AWS access key id
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"),               # Anthropic / OpenAI
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{22,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),                       # Slack
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),                           # Google API key
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
]
# `password = "hunter2hunter2"` / `"apiKey": "..."` / `Password=...;` in a
# connection string: keep the name, mask the value.
_ASSIGNED_SECRET = re.compile(
    r"(?i)((?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|"
    r"client[_-]?secret|private[_-]?key)[\"']?\s*[:=]\s*)"
    r"(\"[^\"\s]{6,}\"|'[^'\s]{6,}'|[^\s;\"',)]{6,}(?=;))"
)
REDACTED = "[REDACTED]"


def redact(text: str) -> str:
    """Mask credentials, and drop invisible characters, in text bound for an
    LLM provider. Invisible characters (zero-width, bidi overrides, Unicode tag
    smuggling) carry nothing a legitimate prompt needs, and are exactly how an
    instruction hides from a human reviewer while the model still reads it."""
    if not text:
        return text
    text = untrusted.strip_invisible(text)
    for pat in _SECRET_PATTERNS:
        text = pat.sub(REDACTED, text)
    text = _ASSIGNED_SECRET.sub(lambda m: m.group(1) + REDACTED, text)
    # detect-secrets' maintained rules catch formats our list does not; ours
    # catch quoted assignments it misses. Both run.
    return guard.mask_secrets(text)


def _redact_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Redacted COPIES — the caller's conversation (agent.py) is left intact."""
    out = []
    for msg in messages:
        content = msg["content"]
        if isinstance(content, str):
            out.append({**msg, "content": redact(content)})
            continue
        blocks = []
        for b in content:
            if b.get("type") == "text":
                b = {**b, "text": redact(b["text"])}
            elif b.get("type") == "tool_result" and isinstance(b.get("content"), str):
                b = {**b, "content": redact(b["content"])}
            blocks.append(b)
        out.append({**msg, "content": blocks})
    return out

NAV_MODEL = "claude-sonnet-4-6"
SYNTH_MODEL = "claude-opus-4-8"
SUMMARY_MODEL = "claude-haiku-4-5-20251001"


def summary_model() -> str:
    """The model that actually writes index-time summaries under this provider."""
    if _provider() == "openai":
        return os.environ.get("OPENAI_MODEL", "")
    return SUMMARY_MODEL


def _provider() -> str:
    return os.environ.get("LLM_PROVIDER", "anthropic").lower()


# --- Normalized response shape ---------------------------------------------


@dataclass
class LLMResponse:
    """Normalized tool-use response. `content` is a list of Anthropic-style
    blocks: {"type": "text", "text": ...} or
            {"type": "tool_use", "id": ..., "name": ..., "input": {...}}.
    `stop_reason` is normalized to Anthropic values ("tool_use" | "end_turn").
    """

    content: list[dict[str, Any]]
    stop_reason: str
    # Token counts for THIS call, so the agent can enforce a per-request budget
    # and callers can charge a per-user quota. 0 when the provider omits usage.
    input_tokens: int = 0
    output_tokens: int = 0
    # The model that actually ran (OPENAI_MODEL under the openai provider, not
    # the Claude name the caller asked for) and its estimated cost — priced
    # here, once, from the same table as /usage, so per-user cost and the
    # process totals can never disagree.
    model: str = ""
    cost_usd: float = 0.0
    # How much of input_tokens was read from, or written to, the provider's
    # prompt cache. input_tokens always counts ALL input, cached or not.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def text(self) -> str:
        return "".join(b["text"] for b in self.content if b.get("type") == "text")

    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]


# --- Lazy singleton clients -------------------------------------------------

_anthropic_client = None
_openai_client = None


def _anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic

        # A hung or very slow call must not hold a request open indefinitely.
        _anthropic_client = anthropic.Anthropic(timeout=_timeout(), max_retries=2)
    return _anthropic_client


def _openai():
    global _openai_client
    if _openai_client is None:
        import openai

        _openai_client = openai.OpenAI(timeout=_timeout(), max_retries=2)
    return _openai_client


def _timeout() -> float:
    """Seconds one provider call may take before the SDK abandons it."""
    return float(os.environ.get("LLM_TIMEOUT_S", "60"))


def _usage(resp) -> tuple[int, int, int, int]:
    """(all input, output, cache read, cache write), the same on both providers.

    The providers count differently. Anthropic's `input_tokens` is only the input
    AFTER the last cache breakpoint, with cached input reported separately, so it
    is added back here: otherwise turning the cache on would quietly shrink every
    token budget and quota check. OpenAI's `prompt_tokens` already includes cached
    input, and it charges nothing extra to write the cache.
    """
    u = getattr(resp, "usage", None)
    if u is None:
        return 0, 0, 0, 0
    if getattr(u, "prompt_tokens", None) is not None:  # OpenAI
        details = getattr(u, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) or 0
        return int(u.prompt_tokens or 0), int(getattr(u, "completion_tokens", 0) or 0), cached, 0
    read = int(getattr(u, "cache_read_input_tokens", 0) or 0)
    write = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
    return (int(getattr(u, "input_tokens", 0) or 0) + read + write,
            int(getattr(u, "output_tokens", 0) or 0), read, write)


def _openai_model() -> str:
    model = os.environ.get("OPENAI_MODEL")
    if not model:
        raise RuntimeError(
            "LLM_PROVIDER=openai requires OPENAI_MODEL to be set in the environment."
        )
    return model


def _report(tracker, resp) -> None:
    """Feed a provider response's usage into telemetry.

    Only COUNTS are recorded — never prompts or completions. Prompts here carry
    customer source, and CLAUDE.md §2.3 permits no third-party egress of it.
    """
    try:
        tracker.usage(*_usage(resp))
    except Exception:  # noqa: BLE001 — accounting must never break a call
        pass


# --- Prompt caching (Anthropic) -----------------------------------------------
# The agent loop sends the same tools and system prompt on every step of every
# question, and each step resends the whole conversation so far. Anthropic caches
# a prompt prefix only where it is marked, and then reads it at about a tenth of the
# input price. Two marks:
#   * the system prompt: covers tools + system, shared by every user's questions
#   * the last block of the conversation: the next step of THIS question reads
#     everything before its new tool results from the cache
# Nothing is shared between users except the tools and system prompt, which are
# the same for everyone. OpenAI caches automatically and needs no marks.
_CACHE = {"type": "ephemeral"}


def _prompt_cache() -> bool:
    return os.environ.get("LLM_PROMPT_CACHE", "1") != "0"


def _with_cache_marks(system: str, messages: list[dict[str, Any]]):
    """Copies of system and messages with the two cache marks. The caller's
    conversation is not modified: a mark left behind would move the cache point.

    Every message goes out in block form. The question is the last message on the
    first step, so it carries the mark and must be a block; on later steps it would
    otherwise go out as a plain string, and the prefix would no longer match."""
    marked_system = [{"type": "text", "text": system, "cache_control": _CACHE}]
    out = [{**m, "content": ([{"type": "text", "text": m["content"]}]
                             if isinstance(m["content"], str)
                             else [dict(b) for b in m["content"]])}
           for m in messages]
    if out and out[-1]["content"]:
        out[-1]["content"][-1]["cache_control"] = _CACHE
    return marked_system, out


# --- Public surface ---------------------------------------------------------


def complete(prompt: str, *, model: str, max_tokens: int = 256, system: str | None = None) -> str:
    """Single-shot text completion. Used for index-time summaries."""
    prompt = redact(prompt)
    if _provider() == "openai":
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        with telemetry.track(_openai_model(), "complete") as t:
            resp = _openai().chat.completions.create(
                model=_openai_model(),
                max_tokens=max_tokens,
                messages=messages,
            )
            _report(t, resp)
        return (resp.choices[0].message.content or "").strip()

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    with telemetry.track(model, "complete") as t:
        resp = _anthropic().messages.create(**kwargs)
        _report(t, resp)
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def create_message(
    *,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    model: str,
    max_tokens: int = 2048,
) -> LLMResponse:
    """One tool-use turn. `messages` and `tools` are in Anthropic shape; for
    OpenAI we translate in and out. Returns the normalized LLMResponse.
    """
    messages = _redact_messages(messages)
    if _provider() == "openai":
        return _create_message_openai(system, messages, tools, max_tokens)
    return _create_message_anthropic(system, messages, tools, model, max_tokens)


def _create_message_anthropic(
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    model: str,
    max_tokens: int,
) -> LLMResponse:
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }
    if tools:  # omit when empty so the model is forced to answer in text
        kwargs["tools"] = tools
        # Only the tool-using steps are marked. The final answer without tools has
        # a different prefix and often a different model, so it can read no cache
        # entry, and a cache write there is paid for and never used.
        if _prompt_cache():
            kwargs["system"], kwargs["messages"] = _with_cache_marks(system, messages)
    with telemetry.track(model, "navigate" if tools else "synthesize") as t:
        resp = _anthropic().messages.create(**kwargs)
        _report(t, resp)
    content: list[dict[str, Any]] = []
    for block in resp.content:
        if block.type == "text":
            content.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            content.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
            )
    tokens_in, tokens_out, read, write = _usage(resp)
    return LLMResponse(content=content, stop_reason=resp.stop_reason,
                       input_tokens=tokens_in, output_tokens=tokens_out, model=model,
                       cache_read_tokens=read, cache_write_tokens=write,
                       cost_usd=telemetry.cost_of(model, tokens_in, tokens_out,
                                                  cache_read=read, cache_write=write))


def _create_message_openai(
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_tokens: int,
) -> LLMResponse:
    """Translate the Anthropic-shaped conversation to OpenAI chat format, call,
    then translate the response back to Anthropic blocks.

    OpenAI caches a repeated prompt prefix by itself (1,024 tokens or more). The
    system prompt and tools come first and never change, so they are reused.
    """
    oai_messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for msg in messages:
        oai_messages.extend(_to_openai_messages(msg))

    oai_kwargs: dict[str, Any] = {
        "model": _openai_model(),
        "max_tokens": max_tokens,
        "messages": oai_messages,
    }
    if tools:
        oai_kwargs["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            }
            for t in tools
        ]

    with telemetry.track(_openai_model(), "navigate" if tools else "synthesize") as t:
        resp = _openai().chat.completions.create(**oai_kwargs)
        _report(t, resp)
    choice = resp.choices[0]
    content: list[dict[str, Any]] = []
    if choice.message.content:
        content.append({"type": "text", "text": choice.message.content})
    for call in choice.message.tool_calls or []:
        content.append(
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.function.name,
                "input": json.loads(call.function.arguments or "{}"),
            }
        )
    stop_reason = "tool_use" if choice.finish_reason == "tool_calls" else "end_turn"
    tokens_in, tokens_out, read, write = _usage(resp)
    used = _openai_model()
    return LLMResponse(content=content, stop_reason=stop_reason,
                       input_tokens=tokens_in, output_tokens=tokens_out, model=used,
                       cache_read_tokens=read, cache_write_tokens=write,
                       cost_usd=telemetry.cost_of(used, tokens_in, tokens_out,
                                                  cache_read=read, cache_write=write))


def _to_openai_messages(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert one Anthropic-shaped message to one-or-more OpenAI messages.

    Anthropic packs an assistant's text + tool_use blocks into one message, and
    packs all tool_results into one user message. OpenAI wants the assistant
    turn to carry tool_calls, and each tool result to be its own `tool` message.
    """
    role = msg["role"]
    content = msg["content"]

    if isinstance(content, str):
        return [{"role": role, "content": content}]

    if role == "assistant":
        text_parts = [b["text"] for b in content if b.get("type") == "text"]
        tool_calls = [
            {
                "id": b["id"],
                "type": "function",
                "function": {"name": b["name"], "arguments": json.dumps(b["input"])},
            }
            for b in content
            if b.get("type") == "tool_use"
        ]
        out: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
        if tool_calls:
            out["tool_calls"] = tool_calls
        return [out]

    # user turn: may contain tool_result blocks and/or text
    out_msgs: list[dict[str, Any]] = []
    text_parts = []
    for b in content:
        if b.get("type") == "tool_result":
            result = b.get("content", "")
            if isinstance(result, list):
                result = "".join(p.get("text", "") for p in result if isinstance(p, dict))
            out_msgs.append(
                {"role": "tool", "tool_call_id": b["tool_use_id"], "content": result}
            )
        elif b.get("type") == "text":
            text_parts.append(b["text"])
    if text_parts:
        out_msgs.append({"role": "user", "content": "".join(text_parts)})
    return out_msgs
