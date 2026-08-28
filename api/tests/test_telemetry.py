"""Cost/token accounting tests.

The point of this module is answering "what is this costing us?" — the one
observability question the system could not answer at all. It records COUNTS
only: prompts carry customer source and must never be exported (CLAUDE.md §2.3).
"""

import json

import telemetry


def setup_function():
    telemetry.reset()


def test_cost_matches_published_rates():
    # Haiku 4.5: $1.00 in / $5.00 out per 1M tokens
    assert telemetry.cost_of("claude-haiku-4-5", 1_000_000, 0) == 1.00
    assert telemetry.cost_of("claude-haiku-4-5", 0, 1_000_000) == 5.00
    assert telemetry.cost_of("claude-opus-5", 1_000_000, 1_000_000) == 30.00
    # unknown models fall back rather than crashing or reporting zero
    assert telemetry.cost_of("some-future-model", 1_000_000, 0) > 0


def test_index_run_cost_projection():
    """125k symbols at ~600 in / 40 out on Haiku should land near $100."""
    total = 125_000 * telemetry.cost_of("claude-haiku-4-5", 600, 40)
    assert 90 < total < 110


def test_totals_accumulate_and_split_by_model_and_operation():
    telemetry.record(model="claude-haiku-4-5", operation="complete",
                     input_tokens=600, output_tokens=40, seconds=0.4)
    telemetry.record(model="claude-sonnet-5", operation="navigate",
                     input_tokens=8_000, output_tokens=300, seconds=2.0)
    snap = telemetry.snapshot()
    assert snap["calls"] == 2 and snap["errors"] == 0
    assert snap["input_tokens"] == 8_600
    assert set(snap["by_model"]) == {"claude-haiku-4-5", "claude-sonnet-5"}
    assert set(snap["by_operation"]) == {"complete", "navigate"}
    assert snap["by_operation"]["navigate"]["cost_usd"] > snap["by_operation"]["complete"]["cost_usd"]


def test_track_records_duration_and_errors():
    with telemetry.track("claude-sonnet-5", "navigate") as t:
        t.usage(100, 10)
    assert telemetry.snapshot()["calls"] == 1

    try:
        with telemetry.track("claude-sonnet-5", "navigate") as t:
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    snap = telemetry.snapshot()
    assert snap["calls"] == 2 and snap["errors"] == 1


def test_recording_never_raises():
    """Telemetry failing must never take a request down with it."""
    telemetry.record(model=None, operation=None, input_tokens="not-an-int")  # type: ignore[arg-type]
    assert telemetry.snapshot()["calls"] in (0, 1)


def test_emitted_events_are_otel_shaped_json(capsys):
    telemetry.record(model="claude-haiku-4-5", operation="complete",
                     input_tokens=10, output_tokens=2, seconds=0.1)
    line = capsys.readouterr().out.strip().splitlines()[-1]
    event = json.loads(line)
    assert event["gen_ai.request.model"] == "claude-haiku-4-5"
    assert event["gen_ai.usage.input_tokens"] == 10
    assert "cost.usd" in event
    # counts only — no prompt or completion content may ever be emitted
    assert not any("prompt" in k or "content" in k or "message" in k for k in event)
