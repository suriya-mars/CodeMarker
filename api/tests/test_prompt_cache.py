"""Prompt caching: the fixed prefix is marked for the provider to cache, token
counts mean the same with or without the cache, and cost reflects cached prices.

No network: the provider clients are replaced with recorders.
"""

from types import SimpleNamespace

import pytest

import agent
import impact
import llm
import telemetry
import tools

SYSTEM = "You navigate a code graph."
TOOLS = [{"name": "find_symbol", "description": "d", "input_schema": {"type": "object"}}]


class FakeAnthropic:
    """Records each request; answers with a fixed usage."""

    def __init__(self, usage=None, stop="end_turn"):
        self.calls = []
        self.usage = usage or SimpleNamespace(input_tokens=10, output_tokens=5,
                                              cache_read_input_tokens=0,
                                              cache_creation_input_tokens=0)
        self.stop = stop
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")],
                               stop_reason=self.stop, usage=self.usage)


@pytest.fixture
def anthropic(monkeypatch):
    fake = FakeAnthropic()
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(llm, "_anthropic", lambda: fake)
    return fake


def _send(messages, tools=TOOLS, model="claude-sonnet-4-6"):
    return llm.create_message(system=SYSTEM, messages=messages, tools=tools, model=model)


# --- where the marks go -----------------------------------------------------


def test_tool_steps_mark_the_shared_prefix_and_the_conversation_end(anthropic):
    _send([{"role": "user", "content": "how does login work?"}])
    sent = anthropic.calls[0]
    # tools render before system, so this one mark caches both
    assert sent["system"] == [{"type": "text", "text": SYSTEM,
                               "cache_control": {"type": "ephemeral"}}]
    assert sent["tools"] == TOOLS
    assert sent["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_only_the_last_block_is_marked_and_the_conversation_is_not_changed(anthropic):
    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1",
                                           "name": "find_symbol", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "a"},
            {"type": "text", "text": "1 call left"},
        ]},
    ]
    before = repr(history)
    _send(history)
    sent = anthropic.calls[0]["messages"]
    marks = [b for m in sent if isinstance(m["content"], list)
             for b in m["content"] if "cache_control" in b]
    assert marks == [sent[-1]["content"][-1]]
    assert sent[-1]["content"][0]["type"] == "tool_result"  # protocol order kept
    assert repr(history) == before  # a mark left behind would move the cache point


def test_the_final_answer_without_tools_writes_no_cache(anthropic):
    """It cannot read the tool steps' cache, and nothing reads its own."""
    _send([{"role": "user", "content": "q"}], tools=[], model="claude-opus-4-8")
    sent = anthropic.calls[0]
    assert sent["system"] == SYSTEM
    assert "tools" not in sent
    assert all("cache_control" not in str(m) for m in sent["messages"])


def test_caching_can_be_switched_off(anthropic, monkeypatch):
    monkeypatch.setenv("LLM_PROMPT_CACHE", "0")
    _send([{"role": "user", "content": "q"}])
    assert anthropic.calls[0]["system"] == SYSTEM


def test_each_agent_step_starts_with_the_previous_steps_bytes(anthropic, monkeypatch):
    """The cache is a prefix match. Step N+1 must begin with exactly what step N
    sent (apart from where the mark sits), or every step pays full price."""
    replies = iter([
        SimpleNamespace(content=[SimpleNamespace(type="tool_use", id=f"t{i}",
                                                 name="find_symbol", input={"query": f"x{i}"})],
                        stop_reason="tool_use", usage=anthropic.usage)
        for i in range(3)
    ] + [SimpleNamespace(content=[SimpleNamespace(type="text", text="done")],
                         stop_reason="end_turn", usage=anthropic.usage)])
    anthropic.create = lambda **kw: anthropic.calls.append(kw) or next(replies)
    monkeypatch.setattr(tools, "run_tool", lambda name, args: {"hits": []})
    agent.ask("how does login work?", max_steps=6)

    def unmarked(messages):
        return [{**m, "content": [{k: v for k, v in b.items() if k != "cache_control"}
                                  for b in m["content"]]} for m in messages]

    calls = anthropic.calls
    assert len(calls) == 4
    for earlier, later in zip(calls, calls[1:], strict=False):
        assert later["system"] == earlier["system"]
        assert later["tools"] == earlier["tools"]
        n = len(earlier["messages"])
        assert unmarked(later["messages"][:n]) == unmarked(earlier["messages"])


def test_the_impact_system_prompt_is_the_same_on_every_call(monkeypatch):
    seen = []

    def fake(*, system, messages, tools, model, max_tokens=2048):
        seen.append(system)
        return llm.LLMResponse(content=[{"type": "text", "text": "x"}], stop_reason="end_turn")

    monkeypatch.setattr(llm, "create_message", fake)
    report = impact.ImpactReport(
        id="r", repo="svc", base="a" * 40, head="b" * 40, created_at=0.0, files=[],
        changed=[], affected=[], importers=[], communities=[],
        risk={"level": "low", "score": 0, "flags": [], "affected_pages": 0, "affected_tests": 0},
        warnings=[])
    impact.ask(report, "one")
    impact.ask(report, "two")
    assert seen[0] == seen[1] == impact.IMPACT_SYSTEM_PROMPT


# --- counting -----------------------------------------------------------------


def test_anthropic_cached_input_still_counts_toward_budgets(anthropic):
    """Anthropic reports only the uncached tail as input_tokens. Counting just that
    would let a cached question run far past AGENT_TOKEN_BUDGET and the quota."""
    anthropic.usage = SimpleNamespace(input_tokens=200, output_tokens=50,
                                      cache_read_input_tokens=1600,
                                      cache_creation_input_tokens=300)
    resp = _send([{"role": "user", "content": "q"}])
    assert resp.input_tokens == 2100
    assert (resp.cache_read_tokens, resp.cache_write_tokens) == (1600, 300)


def test_openai_cached_tokens_are_read(monkeypatch):
    usage = SimpleNamespace(prompt_tokens=1800, completion_tokens=40,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=1536))
    message = SimpleNamespace(content="ok", tool_calls=None)
    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=usage))))
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    monkeypatch.setattr(llm, "_openai", lambda: fake)
    resp = _send([{"role": "user", "content": "q"}])
    assert (resp.input_tokens, resp.cache_read_tokens, resp.cache_write_tokens) == (1800, 1536, 0)
    # 264 plain at $0.15/M + 1536 cached at $0.075/M + 40 out at $0.60/M
    assert resp.cost_usd == pytest.approx((264 * 0.15 + 1536 * 0.075 + 40 * 0.60) / 1e6)


def test_openai_without_cache_details_counts_nothing_as_cached():
    usage = SimpleNamespace(prompt_tokens=900, completion_tokens=10, prompt_tokens_details=None)
    assert llm._usage(SimpleNamespace(usage=usage)) == (900, 10, 0, 0)


def test_agent_reports_what_the_cache_served(anthropic):
    anthropic.usage = SimpleNamespace(input_tokens=100, output_tokens=20,
                                      cache_read_input_tokens=1500,
                                      cache_creation_input_tokens=0)
    out = agent.ask("q", max_steps=2)
    assert out["usage"]["input_tokens"] == 1600
    assert out["usage"]["cache_read_tokens"] == 1500


# --- pricing ------------------------------------------------------------------


def test_claude_cache_reads_cost_a_tenth_and_writes_a_quarter_more():
    full = telemetry.cost_of("claude-sonnet-4-6", 1_000_000, 0)
    assert telemetry.cost_of("claude-sonnet-4-6", 1_000_000, 0,
                             cache_read=1_000_000) == pytest.approx(full * 0.1)
    assert telemetry.cost_of("claude-sonnet-4-6", 1_000_000, 0,
                             cache_write=1_000_000) == pytest.approx(full * 1.25)


def test_a_model_without_a_listed_cached_price_assumes_the_smallest_discount():
    assert telemetry.cost_of("gpt-4o", 1_000_000, 0, cache_read=1_000_000) == pytest.approx(1.25)
    telemetry.PRICING["some-new-model"] = (1.0, 2.0)
    try:
        assert telemetry.cost_of("some-new-model", 1_000_000, 0,
                                 cache_read=1_000_000) == pytest.approx(0.5)
    finally:
        del telemetry.PRICING["some-new-model"]


def test_cost_without_cache_arguments_is_unchanged():
    assert telemetry.cost_of("claude-haiku-4-5", 1_000_000, 1_000_000) == 6.00


def test_usage_totals_include_cache_counts():
    telemetry.reset()
    telemetry.record(model="claude-sonnet-4-6", operation="navigate", input_tokens=2000,
                     output_tokens=10, cache_read_tokens=1500, cache_write_tokens=0)
    snap = telemetry.snapshot()
    assert (snap["cache_read_tokens"], snap["cache_write_tokens"]) == (1500, 0)
    telemetry.reset()
