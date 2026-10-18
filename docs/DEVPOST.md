# GraphSleuth — Devpost submission text

Paste-ready text for https://devpost.com (Team **NexAI**, code **NZ4NTP**).

---

## One-liner (submission tagline)

**GraphSleuth — a fraud investigation agent whose memory, evidence and handoffs all live in a
FalkorDB graph, so it follows connections, remembers across sessions, and can show the exact path
behind every answer.**

---

## Long description (the main "More information / About" box)

Agents don't lack intelligence — they lack context. They see pieces: a row here, a chat there,
nothing from last week, and no idea how any of it connects. GraphSleuth starts from the opposite
assumption: put the agent's context *in a graph*, and let the graph shape what the agent does.

**What it is.** GraphSleuth is a working fraud-investigation agent built on FalkorDB. The fraud
network (accounts, people, devices, merchants, transactions, alerts, cases) and the agent's own
long-term memory live in the same graph, so "what do I already know about ACC-9003?" is a graph
traversal, not a vector guess. A real LLM (Groq's free tier by default, any OpenAI-compatible
endpoint otherwise) drives the loop and picks the tools itself; every tool returns entity ids and
typed relationships, never flat rows.

**The graph does the work, not the demo script.**

* **Follow connections across data** — `trace_money_flow` walks funds hop by hop in both
  directions, `shortest_connection` proves the chain (`via=['ORIGINATED','DESTINED']` keeps the
  path money-only), `graph_neighbors` expands the neighbourhood.
* **Graph algorithms as agent tools** — FalkorDB's native procedures are registered as tools the
  model selects: `connected_components` (`algo.WCC`), `community_detection`
  (`algo.labelPropagation`), `centrality` (`algo.betweenness` / `algo.HarmonicCentrality`), plus
  `page_rank`. Alongside them, a union-find ring detector fuses three signals — shared devices
  (rare, strong), shared beneficiaries (corroboration) and prior alerts — and filters innocent
  device-sharing noise (on the seeded data: 1 real 6-account ring, 4 false clusters dropped).
* **Remember across sessions** — episodic (`Session`/`Message`), semantic and procedural memory as
  `MemoryNote` nodes linked to entities via `:ABOUT`. A second session starts with a briefing
  built from the graph: open cases, past findings, the playbook it learned earlier. Finished
  investigations distil their own tool sequence into a reusable **playbook** (procedural memory),
  de-duplicated and capped per tenant.
* **Share state between agents** — `python -m graphsleuth pipeline` runs **Triage → Investigator**:
  triage writes its verdict, context and entity ids into a `Handoff` node; the investigator's very
  first calls are `list_handoffs` / `accept_handoff`, and it continues from what the graph holds.
  No state is pasted through the caller — FalkorDB is the only channel. Add `--verify` for a third **GraphSleuth-Verifier** agent that treats the verdict adversarially: it re-derives the investigator's claims from the graph itself and returns CONFIRMED / WEAK / REFUTED with any discrepancies. A verification that can only agree is worthless, so it is told to try to break the case.
* **Separate memory per tenant** — multigraph topology: every tenant gets its own graph
  (`graphsleuth:default`, `graphsleuth:acme`). Isolation is structural, not a WHERE clause — a
  canary note in tenant `acme` is invisible from `default` even under raw Cypher.
* **Show the path behind an answer** — every report ships an evidence chain with ids and
  relationship types, a compact graph path, the tools used, the actions taken, and a confidence
  rating with what is still missing. A zero-dependency **web console** (`python -m graphsleuth serve`)
  streams each tool call live and grows the graph as the agent works.
* **Tool selection through MCP** — the same 30 tools (24 fraud + 6 company-brain) are served over
  MCP stdio
  (`python -m graphsleuth.mcp_server`), so Claude Desktop or any MCP client gets the identical
  graph toolbelt (verified: initialize → 30 tools → live calls; an offline test asserts the MCP
  parameters match the built-in toolbelt exactly).

**Measured on the judges' own terms (verified live, not aspirational):** seed 1702 nodes / 2713
relationships; demo 15/15 tool calls ok; ring score 41.05 with 6 members; 6-hop money path from
`ACC-EXT-01` to the collector `ACC-9100`; pipeline handoff created → accepted and case opened;
`eval` 11/11 checks pass; 20-thread concurrency bench 2307 ops in 4 s (575 ops/s, reads p95 71 ms,
writes p95 82 ms, 0 errors); MCP client sees all 30 tools; 32/32 offline tests pass. Run
`python -m graphsleuth eval` to reproduce the scorecard yourself.

**Honest engineering notes:** FalkorDB's `ORDER BY` needed the `WITH … ORDER BY … LIMIT … RETURN`
pattern in memory queries (found by running it live). And `algo.pageRank` takes
`(nodeLabel, relationshipType)` — not `(iterations, damping)`; passing two numbers makes it match
nothing, which is why an early build of this project thought the procedure was broken. We now call
`algo.pageRank(null, null)` natively and filter by label afterwards. Likewise `algo.betweenness`
samples source nodes with a time-based seed by default; the tool pins `samplingSeed=42` and raises
`samplingSize` past the node count for an exact, reproducible ranking. Every algorithm result reports
the `engine` that produced it.

**Try it:** `powershell -ExecutionPolicy Bypass -File .\setup.ps1` (venv → deps → Docker →
FalkorDB → seed → demo), then put a free `GROQ_API_KEY` in `.env` and re-run — the real model
takes over the loop. Repo: https://github.com/Kartikey-Kesarwani-2005/GraphSleuth

---

## Track choice

Enter primarily in **Track 01 — Agents That Act on Connected Data** (multi-hop reasoning, graph
queries/algorithms as tools, tool selection through MCP, explainable results). Explainability goes
beyond "here is the path": `what_if_removed` re-runs the analysis with a node deleted (robustness
0–100, load-bearing score) and `assess_ring_resilience` stress-tests the whole ring at once,
naming the articulation points — the verifier refuses `CONFIRMED` if the case is a single point of
failure. `ring_timeline` reconstructs *when* the ring acted (burst window, per-member activation
order, whether alerts led the money) and `find_similar_rings` fingerprints it against every other
candidate group, so the report answers where/when/how-many, not just what. The console adds a
click-to-stress-test on any node and a one-click printable proof pack. It also covers
Track 02 (episodic/semantic/procedural memory, playbook learning, handoffs between agents — plus an
adversarial verifier agent, high-concurrency reads/writes, per-tenant memory) and Track 03 as a
dedicated second graph: `python -m graphsleuth company` (people, teams, projects, systems, docs,
tickets, chats, decisions) answers *who knows a system*, *who owns it*, *what breaks if it goes
down*, *why a decision was made* (traced to source), and *what's going stale* — every answer names
its source nodes (`demos/COMPANY_BRAIN.md`). Track 03 is agentic too: the same six questions are
tools in the shared 30-tool MCP belt (`python -m graphsleuth company-chat "who knows SYS-ledger-db?"`)
and a web-console mode. For the "why a graph, not SQL/vector" argument, see `docs/WHY_GRAPH.md`.

## Gallery / links to include

* `demos/EXAMPLE_REPORT.md` — full investigation report from a real run
* `demos/PIPELINE_REPORT.md` — multi-agent run: triage verdict → handoff → investigator → verifier
* `demos/LIVE_RUN_3AGENT.md` — the same pipeline driven by a real model (3 agents, verdict CONFIRMED)
* `demos/COMPANY_BRAIN.md` — Track 03 org-graph tour (who knows / owns / depends / decided / stale)
* `docs/WHY_GRAPH.md` — why this is a graph problem, not SQL or vector
* `demos/DEMO_SCRIPT.md` — judged walkthrough with expected outputs
* `docs/BLOG_POST.md` — blog post for the side prize
* `docs/SUBMISSION.md` — verification table with measured numbers
