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
                                        │ 20 tools
                    ┌───────────────────┴────────────────────┐
                    ▼                                        ▼
        read tools (reason over the graph)      write tools (act on the graph)
        entity_search                          flag_account
        graph_neighbors                        open_case
        trace_money_flow                       add_case_note
        find_shared_links                      remember
        detect_suspicious_cluster              create_handoff
        shortest_connection                    accept_handoff
        list_alerts                            recall
        list_handoffs
        page_rank / connected_components
        community_detection / centrality
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
| Run graph algorithms | `connected_components`, `community_detection`, `centrality`, `page_rank` — native FalkorDB `algo.*` procedures (see [Graph algorithms as agent tools](#graph-algorithms-as-agent-tools)) |
| Remember across sessions | `remember` / `recall` + `Session`/`Message`/`MemoryNote` nodes, injected as a "memory briefing" before every question |
| Share state between agents | cases, alerts, memory notes **and handoffs** are graph writes — `create_handoff` / `list_handoffs` / `accept_handoff` move work between agents (including one connected over MCP) through the same state |
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

# agent-to-agent handoff — same graph
(Handoff {from_role, to_role, status: pending|accepted})-[:ABOUT]─▶(any entity)

# agent memory — same graph
(Session)-[:SAID]─▶(Message)
(MemoryNote {kind: episodic|semantic|procedural})-[:ABOUT]─▶(any entity)
```

Seeded dataset (verified live — `python -m graphsleuth seed` reports **1702 nodes / 2713
relationships**): 207 accounts, 207 people, 58 devices, 10 merchants, 1213 transactions, 3 alerts,
1 open case and 3 memory notes (one semantic, one episodic, one procedural) — plus the mule ring
the demo discovers (`ACC-9001…ACC-9005` funnelling into collector `ACC-9100`).

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

A sample report from a real run is committed at [`demos/EXAMPLE_REPORT.md`](demos/EXAMPLE_REPORT.md)
— and if the FalkorDB container ever stops, `docker compose up -d` brings it straight back with the
graph intact (the data is bind-mounted to `./data/falkordb`).

`setup.ps1` does everything end to end: it creates the venv if missing, installs the dependencies,
launches Docker Desktop if the engine is down (waiting for it to come up), brings FalkorDB up, seeds
the graph and runs the demo — one command from a fresh clone.

### Commands

| Command | What it does |
|---|---|
| `python -m graphsleuth status` | check FalkorDB connectivity + node/relationship counts |
| `python -m graphsleuth seed` | (re)build the synthetic fraud network and baseline memory |
| `python -m graphsleuth demo --target ACC-9003` | run the full scripted investigation, print the evidence trail, optionally `--save report.md` |
| `python -m graphsleuth pipeline --target ACC-9003` | two agents on one graph: triage → handoff → investigator; `--save` writes a combined report |
| `python -m graphsleuth bench --writers 4 --readers 16` | concurrency benchmark: mixed reads/writes with latency percentiles; `--save` writes raw JSON |
| `python -m graphsleuth chat` | interactive session; drives a live model when a key is set (Groq/OpenAI-compatible), else the offline fallback |

### LLM modes

`LLM_MODE` in `.env`:

* `auto` (default) — use the live model when a key is set (`GROQ_API_KEY` wins, then `OPENAI_API_KEY`), else the offline fallback
* `live` — require a real model (Groq, or any OpenAI-compatible endpoint: OpenAI, OpenRouter, Gemini, Ollama, …)
* `demo` — always scripted

The scripted brain is **not a "demo mode"**: it is an explicitly-labelled offline fallback. It
performs **the same tool calls** a model would, on the real graph, and the verdict is composed from
the real tool results — so the walkthrough is deterministic and runs with no API key. The moment a
key is present, the live model drives the loop and picks the tools itself.

### Live model in 30 seconds (Groq, free tier)

1. Grab a free key at <https://console.groq.com/keys>.
2. Put it in `.env`:
   ```ini
   GROQ_API_KEY=gsk_...
   ```
3. Confirm:

```powershell
python -m graphsleuth status
#   LLM: LIVE (groq:openai/gpt-oss-120b)
```

Every command — `demo`, `pipeline`, `chat` — now drives the real model (default
`openai/gpt-oss-120b`, Groq's current tool-calling model; override with `GROQ_MODEL`). To use any
other OpenAI-compatible endpoint instead, set `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`.

#### Automatic model fallback (never run out of tokens mid-investigation)

Free tiers cap tokens **per model, per day**, and an agent loop is token-hungry — so a single busy
run can exhaust one model's daily budget. Instead of dying, the live brain walks a **fallback
chain**: when the current model hits its per-day cap (or is retired), it switches to the next model
and continues the same investigation. Configure the chain in `.env` (comma-separated, primary
first):

```ini
GROQ_MODEL=openai/gpt-oss-120b
LLM_MODELS=openai/gpt-oss-120b,openai/gpt-oss-20b,qwen/qwen3.8-27b
```

```
$ python -m graphsleuth demo --target ACC-9003
  [llm] 'openai/gpt-oss-120b' hit its daily cap/unavailable -> switching to 'openai/gpt-oss-20b'
  [1] recall(...) ...
```

To **pin** the brain to one model for a task (no fallback), pass `--model`:

```powershell
python -m graphsleuth demo --target ACC-9003 --model qwen/qwen3.8-27b   # a specific model
python -m graphsleuth pipeline --model openai/gpt-oss-20b                # cheaper/faster for triage
```

A real live run where `120b` ran dry mid-investigation and `20b` finished it is committed at
[`demos/MODEL_FALLBACK.md`](demos/MODEL_FALLBACK.md).

> **Free-tier tip.** Groq's free tier caps tokens/minute *and* tokens/day per model, and an agent
> loop is token-hungry. The transport retries `429`/`5xx` itself, honouring the API's *"try again in
> Xs"* hint, so a burst limit merely slows the run. A **daily** cap is not a burst — retrying would
> hang for hours — so the brain immediately moves to the next model in `LLM_MODELS` (and only gives
> up, with a clear `--offline` hint, once the whole chain is spent). `LLM_REASONING_EFFORT=low`
> (default) keeps reasoning-token spend down.

---

## MCP: the toolbelt is shared infrastructure

`graphsleuth/mcp_server.py` exposes all twenty tools over MCP (stdio), so any MCP client —
Claude Desktop, an IDE agent, another agent on the team — works against the same graph:

```json
{
  "mcpServers": {
    "graphsleuth": {
      "command": "D:\\Hackathon\\GraphSleuth\\.venv\\Scripts\\python.exe",
      "args": ["-m", "graphsleuth.mcp_server"],
      "env": {
        "FALKORDB_HOST": "localhost",
        "FALKORDB_PORT": "6379",
        "GRAPHSLEUTH_TENANT": "default"
      }
    }
  }
}
```

Verified with a real MCP stdio client: `initialize` OK, `tools: 20`, and live `entity_search` /
`detect_suspicious_cluster` calls returned graph data. `GRAPHSLEUTH_TENANT` selects which tenant
graph the server process talks to (one graph per process — see
[Multigraph tenancy](#multigraph-tenancy-one-graph-per-tenant)).

Tool selection, schemas and descriptions are the same ones the built-in agent uses, so a
second agent picks up the graph without any extra wiring.

---

## Cross-session memory (Track 02 flavour)

```powershell
python -m graphsleuth demo            # investigates ACC-9003, writes memory + opens a case
python -m graphsleuth demo            # second run recalls the first run's conclusion
```

On the second run the agent's briefing already contains the previous finding, the open case and
the flagged account — visible in the `recall` step of the evidence trail (the second run's
`recall` returns the first run's own memory note and open case). Memory is scoped by tenant, and with multigraph tenancy each
tenant has its own graph, so the same deployment serves separate teams without leaking state.

---

## Multigraph tenancy (one graph per tenant)

Every tenant gets its own FalkorDB graph — `<FALKORDB_GRAPH>:<tenant>`, e.g. `graphsleuth:default`
and `graphsleuth:acme`. Isolation is therefore **topological, not a `WHERE`-clause convention**:
one tenant's agent physically cannot read another tenant's fraud records or memory, while both run
on the same FalkorDB instance.

The active tenant travels through a `ContextVar` (`db.set_tenant` / `db.tenant_scope`), so a
`db.query()` anywhere in the stack hits the right graph without threading a tenant argument through
every call site. The MCP server picks its tenant from `GRAPHSLEUTH_TENANT` (one graph per process).

Isolation proof — write a canary note in `acme`, then look for it from `default`:

```powershell
.\.venv\Scripts\python.exe -c "from graphsleuth import db, memory as m; db.set_tenant('acme'); m.remember('episodic','canary: ACC-9003 seen in acme',['ACC-9003']); print('acme notes   :', len(m.recall('ACC-9003'))); db.set_tenant('default'); print('default notes:', len(m.recall('ACC-9003'))); print('drop acme    :', db.drop_tenant('acme'))"
```

Verified live:

```
acme notes   : 1
default notes: 0
drop acme    : True
```

The note is visible only in `acme` (1) and never in `default` (0); `db.drop_tenant('acme')` deletes
the whole `graphsleuth:acme` graph, canary included.

---

## Graph algorithms as agent tools

Four of the twenty tools are graph algorithms the model can call like any other tool. They run as
**native FalkorDB procedures**:

| Tool | FalkorDB procedure | Verified output (live) |
|---|---|---|
| `connected_components` | `algo.WCC` (engine reports `falkordb:algo.WCC`) | 1 component of 1701 nodes |
| `community_detection` | `algo.labelPropagation` | 187 communities (sizes 105 / 78 / 65 / 60 / 39 …) |
| `centrality` | `algo.betweenness`, `algo.HarmonicCentrality` | betweenness top: `ACC-0124` (6719.28), `ACC-0170`, `ACC-0172` |
| `page_rank` | see the quirk below | with `label='Account'`: `ACC-0141` (0.00443), `ACC-0187`, `ACC-0145` |

**Honest quirk — `page_rank`.** FalkorDB's native `algo.pageRank` returns 0 rows on
`falkordb:latest` (verified on both a toy graph and the real one), so the tool falls back to an
**in-process power iteration** (20 iterations, damping 0.85) over the same graph and says so in its
result:

```
engine: "in-process power iteration (algo.pageRank yielded 0 rows)"
```

Every algorithm result carries an `engine` field, so you can always tell which path produced the
ranking — documented quirk plus graceful degradation instead of a silent wrong answer.

---

## Two agents, one graph (Track 02)

```powershell
python -m graphsleuth pipeline --target ACC-9003 --save demos/PIPELINE_REPORT.md
```

A **triage agent** works the alert queue fast (prompt `TRIAGE_PROMPT`, max 6–8 rounds):
`list_alerts`, `recall`, `entity_search`, `find_shared_links`, `detect_suspicious_cluster`. If it
escalates, it calls `create_handoff(to_role='investigator', context=..., entity_ids=[...])` — which
writes a `Handoff` node into the shared graph — and `remember`s what it did. The **investigator**
starts with `list_handoffs` / `accept_handoff` and continues from graph state alone.

No state is passed through the caller: the two agents are separate sessions and the graph is the
only thing they share.

Verified offline run (`demos/PIPELINE_REPORT.md`, produced by `seed` → `demo` → `pipeline`):

* triage escalated → a `Handoff` node created in the shared graph, then `accepted`;
* the investigator's evidence-chain step 1 is triage's own memory note;
* the investigator opened `CASE-0003` (8 entities), flagged `ACC-9003` and wrote a memory note;
* each agent block ends with an **Actions taken (verified from the tool log)** line, read from the
  real tool trace — so the report says what actually ran, not just what the model claims.

A **real-model** version of the same pipeline (both brains `groq:openai/gpt-oss-120b`) is committed
at [`demos/LIVE_RUN.md`](demos/LIVE_RUN.md).

---

## Concurrency

`python -m graphsleuth bench --writers 4 --readers 16 --seconds 4` points 20 threads at the same
tenant graph; writes are real `BenchTick` nodes, cleaned up afterwards. Verified live:

| | ops | ops/s | p50 | p95 | mean |
|---|---|---|---|---|---|
| reads (16 threads) | 1956 | 487.8 | 20.55 ms | 71.07 ms | 32.71 ms |
| writes (4 threads) | 351 | 87.5 | 45.43 ms | 82.3 ms | 45.6 ms |
| **total** | **2307 in 4.01 s** | **575.3** | — | — | — |

**0 errors.** `--save demos/out/bench_default.json` writes the raw numbers.

---

## Verified run (real FalkorDB, real graph writes)

Everything here was produced against FalkorDB running in Docker while building the project. The
full committed transcript is [`demos/EXAMPLE_REPORT.md`](demos/EXAMPLE_REPORT.md) (regenerate it
with `seed` → `demo --offline`; the CASE numbers below assume that fresh-seed order). With
`GROQ_API_KEY` set the same command runs on the live model and prints `LLM: LIVE (...)`.

```
$ python -m graphsleuth status
FalkorDB OK  ->  localhost:6379  (multigraph: one graph per tenant)
  active graph: 'graphsleuth:default'  (tenant 'default')
  LLM: offline fallback (scripted brain) — set GROQ_API_KEY for the real model

$ python -m graphsleuth seed
Seeded graph: 1702 nodes, 2713 relationships

$ python -m graphsleuth demo --target ACC-9003 --save demos/EXAMPLE_REPORT.md
  [1] recall(query=ACC-9003)                -> notes, open_cases
  [2] entity_search(query=ACC-9003)         -> matches=[1]
  [3] graph_neighbors(entity_id=ACC-9003)   -> nodes/edges around the target
  [4] trace_money_flow(account_id=ACC-9003) -> outgoing={levels:3, reached:3}, total_out=5530000.0
  [5] find_shared_links(account_id=ACC-9003)-> linked_accounts=4
  [6] detect_suspicious_cluster(min_signals=2)
      -> clusters_found=1, clusters_filtered_out=4
  [7] shortest_connection(ACC-EXT-01 -> ACC-9100, via=[ORIGINATED, DESTINED])
      -> connected=True, hops=6
  [8-11] open_case, add_case_note, flag_account, remember
  all 11 tool calls [ok]
```

Selected output (copied from the report):

> **Verdict** — ACC-9003 is a confirmed hop in a 6-account mule ring that pools same-day transfers
> into beneficiary account ACC-9100. Escalated.

* **Cluster:** the 6-member cluster (`ACC-9001…ACC-9005` + collector `ACC-9100`) scores **41.05**;
  the five mule accounts are linked by shared devices `DEV-9001` / `DEV-9002` and `ACC-9100` is
  attached as the **collector** because it receives from three of them. **4** innocent device-share
  clusters were filtered out as noise.
* **Money path** (money-only, no case/alert detours):
  `ACC-EXT-01 -[ORIGINATED]-> TX-9000 -[DESTINED]-> ACC-9001 -[ORIGINATED]-> TX-9001 -[DESTINED]-> ACC-9002 -[ORIGINATED]-> TX-9008 -[DESTINED]-> ACC-9100`
* **Actions on the graph:** opened `CASE-0002` involving the 8 unique entities, added a case note,
  flagged `ACC-9003` as `high` (`under_review`) and wrote an episodic memory note. The report's
  **Actions taken (verified from the tool log)** section lists exactly these — from the trace.
* **Cross-session memory:** running `demo` a second time, step 1's `recall` returns the first run's
  own conclusion — proof that state lives in the graph, not the process.
* **MCP:** a real MCP stdio client (`initialize` → `list_tools` → `call_tool`) sees all 20 tools and
  gets live graph data back (`tools: 20`; live `entity_search` + `detect_suspicious_cluster` calls).
* **Tests:** `tests/test_offline.py` → 15/15 pass (no database required); `compileall` clean.

> Run ids (`AL-…`, `NOTE-…`, `MEM-…`, `HANDOFF-…`) are generated per run, so they differ between
> runs; the committed report files are the source of truth for any specific id.

A demo run adds case/alert/note/memory nodes, and the pipeline adds a handoff, a case and more
notes, so the node count grows past the seed's 1702 — visible with `python -m graphsleuth status`.

---

## Track coverage

| Track | How GraphSleuth hits it |
|---|---|
| **01 — Act on connected data** | Multi-hop reasoning over accounts/devices/transactions; native graph algorithms as tools (`algo.WCC`, `algo.labelPropagation`, betweenness/harmonic centrality, plus page-rank with a documented fallback); union-find clustering over shared-device links with beneficiary-collector corroboration; BFS pathfinding with an optional relationship filter; Cypher as a read-only tool; actions (alert, case) driven by graph evidence; full evidence trail + Mermaid path |
| **02 — Memory & coordination** | Episodic (`Session`/`Message`), semantic and procedural (`MemoryNote`) memory persisted in the graph; memory briefing before every question; two agents coordinate through `Handoff` nodes written to the shared graph (triage → investigator, no caller-pasted context); cases/alerts/notes are shared state any agent (or MCP client) can pick up; one FalkorDB graph per tenant (topological isolation) |
| **03 — Company brain** | The same pattern applied to an org: people, accounts, records, decisions and cases in one graph, with every answer traceable to its source entities and prior notes |

---

## Project layout

```
GraphSleuth/
├─ graphsleuth/
│  ├─ config.py        # env config (FalkorDB + LLM)
│  ├─ db.py            # the only module that talks to FalkorDB (multigraph tenancy)
│  ├─ seed.py          # synthetic fraud network + baseline memory
│  ├─ tools.py         # the 20 graph tools (reads, algorithms, handoffs, writes)
│  ├─ memory.py        # episodic / semantic / procedural memory
│  ├─ llm.py           # live (Groq / OpenAI-compatible) + offline fallback brains
│  ├─ agent.py         # the investigation loop + system prompt
│  ├─ agents.py        # two-agent pipeline (triage → handoff → investigator)
│  ├─ bench.py         # concurrency benchmark
│  ├─ explain.py       # evidence trail + Mermaid path rendering
│  ├─ cli.py           # status / seed / demo / pipeline / bench / chat
│  └─ mcp_server.py    # the same 20 tools over MCP (stdio)
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
* **Strong links cluster, weak links corroborate.** Shared devices (rare, and hard to fake) drive
  the union-find pass; shared beneficiaries are far too common among ordinary transfers to link
  accounts on their own, so they only *attach* a collector to a cluster. The same discipline applies
  to `shortest_connection` — `via=["ORIGINATED","DESTINED"]` follows money only instead of letting
  the path cut through an unrelated case node.
* **FalkorDB-specific Cypher quirks are handled:** its `ORDER BY` can only reference variables that
  are projected or grouped in that scope, so every sorting query goes through
  `WITH … ORDER BY … LIMIT … RETURN`. The seed and all read tools were run against a live
  `falkordb/falkordb` container, not a mock.
* **Tenancy is a graph boundary, not a filter.** Each tenant is a separate FalkorDB graph selected
  through a `ContextVar`, so isolation is structural; and when a native procedure misbehaves
  (`algo.pageRank` returns 0 rows), the tool degrades gracefully *and reports which engine ran*
  (`engine: in-process power iteration …`) rather than failing silently.
