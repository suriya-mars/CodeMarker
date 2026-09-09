"""Token, cost, and latency accounting for every LLM call.

This is the observability gap that actually hurts today: the system makes tens of
thousands of API calls per index and has **no idea what it spends**. Before
dashboards, before tracing, you want to be able to answer "what did that cost?"

Design notes:
  * Events are emitted as one-line JSON with OpenTelemetry-style attribute names
    (`gen_ai.*`), so wiring an OTel exporter later is a drop-in — no call-site
    changes.
  * Nothing leaves the process. Prompts and completions are NEVER recorded, only
    counts. CLAUDE.md §2.3 forbids third-party egress of source, and prompts here
    contain customer code (see the note on Langfuse in PRODUCTION.md).
  * Recording must never break a request: every entry point swallows its own
    errors.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field

# USD per 1M tokens (input, output). Standard rates — deliberately not the
# promotional ones, so estimates stay conservative.
#   Anthropic: docs.claude.com pricing
#   OpenAI:    developers.openai.com/api/docs/pricing  (verified 2026-08-31)
# Override or extend without editing code:
#   TELEMETRY_PRICING='{"my-model": [0.5, 1.5]}'
PRICING: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    # OpenAI
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
}
_DEFAULT_PRICE = (3.00, 15.00)

try:  # user-supplied rates win, so an unlisted model still reports real cost
    PRICING.update(
        {k: (float(v[0]), float(v[1]))
         for k, v in json.loads(os.environ.get("TELEMETRY_PRICING", "{}")).items()}
    )
except Exception:  # noqa: BLE001 — a bad override must not break startup
    print("[telemetry] ignoring malformed TELEMETRY_PRICING", flush=True)

_warned_unpriced: set[str] = set()

_LOG = os.environ.get("TELEMETRY_LOG", "1") != "0"


@dataclass
class Totals:
    calls: int = 0
    errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    by_model: dict[str, dict[str, float]] = field(default_factory=dict)
    by_operation: dict[str, dict[str, float]] = field(default_factory=dict)


_lock = threading.Lock()
_totals = Totals()


def cost_of(model: str, input_tokens: int, output_tokens: int) -> float:
    """Cost in USD. Warns ONCE per unknown model — a silently wrong cost figure
    is worse than no figure, because it gets quoted in budget conversations."""
    price = PRICING.get(model)
    if price is None:
        price = _DEFAULT_PRICE
        if model and model not in _warned_unpriced:
            _warned_unpriced.add(model)
            print(
                f"[telemetry] no published rate for {model!r}; using "
                f"${_DEFAULT_PRICE[0]}/${_DEFAULT_PRICE[1]} per 1M as a PLACEHOLDER. "
                f"Set TELEMETRY_PRICING to report real cost.",
                flush=True,
            )
    inp, out = price
    return (input_tokens * inp + output_tokens * out) / 1_000_000


def active_model() -> str:
    """The model this deployment will actually bill against, for projections."""
    if os.environ.get("LLM_PROVIDER", "anthropic").lower() == "openai":
        return os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    return "claude-haiku-4-5"


def record(
    *,
    model: str,
    operation: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    seconds: float = 0.0,
    error: str | None = None,
) -> None:
    """Record one LLM call. Never raises."""
    try:
        cost = cost_of(model, input_tokens, output_tokens)
        with _lock:
            _totals.calls += 1
            _totals.errors += 1 if error else 0
            _totals.input_tokens += input_tokens
            _totals.output_tokens += output_tokens
            _totals.cache_read_tokens += cache_read_tokens
            _totals.cost_usd += cost
            _totals.seconds += seconds
            for bucket, key in ((_totals.by_model, model), (_totals.by_operation, operation)):
                slot = bucket.setdefault(key, {"calls": 0, "cost_usd": 0.0, "tokens": 0})
                slot["calls"] += 1
                slot["cost_usd"] += cost
                slot["tokens"] += input_tokens + output_tokens
        if _LOG:
            print(
                json.dumps({
                    "event": "gen_ai.call",
                    "gen_ai.system": "anthropic",
                    "gen_ai.request.model": model,
                    "gen_ai.operation.name": operation,
                    "gen_ai.usage.input_tokens": input_tokens,
                    "gen_ai.usage.output_tokens": output_tokens,
                    "gen_ai.usage.cache_read_input_tokens": cache_read_tokens,
                    "cost.usd": round(cost, 6),
                    "duration.s": round(seconds, 3),
                    "error": error,
                }),
                flush=True,
            )
    except Exception:  # noqa: BLE001 — telemetry must never break a request
        pass


def snapshot() -> dict:
    with _lock:
        return {
            "calls": _totals.calls,
            "errors": _totals.errors,
            "input_tokens": _totals.input_tokens,
            "output_tokens": _totals.output_tokens,
            "cache_read_tokens": _totals.cache_read_tokens,
            "cost_usd": round(_totals.cost_usd, 4),
            "seconds": round(_totals.seconds, 2),
            "by_model": {k: dict(v) for k, v in _totals.by_model.items()},
            "by_operation": {k: dict(v) for k, v in _totals.by_operation.items()},
        }


def reset() -> None:
    global _totals
    with _lock:
        _totals = Totals()


class track:
    """Context manager timing one LLM call and recording it on exit.

        with track(model, "summarize") as t:
            resp = client.messages.create(...)
            t.usage(resp.usage.input_tokens, resp.usage.output_tokens)
    """

    def __init__(self, model: str, operation: str):
        self.model, self.operation = model, operation
        self._in = self._out = self._cache = 0
        self._t0 = 0.0

    def usage(self, input_tokens: int, output_tokens: int, cache_read: int = 0) -> None:
        self._in, self._out, self._cache = input_tokens, output_tokens, cache_read

    def __enter__(self) -> track:
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        record(
            model=self.model,
            operation=self.operation,
            input_tokens=self._in,
            output_tokens=self._out,
            cache_read_tokens=self._cache,
            seconds=time.perf_counter() - self._t0,
            error=f"{exc_type.__name__}" if exc_type else None,
        )
        return False
