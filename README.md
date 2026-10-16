# 🕵️ GraphSleuth

**A fraud investigation agent whose context layer is FalkorDB.**

GraphSleuth doesn't look up records — it follows relationships. Ask it about one suspicious
account and it walks the graph: shared devices, shared beneficiaries, prior alerts, same-day
pass-through transfers, the beneficiary at the end of the chain. Then it **shows the path it
took**, takes an action in the graph (raise an alert, open a case), and **remembers what it
learned** for the next session.

> Track 01 (agents that act on connected data) is the core. Track 02 (memory & coordination)
> and Track 03 (company brain) are covered by the same graph — see [Track coverage](#track-coverage).

---

## The problem, in one run

A transaction looks ordinary. But the account behind it:

* shares device `DEV-9001` with two already-alerted accounts,
* is the middle hop of a same-day chain that moves ₹18.5L from an offshore desk to
  `ACC-9100` in five transfers,
* and sits inside a cluster the graph algorithm ranks as the most suspicious in the network.

No single record says "fraud". The *connections* do. That is the job GraphSleuth automates —
and it can prove every hop with the tool call that produced it.

---

## How it works

```
                ┌────────────────────────────────────────────────┐
  question ───▶ │  GraphSleuth agent loop                        │
                │  memory briefing ─▶ LLM ─▶ tool call ─▶ result │
                └───────────────────────┬────────────────────────┘
                                        │ 12 tools
                    ┌───────────────────┴────────────────────┐
                    ▼                                        ▼
        read tools (reason over the graph)      write tools (act on the graph)
        entity_search                          flag_account
        graph_neighbors                        open_case
        trace_money_flow                       add_case_note
        find_shared_links                      remember
        detect_suspicious_cluster              recall
        shortest_connection
        run_cypher (read-only)
                    └───────────────────┬────────────────────┘
                                        ▼
                        ┌───────────────────────────────┐
                        │  FalkorDB (the context layer) │
                        │  fraud network + agent memory │
                        └───────────────────────────────┘
```

The agent has no private database. Everything it knows — the fraud network, what previous
sessions concluded, the playbook it follows — is nodes and edges in one FalkorDB graph.

### What the graph has to do (and where it does it)

| Brief requirement | Implementation |
|---|---|
| Follow connections across data | `graph_neighbors`, `trace_money_flow`, `shortest_connection`, `run_cypher` |
| Remember across sessions | `remember` / `recall` + `Session`/`Message`/`MemoryNote` nodes, injected as a "memory briefing" before every question |
| Share state between agents | cases, alerts and memory notes are graph writes — any agent (including one connected over MCP) reads the same state |
| Connect people, projects, decisions | `Person -[OWNS]-> Account -[USED_DEVICE]-> Device`, `Transaction` links, `Alert`/`Case`/`Note` attached to the entities they concern |
| Show the path behind an answer | every tool call is recorded; `explain.py` renders a numbered evidence trail **and a Mermaid diagram of the traversed path** |

---

## Graph schema

```
(Person)-[:OWNS]─▶(Account)-[:USED_DEVICE]─▶(Device)
                    │  ▲
      [:ORIGINATED] │  └─[:DESTINED]
                    ▼
              (Transaction)-[:PAID]─▶(Merchant)

(Account)-[:FLAGGED_BY]─▶(Alert)-[:PART_OF]─▶(Case)-[:INVOLVES]─▶(any entity)
(Case)-◀─[:ON_CASE]─(Note)

# agent memory — same graph
(Session)-[:SAID]─▶(Message)
(MemoryNote {kind: episodic|semantic|procedural})-[:ABOUT]─▶(any entity)
```

Seeded dataset: ~215 accounts, 17 people, ~200 devices, 10 merchants, ~1,200 transactions,
3 alerts, 1 open case and a starter memory note — plus the mule ring the demo discovers.

---

## Quickstart (Windows)

Requirements: Python 3.10+, Docker (for FalkorDB).

```powershell
# 1. FalkorDB
docker compose up -d

# 2. Python environment
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3. Config (optional — a key is only needed for live-LLM mode)
copy .env.example .env

# 4. Build the fraud graph
.\.venv\Scripts\python.exe -m graphsleuth seed

# 5. Run the investigation
.\.venv\Scripts\python.exe -m graphsleuth demo --target ACC-9003 --save demos/out/report.md
```

`setup.ps1` does everything end to end: it creates the venv if missing, installs the dependencies,
launches Docker Desktop if the engine is down (waiting for it to come up), brings FalkorDB up, seeds
the graph and runs the demo — one command from a fresh clone.

### Commands

| Command | What it does |
|---|---|
| `python -m graphsleuth status` | check FalkorDB connectivity + node/relationship counts |
| `python -m graphsleuth seed` | (re)build the synthetic fraud network and baseline memory |
| `python -m graphsleuth demo --target ACC-9003` | run the full scripted investigation, print the evidence trail, optionally `--save report.md` |
| `python -m graphsleuth chat` | interactive session; asks a real model if `OPENAI_API_KEY` is set, otherwise uses the scripted brain |

### LLM modes

`LLM_MODE` in `.env`:

* `auto` (default) — use the live model when `OPENAI_API_KEY` is set, else the scripted brain
* `live` — require a real model (any OpenAI-compatible endpoint: OpenAI, OpenRouter, Ollama, …)
* `demo` — always scripted

The scripted brain is not a mock answer: it performs **the same tool calls** a model would, on
the real graph, and the verdict is composed from the real tool results. That keeps the demo
runnable offline and makes the judged walkthrough deterministic.

---

## MCP: the toolbelt is shared infrastructure

`graphsleuth/mcp_server.py` exposes all twelve tools over MCP (stdio), so any MCP client —
Claude Desktop, an IDE agent, another agent on the team — works against the same graph:

```json
{
  "mcpServers": {
    "graphsleuth": {
      "command": "D:\\Hackathon\\GraphSleuth\\.venv\\Scripts\\python.exe",
      "args": ["-m", "graphsleuth.mcp_server"],
      "env": { "FALKORDB_HOST": "localhost", "FALKORDB_PORT": "6379" }
    }
  }
}
```

Tool selection, schemas and descriptions are the same ones the built-in agent uses, so a
second agent picks up the graph without any extra wiring.

---

## Cross-session memory (Track 02 flavour)

```powershell
python -m graphsleuth demo            # investigates ACC-9003, writes memory + opens a case
python -m graphsleuth demo            # second run recalls the first run's conclusion
```

On the second run the agent's briefing already contains the previous finding, the open case and
the flagged account — visible in the `recall` step of the evidence trail. Memory is scoped by
`tenant` (`--tenant acme`), so the same graph can serve separate teams without leaking state.

---

## Track coverage

| Track | How GraphSleuth hits it |
|---|---|
| **01 — Act on connected data** | Multi-hop reasoning over accounts/devices/transactions; graph algorithms (union-find clustering, BFS pathfinding) as tools; Cypher as a read-only tool; actions (alert, case) driven by graph evidence; full evidence trail + Mermaid path |
| **02 — Memory & coordination** | Episodic (`Session`/`Message`), semantic and procedural (`MemoryNote`) memory persisted in the graph; memory briefing before every question; cases/alerts/notes are shared state any agent (or MCP client) can pick up; per-tenant isolation |
| **03 — Company brain** | The same pattern applied to an org: people, accounts, records, decisions and cases in one graph, with every answer traceable to its source entities and prior notes |

---

## Project layout

```
GraphSleuth/
├─ graphsleuth/
│  ├─ config.py        # env config (FalkorDB + LLM)
│  ├─ db.py            # the only module that talks to FalkorDB
│  ├─ seed.py          # synthetic fraud network + baseline memory
│  ├─ tools.py         # the 12 graph tools (read + write)
│  ├─ memory.py        # episodic / semantic / procedural memory
│  ├─ llm.py           # live (OpenAI-compatible) + scripted brains
│  ├─ agent.py         # the investigation loop + system prompt
│  ├─ explain.py       # evidence trail + Mermaid path rendering
│  ├─ cli.py           # status / seed / demo / chat
│  └─ mcp_server.py    # the same tools over MCP (stdio)
├─ tests/              # offline unit tests (no database needed)
├─ demos/              # demo script + saved reports
├─ docs/               # submission notes
├─ data/falkordb/      # graph storage (bind-mounted from the container)
├─ docker-compose.yml  # FalkorDB
├─ setup.ps1
└─ requirements.txt
```

Everything the project needs — code, config, tests, and the graph's on-disk data — lives inside
this one folder. The only machine-wide pieces are Docker Desktop itself and the pulled
`falkordb/falkordb` image.

---

## Design notes

* **The graph is the source of truth, not the prompt.** Tool descriptions teach the model *how to
  think with a graph* (resolve → expand → trace → cluster → prove → act), and every conclusion must
  point at a tool result.
* **Read tools vs write tools are separated**, and the Cypher escape hatch is guarded against
  `CREATE/DELETE/SET/MERGE/...` so the model can explore freely but only change the graph through
  auditable actions.
* **Explainability is a first-class output**: each report contains the verdict, the numbered
  evidence chain, the graph path, the tools used and the actions taken.
