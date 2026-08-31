"""Relative-import and package re-export resolution.

Both were found by running `measure` on a real repository: they accounted for
the two largest groups of dropped calls, and fixing them took internal call
resolution on that repo from 24% to 59%. Unresolved calls are dropped rather
than guessed (CLAUDE.md §2.2), so these gaps did not produce wrong edges — they
produced MISSING ones, which is what makes blast radius quietly under-report.
"""

import os

import parsing
from parsing.python_ext import resolve_relative

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "py_package")


def _edges(edges, etype):
    return {(e.src, e.dst) for e in edges if e.type == etype}


def test_resolve_relative_from_a_module():
    assert resolve_relative(".utils", "pkg.sub.mod") == "pkg.sub.utils"
    assert resolve_relative("..other", "pkg.sub.mod") == "pkg.other"
    assert resolve_relative(".", "pkg.sub.mod") == "pkg.sub"
    assert resolve_relative("absolute.mod", "pkg.sub.mod") == "absolute.mod"


def test_resolve_relative_from_a_package_init():
    """`pkg/__init__.py` has the PACKAGE as its module id, so its own package is
    itself. Treating it like a module walks up one level too far and yields
    `noise_schedule` instead of `pkg.noise_schedule`."""
    assert resolve_relative(".core", "pkg", is_package=True) == "pkg.core"
    assert resolve_relative(".core", "pkg") == "core"  # the wrong answer, if misused


def test_relative_import_produces_edges():
    _, edges = parsing.parse_repo(FIXTURE)
    assert ("pkg.consumer", "pkg.core") in _edges(edges, "IMPORTS")
    assert ("pkg.consumer.use_relative", "pkg.core.helper") in _edges(edges, "CALLS")


def test_package_reexport_resolves_to_the_real_definition():
    """`from pkg import helper` must reach `pkg.core.helper`, even though the
    id `pkg.helper` does not exist."""
    _, edges = parsing.parse_repo(FIXTURE)
    assert ("main.use_reexport", "pkg.core.helper") in _edges(edges, "CALLS")


def test_measure_reports_resolution_stats():
    stats: dict = {}
    parsing.parse_repo(FIXTURE, stats=stats)
    assert stats["calls_seen"] >= 2
    assert stats["calls_resolved"] >= 2
    assert stats["files_failed"] == 0
