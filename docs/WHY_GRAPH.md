# Why a graph (and not SQL or a vector store)

GraphSleuth's context layer is FalkorDB. That is a design choice, not a logo on a slide, so it
is fair to ask: *could you build the same agent with Postgres, or with a vector database and
RAG?* This note answers that with the project's own queries, and is honest about where the other
two do win.

The shortcut version:

| Question | SQL / tables | Vector store | Graph (what we run) |
|---|---|---|---|
| "Is ACC-9003 in a ring?" | recursive CTEs over shared devices/beneficiaries | ✗ (nothing to embed) | `detect_suspicious_cluster` — one traversal |
| "How are ACC-EXT-01 and ACC-9100 linked?" | BFS you write and instrument by hand | ✗ | `shortest_connection` — BFS with a relationship filter |
| "What breaks if SYS-ledger-db dies?" | recursive `WITH RECURSIVE` + cycle guards | ✗ | `blast_radius` — reverse `:DEPENDS_ON*1..6` |
| "Who knows SYS-ledger-db?" | 3-table join, ranked | ✗ (exact ids, not similarity) | `who_knows` — the brief's query, verbatim |
| "Why did we decide ADR-002?" | join to a sources table + a self-join for supersession | ~ (can *find* the doc, cannot trace it) | `decision_trail` — `:SOURCED_FROM` + `:SUPERSEDES` |
| "Show the path behind the answer" | you reconstruct it | ✗ | the path *is* the query result |

---

## 1. The model is relationships; the questions are traversals

Every question GraphSleuth answers is *"how are these things connected?"*, at a depth the user
does not know in advance. In a graph that is one pattern. In SQL it is a recursive query you
have to write, parameterise, and guard against cycles — for each question.

The clearest case is the dependency closure. Our whole tool is:

```cypher
MATCH (down:System {id: $id})<-[:DEPENDS_ON*1..6]-(other:System)
RETURN DISTINCT other.id, other.name, other.criticality
```

The same thing in SQL — where `depends_on(parent, child)` is your edge table — is:

```sql
WITH RECURSIVE affected(id) AS (
    SELECT parent FROM depends_on WHERE child = :id
    UNION            -- UNION (not ALL) is your only cycle guard
    SELECT d.parent FROM depends_on d JOIN affected a ON d.child = a.id
)
SELECT s.id, s.name, s.criticality FROM affected a JOIN systems s ON s.id = a.id;
```

That works, and for a fixed schema it is fine. The cost shows up in the *number* of these:
`shortest_connection` (a BFS that also filters to money-only edges), `who_knows` (a three-hop
join through projects), the ring detector (an undirected connected-components pass over *two*
edge types). Each becomes its own hand-written recursive CTE plus cycle handling. In Cypher they
are one pattern each, and the engine does the traversal.

**Ring detection is the sharpest example.** A mule ring is not a row — it is an undirected
*component* of accounts linked by shared devices and shared beneficiaries, corroborated by prior
alerts. In a graph that is a connected-components over a relationship, which we fuse with domain
rules in `detect_suspicious_cluster`. In SQL you are writing the union-find yourself (or leaning on
recursive CTEs that blow up on dense device-sharing noise). On the seeded data this pass finds the
real 6-account ring and drops 4 innocent device-sharing clusters — the win comes from *structure*,
which is what the graph is for.

## 2. Vectors find *similar*; this problem is *connected*

A vector store answers "what text looks like this text?". Almost none of GraphSleuth's questions
are similarity questions:

* **The ring members are not similar.** ACC-9001…ACC-9005 are ordinary-looking accounts. Nothing
  about their text embeddings is close. What links them is a *shared device id* and a *same-day
  pass-through chain* — exact relationships, not near neighbours.
* **Ids are exact.** "Who knows `SYS-ledger-db`" must return the people who actually touched it,
  not the people whose descriptions are semantically near it. A vector index will happily return a
  plausible-looking engineer who never worked on the system.
* **Destructive/opt-in actions need provenance.** When the agent calls `flag_account`, the report
  must cite the *nodes* it relied on (`DEV-9001`, `AL-9001`, `TX-9002`). "The embedding was close"
  is not an audit trail. In our reports every claim maps to a relationship with an id.
* **Multi-hop is not top-k.** Walking six hops of money movement is a traversal, not a ranked list.
  Vectors have no notion of "the path from A to B".

Where vectors *do* belong here: as a **front door**. "Find me the tickets that feel like this
complaint" is a similarity problem — but the moment you have the ticket, the *answer* (which
system, which owner, which decision superseded the old policy) is a graph traversal. GraphSleuth
treats similarity as a way to *find the starting node*, never as the way to *reason*.

## 3. The same agent, one storage engine

Because the graph holds more than edges, the usual "graph DB for connections + Postgres for the
records + a vector store for search" split disappears:

* **Data** — accounts, devices, transactions, systems, docs, tickets, chats.
* **Agent memory** — episodic (`Session`/`Message`), semantic, procedural (`MemoryNote`).
* **Shared state** — `Handoff`, `Case`, `Alert` nodes other agents read.
* **Multi-tenancy** — one graph *per tenant* (`graphsleuth:default`, `graphsleuth:company`), so
  isolation is topological, not a `WHERE tenant_id = ?` convention someone can forget.

That is the project's whole point: when the *context* is a graph, "the agent remembered", "the
agent found", and "the agent acted" are the same three verbs.

## 4. Where SQL and vectors win (honestly)

* **Aggregates and reporting.** "Total transferred per day per merchant" is a `GROUP BY` and SQL
  will beat a graph database. We don't pretend otherwise — that is why `run_cypher` exists as an
  escape hatch and why the seed keeps a clean, regular relational-ish shape for the bulk data.
* **Huge-scale similarity search.** If the task were nearest-neighbour over millions of documents,
  a purpose-built vector index is the right tool. Our nearest-neighbour needs are tiny and are
  better served by `entity_search` over ids.
* **Team familiarity.** More engineers know SQL. That is a real cost of choosing a graph, and the
  reason the schema here stays small and legible (`README.md` → Graph schema).

The claim is not "graphs are faster". It is narrower and, we think, correct: **for questions whose
answer is a path, the storage engine should speak paths.** GraphSleuth's answers are paths, so the
graph is not a detail of the implementation — it is the reason the answers are possible.

---

## Reproduce the comparison

```powershell
# the multi-hop + component queries, live
python -m graphsleuth demo --target ACC-9003 --offline     # ring + money path
python -m graphsleuth eval                                 # 16/16, incl. native algorithms
python -m graphsleuth company --system SYS-ledger-db       # the transitive blast radius
```

`docs/BLOG_POST.md` tells the same story at narrative length; `README.md` → "Graph algorithms as
agent tools" lists the native `algo.*` procedures the agent selects.
