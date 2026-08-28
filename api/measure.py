"""Phase 0: point this at ONE real module before building anything else.

Costs nothing — no LLM calls, no database. It answers the questions every layer
above it depends on:

  1. Does the extractor survive your actual C#?
  2. **What fraction of call sites resolve?**  <- the gate
  3. Are WebForms handlers being wired, or left as orphans?
  4. What will a full index cost and how long will it take?

The resolution rate matters most. Unresolved calls are dropped rather than
guessed (CLAUDE.md §2.2), so a low rate does not produce wrong edges — it
produces MISSING ones, which means `get_blast_radius` quietly under-reports and
`get_page_brief` misses dependencies. Below ~60% the graph is not trustworthy
enough to guide refactoring, and Roslyn moves from "later" to "now".

    python -m measure /repos/YourModule
"""

from __future__ import annotations

import sys
import time
from collections import Counter

import parsing
import scale
import telemetry


def _bar(pct: float, width: int = 32) -> str:
    filled = int(pct / 100 * width)
    return "#" * filled + "." * (width - filled)


def measure(path: str) -> dict:
    files = list(parsing.iter_source_files(path))
    loc = 0
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                loc += sum(1 for _ in fh)
        except OSError:
            pass

    print(f"\n  scanning {path}")
    print(f"  {len(files):,} parseable files, {loc:,} LOC\n")

    stats: dict = {}
    t0 = time.time()
    symbols, edges = parsing.parse_repo(path, stats=stats)
    parse_s = time.time() - t0

    kinds = Counter(s.kind for s in symbols)
    langs = Counter(s.language for s in symbols)
    etypes = Counter(e.type for e in edges)

    print("  === WHAT WAS EXTRACTED " + "=" * 42)
    print(f"  {len(symbols):,} symbols, {len(edges):,} edges, parsed in {parse_s:.1f}s")
    print(f"  by kind    : {dict(kinds.most_common())}")
    print(f"  by language: {dict(langs.most_common())}")
    print(f"  by edge    : {dict(etypes.most_common())}")
    if stats["files_failed"]:
        print(f"  !! {stats['files_failed']} files failed to parse")

    seen, resolved = stats["calls_seen"], stats["calls_resolved"]
    raw_rate = 100 * resolved / seen if seen else 0.0

    # A call to SqlCommand or ToString can NEVER resolve — those symbols are not
    # in your repo. Counting framework calls as failures makes the number
    # meaningless. What matters is: of calls whose name matches something we DID
    # index, how many resolved? Those are the real misses.
    own_names = {s.name for s in symbols}
    internal_missed = sum(n for name, n in stats["unresolved_names"].items()
                          if name in own_names)
    external_missed = (seen - resolved) - internal_missed
    attempted = resolved + internal_missed
    rate = 100 * resolved / attempted if attempted else 0.0

    print("\n  === CALL RESOLUTION (the gate) " + "=" * 35)
    print(f"  [{_bar(rate)}] {rate:5.1f}%   <- INTERNAL calls (the number that matters)")
    print(f"  {resolved:,} resolved / {attempted:,} calls to symbols defined in this repo")
    print(f"  {external_missed:,} further calls went to framework or third-party code "
          f"(expected, not a failure)")
    print(f"  raw rate over all {seen:,} call sites: {raw_rate:.1f}%")
    if internal_missed:
        print("\n  dropped calls to code we DID index (what Roslyn would recover):")
        for name, n in stats["unresolved_names"].most_common(30):
            if name in own_names:
                print(f"      {n:>6}x  {name}")

    pages = [s for s in symbols if s.kind == "page"]
    handled = {e.dst for e in edges if e.type == "HANDLES"}
    handler_like = [
        s for s in symbols
        if s.kind == "method" and (s.name.startswith("Page_") or "_" in s.name)
    ]
    orphans = [s for s in handler_like if s.id not in handled]
    if pages or handler_like:
        print("\n  === WEBFORMS " + "=" * 53)
        print(f"  {len(pages):,} pages, {len(handled):,} handlers wired via HANDLES")
        print(f"  {len(orphans):,} handler-shaped methods with NO page wiring")
        if orphans[:5]:
            print("      e.g. " + ", ".join(s.name for s in orphans[:5]))

    legacy = Counter()
    for s in symbols:
        legacy.update(s.legacy_apis)
    procs = {p for s in symbols for p in s.sql_procs}
    if legacy or procs:
        print("\n  === MIGRATION SIGNAL " + "=" * 45)
        print(f"  legacy API uses : {dict(legacy.most_common(8))}")
        print(f"  stored procs referenced: {len(procs)}"
              + (f"  e.g. {sorted(procs)[:4]}" if procs else ""))

    print("\n  === PROJECTION TO YOUR FULL CODEBASE " + "=" * 29)
    per_loc = len(symbols) / loc if loc else 0
    for target in (500_000, 2_000_000):
        n = int(target * per_loc)
        prof = scale.derive(n, n * 3)
        cost = n * telemetry.cost_of("claude-haiku-4-5", 600, 40)
        serial_h = n * 0.6 / 3600
        print(f"  {target:>9,} LOC -> ~{n:>7,} symbols | tier {prof.tier:<6} "
              f"| ~${cost:>6.0f} | {serial_h:>5.1f}h serial, "
              f"{serial_h/prof.summary_workers:>4.1f}h at {prof.summary_workers} workers")

    print("\n  === VERDICT " + "=" * 54)
    if not seen:
        print("  NO CALL SITES FOUND — check the path and that it contains source.")
    elif not attempted:
        print("  Every call went to external code — try a module with more")
        print("  internal structure, or check that dependencies are in scope.")
    elif rate >= 60:
        print(f"  {rate:.0f}% resolution: GOOD ENOUGH to guide refactoring.")
        print("  Next: run --stage parse + --stage summarize on this module,")
        print("        then sanity-check get_page_brief on a page you know well.")
    else:
        print(f"  {rate:.0f}% resolution: TOO LOW to trust blast radius.")
        print("  Blast radius will UNDER-report, so get_page_brief will miss")
        print("  dependencies. Escalate to Roslyn before building on top of this.")
    print()
    return {"symbols": len(symbols), "edges": len(edges), "loc": loc,
            "resolution_rate": round(rate, 1)}


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python -m measure <path-to-one-real-module>")
        raise SystemExit(2)
    measure(sys.argv[1])


if __name__ == "__main__":
    main()
