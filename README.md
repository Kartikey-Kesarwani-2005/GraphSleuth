# 🕵️ GraphSleuth

[![CI](https://github.com/Kartikey-Kesarwani-2005/GraphSleuth/actions/workflows/ci.yml/badge.svg)](https://github.com/Kartikey-Kesarwani-2005/GraphSleuth/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Tests](https://img.shields.io/badge/tests-32%20passing-brightgreen)
![MCP](https://img.shields.io/badge/MCP-30%20tools-8A2BE2)
![License](https://img.shields.io/badge/license-MIT-green)

**A fraud investigation agent whose context layer is FalkorDB.**

GraphSleuth doesn't look up records — it follows relationships. Ask it about one suspicious
account and it walks the graph: shared devices, shared beneficiaries, prior alerts, same-day
pass-through transfers, the beneficiary at the end of the chain. Then it **shows the path it
took**, takes an action in the graph (raise an alert, open a case), and **remembers what it
learned** for the next session.

> Track 01 (agents that act on connected data) is the core. Track 02 (memory & coordination) is
> the same agent system. Track 03 (company brain) ships as a second, dedicated graph —
> `python -m graphsleuth company` — with people, teams, projects, systems, docs, tickets, chats
> and decisions. See [Track coverage](#track-coverage).

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
                                        │ 30 tools (24 fraud + 6 company)
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
| `python -m graphsleuth pipeline --target ACC-9003` | two agents on one graph: triage → handoff → investigator; add `--verify` for a third adversarial verifier agent; `--save` writes a combined report |
| `python -m graphsleuth bench --writers 4 --readers 16` | concurrency benchmark: mixed reads/writes with latency percentiles; `--save` writes raw JSON |
| `python -m graphsleuth eval` | reproducible scorecard: 11 pass/fail checks over the live graph (ring precision, money path, algorithms, memory, tenant isolation, explainability, read-only guard); `--save` writes the JSON |
| `python -m graphsleuth memory` | inspect the agent's memory: learned playbooks, notes and open cases for a tenant; `--kind episodic\|semantic\|procedural` filters |
| `python -m graphsleuth serve` | interactive web console: streams the investigation live and grows the graph as it runs (`--port`, `--host`) |
| `python -m graphsleuth company` | **Track 03 — Company Brain:** seed a second graph (people, teams, projects, systems, docs, tickets, chats, decisions) and tour six questions — who knows a system, who owns it, what breaks if it goes down, why a decision was made (traced to source), what we know (docs · tickets · chats), and what's going stale (`--system`, `--decision`, `--save`) |
| `python -m graphsleuth company-chat "who knows SYS-ledger-db?"` | **Track 03 as an agent:** ask an org question and the LLM picks the company tools itself, answering with node ids as citations (`--offline` runs the scripted brain, `--keep` reuses the graph, `--save` writes the answer) |
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

`graphsleuth/mcp_server.py` exposes all twenty-six tools over MCP (stdio) — the twenty fraud
tools plus the six company-brain tools — so any MCP client —
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

Verified with a real MCP stdio client: `initialize` OK, `tools: 26`, and live `entity_search` /
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

**Procedural memory — the agent learns how it works.** At the end of every substantial
investigation the agent distils its own tool sequence into a **playbook** (`MemoryNote
kind=procedural`, signature-deduplicated and capped per tenant) and writes it back to the graph. The
next session's briefing suggests that procedure, so the routine is not rediscovered from scratch:

```powershell
python -m graphsleuth memory
# PROCEDURAL — learned playbooks
#   * MEM-…  Playbook: recall > entity_search > graph_neighbors > trace_money_flow > …
```

The playbook is *how* the agent worked; the episodic note is *what* it found; the semantic note is
*what is durably true*. All three are `MemoryNote` nodes linked to the entities they concern via
`:ABOUT`, so recalling them is a graph traversal, not a vector guess.

---

## Reproducible evaluation (`eval`)

A judged demo should not be "trust me, it works":

```powershell
python -m graphsleuth eval
#   [PASS] graph is seeded            1702 nodes / 2713 relationships
#   [PASS] mule ring detected         top cluster 6 members, score 41.05, expected 6/6 present
#   [PASS] noise filtered (precision) 6 accounts flagged out of 207 (2%)
#   [PASS] money path traced          6 hops: ACC-EXT-01 -> … -> ACC-9100
#   [PASS] native graph algorithms    1 components, 1702 nodes ranked (betweenness), pageRank via falkordb:algo.pageRank
#   [PASS] memory round-trip / tenant isolation / procedural learning / verified actions / read-only guard
#   score: 11/11 checks passed (100%)
```

The seed is deterministic (`random.Random(42)`), so the numbers are reproducible run to run. The
isolation and memory checks run in a throwaway tenant (`eval-harness`) that is dropped afterwards,
so `eval` never leaves residue in your real graphs.

---

## Web console (`serve`)

The agent's value is *the path behind the answer*, and a terminal transcript hides it:

```powershell
python -m graphsleuth serve            # then open http://127.0.0.1:8765
```

A single-page app (no CDN, no build step) that streams the agent's live reasoning — each tool call
and result — over newline-delimited JSON, grows a force-directed graph of the entities it touches as
they are discovered, and shows the verdict, the graph-verified write actions and the tenant's
memory. Switch the tenant in the UI and the whole view is a different graph on the same FalkorDB
instance. Uncheck "live model" to run the deterministic offline brain.

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

Four of the fraud tools are graph algorithms the model can call like any other tool. They run as
**native FalkorDB procedures**:

| Tool | FalkorDB procedure | Verified output (fresh seed, reproducible) |
|---|---|---|
| `connected_components` | `algo.WCC` | 1 component of 1702 nodes |
| `community_detection` | `algo.labelPropagation` | 181 communities |
| `centrality` | `algo.betweenness` (exact) / `algo.HarmonicCentrality` | betweenness top: `ACC-0084`, `ACC-0141`, `ACC-0124`; harmonic top: `ACC-0188`, `ACC-0176`, `ACC-0084` |
| `page_rank` | `algo.pageRank(null, null)` | 1702 nodes ranked; most central accounts `ACC-0141`, `ACC-0187`, `ACC-0145`; merchants `M-02`, `M-08`, `M-10` lead overall |

Every algorithm result carries an `engine` field, so you can always tell which path produced the
ranking. Two FalkorDB behaviours are worth knowing — the tools handle them rather than hiding them:

* **`algo.pageRank` takes `(nodeLabel, relationshipType)`**, *not* `(iterations, damping)`. Passing
  two numbers is silently read as a label/type filter that matches nothing (which is why an earlier
  version of this project thought the procedure was broken and fell back to Python). We call
  `algo.pageRank(null, null)` for the whole graph and filter the output by `label` in Python, so
  `label='Account'` means *accounts central to the whole flow* — ranking strictly *within* accounts
  would be uniform, since accounts have no account→account edges.
* **`algo.betweenness` samples 32 source nodes with a time-based seed** by default, so its ranking
  changes between runs. The tool pins `samplingSeed=42` and sets `samplingSize` above the node
  count, which makes FalkorDB use *every* source node — an exact, reproducible ranking in ~0.4s
  (`engine: "falkordb:algo.betweenness (exact, samplingSeed=42)"`).

If a build ever lacks a procedure, the tool degrades gracefully and says so in its `engine` field
instead of failing silently.

---

## Counterfactual explainability (the part nobody else has)

Most "explainable agent" projects show the path the agent walked. GraphSleuth goes one step
further and **proves the verdict does not depend on a single link** — by removing nodes from the
graph (mentally, read-only) and re-running the same analysis:

| Tool | What it answers | Verified output (demo graph) |
|---|---|---|
| `what_if_removed(entity, from, to)` | "If this device/account didn't exist, would the case still hold?" | Removing `DEV-9001` → `impact=minor, robustness=100/100` (the money trail and the ring both survive). Removing `ACC-9001` → `impact=critical, robustness=0/100` (every chain runs through it). |
| `assess_ring_resilience(ring_key)` | "Where is this whole network actually vulnerable?" | `resilience=bottlenecked`: mean robustness 67/100, articulation points `ACC-9001, ACC-9100` — remove those and the case collapses. |
| `ring_timeline(ring_key)` | "When did this actually happen?" | 19 timestamped events (10 transfers / INR 12.8M, 3 merchant spends, alerts), burst of 9 events in one hour from `2026-10-12T11:00`, activation order `ACC-9001 → … → ACC-9100` (11:00 → 11:35), and alerts fired **before** the first traced transfer — detection led the money. |
| `find_similar_rings(ring_key)` | "Is this playbook running anywhere else?" | Compared against 4 other candidate groups: closest look-alike **19/100** → `unique_playbook` — the device-funnel-alert combination is unique to this case (innocent shared-device pairs score ~19, the real ring scores 100). |

Both tools are **read-only** (the graph is never modified), reuse the *same* union-find/BFS
implementations as the investigative tools (so the counterfactual can't drift from the analysis),
and return quantitative verdicts:

* `robustness` (0–100) — how much of the conclusion survives the removal,
* `load_bearing_score` (0–1) — how much the conclusion leans on exactly this node,
* `articulation_points` — the graph-theory answer to "where do we intervene first".

The demo, the pipeline's adversarial **verifier** agent, the offline verdict and the web console
all surface this: the verifier will not say `CONFIRMED` if the verdict is a single point of
failure, and the final report prints a **Disruption guidance** line naming the bottlenecks.
That is "explainable results" with a falsifiable claim attached — anyone can re-run the removal
and check the numbers themselves.

The same section of the report also answers *when* and *how many* — **Timeline** (burst window,
member-by-member activation order, alerts-vs-money lead time) and **Pattern check** (structural
fingerprint of the ring vs every other candidate group) — so the verdict is a story with a clock
and a base rate, not just a list of links. The stated **Confidence** is generated *from* those
numbers (a verdict that survives the counterfactual at robustness 100/100 says so; a
single-point-of-failure verdict can never claim "high"), which makes the agent's self-report
honest by construction.

It is also **live and interactive in the web console**: click any node on the graph and the
server runs the same read-only counterfactual against FalkorDB (`GET /api/stress`), the node is
ghosted on the canvas with its edges drawn as severed red dashes, and a panel shows the impact
badge, robustness `/100`, load-bearing score and the money trail *before → after*. A **Restore**
button puts the graph back. Nothing is written — the whole demo is one click away from being
reproduced by a judge.

After any run the console offers a **Proof pack**: *Copy verdict*, *Download report (.md)* and
*Download proof pack (.html)* — a single self-contained HTML file with the verdict, the numbered
evidence trail, the **live canvas snapshot**, the counterfactual numbers and the reproducibility
commands, printable to PDF straight from the browser. That is the artifact you hand a judge.

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

Three agents run against the same graph: **Triage** → **Investigator** → **Verifier**. No state passes through the caller: handoff and the open case live only in FalkorDB. The verifier is adversarial — it
re-proves the claim before adding its own notes.

### Three agents: the adversarial verifier (`--verify`)

```powershell
python -m graphsleuth pipeline --target ACC-9003 --verify --save demos/PIPELINE_REPORT.md
```

Adding `--verify` appends a third agent, **GraphSleuth-Verifier** (`VERIFIER_PROMPT`), whose whole
job is to *disagree*. It does not trust the investigator's prose: it re-derives the key claims from
the graph itself (`find_shared_links`, `list_alerts`, `detect_suspicious_cluster`,
`shortest_connection`) and returns **CONFIRMED / WEAK / REFUTED** with the discrepancies it found and
the writes it really made. It reads the case, alerts and notes the earlier agents left — again
through the graph only — and records its verdict as a case note plus a semantic memory note. A
verification that can only agree is worthless, so the prompt says so explicitly.

Verified offline run (`demos/PIPELINE_REPORT.md`, produced by `seed` → `demo` → `pipeline`):

* triage escalated → a `Handoff` node created in the shared graph, then `accepted`;
* the investigator's evidence-chain step 1 is triage's own memory note;
* the investigator opened `CASE-0003` (8 entities), flagged `ACC-9003` and wrote a memory note;
* each agent block ends with an **Actions taken (verified from the tool log)** line, read from the
  real tool trace — so the report says what actually ran, not just what the model claims.

A **real-model** version of the same pipeline (both brains `groq:openai/gpt-oss-120b`) is committed
at [`demos/LIVE_RUN.md`](demos/LIVE_RUN.md), and a **three-agent** real-model run — triage →
handoff → investigator → adversarial verifier, which returned **CONFIRMED** with no discrepancies —
is committed at [`demos/LIVE_RUN_3AGENT.md`](demos/LIVE_RUN_3AGENT.md).

---

## Company brain (Track 03)

The fraud graph proves the pattern; the company brain shows the *same pattern* on an org-shaped
question. `python -m graphsleuth company` seeds a **second FalkorDB graph** (tenant `company`) —
people, teams, projects, systems, docs, tickets, chats and decisions — and tours six questions,
each a graph traversal that names its sources:

```powershell
python -m graphsleuth company --system SYS-ledger-db --decision ADR-002 --save demos/COMPANY_BRAIN.md
```

1. **Who knows about this system?** — the brief's exact query, ranked:
   ```cypher
   MATCH (p:Person)-[:WORKED_ON]->(pr:Project)-[:USES]->(s:System {id: $system})
   WITH p, count(pr) AS projects
   ORDER BY projects DESC, p.id
   RETURN [p.id, p.name, projects] AS row
   ```
   …plus explicitly declared `:KNOWS` experts, so "knows" is both *inferred from work* and *stated*.
2. **Who owns it?** — owning person/team, and the teammates reachable from that owner.
3. **What breaks if it goes down?** — the reverse transitive `:DEPENDS_ON` closure, the projects at
   risk and the teams that own them.
4. **Why was this decision made?** — traced through `:SOURCED_FROM` to the doc/ticket/chat that
   caused it, who decided it, and which earlier decision it supersedes.
5. **What do we know, and where from?** — docs · tickets · chats about the system, each with its
   author, all from one graph.
6. **What knowledge is going stale?** — ageing docs, superseded decisions, and bus-factor risk
   (a system with ≤1 declared expert that projects depend on).

The committed tour is [`demos/COMPANY_BRAIN.md`](demos/COMPANY_BRAIN.md). The graph is hand-authored
(no RNG), so every number reproduces; `tests/test_offline.py` checks the dataset for referential
integrity, a real dependency chain, a superseded decision, and a source on every decision — with no
database needed.

**It is also an agent, not just a scripted tour.** The six questions are real tools in the shared
registry (`who_knows`, `owners_of`, `blast_radius`, `decision_trail`, `knowledge_sources`,
`stale_knowledge`), exposed over MCP and selectable by the model:

```powershell
python -m graphsleuth company-chat "who knows SYS-ledger-db, and what breaks if it goes down?"
python -m graphsleuth company-chat --offline "why did we decide ADR-002?"   # scripted brain, no key
```

The agent answers in an **Answer / Evidence / Sources / Confidence** format that cites node ids
(e.g. `EMP-001`, `DOC-002`, `TKT-102`), and the web console has a **Company brain** mode that runs
the same agent and grows the org graph live — no CLI needed.

Two FalkorDB quirks surfaced here and are handled in the queries: `count(*)` can collapse rows
(so the "who knows" query counts a bound variable, `count(pr)`), and the `ORDER BY` scoping rule
applies as everywhere else.

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
  all 15 tool calls [ok]
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
* **Actions on the graph:** opened the case involving the entities, added a case note,
  flagged `ACC-9003` as `high` (`under_review`) and wrote an episodic memory note. The report's
  **Actions taken (verified from the tool log)** section lists exactly these — from the trace.
* **Cross-session memory:** running `demo` a second time, step 1's `recall` returns the first run's
  own conclusion — proof that state lives in the graph, not the process.
* **MCP:** a real MCP stdio client (`initialize` → `list_tools` → `call_tool`) sees all 30 tools and
  gets live graph data back (live `entity_search` + `detect_suspicious_cluster` calls).
* **Tests:** `tests/test_offline.py` → **32/32** pass (no database required; includes MCP/agent
  parity, web console wiring, timeline/pattern, stress-test, proof-pack and counterfactual
  read-only guard); `compileall` clean. CI runs the suite and a real FalkorDB end-to-end job.

> Run ids (`AL-…`, `NOTE-…`, `MEM-…`, `HANDOFF-…`) are generated per run, so they differ between
> runs; the committed report files are the source of truth for any specific id.

> Run ids (`AL-…`, `NOTE-…`, `MEM-…`, `HANDOFF-…`) are generated per run, so they differ between
> runs; the committed report files are the source of truth for any specific id.

A demo run adds case/alert/note/memory nodes, and the pipeline adds a handoff, a case and more
notes, so the node count grows past the seed's 1702 — visible with `python -m graphsleuth status`.

---

## Track coverage

| Track | How GraphSleuth hits it |
|---|---|
| **01 — Act on connected data** | Multi-hop reasoning over accounts/devices/transactions; native graph algorithms as tools (`algo.WCC`, `algo.labelPropagation`, exact betweenness / harmonic centrality, native `algo.pageRank`); union-find clustering over shared-device links with beneficiary-collector corroboration; BFS pathfinding with an optional relationship filter; Cypher as a read-only tool; actions (alert, case) driven by graph evidence; full evidence trail + Mermaid path |
| **02 — Memory & coordination** | Episodic (`Session`/`Message`), semantic and procedural (`MemoryNote`) memory persisted in the graph; memory briefing before every question; **playbooks distilled from finished investigations and reused**; agents coordinate through `Handoff` nodes written to the shared graph (triage → investigator, optionally → an adversarial **verifier**, no caller-pasted context); cases/alerts/notes are shared state any agent (or MCP client) can pick up; one FalkorDB graph per tenant (topological isolation); concurrency benchmark + `eval` scorecard |
| **03 — Company brain** | A dedicated second graph (`python -m graphsleuth company`, tenant `company`): people, teams, projects, systems, docs, tickets, chats and decisions in one FalkorDB graph. It answers the brief's six questions — **who knows a system** (the brief's exact `Person-[:WORKED_ON]->Project-[:USES]->System` query, ranked, plus declared `:KNOWS` experts), **who owns what**, **what breaks if a service goes down** (reverse transitive `:DEPENDS_ON` closure), **why a decision was made** traced to the doc/ticket/chat it was sourced from, **what we know** (docs · tickets · chats, each with a named author), and **what knowledge is going stale** (ageing docs, superseded decisions, bus-factor risk). Beyond the scripted tour it also ships as an **agent** (`python -m graphsleuth company-chat`) whose six tools are in the shared MCP toolbelt, and as a **web-console mode**. See [`demos/COMPANY_BRAIN.md`](demos/COMPANY_BRAIN.md). |

---

## Project layout

```
GraphSleuth/
├─ graphsleuth/
│  ├─ config.py        # env config (FalkorDB + LLM)
│  ├─ db.py            # the only module that talks to FalkorDB (multigraph tenancy)
│  ├─ seed.py          # synthetic fraud network + baseline memory
│  ├─ company.py       # Track 03: company-brain graph, tour + agent (`company`, `company-chat`)
│  ├─ tools.py         # the 30 graph tools (24 fraud reads/algorithms/writes + 6 company-brain)
│  ├─ memory.py        # episodic / semantic / procedural memory
│  ├─ llm.py           # live (Groq / OpenAI-compatible) + offline fallback brains
│  ├─ agent.py         # the investigation loop + system prompt
│  ├─ agents.py        # multi-agent pipeline (triage → handoff → investigator → verifier)
│  ├─ evaluate.py      # reproducible evaluation scorecard (`eval`)
│  ├─ bench.py         # concurrency benchmark
│  ├─ explain.py       # evidence trail + Mermaid path rendering
│  ├─ cli.py           # status / seed / demo / pipeline / bench / eval / memory / company / company-chat / serve / chat
│  ├─ server.py        # zero-dependency web console (`serve`): live stream + growing graph
│  ├─ web/             # the console's single-page app (plain HTML/CSS/JS, no CDN)
│  └─ mcp_server.py    # the same 30 tools over MCP (stdio)
├─ tests/              # offline unit tests (no database needed)
├─ demos/              # demo script + saved reports
├─ docs/               # submission notes + why-graph comparison
├─ .github/workflows/  # CI: offline tests + FalkorDB end-to-end
├─ data/falkordb/      # graph storage (bind-mounted from the container)
├─ docker-compose.yml  # FalkorDB (and an optional `app` service: --profile app)
├─ Dockerfile          # the agent + web console image (optional)
├─ LICENSE             # MIT
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
* **The storage engine speaks paths.** Every question here is "how are these connected?" at an
  unknown depth — a traversal, not a scan or a similarity search. [`docs/WHY_GRAPH.md`](docs/WHY_GRAPH.md)
  makes that concrete (the dependency closure as one Cypher pattern vs a recursive CTE, and why a
  vector store cannot answer "is this account in a ring?").
* **FalkorDB-specific Cypher quirks are handled:** its `ORDER BY` can only reference variables that
  are projected or grouped in that scope, so every sorting query goes through
  `WITH … ORDER BY … LIMIT … RETURN`. The seed and all read tools were run against a live
  `falkordb/falkordb` container, not a mock.
* **Tenancy is a graph boundary, not a filter.** Each tenant is a separate FalkorDB graph selected
  through a `ContextVar`, so isolation is structural. Native procedures are invoked with their real
  signatures — and where a procedure is non-reproducible by default (`algo.betweenness` samples with
  a time-based seed), the tool pins a seed and reports the exact `engine` that ran instead of hiding
  the variability.
