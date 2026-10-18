# GraphSleuth — Submission (Graph Hacks)

**Team:** NexAI (codeNZ4NTP)  
**Project:** GraphSleuth — Agentic fraud investigation + company brain with FalkorDB as the context layer  
**Tech:** Python, FalkorDB, Groq/OpenAI-compatible LLM, MCP (stdio), multigraph tenancy, Docker

## Summary
GraphSleuth is a graph-first agent system where FalkorDB is the context layer. A single fraud investigation agent (Tracks 01+02) acts on connected data, maintains persistent memory across sessions, learns procedural playbooks, and coordinates via handoffs. A second agent (Track 03) answers org questions over a dedicated company graph. Both share the same 30-tool MCP toolbelt and explain every answer with graph-traced evidence.

## Tracks coverage (all three in one project)

| Track | Implementation | Evidence |
|---|---|---|
| **01 — Agents that act on connected data** | Multi-hop reasoning, graph algorithms as tools, actions from evidence, explainable results. Tools: entity_search, graph_neighbors, trace_money_flow, find_shared_links, detect_suspicious_cluster, shortest_connection (money-only path via), page_rank, connected_components (WCC), community_detection (LPA), centrality (betweenness/harmonic), run_cypher (read-only), what_if_removed + assess_ring_resilience (counterfactual explainability: robustness 0-100, load-bearing score, articulation points), ring_timeline (when: burst window, activation order, alerts-vs-money lead), find_similar_rings (is the playbook repeating: structural fingerprint + cross-links), flag_account, open_case, add_case_note. | `demo` produces verdict + numbered evidence chain + Mermaid graph path + **counterfactual check + ring-resilience verdict + timeline + pattern check + verified actions** + click-to-stress-test in the console + downloadable proof pack (standalone HTML). Native FalkorDB algos used with pinned reproducible configs (betweenness exact). |
| **02 — Agent memory & coordination** | Episodic (Session+Message), semantic/procedural (MemoryNote), memory briefing before each turn, playbook distillation/reuse, handoffs (Handoff nodes) between Triage→Investigator→Verifier, per-tenant multigraph isolation, concurrency 0-errors, atomic IDs. | `memory` shows sessions/notes/cases/playbooks. `pipeline --verify` shows handoff accepted and adversarial verifier returns CONFIRMED/WEAK/REFUTED. Cross-session recall proven. |
| **03 — Company brain** | Dedicated org graph (people/teams/projects/systems/docs/tickets/chats/decisions). Tools: who_knows, owners_of, blast_radius, decision_trail, knowledge_sources, stale_knowledge. Agentic (`company-chat`) + scripted tour (`company`) + web console mode. | Answers cite node IDs (EMP-*, DOC-*, TKT-*, ADR-*, SYS-*). Auto-seeded on demand. Deterministic dataset with dependency chain + source-traced decisions. |

## What the graph does
- **Follow connections:** multi-hop across accounts/devices/transactions
- **Remember across sessions:** Session/Message/MemoryNote persisted in graph
- **Share state:** Handoff nodes move context between agents (no pasted text)
- **Connect org:** people↔projects↔systems, decisions↔sources, ownership/teams
- **Show the path:** evidence chain + graph path in every answer

## Run (local)

```powershell
# FalkorDB
docker compose up -d

# Setup
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

# Fraud (Tracks 01+02)
.\.venv\Scripts\python.exe -m graphsleuth seed
.\.venv\Scripts\python.exe -m graphsleuth demo --target ACC-9003 --save demos/out/report.md
.\.venv\Scripts\python.exe -m graphsleuth pipeline --target ACC-9003 --verify
.\.venv\Scripts\python.exe -m graphsleuth memory
.\.venv\Scripts\python.exe -m graphsleuth eval  # 11/11

# Company Brain (Track 03)
.\.venv\Scripts\python.exe -m graphsleuth company --save demos/COMPANY_BRAIN.md
.\.venv\Scripts\python.exe -m graphsleuth company-chat --offline "who knows SYS-ledger-db?"

# Console + MCP
.\.venv\Scripts\python.exe -m graphsleuth serve  # http://127.0.0.1:8765
python -m graphsleuth.mcp_server  # stdio, 30 tools
```

## Live runs (with GROQ_API_KEY)
- `demos/LIVE_RUN.md` (2-agent)
- `demos/LIVE_RUN_3AGENT.md` (triage→investigator→verifier, verdict CONFIRMED)

## Verification
- **Offline tests:** `python tests/test_offline.py` → 33/33 PASS (no DB)
- **Eval:** `python -m graphsleuth eval` → 11/11 (live FalkorDB)
- **MCP:** 30 tools (24 fraud + 6 company), schemas match registry
- **Isolation:** per-tenant multigraph proven
- **Concurrency:** bench shows 0 errors
- **Unicode-safe:** UTF-8 reconfigure on Windows
