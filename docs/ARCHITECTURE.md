# GraphSleuth Architecture

GraphSleuth is a graph-first agent: FalkorDB is the context layer (not a lookup table). Every investigation, memory, handoff, and org question is a traversal over typed relationships in a per-tenant multigraph.

## High-level architecture

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                           User / Judge                               │
│  CLI (python -m graphsleuth ...)  │  Web Console (http://127.0.0.1:8765)│
│                                   │  MCP Client (stdio)              │
└───────────────┬───────────────────────────────────┬───────────────────┘
                │                                   │
                ▼                                   ▼
        ┌──────────────────────────────────────────────────────────┐
        │                    Agent Runtime                        │
        │  • GraphSleuth (investigator/fraud)                    │
        │  • Triage/Investigator/Verifier (pipeline)             │
        │  • Company agent (Track 03)                           │
        │  • Tool selection (LLM chooses from tool specs)       │
        │  • Memory briefing before each turn                   │
        │  • Event stream (tool_call/tool_result/done)           │
        └───────────────┬───────────────────────────┬──────────┘
                        │                           │
                        ▼                           ▼
                ┌───────────────┐           ┌────────────┐
                │  Tool Registry│           │   Memory   │
                │  (26 tools)   │           │ (episodic/ │
                │  Fraud + Org  │           │ semantic/  │
                │               │           │ procedural)│
                └──────┬────────┘           └─────┬──────┘
                       │                        │
                       ▼                        ▼
                ┌─────────────────────────────────────┐
                │           FalkorDB (Graph DB)      │
                │  • Multigraph: graphsleuth:{tenant} │
                │  • Native algorithms (WCC, LPA,    │
                │    betweenness, harmonic, pageRank)│
                │  • Handoffs (Handoff nodes)        │
                │  • Cases/Alerts/Notes/Messages     │
                │  • Session + Message (episodic)    │
                │  • MemoryNote (semantic/procedural)│
                └─────────────────────────────────────┘
```

## Per-tenant multigraph

- Every tenant is an **isolated FalkorDB graph**: `graphsleuth:{tenant}` (e.g. `graphsleuth:default`, `graphsleuth:company`, `graphsleuth:acme`).
- Isolation is **topological** (separate graphs) not a `WHERE` clause. One tenant cannot read another's data.
- Active tenant is tracked via a `ContextVar` (`db.set_tenant` / `db.tenant_scope`) so all queries hit the correct graph without threading tenant everywhere.
- `db.drop_tenant(name)` deletes the entire tenant graph.
- MCP server uses `GRAPHSLEUTH_TENANT` (defaults to `default`) — one graph per process.

## Memory model (Track 02)

Stored as graph nodes/edges in the active tenant:

| Type | Node/Structure | Purpose |
|---|---|---|
| **Episodic** | `Session(id)` + `Message(session_id, role, content, ts)` | Conversation turns across sessions; persistent history |
| **Semantic** | `MemoryNote(kind='semantic', text, about_ids)` | Durable facts learned |
| **Procedural** | `MemoryNote(kind='procedural', text, about_ids)` | Reusable playbooks distilled from finished investigations |
| **Briefing** | Injected into system prompt before every question | Agent sees prior sessions, open cases, alerts, playbooks first |
| **State sharing** | `Handoff` nodes (`Handoff{id,to_role,from_role,context,case_id,entities[],status}`) | Triage ↔ Investigator ↔ Verifier coordination, no pasted context |

Before answering, the agent calls `recall()` (memory briefing). After finishing, it writes procedural playbooks via `remember(kind='procedural', ...)` when patterns emerge.

## Agent loop

1. **Brief**: load memory (sessions, notes, cases, alerts, playbooks) for tenant.
2. **Think**: LLM receives system prompt + memory + tool specs (20 fraud or 6 company + shared).
3. **Act**: LLM selects tools via function/tool calling; each call streamed as `tool_call` event.
4. **Observe**: Tool executes against FalkorDB (graph reads/writes), returns structured result; streamed as `tool_result` + appended to trace.
5. **Reason**: Iterate up to `max_rounds` (12–15) until it can produce a confident answer with evidence.
6. **Conclude**: Compose Answer/Evidence/Graph path/Actions; write episodic/semantic/procedural memory; emit `done`.
7. **Explain**: `explain.py` builds evidence chain, graph facts (nodes/edges) for visualization, and verified actions.

## Tools (shared toolbelt)

- **Fraud (Track 01):** 20 tools: `entity_search`, `graph_neighbors`, `trace_money_flow`, `find_shared_links`, `detect_suspicious_cluster`, `shortest_connection`, `page_rank`, `connected_components`, `community_detection`, `centrality`, `list_alerts`, `create_handoff`, `list_handoffs`, `accept_handoff`, `run_cypher` (read-only), `flag_account`, `open_case`, `add_case_note`, `remember`, `recall`.
- **Company (Track 03):** 6 tools: `who_knows`, `owners_of`, `blast_radius`, `decision_trail`, `knowledge_sources`, `stale_knowledge`.
- **Total:** 26 tools exposed over **MCP (stdio)** with identical JSON schemas (`mcp_server.py`).
- **Graph algorithms:** Native FalkorDB procedures (`algo.WCC`, `algo.labelPropagation`, `algo.betweenness`/`HarmonicCentrality`, `algo.pageRank`). Betweenness uses `samplingSeed=42` + sufficient sampling for **exact, reproducible** ranking; `engine` field reports the path used.

## Coordination (multi-agent)

- **Triage → Investigator:** Triage uses lightweight signals (`list_alerts`, `recall`, `find_shared_links`, clustering), writes a `Handoff` (context + entities + case) to the shared graph; Investigator calls `list_handoffs`/`accept_handoff` and picks up state **without caller-pasted context**.
- **Investigator → Verifier (adversarial):** With `--verify`, a third agent re-examines the case independently (reads same graph, forms its own hypothesis), adds verification note and reports `CONFIRMED` / `WEAK` / `REFUTED`.

## Data & Seeding

- **Fraud:** `graphsleuth/seed.py` — deterministic synthetic network (accounts/devices/tx/merchants/alerts/cases) designed so the mule ring is discoverable via connections while noise exists. Default ~1702 nodes / 2713 rels.
- **Company:** `graphsleuth/company.py` — hand-authored org graph (people/teams/projects/systems/docs/tickets/chats/decisions). Deterministic, referentially consistent. Default ~55 nodes / 131 rels. Auto-seeded on demand in Company mode.

## I/O surfaces

- **CLI:** `python -m graphsleuth {status,seed,demo,pipeline,bench,memory,eval,chat,serve,company,company-chat}`
- **Web Console:** Single-page app (no CDN/build), streams NDJSON over SSE (`/api/investigate`), renders force-directed graph, verdict, live steps, memory. Company mode has free-text question input; static assets served with no-cache headers.
- **MCP:** `graphsleuth.mcp_server:main` over stdio — same toolbelt, tool selection via MCP protocol.
- **API:** `POST /api/investigate` (NDJSON stream), `GET /api/status`, `/api/graph`, `/api/memory`.

## Design principles

1. **Graph is the source of truth** — relationships first, traversals over joins.
2. **Explainability** — every answer includes Evidence chain + Graph path + Actions (with node IDs).
3. **Reproducibility** — deterministic seeds, exact algorithm configs, offline brain for CI/no-LLM runs.
4. **Isolation by graph** — per-tenant multigraph boundary, verified by isolation proof.
5. **Shared infrastructure** — one tool registry, one memory model, one explain layer used by CLI/web/MCP/agents.
6. **Graceful degradation** — algorithm fallbacks report `engine`, Unicode-safe on Windows, offline-first for tests.
