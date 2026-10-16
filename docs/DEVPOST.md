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
  built from the graph: open cases, past findings, the playbook it learned earlier.
* **Share state between agents** — `python -m graphsleuth pipeline` runs **Triage → Investigator**:
  triage writes its verdict, context and entity ids into a `Handoff` node; the investigator's very
  first calls are `list_handoffs` / `accept_handoff`, and it continues from what the graph holds.
  No state is pasted through the caller — FalkorDB is the only channel.
* **Separate memory per tenant** — multigraph topology: every tenant gets its own graph
  (`graphsleuth:default`, `graphsleuth:acme`). Isolation is structural, not a WHERE clause — a
  canary note in tenant `acme` is invisible from `default` even under raw Cypher.
* **Show the path behind an answer** — every report ships an evidence chain with ids and
  relationship types, a compact graph path, the tools used, the actions taken, and a confidence
  rating with what is still missing.
* **Tool selection through MCP** — the same 20 tools are served over MCP stdio
  (`python -m graphsleuth.mcp_server`), so Claude Desktop or any MCP client gets the identical
  graph toolbelt (verified: initialize → 20 tools → live calls).

**Measured on the judges' own terms (verified live, not aspirational):** seed 1702 nodes / 2713
relationships; demo 11/11 tool calls ok; ring score 41.05 with 6 members; 6-hop money path from
`ACC-EXT-01` to the collector `ACC-9100`; pipeline handoff created → accepted and case opened;
20-thread concurrency bench 2307 ops in 4 s (575 ops/s, reads p95 71 ms, writes p95 82 ms, 0
errors); MCP client sees all 20 tools; 15/15 offline tests pass.

**Honest engineering notes:** FalkorDB's `ORDER BY` needed the `WITH … ORDER BY … LIMIT … RETURN`
pattern in memory queries (found by running it live); native `algo.pageRank` returns 0 rows on the
current `falkordb:latest` build, so the `page_rank` tool tries the procedure first and falls back
to an in-process power iteration, always reporting which engine produced the answer.

**Try it:** `powershell -ExecutionPolicy Bypass -File .\setup.ps1` (venv → deps → Docker →
FalkorDB → seed → demo), then put a free `GROQ_API_KEY` in `.env` and re-run — the real model
takes over the loop. Repo: https://github.com/Kartikey-Kesarwani-2005/GraphSleuth

---

## Track choice

Enter primarily in **Track 01 — Agents That Act on Connected Data** (multi-hop reasoning, graph
queries/algorithms as tools, tool selection through MCP, explainable results). It also covers
Track 02 (episodic/semantic/procedural memory, handoffs between agents, high-concurrency
reads/writes, per-tenant memory) and demonstrates Track 03's pattern (every answer traced back to
source entities).

## Gallery / links to include

* `demos/EXAMPLE_REPORT.md` — full investigation report from a real run
* `demos/PIPELINE_REPORT.md` — two-agent run: triage verdict → handoff → investigator verdict
* `demos/DEMO_SCRIPT.md` — judged walkthrough with expected outputs
* `docs/BLOG_POST.md` — blog post for the side prize
* `docs/SUBMISSION.md` — verification table with measured numbers
