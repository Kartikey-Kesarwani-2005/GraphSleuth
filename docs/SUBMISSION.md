# GraphSleuth — submission summary

**Track:** 01 — Agents That Act on Connected Data (also demonstrates 02 and 03)
**Stack:** Python 3, FalkorDB (Docker), Groq / any OpenAI-compatible LLM, MCP server (26 tools), multigraph tenancy
**Repo layout:** see `README.md` → Project layout

---

## One-liner

GraphSleuth is a fraud investigation agent whose entire context — the transaction network *and*
its own long-term memory — lives in one FalkorDB graph, so it reasons by following relationships
instead of guessing, acts on what it finds, and shows the exact path behind every answer.

## The problem it solves

Fraud is rarely visible in a single record. It shows up as *structure*: accounts that share a
device, a chain of same-day pass-through transfers, repeated alerts, one beneficiary at the end.
An agent that only reads rows cannot see that. GraphSleuth's context layer is the graph, and the
graph is what makes the answer possible.

## How the graph shapes the agent (not bolted on the side)

1. **The system prompt is a graph workflow.** The agent is instructed to resolve an entity, expand
   its neighbourhood, trace money flow, check shared devices/beneficiaries, rank suspect clusters,
   prove the connection, then act — in that order. Every tool returns typed relationships
   (`ORIGINATED`, `DESTINED`, `USED_DEVICE`, `FLAGGED_BY`, `PART_OF`, `ABOUT`), not flat rows.
2. **Multi-hop reasoning is the default.** `trace_money_flow` walks both directions hop by hop;
   `graph_neighbors` expands 1–3 hops; `shortest_connection` BFS-searches for the chain that links
   two suspects.
3. **Graph algorithms are tools.** `detect_suspicious_cluster` runs a union-find community pass over
   shared-device links, shared-beneficiary links and prior alerts, then ranks clusters — and the
   FalkorDB-native procedures are wired in as first-class tools too: `connected_components`
   (`algo.WCC`), `community_detection` (`algo.labelPropagation`), `centrality`
   (`algo.betweenness` / `algo.HarmonicCentrality`), `page_rank` (see honest notes).
4. **Cypher is a tool.** `run_cypher` gives the model a guarded, read-only escape hatch — with the
   guard *outside* the query (write verbs are rejected) so exploration is safe.
5. **Actions are graph writes.** `flag_account` creates an `Alert` node, flips the account to
   `under_review` and can attach it to a `Case`; `open_case` / `add_case_note` build the audit trail;
   `create_handoff` writes a `Handoff` node another agent picks up.
6. **One graph per tenant (multigraph).** Every tenant is a separate FalkorDB graph
   (`graphsleuth:default`, `graphsleuth:acme`, …), selected through a context tenant
   (`db.set_tenant` / `db.tenant_scope`). Isolation is topological: a canary memory note written in
   tenant `acme` was invisible from `default` (1 vs 0 notes) even to `run_cypher`, which only ever
   sees the active tenant's graph.
7. **Agents share state through the graph.** `pipeline` runs Triage → Investigator: triage
   writes its findings into a `Handoff` node (`create_handoff`), the investigator reads it back
   (`list_handoffs` / `accept_handoff`) as its *first* tool call. No context is pasted through the
   caller — FalkorDB is the only channel. Add `--verify` for a third adversarial agent that
   re-derives the claims from the graph and returns CONFIRMED / WEAK / REFUTED.
8. **The agent learns procedures, not just facts.** A finished investigation is distilled into a
   `MemoryNote kind=procedural` playbook (its own tool sequence, de-duplicated and capped per
   tenant) and suggested to the next session's briefing — procedural memory in the same graph as
   the data.
9. **Every claim is machine-checkable.** `python -m graphsleuth eval` runs 11 pass/fail checks
   against the live graph (ring precision, money path, native algorithms, memory round-trip, tenant
   isolation, procedural learning, explainability, read-only guard) and the report's "Actions taken"
   comes from the tool trace, never the model's prose.

## Track coverage

| Track | Evidence in the project |
|---|---|
| **01 — Connected data** | 12 read tools (incl. native graph algorithms + Cypher); agent acts via 8 write tools; all 20 exposed over MCP stdio (verified with a real MCP client, plus an offline test that the MCP parameters match the built-in toolbelt); evidence trail and Mermaid path in every report |
| **02 — Memory & coordination** | `Session`/`Message` episodic memory, `MemoryNote` semantic + procedural memory, **playbook learning**, briefing injected before each question; **multigraph isolation per tenant**; **Triage → Investigator → (optional) Verifier handoff through `Handoff` nodes**; **concurrency bench: 0 errors**, atomic case-id allocation |
| **03 — Company brain** | Dedicated second graph (`python -m graphsleuth company`, tenant `company`): people, teams, projects, systems, docs, tickets, chats and decisions. Answers the brief's six questions — who knows a system (brief's exact `Person-[:WORKED_ON]->Project-[:USES]->System` query + declared `:KNOWS` experts), who owns what, what breaks if a service goes down (reverse transitive `:DEPENDS_ON`), why a decision was made (traced via `:SOURCED_FROM` to docs/tickets/chats), what we know and from where, and what's going stale (ageing docs, superseded decisions, bus-factor risk). Also **agentic** — the six questions are tools in the shared 26-tool MCP toolbelt (`python -m graphsleuth company-chat`), and a **web-console mode**. Committed tour: `demos/COMPANY_BRAIN.md` |

## Demo

```powershell
docker compose up -d
.\.venv\Scripts\python.exe -m graphsleuth seed
.\.venv\Scripts\python.exe -m graphsleuth demo --target ACC-9003 --save demos/out/report.md
.\.venv\Scripts\python.exe -m graphsleuth pipeline --target ACC-9003 --verify   # triage -> investigator -> verifier
.\.venv\Scripts\python.exe -m graphsleuth eval                                  # 11/11 reproducibility scorecard
.\.venv\Scripts\python.exe -m graphsleuth company --save demos/COMPANY_BRAIN.md # Track 03 company brain
.\.venv\Scripts\python.exe -m graphsleuth company-chat "who knows SYS-ledger-db?" # Track 03 as an agent
.\.venv\Scripts\python.exe -m graphsleuth serve                                 # live web console
```

Result (verified run, offline fallback brain — with `GROQ_API_KEY` set the *same* loop is driven
by a live model choosing tools itself): the agent identifies a 6-account mule ring (5 mules +
collector `ACC-9100`, 4 false clusters filtered out, score 41.05), narrates the money-only path
`ACC-EXT-01 → TX-9000 → ACC-9001 → … → ACC-9100` (6 hops), flags the account, opens a case and
writes a memory note. Running it again shows the second session recalling the first.

The multi-agent pipeline additionally shows a `Handoff` created by triage and accepted by the
investigator, whose first evidence line comes from triage's memory note — state moved through the
graph only. With `--verify`, a third adversarial agent re-checks the case against the graph and
reports CONFIRMED / WEAK / REFUTED. Each agent block ends with an **Actions taken (verified from the
tool log)** line.

Full walkthrough: `demos/DEMO_SCRIPT.md`.

**Company brain (Track 03).** `python -m graphsleuth company` seeds a second graph (tenant
`company`) — 55 nodes / 131 relationships covering people, teams, projects, systems, docs, tickets,
chats and decisions — and tours six questions, each a traversal that names its sources. The
committed tour is `demos/COMPANY_BRAIN.md`. The dataset is hand-authored and checked offline for
referential integrity, a real dependency chain and source-traced decisions.

It is also an **agent**: `python -m graphsleuth company-chat "who knows SYS-ledger-db?"` gives the
model the six org tools and it picks them itself, answering in an Answer/Evidence/Sources/Confidence
format with node ids (`EMP-001`, `DOC-002`, `TKT-102`). The same tools are in the shared MCP toolbelt
(26 total) and in a **web-console company mode**. For why this domain is a graph and not SQL/vector,
see `docs/WHY_GRAPH.md`.

## What is real vs. synthetic

* **Real:** FalkorDB running in Docker, actual Cypher, FalkorDB's native `algo.*` procedures
  (`WCC`, `labelPropagation`, `betweenness`, `HarmonicCentrality`) called as agent tools, actual
  tool loop with a live LLM choosing tools (Groq; offline fallback brain only when no key is set),
  actual MCP stdio server, actual multi-tenant multigraph isolation, actual concurrent
  reads/writes, actual persistence across sessions and processes.
* **Synthetic:** the fraud network (seeded by `graphsleuth/seed.py`) and the company brain
  (seeded by `graphsleuth/company.py`) — generated so that the ring is *findable through
  connections* while ~200 ordinary accounts create realistic noise, and so the org graph has a
  real dependency chain, a reversed decision and bus-factor risk.

## Verification (measured on this machine)

```powershell
.\.venv\Scripts\python.exe tests/test_offline.py        # 27/27 pass, no database needed
.\.venv\Scripts\python.exe -m graphsleuth status        # connectivity + graph + LLM check
.\.venv\Scripts\python.exe -m graphsleuth eval          # 11/11 live scorecard
```

| Check | Result |
|---|---|
| Seed (`graphsleuth:default`) | 1702 nodes / 2713 relationships (207 accounts, 1213 tx, 58 devices, 10 merchants, 3 alerts, 1 case) |
| Demo tool calls | 11/11 `[ok]`, saved to `demos/EXAMPLE_REPORT.md` (opens `CASE-0002`; alert/note/memory ids are generated per run) |
| Cluster detection | 1 cluster, 6 members (ACC-9001..9005 + collector ACC-9100), 4 false clusters filtered, score 41.05 |
| Money path (`via=ORIGINATED,DESTINED`) | 6 hops, ACC-EXT-01 → TX-9000 → ACC-9001 → … → ACC-9100 |
| Native algorithms | WCC: 1 component/1702 nodes; label propagation: 181 communities; betweenness (exact, pinned seed) top: ACC-0084 (181363.62), ACC-0141, ACC-0124 |
| `page_rank` | native `algo.pageRank(null, null)`: 1702 nodes ranked; most central accounts ACC-0141, ACC-0187, ACC-0145 |
| Multi-agent pipeline | handoff created → accepted, `CASE-0003` opened with 8 entities, report `demos/PIPELINE_REPORT.md`; with `--verify` a third agent re-checks the case (CONFIRMED/WEAK/REFUTED); real-model runs in `demos/LIVE_RUN.md` (2 agents) and `demos/LIVE_RUN_3AGENT.md` (3 agents, verdict CONFIRMED) |
| Procedural learning | `python -m graphsleuth memory` lists the playbooks distilled from finished investigations |
| `eval` scorecard | 11/11 checks pass against the live graph, reproducible thanks to the deterministic seed |
| Web console | `python -m graphsleuth serve` streams the run (start → 11 tool calls → answer) and serves the graph/memory APIs |
| Multigraph isolation | canary note: tenant `acme` = 1, tenant `default` = 0; `drop_tenant('acme')` removes the graph |
| Concurrency bench (20 threads, 4 s) | 2307 ops, 575 ops/s; reads p50 20.6 ms / p95 71.1 ms; writes p50 45.4 ms / p95 82.3 ms; **0 errors**; 20 concurrent `open_case` calls yield 20 unique ids |
| MCP server (stdio) | real client: `initialize` OK, **26 tools** (20 fraud + 6 company-brain), live `entity_search` + `detect_suspicious_cluster` calls |
| Model fallback | `gpt-oss-120b` hit its daily cap mid-run → brain auto-switched to `gpt-oss-20b` and finished the investigation; real capture in `demos/MODEL_FALLBACK.md` |
| Tests / compile | 27/27 offline tests pass (incl. MCP/agent toolbelt parity + company-brain dataset integrity + company graph facts), `compileall` clean; GitHub Actions runs the suite plus a FalkorDB end-to-end job |

## Limits / honest notes

* The offline fallback brain exists only so the project runs without an API key (it is labelled
  offline everywhere, never "demo mode"). The primary path is the live model: set `GROQ_API_KEY`
  (free) and the same loop is driven by a real LLM choosing tools itself — no code changes.
* Small-scale dataset by design (a few thousand nodes) — enough to prove the approach, not a
  production fraud engine.
* `connected_components`, `community_detection`, `centrality` and `page_rank` call FalkorDB's native
  `algo.*` procedures directly. Two signatures needed care: `algo.pageRank(nodeLabel, relationshipType)`
  (a numeric `(iterations, damping)` call silently matches nothing — the tool calls
  `algo.pageRank(null, null)` and filters by label in Python), and `algo.betweenness`, which samples
  source nodes with a time-based seed by default (the tool pins `samplingSeed=42` and raises
  `samplingSize` past the node count for an exact, reproducible ranking). `detect_suspicious_cluster`
  (device-union + collector corroboration) stays in Python/Cypher by design: its signal rules are the
  domain logic.
* Data is synthetic and deliberately small: it exists to make one ring *findable through
  connections* while 200 ordinary accounts provide realistic noise.
