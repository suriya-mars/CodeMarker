# PRODUCTION-DOTNET.md

Production plan for retargeting Codebase Explorer at a **~2M LOC C# / WebForms
codebase undergoing refactoring and migration to .NET Core**, with SQL Server as a
first-class part of the graph, delivered to the team as a **Claude Code plugin**.

Written 2026-08-25, revised 2026-08-27 (scale corrected to 2M LOC; agent tiering
added; incremental re-index promoted to blocking).

Companion to [PRODUCTION.md](PRODUCTION.md) — every P0 in that document still
applies to this build; this file covers only what is *different*.

**The motive is refactoring, not just understanding.** That single fact drives two
non-obvious conclusions below: incremental re-index is blocking rather than a later
phase (§8.4), and the graph's own blast-radius query is the agent router (§9.2).

---

## 1. The scope change

The original product answers *"help me understand an unfamiliar codebase."*
Yours answers a different question:

> **"What do we refactor next, what will it break, and what's hiding in the database?"**

That is a **migration planning** tool, not an onboarding tool. It changes what the
graph must contain and which questions matter. Migration questions are mostly
*graph* questions, not chat questions:

| Question | How it's answered |
|---|---|
| "What order do we refactor in?" | topological sort over the dependency graph |
| "What breaks if I change `OrderService.Save`?" | transitive reverse-reachability |
| "Which pages touch `usp_GetOrders`?" | traverse `EXECUTES` edges |
| "What's still using `Session` / `ViewState`?" | a labelled node property |
| "How much logic is in T-SQL vs C#?" | node counts by kind |

Only the last mile ("explain this page to me") needs the LLM. **Design for the
graph queries first** — they're the part that survives a manager's scrutiny.

---

## 2. Status — built and tested, not merely feasible

The multi-language extractor layer **is implemented and passing tests** as of
2026-08-27: `parsing/` is now a package with Python, C#, and `.aspx` extractors
side by side (CLAUDE.md §10). 27 tests pass — 17 unit, 10 integration against a
live Neo4j.

Implemented and covered by tests:

| Capability | Where |
|---|---|
| C# types, members, `using`, inheritance, calls | `parsing/csharp_ext.py` |
| **Partial-class merging** (§5.2) | `parsing/__init__.py::_merge_partial` |
| **`HANDLES` edges** from markup + lifecycle (§5.1) | `parsing/aspx_ext.py` |
| **Legacy-API detection** (§5.5) | `csharp_ext.LEGACY_APIS` |
| **Stored-proc extraction** (§5.4) | `csharp_ext._collect_procs` |
| Declared-type call resolution (`_svc.Save()`) | `csharp_ext.resolve_call` tier 3 |
| `get_blast_radius`, `get_migration_blockers` | `tools.py` |
| Python extractor, unchanged behaviour | `parsing/python_ext.py` |

Two bugs were found and fixed during that work, both pre-existing:

* **`find_symbol` had never worked.** `session.run(cypher, **params)` collided with
  `Session.run`'s own `query` argument. Fixed in `graph.py::_run` by passing
  `parameters=params`. It survived because no test had ever executed Cypher.
* **Pages were unfindable.** Lucene keeps `OrderList.aspx` as one token, so a
  search for `OrderList` never matched. Page symbols now use the stem as `name`.

Still to build: SQL Server catalog ingestion (§5.3), parallel summarization (§8.2),
incremental re-index (§8.4), the MCP server (§7), agent tiering (§9).

Original grammar probe against the pinned `tree-sitter-languages==1.10.2`:

| Capability | Status |
|---|---|
| `c_sharp` grammar available | ✅ no new dependency |
| `namespace_declaration`, `class_declaration`, `method_declaration` | ✅ clean |
| `partial` visible as a `modifier` node | ✅ — critical, see §5.2 |
| `base_list` exposes `System.Web.UI.Page` | ✅ — this is how we classify WebForms pages |
| C# string literals → stored proc names | ✅ caught constructor arg, `CommandText =`, and Dapper call |
| `sql` grammar on T-SQL | ❌ **`has_error: True`**, no `create_procedure` node |

**The T-SQL grammar does not work** on `CREATE PROCEDURE … AS BEGIN … EXEC`. It
partially parses (`select_statement`, `update_statement`) but cannot see procedure
declarations. Don't build on it. §5.3 has the replacement, which is better anyway.

---

## 3. What carries over unchanged

Roughly **60% of the existing code is language-agnostic** and needs no edits:

| Module | Status |
|---|---|
| [graph.py](api/graph.py) | ✅ generic — one bug fixed in `_run` (§2) |
| [community.py](api/community.py) | ✅ unchanged — operates on edges, not syntax |
| [summarize.py](api/summarize.py) | ⚠️ prompts need migration framing (§6) |
| [indexer.py](api/indexer.py) | ⚠️ edge types added; **still serial** — see §8.2 |
| [llm.py](api/llm.py) | ⚠️ model ids need updating (§8) |
| [tools.py](api/tools.py) | ✅ migration tools added; kinds/projection widened |
| [agent.py](api/agent.py) | ❓ **may become unnecessary** — see §7 |
| [parsing/](api/parsing/) | ✅ **done** — package with 3 extractors |
| [web/](web/) | ⚠️ keep for the demo; add `dagre` for flow views |

This is the strongest argument for building on this codebase rather than starting
over: the graph layer, clustering, and indexing pipeline are already correct and
tested.

---

## 4. Domain model

### New Symbol kinds
```
page          .aspx / .ascx        (the migration unit)
codebehind    .aspx.cs partial class
class, method, interface           (ordinary C#)
proc          stored procedure
table         SQL Server table
config        Web.config section
```

### New edge types
```
HANDLES      page → method          btnSave_Click wired from markup   ★ §5.1
EXECUTES     method → proc          SqlCommand("usp_X")               ★ §5.4
READS        proc → table
WRITES       proc → table
RENDERS      page → usercontrol
USES_LEGACY  method → api           Session/ViewState/HttpContext     ★ §5.5
```

Plus the existing `CONTAINS` / `CALLS` / `INHERITS`. Keep `IMPORTS` for `using`
directives at namespace granularity.

### New node properties
```
is_webforms      bool     base_list contains System.Web.UI.Page/UserControl
legacy_apis      list     ["Session","ViewState","HttpContext.Current"]
migration_score  int      computed — see §6
```

---

## 5. The five hard problems

### 5.1 The event model breaks the call graph  ★ most important

WebForms handlers are **never called by any C# code**. `Page_Load` and
`btnSave_Click` are invoked by the ASP.NET runtime via markup wiring:

```xml
<asp:Button ID="btnSave" runat="server" OnClick="btnSave_Click" />
```

A pure C# parser sees these as orphans — zero callers, zero community membership,
invisible to `get_callers`. **In a WebForms app that is most of the entry points.**
The graph would be disconnected exactly where the migration work is.

**Fix — two mechanisms:**
1. Parse `.aspx`/`.ascx` with the `html` grammar (verified available); extract
   `On*="Handler"` attributes → synthesize `HANDLES` edges from page to method.
2. Recognize lifecycle methods by convention (`Page_Load`, `Page_Init`,
   `Page_PreRender`, `OnInit`, …) → `HANDLES` edge from the page itself.

Without this the tool is close to useless on WebForms. Do it in week 1.

### 5.2 Partial classes must merge

`Default.aspx.cs` and `Default.aspx.designer.cs` are **one class in two files**.
Naively they become two Symbols with the same id — a uniqueness-constraint
collision, or worse, a silent overwrite that loses half the methods.

**Fix:** the `partial` modifier is visible in the AST (verified). Key Symbols by
`namespace.ClassName`, `MERGE` rather than create, and keep `path` as a *list* of
contributing files. The existing `MERGE (s:Symbol {id: row.id}) SET s += row` in
[indexer.py:41-43](api/indexer.py#L41-L43) already does the right thing — but
`SET s += row` will clobber `path`, `start_line`, `source`. Needs a merge strategy.

### 5.3 Don't parse T-SQL — read the catalog  ★ better than the original plan

The bundled grammar can't parse T-SQL, but **SQL Server has already computed the
dependency graph for you**:

```sql
-- every proc/view/function definition
SELECT OBJECT_SCHEMA_NAME(object_id) AS sch, OBJECT_NAME(object_id) AS name,
       definition
FROM   sys.sql_modules m JOIN sys.objects o ON m.object_id = o.object_id;

-- proc → table and proc → proc edges, computed by the engine
SELECT OBJECT_NAME(referencing_id)          AS referencing,
       referenced_entity_name               AS referenced,
       referenced_minor_name                AS column_name,
       is_updated                           -- distinguishes READS from WRITES
FROM   sys.sql_expression_dependencies;
```

This is **exact, free, and requires zero parsing** — and it upholds the CLAUDE.md
§2.2 invariant (edges parsed, not inferred) better than a hand-rolled T-SQL parser
would. `is_updated` gives you `READS` vs `WRITES` for free.

**Requires read access to a SQL Server instance** (dev/UAT restore is fine —
`VIEW DEFINITION` permission, no data read needed). If you only have `.sql` files
in source control, the fallback is `Microsoft.SqlServer.TransactSql.ScriptDom` —
Microsoft's official T-SQL parser, free, and a .NET library your team can already
run. Do **not** hand-roll a T-SQL parser.

### 5.4 The C# → stored proc bridge  ★ highest-value edge

This is the edge that connects the application graph to the data graph, and it's
what makes the tool worth more than a C# call-graph viewer. Verified working —
all three patterns yield the literal proc name:

```csharp
new SqlCommand("usp_GetOrders", conn)                     // constructor arg
cmd.CommandText = "usp_UpdateOrder";                      // assignment
db.Query<Order>("usp_ListOrders", p, commandType: ...)    // Dapper
```

**Strategy:** collect every string literal in a method, match against the set of
known proc names harvested from `sys.sql_modules`. Because you have the authoritative
proc list, this is a *set-membership test*, not a guess — high precision.

**Known gap:** dynamic SQL (`"usp_Get" + entityType`) won't resolve. Follow the
existing invariant — mark it `unresolved`, don't guess — and **count** them. "We
found 412 call sites we cannot statically resolve" is itself a valuable migration
risk finding to hand the manager.

### 5.5 Legacy API usage is the migration-blocker signal

`Session`, `ViewState`, `HttpContext.Current`, `Server.Transfer`, `Request.Form`,
postback lifecycle — none survive the move to .NET Core unchanged. Detecting them
is a simple identifier scan during C# extraction, and it turns the tool from
"interesting" into "operationally useful":

```cypher
MATCH (p:Symbol {kind:'page'})-[:CONTAINS|HANDLES*1..2]->(m:Symbol)
WHERE size(m.legacy_apis) > 0
RETURN p.name, count(DISTINCT m) AS blockers, collect(DISTINCT m.legacy_apis)
ORDER BY blockers DESC
```

That query alone answers *"which pages are hardest to migrate?"* — no LLM involved.

---

## 6. Migration-specific tools

Add to `TOOLS` + `DISPATCH` per the CLAUDE.md §10 recipe. Keep them few and capable.

| Tool | Returns | Why |
|---|---|---|
| `get_blast_radius(symbol_id, depth)` | transitive reverse-reachability incl. pages and procs | "what breaks if I touch this" |
| `get_migration_blockers(symbol_id)` | legacy API usage in the subtree, with counts | "why is this page hard" |
| `suggest_migration_order(limit)` | leaf-first topological ranking | **the manager question** |
| `get_data_dependencies(symbol_id)` | procs + tables reachable from a page | "what data does this own" |

Also fix two carry-over gaps from PRODUCTION.md while you're in there:
- add `s.community_id` to `_SYMBOL_FIELDS` (§3.1 there) so the model can pivot from
  a symbol to its cluster
- word-boundary citation matching (§3.2 there)

**Summary prompts need migration framing.** Change [summarize.py](api/summarize.py)
from *"what does this do"* to *"what does this do, and what would it become in .NET
Core?"* — the summary is the search index, so migration vocabulary in the summary is
what makes migration questions findable.

---

## 7. Two surfaces, one core  ★ this simplifies the build

You asked for a Claude Code plugin. That changes the architecture favourably:

```
                    ┌──────────────────────────────┐
                    │  SHARED CORE (existing)      │
                    │  parsing · graph · summarize │
                    │  community · indexer         │
                    └───────┬──────────────┬───────┘
                            │              │
              ┌─────────────▼───┐    ┌─────▼──────────────┐
              │ MCP SERVER      │    │ FastAPI + web/     │
              │ → Claude plugin │    │ → the visual demo  │
              │ (team daily use)│    │ (manager POC)      │
              └─────────────────┘    └────────────────────┘
```

**The MCP server replaces `agent.py` entirely.** Expose the graph tools over MCP and
Claude Code's own agent loop does the navigation — no `MAX_STEPS`, no tool-use
protocol code, no model selection, no budget hints to maintain. That's ~157 lines of
the trickiest code in the repo you no longer own.

It's also the right distribution answer: your team already has Claude Code. A plugin
means `/plugin install` and they're working — no separate web app to deploy, log
into, or train people on.

**Keep the web UI for the demo.** The visual graph is what sells this to a manager;
a terminal transcript is not. Both surfaces read the same Neo4j.

---

## 8. Scale — corrected for 2M LOC

### 8.1 The numbers

Earlier drafts assumed 35,000 symbols. The real target is **~2,000,000 LOC of C#**.
Realistic symbol counts for C# at that size (methods dominate; ~15–25 LOC each):

```
~2,000,000 LOC   ->   ~100,000-150,000 symbols
                      (~10k types, ~100k members, ~15k files, ~2-3k pages)
```

Do NOT extrapolate from the test fixture — small files carry far more declarations
per line than real code and will overestimate by 3-5x. Measure on one real module
before committing to numbers in front of anyone.

**Indexing cost** (Haiku 4.5 at $1.00/$5.00 per MTok, ~600 in / 40 out per symbol,
at 125k symbols):

```
input    125,000 x 600 = 75.0M tok x $1.00  =  $75
output   125,000 x  40 =  5.0M tok x $5.00  =  $25
                                     total  ~  $100  per full index
                        with Batch API (50%) ~  $50
```

**Cost is not the constraint.** Even a full re-index is a rounding error against
engineer time. Do not let it become the objection in a budget conversation.

**Querying** (Sonnet 5 at $3/$15): ~$0.05 for a tier-2 lookup, ~$1-3 for a tier-3
refactor analysis (§9). Twenty engineers ~ **$400-800/month**. Enable **prompt
caching** on the stable prefix (tool definitions + system prompt) — the loop in
`agent.py` resends full history every turn, and that prefix never changes.

**Update the model ids** in [llm.py:26-28](api/llm.py#L26-L28) — stale, and one
carries a date suffix that should not be there:

```python
NAV_MODEL     = "claude-sonnet-5"    # was claude-sonnet-4-6
SYNTH_MODEL   = "claude-opus-5"      # was claude-opus-4-8
SUMMARY_MODEL = "claude-haiku-4-5"   # was claude-haiku-4-5-20251001
```

### 8.2 Time is the constraint, not money

Serial summarization ([indexer.py:69-73](api/indexer.py#L69-L73)) at 125k symbols:

```
serial            125,000 x 0.6s   ~  21 hours     <- unusable
16 workers                         ~  1.3 hours
Batch API                          ~  hours, unattended, half price
```

This is the **first thing to fix**, before any agent work. PRODUCTION.md §2.6 is a
week-1 blocker at this scale, not a later item.

### 8.3 Memory: stream the parse

`parse_repo` accumulates every Symbol — each carrying its `source` — in RAM before
the first write. C# stores source twice over (a type Symbol holds its whole body;
each member holds its own), so expect **several hundred MB to low GB** of live
Python objects at 2M LOC. Stream files through to Neo4j in batches instead of
buffering the whole repo (PRODUCTION.md §3.4).

Also raise the Neo4j heap — [docker-compose.yml:11](docker-compose.yml#L11) pins
1G, which is not enough for a graph this size plus stored source. Consider not
persisting `source` on `file`/`page` symbols at all and reading from disk on demand.

### 8.4 Incremental re-index is BLOCKING, not Phase 3  ★

This is what changes when the motive is **refactoring** rather than understanding:

```
refactor OrderService  ->  the graph is now WRONG  ->  every later answer is stale
```

If a full re-index takes 21 hours, the tool is useless after the first change
anyone makes. Worse, a *stale* graph guiding a refactor is more dangerous than no
graph, because blast-radius answers will describe code that no longer exists.

Required: git-diff the changed files -> re-parse only those -> re-summarize only
changed symbols -> patch the subgraph. **Minutes, not hours.**

This forces two other items to happen at the same time, so treat them as one piece
of work:
* repo scoping on Symbol ids (PRODUCTION.md §3.1) — needed to delete a subgraph
* removing the full wipe at [indexer.py:137](api/indexer.py#L137)

### 8.5 Measure call-resolution rate before trusting blast radius  ★

The C# resolver's last tier only fires on a **repo-wide unique** method name:

```python
hits = index.methods_by_name.get(callee_name, [])
return hits[0] if len(hits) == 1 else None      # csharp_ext.resolve_call tier 5
```

At ~100k methods, `Save` / `Execute` / `Process` / `Validate` will each have
hundreds of hits, so this tier will almost never fire. That is **safe** — no wrong
edges, per CLAUDE.md §2.2 — but recall drops, and the declared-type tier (tier 3)
becomes the workhorse.

**Gate:** index one real module and measure resolved-vs-dropped CALLS. Below ~60%,
blast radius is not trustworthy enough to guide refactoring, and Roslyn
(`Microsoft.CodeAnalysis`, true semantic resolution) moves up the priority list
from Phase 2 to now.

---

## 9. Switchable agents — scaling effort to the task

At 2M LOC, running every question through the same agent is wrong in both
directions: it overpays for lookups and underpowers real refactors.

### 9.1 Scale on three axes, not one

These are independent and routinely conflated:

| Axis | Cheap | Expensive |
|---|---|---|
| **model** | Haiku | Sonnet -> Opus |
| **effort** | `low` | `high` / `xhigh` |
| **topology** | one agent | fan-out |

`agent.py` already does a crude version of the model axis — Sonnet navigating,
Opus forced at the step cap. That is the right primitive with the wrong *trigger*:
it escalates when the budget runs out, not when the task is hard.

### 9.2 The router is a graph query, not a classifier  ★

You do not need an LLM to judge how hard a request is. **Ask the graph first** —
`get_blast_radius` is deterministic, ~50ms, and free:

```python
radius = get_blast_radius(target, depth=3)

len(radius) <   5   -> tier 2   isolated leaf; cheap lookup
len(radius) <  50   -> tier 3   real refactor; must read code
len(radius) < 200   -> tier 4   fan out over affected slices
len(radius) >= 200  -> REFUSE   "touches 400 symbols across 31 pages —
                                 scope it down or plan it as a project"
```

That last branch is the most valuable one in a 2M LOC refactor: **the highest-value
thing this tool can say is "do not attempt this one in a single pass."** No agent
design gives you that; the graph does, before a token is spent.

Secondary escalation signals, layered on top:
* model called `get_definition` 3+ times -> it needs real source -> bump to Opus
* request carries refactor/optimize intent -> start at tier 3
* step cap approached -> the existing escalation, kept as a backstop

### 9.3 The tiers

| Tier | Example request | Config | Cost |
|---|---|---|---|
| **0** | "what order do we refactor?", "what uses `usp_X`?" | **no agent** — Cypher | ~$0 |
| **1** | index-time summaries | Haiku, no tools, Batch API | ~$50 once |
| **2** | "how does `OrderService` work?" | Sonnet, effort `low`, 5 steps | ~$0.05 |
| **3** | "refactor `OrderService.Save`" | Opus, effort `high`, 12 steps | ~$1-3 |
| **4** | "refactor this whole slice" | Opus orchestrator + N workers | ~$5-20 |

**Tier 0 is the one teams skip, and at this scale it is the most valuable.** Most
refactoring questions — sequencing, impact, "who touches this proc" — are graph
computations, not judgment calls. Route them to Cypher and answer in milliseconds
for nothing.

### 9.4 What is NOT multi-agent

The most tempting thing to mislabel:

```python
for s in symbols:                       # indexer.py:70 — 125,000 iterations
    s.summary = summarize.summarize_symbol(s)
```

That is a thread pool or the Batch API. Stateless calls in a loop are
**concurrency, not agents** — no orchestration, no context, no judgment. It is the
single biggest win available (§8.2) and it needs no agent framework at all.

### 9.5 If you do fan out, fan out over communities — not files

The natural unit is not the page. Pages share code-behind bases, services, and
stored procs, so one-agent-per-page re-analyses the same substrate dozens of times
and reaches inconsistent conclusions about it.

Leiden already computes the right unit. A community is "densely connected
internally, loosely connected outward" — which is the definition of a slice that
migrates or refactors together. For a 50-page module expect **8-15 communities, not
50 pages**, and the shared service lands *inside* one agent's slice instead of being
re-derived by twenty.

Fan out **lazily and narrowly**: rank everything with a tier-0 query (free), then
spawn agents only on the 3-5 slices actually being worked this sprint. Deep-
analysing all of them up front produces reports that go stale before anyone reads
them.

### 9.6 Build it in Claude Code, not in `agent.py`

Because the delivery surface is a Claude Code plugin (§7), **tiering and fan-out
are prompt-level concerns, not architecture.** Claude Code already supplies subagent
orchestration; the MCP server supplies the graph tools; a subagent inherits them.

```
you build:      the MCP server (needed anyway) + one skill file per tier
you do NOT build: orchestrator, context passing, result merging, retry,
                  cost accounting, model selection plumbing
```

Extending `agent.py` into a multi-agent framework means owning all of that while
PRODUCTION.md still lists an unauthenticated arbitrary-path read and a 21-hour
indexer. Wrong place to spend two weeks.

### 9.7 Costs to watch

* **Cost multiplies by fan-out width.** N agents = N context windows, each
  accumulating its own tool results. A $0.10 question becomes $5.
* **Debugging gets much harder.** One agent's bad turn is traceable; N
  non-deterministic branches are not.
* **Synthesis is its own context problem.** N reports may not fit one window; you
  end up needing a reduce tree.

---

## 10. Neo4j vs SQL Server for the graph store

You asked whether this can run on SQL Server. Two different questions:

**Analyzing SQL Server: yes, essential** — that's §5.3.

**Storing the graph in SQL Server: possible, but don't.** Recursive CTEs can do
transitive closure, and SQL Server has full-text indexing, so it's not absurd. But
blast-radius and migration-ordering queries are variable-depth traversals — exactly
what Cypher is for and what recursive CTEs make painful and slow.

**Recommendation:** Neo4j Community, in Docker, local only. The pitch for approval is
that it's free, runs on a developer laptop, holds only derived metadata, and — per
CLAUDE.md §2.3 — **no source code ever leaves the machine except inside your own LLM
API call**. In a regulated .NET shop that data-sovereignty story is the approval, and
it's true here rather than marketing.

If Neo4j is genuinely blocked, the fallback is an in-process graph
(`python-igraph`, already a dependency) rebuilt at load time from a SQL Server
metadata table. Slower to start, no Cypher, but no new infrastructure.

---

## 11. Phased plan

Reordered 2026-08-27: refactoring as the motive promotes incremental re-index into
Phase 1, and the resolution-rate gate now sits before anything is built on top of
blast radius.

**Phase 0 — measure first (3 days).** Do this before committing to any of it.
- Index **one real module** with the extractors already built (§2)
- **Measure resolved-vs-dropped CALLS** (§8.5). Below ~60% -> Roslyn now, not later
- Measure actual symbols/LOC and bytes/symbol -> replace the §8.1 estimates
- Confirm SQL Server catalog access (§14.1)

**Phase 1 — make it usable at scale (4 weeks).**
- **Parallel / Batch summarization** (§8.2) — 21h -> ~1.3h. Nothing else works until this does
- **Streaming parse** (§8.3) — avoid buffering 2M LOC of source in RAM
- **Incremental re-index + repo scoping + drop the full wipe** (§8.4) — one piece of work;
  without it, the first refactor invalidates the tool
- SQL catalog ingestion + `EXECUTES` / `READS` / `WRITES` (§5.3)
- MCP server + plugin packaging (§7)
- Tier 0 and tier 2 routing (§9.3) with the blast-radius router (§9.2)
- PRODUCTION.md P0 items (auth, path confinement, prod compose)
- Ship to 3-5 engineers on one real module

**Phase 2 — refactoring support (6 weeks).**
- Tier 3: Opus + high effort for refactor analysis; the "too big, scope it down" refusal
- Post-refactor verification: re-index changed files, diff the subgraph, flag lost edges
- Roslyn semantic resolution if Phase 0 said so
- Structured logging, cost metrics, CI
- Team-wide rollout

**Phase 3 — leverage.**
- Tier 4 fan-out over communities, *only* if tier 3 proves insufficient (§9.5)
- Track refactoring progress over time (done vs remaining, by community)

---

## 12. The POC — scope and demo script

**Goal: make the manager ask a follow-up question.** That means answering something
they already worry about, with real numbers from the real codebase.

**Scope — build exactly this, nothing more (2 weeks):**

| Build | Skip |
|---|---|
| C# extractor: class, method, `CALLS`, `INHERITS` | generics, extension methods, DI resolution |
| `.aspx` → `HANDLES` edges | `.ascx`, master pages, Web.config |
| `legacy_apis` detection | full migration scoring |
| SQL catalog: procs, `EXECUTES`, `READS`/`WRITES` | views, functions, dynamic SQL |
| Summaries via Batch API | incremental indexing |
| Existing web UI + `dagre` | auth, multi-repo, polish |
| **One real module** (~50 pages) | the whole app |

Scope it to one module you know well. A believable slice beats a broken whole, and
you can state the extrapolation honestly.

**The demo, in order — five minutes:**

1. **The number.** "We indexed the Orders module: N pages, N classes, N methods,
   N stored procs. X% of business logic is in T-SQL, not C#."
   *Fill these in from Phase 0 — do not present illustrative figures. Nobody in
   the room knows the real ones today, which is exactly why they land.*

2. **The blast radius.** Click `OrderService.Save` → the graph lights up 23 pages and
   7 stored procs. "This is what changes when we touch one method."

3. **The blockers.** Run the §5.5 query → ranked list of the hardest pages with
   *why*. "These 8 pages hold 70% of our `Session` usage."

4. **The order.** `suggest_migration_order()` → a ranked list of leaf-first
   candidates. **This is the money slide** — it's the question they're already
   guessing at in spreadsheets.

5. **The plugin.** Ask a question in Claude Code, get an answer citing real symbols.
   "Every engineer gets this in their terminal — no new tool to learn."

**Close with the honest limits:** dynamic SQL is unresolved (with a count),
cross-file resolution is import-level, one module indexed so far. Stating limits
unprompted is what makes the working parts credible.

---

## 13. Risks

| Risk | Mitigation |
|---|---|
| **T-SQL grammar unusable** — confirmed | Use `sys.sql_expression_dependencies` (§5.3). No parser needed. |
| **No SQL Server access** | Fallback to `.sql` files + ScriptDom. **Confirm access before week 1** — the data half of the graph depends on it. |
| **C# resolution shallower than expected** | Interfaces + DI mean the static call target is often an interface. Roslyn (`Microsoft.CodeAnalysis`) gives true semantic resolution — a .NET-shop-friendly Phase-2 upgrade. Roslyn is what tree-sitter can't be: a real compiler with a symbol table. |
| **Indexing takes ~21 hours** at 2M LOC | Batch API / thread pool, Phase 1 (§8.2). |
| **Neo4j not approved** | Get the ask in early; have the igraph fallback ready (§10). |
| **Scope creep to the whole app** | One module for the POC. Non-negotiable. |
| **Graph goes stale on every refactor** | Incremental re-index is Phase 1, not Phase 3 (§8.4). A stale graph guiding a refactor is worse than no graph. |
| **Call resolution too sparse to trust** | Measure in Phase 0 (§8.5). Below ~60%, blast radius cannot guide refactoring — escalate to Roslyn. |
| **Agent costs balloon via fan-out** | Route through the blast-radius oracle (§9.2); tier 0 answers most questions for ~$0; fan out lazily (§9.5). |
| **Memory blowup buffering 2M LOC** | Stream the parse; stop storing `source` on file/page nodes (§8.3). |
| **Manager sees "AI chatbot"** | Lead with graph queries and numbers; the LLM is the last slide, not the first. |

---

## 14. Open questions — confirm before Phase 0

1. **SQL Server access** — can you read `sys.sql_modules` and
   `sys.sql_expression_dependencies` on a dev/UAT restore? (`VIEW DEFINITION` only,
   no data read.) This gates the entire data half of the graph (§5.3).
2. **Neo4j approval** — Docker-local, free, derived metadata only, no source
   egress. Blocked, or fine? Have the igraph fallback ready either way (§10).
3. **Which module for Phase 0** — smallest slice that still touches the database
   and has at least one partial-class page. Everything downstream is calibrated
   from what it measures.
4. **Is the codebase in git?** Incremental re-index (§8.4) diffs against git. If it
   is in TFVC or another VCS, the change-detection strategy needs rethinking, and
   that is now a Phase 1 dependency rather than a detail.
5. **Who owns the index?** A stale graph guiding a refactor is worse than no graph.
   Someone must own re-indexing on merge — otherwise it degrades silently and the
   team quietly stops trusting it.
6. **Demo date** — drives how much of §12 is realistic.
