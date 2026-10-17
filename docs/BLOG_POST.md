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

**Native algorithms as tools.** Four of the fraud tools are graph algorithms the model can call
like any other: `connected_components` (`algo.WCC`), `community_detection`
(`algo.labelPropagation`), `centrality` (`algo.betweenness` / `algo.HarmonicCentrality`) and
`page_rank` (`algo.pageRank`). They ran live and reproducibly: label propagation found 181
communities, WCC found one component of 1,702 nodes, betweenness (exact, pinned seed) put
`ACC-0084` at the top, and PageRank ranks 1,702 nodes natively.

**Tool selection is the agent's job.** Over MCP, all twenty-six tools are exposed with the same names,
schemas and descriptions the built-in agent uses — there is an offline test that asserts the MCP
parameters match the agent's toolbelt exactly, so the two can't silently drift. A real MCP stdio
client saw `tools: 26`, initialised cleanly, and got live graph data back from `entity_search` and
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

## Agents that share a graph — and check each other

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

Add `--verify` and a third agent joins whose entire purpose is to *disagree*. **GraphSleuth-Verifier**
doesn't trust the investigator's prose: it re-derives the claims from the graph itself
(`find_shared_links`, `list_alerts`, `detect_suspicious_cluster`, `shortest_connection`) and returns
CONFIRMED / WEAK / REFUTED with the discrepancies it found. A verification that can only ever agree
is worthless, so the prompt says exactly that.

The agent doesn't only remember *facts* either. When an investigation finishes, it distils its own
tool sequence into a **playbook** — a `MemoryNote kind=procedural`, de-duplicated by signature and
capped per tenant — and the next session's briefing suggests it. That is procedural memory in the
same graph as the data: `python -m graphsleuth memory` shows the playbooks it has learned.

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

## The same graph, a second domain: a company brain

The fraud graph is one instance of the pattern, not the pattern itself. `python -m graphsleuth
company` seeds a *second* FalkorDB graph — people, teams, projects, systems, docs, tickets, chats
and decisions — and answers the questions a company actually asks of its knowledge base:

* **who knows a system** — the brief's exact `Person-[:WORKED_ON]->Project-[:USES]->System` query,
  ranked, cross-checked against declared `:KNOWS` expertise;
* **who owns what**, and what **breaks if a service goes down** (the reverse transitive
  `:DEPENDS_ON` closure);
* **why a decision was made** — traced through `:SOURCED_FROM` to the doc, ticket or chat that
  caused it, and which earlier decision it supersedes;
* **what's going stale** — ageing docs, superseded decisions, and bus-factor risk.

Docs, tickets and chats live in one graph, each edge carrying the author, so an answer can always
cite where it came from. The dataset is hand-authored and checked offline, so every number in the
committed tour (`demos/COMPANY_BRAIN.md`) reproduces.

It isn't only a scripted tour: the six questions are real tools in the shared MCP toolbelt, so the
agent selects them itself (`python -m graphsleuth company-chat "who knows SYS-ledger-db?"`), answers
with node ids as citations, and the same mode exists in the web console.

## Why a graph at all?

Because every question above is a *path*, not a row or a similarity match. `docs/WHY_GRAPH.md` lays
the comparison out concretely — the reverse dependency closure as one Cypher pattern versus a
recursive `WITH RECURSIVE` CTE, and why a vector store cannot answer *"is this account in a ring?"*
(mule ring members are connected, not similar; ids are exact; the answer needs provenance). The
short version: vectors are a good *front door* to find the starting node, but the reasoning — and the
answer — is a traversal.

## Honest engineering notes

We kept the warts in the repo, because they're more useful than a polished story:

* **FalkorDB's `ORDER BY` in subqueries** needs the `WITH … ORDER BY … LIMIT … RETURN` pattern
  (it can only sort variables projected or grouped in that scope). Every sorting query follows it.
* **`algo.pageRank` takes `(nodeLabel, relationshipType)`**, not `(iterations, damping)`. Our first
  version passed two numbers, which FalkorDB reads as a label/type filter matching nothing — so
  `page_rank` looked broken and fell back to a Python power iteration. Reading the procedure docs
  fixed it: `algo.pageRank(null, null)` ranks the whole graph natively, and we filter the output by
  label in Python.
* **`algo.betweenness` is sampled by default** (32 source nodes, time-based seed), so its top nodes
  changed between runs. We pass a config map pinning `samplingSeed` and raising `samplingSize` above
  the node count, which makes FalkorDB use every source node — an exact, reproducible ranking the
  tool labels `engine: "falkordb:algo.betweenness (exact, samplingSeed=42)"`.
* **`count(*)` can collapse rows.** While building the company brain, `count(*)` reported 1 project
  for a person who had matched 2 — counting a *bound* variable (`count(pr)`) returns the right
  number. The who-knows query counts the variable.

Neither is hidden: every algorithm result carries the `engine` that produced it, and there is a
`python -m graphsleuth eval` check that fails if PageRank stops being native.

## Reproducible numbers

Everything above was produced against a live `falkordb/falkordb` container:

* seed **1,702 nodes / 2,713 relationships** in `graphsleuth:default`;
* demo: **11/11 tool calls `[ok]`**, cluster score 41.05, 6 hops proved, `CASE-0002` opened;
* `eval`: **11/11 checks pass** against the live graph (ring precision, money path, algorithms,
  memory round-trip, tenant isolation, procedural learning, explainability, read-only guard);
* tests: **27/27** offline tests pass (`tests/test_offline.py`), `compileall` clean;
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

# 3. agents sharing state through the graph, then an adversarial verifier
.\.venv\Scripts\python.exe -m graphsleuth pipeline --target ACC-9003 --verify --save demos/PIPELINE_REPORT.md

# 4. reproduce the scorecard, or open the live console
.\.venv\Scripts\python.exe -m graphsleuth eval
.\.venv\Scripts\python.exe -m graphsleuth serve
```

**The takeaway.** Agents don't lose intelligence — they lose context. Put that context in a graph
and the relationships themselves become the reasoning: money you can trace hop by hop, clusters you
can prove, memory that outlives the session, and agents that coordinate through shared state
instead of a pasted prompt. Give your agent a memory that has edges.

*GraphSleuth — Team NexAI, "Graph Hacks: Context for AI Agents". Repo:
<https://github.com/Kartikey-Kesarwani-2005/GraphSleuth>.*
