# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Engineers working in a codebase they did not write, or do not fully hold in their
heads. They ask questions in plain English to understand how the code works, review
what a change affects, and get suggestions for updating code. Within a customer team,
admins onboard and index repos; other members only ask questions and read results.

## Product Purpose

RepoDigger builds a graph of a repository's files, classes and functions and the
relationships between them, then answers natural-language questions about the code,
supports code review with a report of what a commit affects, and suggests code updates
for a human to review and apply. It never edits code itself.

Success is an engineer getting a correct answer about unfamiliar code, with the path
through the code that backs it, without their source going anywhere they did not choose.

## Positioning

**Your code stays yours.** Source code is stored only in the customer's own dedicated
stack, and the only thing that leaves it is the LLM call to the provider the customer
chooses, on their own key. There are no third-party analytics, telemetry or guardrail
services: guardrail models run inside the stack, and telemetry carries counts, never
prompts or source.

## Operating Context

- **Ask:** a question goes to an agent that navigates the graph with read-only tools and
  cites the symbols it used; those symbols light up in the graph view.
- **Review:** after a commit, a report lists changed symbols, everything upstream of them
  with the path back to the change, the architectural areas touched, and risk flags.
- **Keep fresh:** the repo is re-indexed after merges; only changed code is re-summarized.
- **In the editor:** engineers can reach the same graph tools from Claude Code over MCP.

## Capabilities and Constraints

**Built today**

- Runs locally with Docker Compose: React + Cytoscape.js web UI, FastAPI, Neo4j.
- Indexes Python, C#, ASP.NET WebForms markup, JavaScript and CSS.
- Facts (call edges, blast radius, paths, risk flags) come from parsed code; the LLM only
  explains them.
- Vectorless by design: full-text search over LLM-written summaries plus graph traversal.
  No embeddings or vector database.
- Guardrails, per-user rate limits and daily token quotas, and estimated cost per user.
- Each question is independent. Conversation memory was deliberately not built, because
  of cost.
- Graph data survives container restarts; `docker compose down -v` deletes it.

**Limits to state honestly**

- Call resolution is partial. Unresolved calls are dropped, never guessed, so blast radius
  can under-report; it never invents a dependency.
- Guardrails lower the odds of prompt injection and contain its damage. They do not prevent
  it, and the classifier occasionally flags ordinary code (a label, not a block).
- Cost figures are estimates; the provider's console shows what is billed.

**Decided, not built yet**

- A dedicated stack per customer on AWS ECS, with RepoDigger's own client app running the
  agent on the customer's LLM key and reaching the graph over MCP.
- Admin and member roles through Cognito.

**Undecided**

- Go-to-market stage and pricing.
- Logo and visual identity.

**Terminology:** symbol, blast radius, community (a Leiden cluster of related code), impact
report, flagged.

## Brand Commitments

- The product name is **RepoDigger**. "CodeMarker" survives only as the repository folder,
  which also names the Docker containers and the `codemarker_neo4j-data` volume.
- The promise above is binding: never claim "code never leaves" when the LLM call does, and
  never claim a dedicated stack exists before it does.

## Evidence on Hand

- `Prompt-Injection.pdf`: security assessment and fixes.
- `TESTING.md`: run commands, expected results and known issues.
- `api/guard.py`: measured guardrail results on real code (0.9% false flags at the chosen
  threshold).
- `api/tests/`: hostile-repo, prompt-injection and API-security tests.

None of the following are in the repo, and none may be invented: customers, testimonials,
case studies, benchmarks, pricing, certifications.

## Product Principles

1. **Facts first, narrative second.** Show what was computed from the code separately from
   what an LLM wrote, so people can tell which is which.
2. **Every answer can be checked.** Claims point at real symbols, one step from the source.
3. **State the limits unprompted.** Under-reporting, estimates and flags are said plainly,
   because that is what makes the rest credible.
4. **Earn the privacy promise in the product.** Where code goes and what it costs is
   visible, not buried; cost constraints are real, for the customer and for us.
5. **Repository content is untrusted.** It is displayed as data and never allowed to steer
   the interface or the people reading it.
