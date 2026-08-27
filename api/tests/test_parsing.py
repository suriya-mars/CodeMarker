"""Unit tests for the Python extractor: assert the exact Symbols and edges
produced for the known fixture repo."""

import os

import parsing

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_repo")


def _symbols_and_edges():
    return parsing.parse_repo(FIXTURE)


def test_symbols_extracted():
    symbols, _ = _symbols_and_edges()
    by_id = {s.id: s for s in symbols}

    assert by_id["auth"].kind == "file"
    assert by_id["utils"].kind == "file"
    assert by_id["auth.AuthService"].kind == "class"
    assert by_id["auth.AuthService.login"].kind == "method"
    assert by_id["auth.AuthService._check"].kind == "method"
    assert by_id["auth.authenticate"].kind == "function"
    assert by_id["utils.log"].kind == "function"

    # signatures captured for callables
    assert "user" in by_id["auth.AuthService.login"].signature


def _edge_set(edges, etype):
    return {(e.src, e.dst) for e in edges if e.type == etype}


def test_contains_edges():
    _, edges = _symbols_and_edges()
    contains = _edge_set(edges, "CONTAINS")
    assert ("auth", "auth.AuthService") in contains
    assert ("auth", "auth.authenticate") in contains
    assert ("auth.AuthService", "auth.AuthService.login") in contains
    assert ("auth.AuthService", "auth.AuthService._check") in contains
    assert ("utils", "utils.log") in contains


def test_imports_edge():
    _, edges = _symbols_and_edges()
    assert ("auth", "utils") in _edge_set(edges, "IMPORTS")


def test_calls_resolution():
    _, edges = _symbols_and_edges()
    calls = _edge_set(edges, "CALLS")
    # import-level call: login() -> utils.log
    assert ("auth.AuthService.login", "utils.log") in calls
    # self call: login() -> _check
    assert ("auth.AuthService.login", "auth.AuthService._check") in calls
    # constructor call: authenticate() -> AuthService
    assert ("auth.authenticate", "auth.AuthService") in calls


def test_call_count_weighting():
    _, edges = _symbols_and_edges()
    for e in edges:
        if e.type == "CALLS":
            assert e.count >= 1
