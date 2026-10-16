# Give your agent a memory that has edges

**Agents don't lose intelligence — they lose context.** A frontier model can reason beautifully
inside one session and then wake up tomorrow having forgotten everything: who the suspect was, what
it already ruled out, which decision it made and why. We built **GraphSleuth** for the hackathon
*Graph Hacks: Context for AI Agents* on a single bet: if the context lives in a graph — the data,
the memory, and the decisions, all as nodes and edges — then the agent stops guessing and starts
following relationships.

GraphSleuth is a real fraud-investigation agent. Its context layer is **FalkorDB**. A live LLM picks
its own tools over a shared toolbelt; a deterministic scripted brain exists **only** as an
explicitly-labelled offline fallback, so the project runs with no API key but never pretends to be
something it isn't.

## The problem: smart model, amnesiac session

Fraud is rarely visible in a single record. It shows up as *structure* — accounts that share a
device, a same-day chain of pass-through transfers, a beneficiary that receives from three accounts
at once. An agent that only reads rows cannot see that. Worse, an agent that keeps its findings in
the conversation loses them the moment the process exits.

So we asked a blunt question: what if the agent has no private database at all? Everything it
knows — the fraud network, what previous sessions concluded, and what it decided to do — is nodes
and edges in one FalkorDB graph. The session becomes disposable; the graph is the memory.

## FalkorDB as the context layer

One graph holds the whole world:

```
(Person)-[:OWNS]->(Account)-[:USED_DEVICE]->(Device)
(Account)-[:ORIGINATED]->(Transaction)-[:DESTINED]->(Account)
(Account)-[:FLAGGED_BY]->(Alert)-[:PART_OF]->(Case)-[:INVOLVES]->(any entity)
(Handoff {status: pending|accepted})-[:ABOUT]->(any entity)
(Session)-[:SAID]->(Message)
(MemoryNote {kind: episodic|semantic|procedural})-[:ABOUT]->(any entity)
```

The seeded graph is 1,702 nodes and 2,713 relationships: 207 accounts, 207 people, 58 devices,
10 merchants, 1,213 transactions, plus the mule ring the demo discovers. Nothing is a special case —
an alert, a case note, a memory, and a handoff between agents are all just graph writes.

## How the graph shapes decisions

This is the part that matters: the graph isn't a database we bolt onto an agent, it's what makes the
reasoning possible.

**Multi-hop money tracing.** `trace_money_flow` walks `ORIGINATED`/`DESTINED` hops in both
directions. `shortest_connection` runs a BFS with an optional relationship filter, so the agent can
ask for a *money-only* chain — `via=["ORIGINATED","DESTINED"]` — and avoid cutting through an
unrelated case node. In the demo it proves a 6-hop chain from an offshore desk to the collector:

```
ACC-EXT-01 -[ORIGINATED]-> TX-9000 -[DESTINED]-> ACC-9001 -[ORIGINATED]-> TX-9001 -[DESTINED]-> ACC-9002 -[ORIGINATED]-> TX-9008 -[DESTINED]-> ACC-9100
```

**Clustering by structure, not vibes.** `detect_suspicious_cluster` runs a union-find pass where a
shared device is a *union link* (rare and hard to fake) and a shared beneficiary is only
*corroboration* — it attaches a collector that receives from 2+ members but never links accounts on
its own, because ordinary transfers share beneficiaries all the time. The demo finds a 6-member
cluster (mules `ACC-9001…ACC-9005` funnelling into `ACC-9100`, score 41.05) and filters 4 innocent
device-share clusters as noise. Strong links cluster; weak links corroborate.

**Native algorithms as tools.** Four of the twenty tools are graph algorithms the model can call
like any other: `connected_components` (`algo.WCC`), `community_detection`
(`algo.labelPropagation`), `centrality` (`algo.betweenness` / `algo.HarmonicCentrality`) and
`page_rank`. They ran live: label propagation found 187 communities, WCC found one component of
1,701 nodes, and betweenness put `ACC-0124` at the top with a score of 6,719.28.

**Tool selection is the agent's job.** Over MCP, all twenty tools are exposed with the same names,
schemas and descriptions the built-in agent uses. A real MCP stdio client saw `tools: 20`,
initialised cleanly, and got live graph data back from `entity_search` and
`detect_suspicious_cluster`. A second agent — Claude Desktop, an IDE plugin, a teammate's agent —
picks up the graph with no extra wiring.

## Memory that survives the session

Memory is not a side channel. It is three kinds of `MemoryNote` in the same graph, linked to the
entities they concern via `:ABOUT`:

* **episodic** — what a session did ("investigated ACC-9003, opened a case"),
* **semantic** — durable facts learned ("DEV-9001 is a known mule device"),
* **procedural** — how to work (the investigation playbook).

Before every question the agent gets a *memory briefing* assembled from the graph. Run the demo
twice and the second run's `recall` step returns the first run's own conclusion — restart the
process, even the machine, and it still knows. That is the whole point: state lives in FalkorDB,
not in a prompt.

## Two agents, one graph

Track 02 asked for coordination, so we stopped passing context between agents and started passing
it *through* the graph. `python -m graphsleuth pipeline` runs two agents:

1. A **triage** agent works the alert queue fast (`list_alerts`, `recall`, `entity_search`,
   `find_shared_links`, `detect_suspicious_cluster`) and, if it escalates, calls `create_handoff` —
   a `Handoff` node written into the shared graph carrying context and entity ids — plus a
   `remember`.
2. An **investigator** starts with `list_handoffs` / `accept_handoff`, reads what triage left
   behind, and continues the investigation.

Verified in a real run: triage escalated, a `Handoff` node was created and then `accepted`;
the investigator's evidence chain began with triage's own memory note, opened `CASE-0003` (8
entities), flagged `ACC-9003` and wrote a memory note. No state was passed through
the caller — the two agents share exactly one thing, the graph. Each agent block ends with an
**Actions taken (verified from the tool log)** line, read from the trace rather than the model's
prose.

## One graph per tenant

FalkorDB's multigraph topology let us make isolation *structural*: every tenant gets its own graph,
`<FALKORDB_GRAPH>:<tenant>` — `graphsleuth:default`, `graphsleuth:acme`. The active tenant travels
through a `ContextVar`, so a `db.query()` anywhere in the code hits the right graph without
threading a tenant argument through every call site.

The proof we like to show: write a canary memory note in tenant `acme`, then look for it from
`default`. `acme` sees 1 note; `default` sees 0. `db.drop_tenant('acme')` deletes the whole
`graphsleuth:acme` graph, canary included. Isolation is a graph boundary, not a `WHERE` clause — one
tenant's agent physically cannot read another's fraud records.

## Every answer ships its evidence

Explainability is a first-class output, not a paragraph of hedging. Each report contains the verdict,
a numbered evidence chain, the graph path (rendered as Mermaid), the tools used, the actions taken,
and a confidence note with what's still missing. If the graph didn't show it, the agent doesn't
claim it — every hop in the answer maps to the tool call that produced it.

## Honest engineering notes

We kept the warts in the repo, because they're more useful than a polished story:

* **FalkorDB's `ORDER BY` in subqueries** needs the `WITH … ORDER BY … LIMIT … RETURN` pattern
  (it can only sort variables projected or grouped in that scope). Every sorting query follows it.
* **`algo.pageRank` returns 0 rows** on `falkordb:latest` — verified on both a toy graph and the
  real one. So `page_rank` falls back to an in-process power iteration (20 iterations, damping 0.85)
  over the same graph and reports `engine: "in-process power iteration (algo.pageRank yielded 0
  rows)"`. A documented quirk plus graceful degradation, never a silent wrong answer.

## Reproducible numbers

Everything above was produced against a live `falkordb/falkordb` container:

* seed **1,702 nodes / 2,713 relationships** in `graphsleuth:default`;
* demo: **11/11 tool calls `[ok]`**, cluster score 41.05, 6 hops proved, `CASE-0002` opened;
* tests: **15/15** offline tests pass (`tests/test_offline.py`), `compileall` clean;
* concurrency: 20 threads for 4 seconds → **2,307 ops (575.3 ops/s), 0 errors**
  (reads 1,956 @ 487.8/s, p50 20.55 ms; writes 351 @ 87.5/s, p50 45.43 ms).

## Try it

```powershell
# 1. FalkorDB + venv + seed + demo, end to end
.\setup.ps1

# 2. go live: free key from https://console.groq.com/keys, put it in .env
#    GROQ_API_KEY=gsk_...
.\.venv\Scripts\python.exe -m graphsleuth status
#   LLM: LIVE (groq:openai/gpt-oss-120b)
#   fallback chain: openai/gpt-oss-120b -> openai/gpt-oss-20b -> qwen/qwen3.8-27b

# 3. two agents sharing state through the graph
.\.venv\Scripts\python.exe -m graphsleuth pipeline --target ACC-9003 --save demos/PIPELINE_REPORT.md
```

**The takeaway.** Agents don't lose intelligence — they lose context. Put that context in a graph
and the relationships themselves become the reasoning: money you can trace hop by hop, clusters you
can prove, memory that outlives the session, and two agents that coordinate through shared state
instead of a pasted prompt. Give your agent a memory that has edges.

*GraphSleuth — Team NexAI, "Graph Hacks: Context for AI Agents". Repo:
<https://github.com/Kartikey-Kesarwani-2005/GraphSleuth>.*
