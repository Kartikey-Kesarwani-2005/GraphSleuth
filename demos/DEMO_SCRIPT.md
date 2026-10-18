# GraphSleuth — 3-minute demo script for judges

Core walkthrough (segments 0–4) runs fully offline: a labelled fallback brain picking the same tool
calls against a real FalkorDB graph. Set `GROQ_API_KEY` in `.env` and the same commands are driven
by the live model (`LLM: LIVE (groq:openai/gpt-oss-120b)`) — nothing else changes. Segments 5–8
are 20–45s deep-dives.

> **Free tier tip.** Groq's free tier caps tokens/minute *and* tokens/day per model, and an agent
> loop is token-hungry. The transport retries `429`/`5xx` itself (honouring the API's *"try again in
> Xs"*), so a per-minute limit just slows a run down. A **daily** cap is not a burst — the brain
> switches to the next model in `LLM_MODELS` and carries on (`[llm] … switching to …`); a real
> capture is in [`MODEL_FALLBACK.md`](MODEL_FALLBACK.md). Only if the whole chain is spent do you
> need `--offline` (the whole walkthrough still works) or to wait it out. Pin one model for a task
> with `--model`. The default `openai/gpt-oss-120b` uses little reasoning budget
> (`LLM_REASONING_EFFORT=low`).

## 0. Setup (once, ~1 minute)

```powershell
docker compose up -d
.\.venv\Scripts\python.exe -m graphsleuth seed
.\.venv\Scripts\python.exe -m graphsleuth status
```

`status` proves the context layer is live **and** that it is a multigraph — one FalkorDB graph per
tenant:

```
FalkorDB OK  ->  localhost:6379  (multigraph: one graph per tenant)
  active graph: 'graphsleuth:default'  (tenant 'default')
  nodes: 1702   relationships: 2713
  LLM: offline fallback (scripted brain) — set GROQ_API_KEY for the real model
```

## 1. The one-agent problem (15s)

> "A transfer lands on ACC-9003. Individually every record looks normal. The fraud is in the
> connections — shared devices, a same-day chain, prior alerts, the beneficiary at the end."

## 2. Run the investigation (60s)

```powershell
.\.venv\Scripts\python.exe -m graphsleuth demo --target ACC-9003 --save demos/out/report.md
```

Point at the live tool log as it streams:

| Step | Tool | What the graph says |
|---|---|---|
| 1 | `recall` | memory already knows ACC-9002 was alerted and DEV-9001 is suspect |
| 2 | `entity_search` | resolves ACC-9003 (risk 0.77) |
| 3 | `graph_neighbors` | device DEV-9001/DEV-9002, person, transactions |
| 4 | `trace_money_flow` | ACC-EXT-01 → 9001 → 9002 → **9003** → 9004 → 9005 → ACC-9100, same day |
| 5 | `find_shared_links` | 9001/9002/9004 share devices; alerts on 9002 and 9004 |
| 6 | `detect_suspicious_cluster` | union-find ranks the 6-member cluster top (score 41.05) |
| 7 | `shortest_connection` | narrates the exact chain EXT → ... → ACC-9100 (6 hops) |
| 8-9 | `what_if_removed`, `assess_ring_resilience` | counterfactual + whole-ring resilience (read-only) |
| 10-11 | `ring_timeline`, `find_similar_rings` | when it happened (burst + activation order) + is the playbook repeating elsewhere (read-only) |
| 12-15 | `open_case`, `add_case_note`, `flag_account`, `remember` | acts on the graph + writes memory |

Expected trail (all 15 calls `[ok]`, from `demos/EXAMPLE_REPORT.md`):

```
[6] detect_suspicious_cluster(min_signals=2)
    -> clusters_found=1, clusters_filtered_out=4
[7] shortest_connection(ACC-EXT-01 -> ACC-9100, via=[ORIGINATED, DESTINED])
    -> connected=True, hops=6
[8] what_if_removed(DEV-9001, ACC-EXT-01 -> ACC-9100)
    -> impact=minor, robustness=100/100 (verdict survives losing the device)
[9] assess_ring_resilience(ring_key=top)
    -> resilience=bottlenecked, articulation points: ACC-9001, ACC-9100
[10] ring_timeline(ring_key=top)
    -> 19 events, burst 9/hour, activation order 11:00 -> 11:35, alerts led the money
[11] find_similar_rings(ring_key=top)
    -> closest look-alike 19/100, verdict unique_playbook (no copycat cell)
[12-15] open_case, add_case_note, flag_account, remember
```

Actions taken: opened `CASE-0002` (8 entities), added a case note, flagged `ACC-9003` as `high`
(`under_review`) and wrote an episodic memory note. The report also prints an **Actions taken
(verified from the tool log)** list so the actions are machine-checked, not just model prose.

## 3. Show the path (30s)

Open `demos/out/report.md`: verdict, numbered evidence chain, **Mermaid graph of the traversed
path**, tools used, actions taken, confidence.

Point at the money-only path (proved by `shortest_connection` with `via=["ORIGINATED","DESTINED"]`):

```
ACC-EXT-01 -[ORIGINATED]-> TX-9000 -[DESTINED]-> ACC-9001 -[ORIGINATED]-> TX-9001 -[DESTINED]-> ACC-9002 -[ORIGINATED]-> TX-9008 -[DESTINED]-> ACC-9100
```

> "Every hop in the answer maps to a tool call. If the graph didn't show it, the agent doesn't
> claim it."

## 4. Prove memory across sessions (30s)

```powershell
.\.venv\Scripts\python.exe -m graphsleuth chat
you> What was the last thing you concluded about ACC-9003?
```

The answer comes from the `remember` step of the previous run (and the open case), not from the
conversation — restart the process, even the machine, and it still knows. On a second `demo` run the
`recall` step returns the first run's own conclusion (its memory note and open case).

## 5. Prove tenant isolation (20s)

Each tenant is its own FalkorDB graph (`graphsleuth:default`, `graphsleuth:acme`, …), so isolation
is structural, not a `WHERE` clause. Write a canary note in `acme` and look for it from `default`:

```powershell
.\.venv\Scripts\python.exe -c "from graphsleuth import db, memory as m; db.set_tenant('acme'); m.remember('episodic','canary: ACC-9003 seen in acme',['ACC-9003']); print('acme notes   :', len(m.recall('ACC-9003'))); db.set_tenant('default'); print('default notes:', len(m.recall('ACC-9003'))); print('drop acme    :', db.drop_tenant('acme'))"
```

```
acme notes   : 1
default notes: 0
drop acme    : True
```

Point at it: the note exists only in `acme` (1) and never in `default` (0); `db.drop_tenant('acme')`
removes the whole `graphsleuth:acme` graph, canary with it.

## 6. Native graph algorithms as tools (30s)

The agent can call FalkorDB's `algo.*` procedures like any other tool:

```powershell
.\.venv\Scripts\python.exe -c "from graphsleuth import tools; import json; print(json.dumps(tools.connected_components(), indent=2)[:400]); print(json.dumps(tools.community_detection(), indent=2)[:400]); print(json.dumps(tools.centrality(label='Account'), indent=2)[:400]); print(json.dumps(tools.page_rank(label='Account'), indent=2)[:400])"
```

Point at the `engine` line in each result:

```
connected_components  -> engine: falkordb:algo.WCC            components_found: 1 (1702 nodes)
community_detection   -> engine: falkordb:algo.labelPropagation  communities_found: 181
centrality            -> engine: falkordb:algo.betweenness (exact, samplingSeed=42)
                         top: ACC-0084 (181363.62), ACC-0141, ACC-0124
page_rank             -> engine: falkordb:algo.pageRank
                         accounts: ACC-0141, ACC-0187, ACC-0145
```

> "Every algorithm result carries the `engine` that produced it — and two FalkorDB signatures are
> worth knowing. `algo.pageRank` takes `(nodeLabel, relationshipType)`, not `(iterations, damping)`;
> passing two numbers matches nothing, which is why our first version *thought* PageRank was broken.
> And `algo.betweenness` samples source nodes with a time-based seed by default, so we pin
> `samplingSeed=42` and raise `samplingSize` past the node count for an exact, reproducible ranking."

## 7. Multi-agent pipeline (45s)

```powershell
.\.venv\Scripts\python.exe -m graphsleuth pipeline --target ACC-9003 --verify --save demos/PIPELINE_REPORT.md
```

Walk the printed blocks:

| Block | What to point at |
|---|---|
| **TRIAGE** | runs `list_alerts` / `recall` / `entity_search` / `find_shared_links` / `detect_suspicious_cluster`, then escalates with `create_handoff(to_role='investigator', …)` |
| **HANDOFF (state shared through the graph)** | `created` and then `accepted` — a `Handoff` node written to the shared graph, nothing passed through the caller |
| **INVESTIGATOR** | starts from `list_handoffs` / `accept_handoff`; evidence-chain step 1 is triage's memory note; opens `CASE-0002` (8 entities), flags `ACC-9003`, writes a memory note |
| **VERIFIER** (with `--verify`) | an adversarial third agent that re-derives the claims from the graph and returns **CONFIRMED / WEAK / REFUTED**, plus the writes it really made |

> "Three separate agent sessions, no shared memory object — FalkorDB is the entire channel between
> them. The verifier's whole job is to disagree: it only ever agrees when the graph makes it."

## 7b. Reproducible scorecard (20s)

```powershell
.\.venv\Scripts\python.exe -m graphsleuth eval
```

Eleven pass/fail lines against the live graph — ring precision, money path, native algorithms,
memory round-trip, tenant isolation, procedural learning, verified actions, read-only guard:

```
score: 11/11 checks passed (100%)
```

> "A judged demo shouldn't be 'trust me' — the seed is deterministic, so you can reproduce every
> number on your own machine."

## 7c. Live web console (30s)

```powershell
.\.venv\Scripts\python.exe -m graphsleuth serve      # open http://127.0.0.1:8765
```

Click **Investigate** and point at the stream: each tool call appears the moment the agent makes
it, and the graph on the canvas grows as entities are discovered. Switch the tenant box to show a
different graph on the same FalkorDB instance; untick "live model" to run the offline brain.

> **Same console, second mode.** Set the mode dropdown to **Company brain · Track 03**, type
> `SYS-ledger-db`, and press **Ask the company brain** — the agent picks the org tools itself and the
> canvas grows with people, systems, docs and decisions.

## 7d. Company brain — Track 03 (30s)

```powershell
.\.venv\Scripts\python.exe -m graphsleuth company --save demos/COMPANY_BRAIN.md
```

A *second* graph on the same FalkorDB instance (tenant `company`): people, teams, projects,
systems, docs, tickets, chats and decisions. Read the six sections aloud — each is a traversal that
names its sources:

* **who knows `SYS-ledger-db`** — the brief's exact
  `Person-[:WORKED_ON]->Project-[:USES]->System` query, ranked, plus declared `:KNOWS` experts;
* **who owns it**, and the teammates reachable from that owner;
* **what breaks if it goes down** — the reverse transitive `:DEPENDS_ON` closure (3 systems,
  4 projects, 3 teams at risk);
* **why a decision was made** — traced via `:SOURCED_FROM` to the doc/ticket/chat that caused it;
* **what we know, and where from** — docs · tickets · chats in one graph, each with its author;
* **what's going stale** — ageing docs, a superseded decision, and bus-factor risk.

> "It is not just a scripted tour — the six questions are real tools in the same MCP toolbelt, so
> the agent can pick them itself:"

```powershell
.\.venv\Scripts\python.exe -m graphsleuth company-chat --offline "who knows SYS-ledger-db?"
```

The answer comes back as **Answer / Evidence / Sources / Confidence**, every claim carrying its node
ids (`EMP-001`, `DOC-002`, `TKT-102`).

> "Same FalkorDB, same pattern, different domain. The fraud graph asks *is this account in a ring?*;
> the company graph asks *who knows this service, and why did we decide that?* — and every answer
> points at the nodes it came from."

## 8. Concurrency under load (20s)

```powershell
.\.venv\Scripts\python.exe -m graphsleuth bench --writers 4 --readers 16 --seconds 4 --save demos/out/bench_default.json
```

Point at the summary (20 threads, real `BenchTick` writes, cleaned up afterwards):

```
threads : 20 (4 writers + 16 readers), 4.01s
reads   : 1956  (487.8/s)   p50 20.55ms  p95 71.07ms  mean 32.71ms
writes  : 351   (87.5/s)    p50 45.43ms  p95 82.3ms   mean 45.6ms
total   : 2307 ops (575.3/s)
errors  : 0
```

## 9. MCP (optional, 20s)

Show the config in `README.md` — all twenty-six tools over MCP (stdio), so Claude Desktop or a
teammate's agent works against the same FalkorDB graph with no extra wiring (`GRAPHSLEUTH_TENANT`
picks the tenant graph per process). Verified with a real client: `initialize` OK, `tools: 26`.

## Talking points

* **The graph shapes the agent.** The system prompt teaches a graph workflow (resolve → expand →
  trace → cluster → prove → act) and every tool returns typed relationships.
* **Actions are graph writes.** Flagging an account creates an `Alert` node and links it to a
  `Case` — auditable, shared, persistent.
* **Memory and coordination are one graph.** Sessions, memory notes (episodic / semantic /
  procedural) and `Handoff` nodes all live beside the fraud data, so a second agent picks up work
  without any caller-pasted context.
* **Tenancy is a graph boundary.** Each tenant gets its own FalkorDB graph, so isolation is
  structural rather than a `WHERE` clause.
* **No dead ends.** `run_cypher` is a guarded read-only escape hatch for questions the twenty-six tools
  don't cover.
