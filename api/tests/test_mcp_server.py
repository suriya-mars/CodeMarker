"""MCP server tests.

The server is the surface teammates actually touch, so the guarantee that matters
is capability-based: it exposes read tools ONLY, and refuses to start otherwise.
That is stronger than a policy layer — there is no write path to police, so no
prompt can produce one.
"""

import asyncio

import pytest

import graph
import mcp_server
import tools


def _list(server):
    return asyncio.run(server.list_tools())


def test_exposes_every_read_tool_with_schemas():
    listed = {t.name: t for t in _list(mcp_server.build_server())}
    assert set(listed) == set(tools.DISPATCH)
    brief = listed["get_page_brief"]
    assert brief.input_schema["required"] == ["page_id"]
    assert "depth" in brief.input_schema["properties"]
    assert "migrat" in brief.description.lower()


def test_all_tools_advertise_read_only_and_non_destructive():
    for t in _list(mcp_server.build_server()):
        assert t.annotations.read_only_hint is True, t.name
        assert t.annotations.destructive_hint is False, t.name


def test_refuses_to_start_if_a_writing_tool_is_registered(monkeypatch):
    def evil_wipe(x: str) -> list:
        """Writes to the graph."""
        return graph._run("MATCH (n) DETACH DELETE n")

    monkeypatch.setitem(tools.DISPATCH, "evil_wipe", evil_wipe)
    with pytest.raises(RuntimeError, match="evil_wipe"):
        mcp_server.build_server()


def test_fails_closed_when_a_tool_cannot_be_verified(monkeypatch):
    """An unreadable tool is unverifiable, so it must be rejected — never
    waved through because the check happened to error."""
    monkeypatch.setitem(tools.DISPATCH, "opaque", eval("lambda x: x"))
    with pytest.raises(RuntimeError, match="cannot verify"):
        mcp_server.build_server()


def test_preflight_reports_an_unreachable_or_empty_graph(monkeypatch):
    monkeypatch.setattr(graph, "_run", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    status = mcp_server.preflight()
    assert status["ok"] is False and "hint" in status

    monkeypatch.setattr(graph, "_run", lambda *a, **k: [{"n": 0}])
    monkeypatch.setattr(graph, "get_profile", lambda: {})
    empty = mcp_server.preflight()
    assert empty["ok"] is False and "indexer" in empty["hint"]


def test_server_identity_guides_the_client():
    server = mcp_server.build_server()
    assert server.name == "codebase-explorer"
    assert "get_page_brief" in server.instructions
    assert "cannot modify" in server.instructions
