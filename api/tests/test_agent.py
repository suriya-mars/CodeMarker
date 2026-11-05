"""Agent navigation-loop tests with llm.py and the graph mocked out — verifies
the tool-use protocol (results first, ids matched) and lit-path extraction."""

import agent
import llm
import tools


def test_ask_traces_and_cites(monkeypatch):
    # Scripted model: turn 1 calls find_symbol, turn 2 answers citing the id.
    responses = iter(
        [
            llm.LLMResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "find_symbol",
                        "input": {"query": "login"},
                    }
                ],
                stop_reason="tool_use",
            ),
            llm.LLMResponse(
                content=[
                    {
                        "type": "text",
                        "text": "Login is handled by `auth.AuthService.login`.",
                    }
                ],
                stop_reason="end_turn",
            ),
        ]
    )

    captured = {}

    def fake_create_message(*, system, messages, tools, model, max_tokens=2048):
        captured["messages"] = list(messages)  # snapshot — the loop mutates it
        return next(responses)

    def fake_run_tool(name, tool_input):
        assert name == "find_symbol"
        return [{"id": "auth.AuthService.login", "name": "login", "summary": "logs a user in"}]

    monkeypatch.setattr(llm, "create_message", fake_create_message)
    monkeypatch.setattr(tools, "run_tool", fake_run_tool)

    result = agent.ask("how does login work?")

    assert "auth.AuthService.login" in result["answer"]
    assert result["cited_ids"] == ["auth.AuthService.login"]

    # protocol check: the user turn that follows the tool call has the
    # tool_result block FIRST, matching the tool_use id.
    user_turn = captured["messages"][-1]
    assert user_turn["role"] == "user"
    first_block = user_turn["content"][0]
    assert first_block["type"] == "tool_result"
    assert first_block["tool_use_id"] == "tu_1"


def test_unknown_tool_returns_error_block(monkeypatch):
    responses = iter(
        [
            llm.LLMResponse(
                content=[
                    {"type": "tool_use", "id": "tu_x", "name": "nope", "input": {}}
                ],
                stop_reason="tool_use",
            ),
            llm.LLMResponse(
                content=[{"type": "text", "text": "done"}], stop_reason="end_turn"
            ),
        ]
    )
    captured = {}

    def fake_create_message(*, system, messages, tools, model, max_tokens=2048):
        captured["messages"] = list(messages)  # snapshot — the loop mutates it
        return next(responses)

    monkeypatch.setattr(llm, "create_message", fake_create_message)

    result = agent.ask("trigger an unknown tool")
    assert result["answer"] == "done"

    user_turn = captured["messages"][-1]
    err_block = user_turn["content"][0]
    assert err_block["type"] == "tool_result"
    assert err_block.get("is_error") is True
    assert err_block["tool_use_id"] == "tu_x"


def test_cited_ids_match_whole_tokens_and_resolve_unambiguous_names():
    seen = {"auth", "auth.AuthService.login", "src.base.Selection", "a.main", "b.main"}
    answer = (
        "Authentication starts in auth.AuthService.login. Results land in `Selection`; "
        "see `main` for setup.\n```python\nSelection()\n```"
    )
    # "auth" is not cited by "Authentication"; `main` has two candidates, so it is dropped
    assert agent.cited_ids(answer, seen) == ["auth.AuthService.login", "src.base.Selection"]
    assert agent.cited_ids("`base.Selection` then `login()`", seen) == [
        "auth.AuthService.login", "src.base.Selection",
    ]
    assert agent.cited_ids("the `auth` module", seen) == ["auth"]
    assert agent.cited_ids("`src/base.py` and `Nonexistent`", seen) == []
    assert agent.cited_ids("Sentence ends at auth.AuthService.login.", seen) == [
        "auth.AuthService.login",
    ]
