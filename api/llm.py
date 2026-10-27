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
    return _ASSIGNED_SECRET.sub(lambda m: m.group(1) + REDACTED, text)


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


def _usage(resp) -> tuple[int, int]:
    u = getattr(resp, "usage", None)
    if u is None:
        return 0, 0
    return (getattr(u, "input_tokens", None) or getattr(u, "prompt_tokens", 0) or 0,
            getattr(u, "output_tokens", None) or getattr(u, "completion_tokens", 0) or 0)


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
        u = getattr(resp, "usage", None)
        if u is None:
            return
        tracker.usage(
            getattr(u, "input_tokens", None) or getattr(u, "prompt_tokens", 0) or 0,
            getattr(u, "output_tokens", None) or getattr(u, "completion_tokens", 0) or 0,
            getattr(u, "cache_read_input_tokens", 0) or 0,
        )
    except Exception:  # noqa: BLE001 — accounting must never break a call
        pass


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
    tokens_in, tokens_out = _usage(resp)
    return LLMResponse(content=content, stop_reason=resp.stop_reason,
                       input_tokens=tokens_in, output_tokens=tokens_out)


def _create_message_openai(
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_tokens: int,
) -> LLMResponse:
    """Translate the Anthropic-shaped conversation to OpenAI chat format, call,
    then translate the response back to Anthropic blocks.
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
    tokens_in, tokens_out = _usage(resp)
    return LLMResponse(content=content, stop_reason=stop_reason,
                       input_tokens=tokens_in, output_tokens=tokens_out)


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
