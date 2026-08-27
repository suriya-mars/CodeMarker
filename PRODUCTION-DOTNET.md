# PRODUCTION-DOTNET.md

Production plan for retargeting Codebase Explorer at a **.NET WebForms → .NET Core
migration**, with SQL Server as a first-class part of the graph, delivered to the
team as a **Claude Code plugin**.

Written: 2026-08-25. Companion to [PRODUCTION.md](PRODUCTION.md) — every P0 in that
document still applies to this build; this file covers only what is *different*.

---

## 1. The scope change

The original product answers *"help me understand an unfamiliar codebase."*
Yours answers a different question:

> **"What do we migrate next, what will it break, and what's hiding in the database?"**

That is a **migration planning** tool, not an onboarding tool. It changes what the
graph must contain and which questions matter. Migration questions are mostly
*graph* questions, not chat questions:

| Question | How it's answered |
|---|---|
| "What order do we migrate pages in?" | topological sort over the dependency graph |
| "What breaks if I change `OrderService.Save`?" | transitive reverse-reachability |
| "Which pages touch `usp_GetOrders`?" | traverse `EXECUTES` edges |
| "What's still using `Session` / `ViewState`?" | a labelled node property |
| "How much logic is in T-SQL vs C#?" | node counts by kind |

Only the last mile ("explain this page to me") needs the LLM. **Design for the
graph queries first** — they're the part that survives a manager's scrutiny.

---

## 2. Feasibility — verified, not assumed

Probed against the already-pinned `tree-sitter-languages==1.10.2`:

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
| [graph.py](api/graph.py) | ✅ unchanged — Symbol/Edge model is generic |
| [community.py](api/community.py) | ✅ unchanged — operates on edges, not syntax |
| [summarize.py](api/summarize.py) | ⚠️ prompts need migration framing (§6) |
| [indexer.py](api/indexer.py) | ⚠️ add edge types to `_EDGE_TYPES`; parallelize (§8) |
| [llm.py](api/llm.py) | ⚠️ model ids need updating (§8) |
| [tools.py](api/tools.py) | ➕ add migration tools (§6) |
| [agent.py](api/agent.py) | ❓ **may become unnecessary** — see §7 |
| [parsing.py](api/parsing.py) | 🔄 **rewrite** — new extractors |
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

## 8. Cost and scale — real numbers

Assume a large WebForms app: ~1,500 pages, ~3,000 classes, ~25,000 methods,
~2,000 procs ≈ **35,000 symbols**.

**Indexing** (Haiku 4.5 at $1.00/$5.00 per MTok, ~600 in / 40 out per symbol):

```
input   35,000 × 600 = 21.0M tok × $1.00  =  $21
output  35,000 ×  40 =  1.4M tok × $5.00  =   $7
                                    total ≈  $28  per full index
```

**Halve it with the Batch API** — index-time summarization is the ideal batch
workload (thousands of independent, non-latency-sensitive calls, 50% cost). That
also sidesteps rate limits entirely. **≈ $14 per full index.** Cost is a non-issue;
do not let it become the objection.

**Querying** (Sonnet 5 at $3/$15): ~$0.10 for a typical 3-turn question, ~$0.55 for
a hard 8-turn one. Twenty engineers asking ten questions a day ≈ **$400–600/month**.
Enable **prompt caching** on the stable prefix (tool definitions + system prompt) to
cut the resend cost substantially — the existing loop resends full history every turn.

**Update the model ids** in [llm.py:26-28](api/llm.py#L26-L28). Current strings are
stale and one carries a date suffix that shouldn't be there:

```python
NAV_MODEL     = "claude-sonnet-5"    # was claude-sonnet-4-6
SYNTH_MODEL   = "claude-opus-5"      # was claude-opus-4-8
SUMMARY_MODEL = "claude-haiku-4-5"   # was claude-haiku-4-5-20251001 — drop the suffix
```

**The real scale blocker is time, not money.** Serial summarization
([indexer.py:69-73](api/indexer.py#L69-L73)) at 35,000 symbols is **~6 hours**. This
makes PRODUCTION.md §2.6 a **week-1 blocker, not a later item.** Batch API or a
16-worker thread pool brings it to 20–40 minutes.

Also revisit [parsing.py](api/parsing.py) memory: 35,000 symbols with full `source`
held in RAM before writing (PRODUCTION.md §3.4) will hurt. Stream it.

---

## 9. Neo4j vs SQL Server for the graph store

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

## 10. Phased plan

**Phase 0 — POC (2 weeks).** §11.
**Phase 1 — pilot (4 weeks).**
- Full C# extractor: namespaces, partial-class merging, interfaces, generics
- `.aspx`/`.ascx` extractor + `HANDLES` edges
- SQL catalog ingestion + `EXECUTES`/`READS`/`WRITES`
- Parallel/batch summarization
- MCP server + plugin packaging
- All PRODUCTION.md P0 items (auth, path confinement, prod compose, repo scoping)
- Ship to 3–5 engineers on one real module

**Phase 2 — production (6 weeks).**
- Incremental re-index on git push (only changed files)
- Migration-order and blast-radius tools with a real UI
- Cross-file call resolution via Roslyn (see §12)
- Structured logging, cost metrics, CI
- Team-wide rollout

**Phase 3 — leverage.** Track migration progress over time (migrated vs remaining,
by community); feed the graph into the actual porting work.

---

## 11. The POC — scope and demo script

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

1. **The number.** "We indexed the Orders module: 52 pages, 340 classes, 1,890
   methods, 210 stored procs. 62% of business logic is in T-SQL, not C#."
   *Nobody in the room knows this today. Lead with it.*

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

## 12. Risks

| Risk | Mitigation |
|---|---|
| **T-SQL grammar unusable** — confirmed | Use `sys.sql_expression_dependencies` (§5.3). No parser needed. |
| **No SQL Server access** | Fallback to `.sql` files + ScriptDom. **Confirm access before week 1** — the data half of the graph depends on it. |
| **C# resolution shallower than expected** | Interfaces + DI mean the static call target is often an interface. Roslyn (`Microsoft.CodeAnalysis`) gives true semantic resolution — a .NET-shop-friendly Phase-2 upgrade. Roslyn is what tree-sitter can't be: a real compiler with a symbol table. |
| **Indexing takes 6 hours** | Batch API / thread pool, week 1 (§8). |
| **Neo4j not approved** | Get the ask in early; have the igraph fallback ready (§9). |
| **Scope creep to the whole app** | One module for the POC. Non-negotiable. |
| **Manager sees "AI chatbot"** | Lead with graph queries and numbers; the LLM is the last slide, not the first. |

---

## 13. Open questions — confirm before week 1

1. **SQL Server access** — can you read `sys.sql_modules` on a dev/UAT restore?
   (`VIEW DEFINITION` only, no data.) This gates the entire data half.
2. **Neo4j approval** — Docker-local, free, derived metadata only. Blocked or fine?
3. **Scale** — rough counts of `.aspx` pages, C# LOC, and stored procs. Changes the
   parallelization target.
4. **Which module** for the POC — smallest slice that still touches the database.
5. **Demo date** — drives how much of §11 is realistic.
