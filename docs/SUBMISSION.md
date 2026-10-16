# GraphSleuth — submission summary

**Track:** 01 — Agents That Act on Connected Data (also demonstrates 02 and 03)
**Stack:** Python 3, FalkorDB (Docker), optional OpenAI-compatible LLM, MCP server for tool sharing
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
   shared-device links, shared-beneficiary links and prior alerts, then ranks clusters.
4. **Cypher is a tool.** `run_cypher` gives the model a guarded, read-only escape hatch — with the
   guard *outside* the query (write verbs are rejected) so exploration is safe.
5. **Actions are graph writes.** `flag_account` creates an `Alert` node, flips the account to
   `under_review` and can attach it to a `Case`; `open_case` / `add_case_note` build the audit trail.

## Track coverage

| Track | Evidence in the project |
|---|---|
| **01 — Connected data** | 7 read tools + Cypher + clustering/pathfinding algorithms; agent acts via 4 write tools; evidence trail and Mermaid path in every report |
| **02 — Memory & coordination** | `Session`/`Message` episodic memory, `MemoryNote` semantic + procedural memory, memory briefing injected before each question, per-tenant scoping (`--tenant`), shared cases/alerts that any agent or MCP client can read |
| **03 — Company brain** | The same "one graph, traceable answers" pattern applied to accounts, people, merchants, alerts and decisions; every claim cites its source entities and prior notes |

## Demo (offline, deterministic)

```powershell
docker compose up -d
.\.venv\Scripts\python.exe -m graphsleuth seed
.\.venv\Scripts\python.exe -m graphsleuth demo --target ACC-9003 --save demos/out/report.md
```

Result: the agent identifies a 5-account mule ring, narrates the chain
`ACC-EXT-01 → ACC-9001 → … → ACC-9005 → ACC-9100`, flags the account, opens a case and writes a
memory note. Running it again shows the second session recalling the first.

Full walkthrough: `demos/DEMO_SCRIPT.md`.

## What is real vs. synthetic

* **Real:** FalkorDB running in Docker, actual Cypher, actual graph algorithms (union-find, BFS),
  actual tool loop with the LLM choosing tools, actual persistence across sessions and processes.
* **Synthetic:** the fraud network (seeded by `graphsleuth/seed.py`) — generated so that the ring is
  *findable through connections* while ~200 ordinary accounts create realistic noise.

## Verification

```powershell
.\.venv\Scripts\python.exe tests/test_offline.py   # 9/9 pass, no database needed
.\.venv\Scripts\python.exe -m graphsleuth status   # connectivity + node/relationship counts
```

## Limits / honest notes

* The scripted demo brain exists so the project is demonstrable without an API key; with a key set,
  `LLM_MODE=auto` switches to a live model with no code changes.
* Small-scale dataset by design (a few thousand nodes) — enough to prove the approach, not a
  production fraud engine.
* Graph algorithms are implemented in Cypher + Python rather than relying on a specific FalkorDB
  procedure, so the project runs on any FalkorDB build.
