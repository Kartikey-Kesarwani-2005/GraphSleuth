# GraphSleuth — submission summary

**Track:** 01 — Agents That Act on Connected Data (also demonstrates 02 and 03)
**Stack:** Python 3, FalkorDB (Docker), Groq / any OpenAI-compatible LLM, MCP server (20 tools), multigraph tenancy
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
7. **Two agents share state through the graph.** `pipeline` runs Triage → Investigator: triage
   writes its findings into a `Handoff` node (`create_handoff`), the investigator reads it back
   (`list_handoffs` / `accept_handoff`) as its *first* tool call. No context is pasted through the
   caller — FalkorDB is the only channel.

## Track coverage

| Track | Evidence in the project |
|---|---|
| **01 — Connected data** | 12 read tools (incl. native graph algorithms + Cypher); agent acts via 8 write tools; all 20 exposed over MCP stdio (verified with a real MCP client); evidence trail and Mermaid path in every report |
| **02 — Memory & coordination** | `Session`/`Message` episodic memory, `MemoryNote` semantic + procedural memory, briefing injected before each question; **multigraph isolation per tenant**; **Triage → Investigator handoff through `Handoff` nodes**; **concurrency bench: 20 threads, 575 ops/s, 0 errors** |
| **03 — Company brain** | The same "one graph, traceable answers" pattern applied to accounts, people, merchants, alerts and decisions; every claim cites its source entities and prior notes |

## Demo

```powershell
docker compose up -d
.\.venv\Scripts\python.exe -m graphsleuth seed
.\.venv\Scripts\python.exe -m graphsleuth demo --target ACC-9003 --save demos/out/report.md
.\.venv\Scripts\python.exe -m graphsleuth pipeline --target ACC-9003   # two agents
```

Result (verified run, offline fallback brain — with `GROQ_API_KEY` set the *same* loop is driven
by a live model choosing tools itself): the agent identifies a 6-account mule ring (5 mules +
collector `ACC-9100`, 4 false clusters filtered out, score 41.05), narrates the money-only path
`ACC-EXT-01 → TX-9000 → ACC-9001 → … → ACC-9100` (6 hops), flags the account, opens a case and
writes a memory note. Running it again shows the second session recalling the first.

The two-agent pipeline additionally shows a `Handoff` created by triage and accepted by the
investigator, whose first evidence line comes from triage's memory note — state moved through the
graph only. Each agent block ends with an **Actions taken (verified from the tool log)** line.

Full walkthrough: `demos/DEMO_SCRIPT.md`.

## What is real vs. synthetic

* **Real:** FalkorDB running in Docker, actual Cypher, FalkorDB's native `algo.*` procedures
  (`WCC`, `labelPropagation`, `betweenness`, `HarmonicCentrality`) called as agent tools, actual
  tool loop with a live LLM choosing tools (Groq; offline fallback brain only when no key is set),
  actual MCP stdio server, actual multi-tenant multigraph isolation, actual concurrent
  reads/writes, actual persistence across sessions and processes.
* **Synthetic:** the fraud network (seeded by `graphsleuth/seed.py`) — generated so that the ring is
  *findable through connections* while ~200 ordinary accounts create realistic noise.

## Verification (measured on this machine)

```powershell
.\.venv\Scripts\python.exe tests/test_offline.py        # 12/12 pass, no database needed
.\.venv\Scripts\python.exe -m graphsleuth status        # connectivity + graph + LLM check
```

| Check | Result |
|---|---|
| Seed (`graphsleuth:default`) | 1702 nodes / 2713 relationships (207 accounts, 1213 tx, 58 devices, 10 merchants, 3 alerts, 1 case) |
| Demo tool calls | 11/11 `[ok]`, saved to `demos/EXAMPLE_REPORT.md` (opens `CASE-0002`; alert/note/memory ids are generated per run) |
| Cluster detection | 1 cluster, 6 members (ACC-9001..9005 + collector ACC-9100), 4 false clusters filtered, score 41.05 |
| Money path (`via=ORIGINATED,DESTINED`) | 6 hops, ACC-EXT-01 → TX-9000 → ACC-9001 → … → ACC-9100 |
| Native algorithms | WCC: 1 component/1701 nodes; label propagation: 187 communities; betweenness top: ACC-0124 (6719.28) |
| `page_rank` | native `algo.pageRank` yields 0 rows on this FalkorDB build → tool falls back to in-process power iteration (engine reported in result) |
| Two-agent pipeline | handoff created → accepted, `CASE-0003` opened with 8 entities, report `demos/PIPELINE_REPORT.md`; real-model run in `demos/LIVE_RUN.md` |
| Multigraph isolation | canary note: tenant `acme` = 1, tenant `default` = 0; `drop_tenant('acme')` removes the graph |
| Concurrency bench (20 threads, 4 s) | 2307 ops, 575 ops/s; reads p50 20.6 ms / p95 71.1 ms; writes p50 45.4 ms / p95 82.3 ms; **0 errors** |
| MCP server (stdio) | real client: `initialize` OK, **20 tools**, live `entity_search` + `detect_suspicious_cluster` calls |
| Tests / compile | 12/12 offline tests pass, `compileall` clean |

## Limits / honest notes

* The offline fallback brain exists only so the project runs without an API key (it is labelled
  offline everywhere, never "demo mode"). The primary path is the live model: set `GROQ_API_KEY`
  (free) and the same loop is driven by a real LLM choosing tools itself — no code changes.
* Small-scale dataset by design (a few thousand nodes) — enough to prove the approach, not a
  production fraud engine.
* `connected_components`, `community_detection` and `centrality` call FalkorDB's native `algo.*`
  procedures directly. `page_rank` tries native `algo.pageRank` first, but that procedure returns
  0 rows on the `falkordb/falkordb:latest` build used here (verified on a toy graph too), so the
  tool computes PageRank in-process (20 power iterations, d=0.85) over the same graph and reports
  which engine produced the answer. `detect_suspicious_cluster` (device-union + collector
  corroboration) stays in Python/Cypher by design: its signal rules are the domain logic.
* Data is synthetic and deliberately small: it exists to make one ring *findable through
  connections* while 200 ordinary accounts provide realistic noise.
