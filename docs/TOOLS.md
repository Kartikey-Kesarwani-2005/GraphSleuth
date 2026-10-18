# Tool reference

All 30 tools the agents, the CLI and the MCP server can call. This
file is generated from the tool specs (`graphsleuth/tools.py`) — run
`python -m graphsleuth tools --write` after changing a tool, and the offline tests will fail
if it is out of date.

**Read vs write.** Six tools mutate the graph — `remember`, `open_case`, `add_case_note`,
`flag_account`, `create_handoff`, `accept_handoff` — and they are the only entries in
`tools.MUTATING_TOOLS`, which is what the report uses to list *verified* actions. Everything
else reads. `run_cypher` is the one raw-Cypher entry point and refuses writes
(`CREATE`, `DELETE`, `SET`, `MERGE`, `REMOVE`, `DROP`, `ALTER`, `LOAD`) via `READ_ONLY_BLOCKLIST`.

**Every agent call is traced.** A tool result goes back into the model's context and into the
session's evidence trail, which is what `demo --save`, the console's evidence pane and
`explain.to_markdown` render — so the report can show the payload a claim came from.

## Fraud-investigation toolbelt (24 tools)

### `entity_search`

**Read-only.** Find entities (accounts, people, devices, merchants, transactions, alerts, cases) by id or name. Use this first to resolve whatever id or name the investigation starts from.

| argument | type | required | description |
| --- | --- | --- | --- |
| `query` | string | yes | id or name, full or partial (e.g. ACC-9003) |
| `labels` | array<string> | no | optional label filter, e.g. ['Account'] |
| `limit` | integer | no | max matches, default 10 |

### `graph_neighbors`

**Read-only.** Expand an entity's neighbourhood hop by hop. Returns the connected subgraph (nodes + typed edges). Use it to see how an account is wired to devices, people and other accounts. depth 1-3.

| argument | type | required | description |
| --- | --- | --- | --- |
| `entity_id` | string | yes | — |
| `depth` | integer | no | hops to expand, default 1, max 3 |
| `limit` | integer | no | — |

### `trace_money_flow`

**Read-only.** Follow funds from an account in both directions, hop by hop, with transaction ids, amounts and timestamps. Use it to find pass-through chains, round-tripping and the final beneficiary.

| argument | type | required | description |
| --- | --- | --- | --- |
| `account_id` | string | yes | — |
| `depth` | integer | no | hops, default 4, max 6 |

### `find_shared_links`

**Read-only.** Given an account, find the accounts it shares devices or a beneficiary with, plus any alerts on them, ranked by combined mule-ring signal score.

| argument | type | required | description |
| --- | --- | --- | --- |
| `account_id` | string | yes | — |

### `detect_suspicious_cluster`

**Read-only.** Run the ring-detection algorithm over the whole graph: union-find clustering on shared-device links, shared-beneficiary links and prior alerts. Use it when you need to look beyond one account.

| argument | type | required | description |
| --- | --- | --- | --- |
| `min_signals` | integer | no | minimum distinct signals per member, default 2 |
| `top` | integer | no | how many clusters to return, default 5 |

### `shortest_connection`

**Read-only.** Breadth-first search for the shortest chain of relationships between two entities. Use it to prove how two suspects are linked and to narrate the path. Pass `via` to follow only certain relationship types (e.g. a money-only chain).

| argument | type | required | description |
| --- | --- | --- | --- |
| `id_a` | string | yes | — |
| `id_b` | string | yes | — |
| `max_depth` | integer | no | default 6 |
| `via` | array<string> | no | optional relationship types to restrict the path, e.g. ['ORIGINATED','DESTINED'] for a money-only chain |

### `what_if_removed`

**Read-only.** Counterfactual explainability: mentally delete one entity (a device, account or person) and re-check the conclusion — does the mule ring still form, do its neighbours still reach each other, and do a chain and the money trail still exist without it? Call it before you escalate, so the verdict provably does not hinge on a single link. Read-only.

| argument | type | required | description |
| --- | --- | --- | --- |
| `entity_id` | string | yes | node id to remove mentally, e.g. 'DEV-9001' or 'ACC-9100' |
| `from_id` | string | no | optional: start of a chain to re-check, e.g. 'ACC-EXT-01' |
| `to_id` | string | no | optional: end of a chain to re-check, e.g. 'ACC-9100' |
| `max_depth` | integer | no | hops for the chain check, default 6 |

### `assess_ring_resilience`

**Read-only.** Batch counterfactual: stress-test every member of the suspected ring at once and rank them by how load-bearing they are. Returns per-member robustness, the articulation points (remove those and the case collapses) and a resilience verdict for the whole ring: resilient / mostly_resilient / bottlenecked / fragile. Read-only.

| argument | type | required | description |
| --- | --- | --- | --- |
| `ring_key` | string | no | 'top' (default) or a member account id to locate its cluster |
| `min_signals` | integer | no | cluster sensitivity, default 2 |
| `top` | integer | no | cluster selection size, default 5 |
| `depth` | integer | no | reach depth for each probe, default 2, max 4 |
| `from_id` | string | no | chain start for the money-trail probe, default 'ACC-EXT-01' |
| `to_id` | string | no | chain end for the money-trail probe, default 'ACC-9100' |

### `ring_timeline`

**Read-only.** Reconstruct when the suspected ring actually acted: every transfer touching the cluster (inbound/internal/outbound), merchant spends and alerts merged into one timestamp-ordered event list. Returns first/last activity, value moved, the busiest one-hour burst, per-member activation order and whether alerts led or followed the money. Use for 'when did this happen' / 'build the sequence of events'. Read-only.

| argument | type | required | description |
| --- | --- | --- | --- |
| `ring_key` | string | no | 'top' (default) or a member account id |
| `min_signals` | integer | no | cluster sensitivity, default 2 |
| `top` | integer | no | cluster selection size, default 5 |
| `limit` | integer | no | max events, default 30, max 60 |

### `find_similar_rings`

**Read-only.** Pattern generalisation: is this playbook running anywhere else? Fingerprints the suspected ring (size, collector shape, shared devices, alert load, mean risk) and ranks every other candidate group by structural similarity, checking for money flowing between them (cross-links). Separates a repeat cell from innocent device coincidences. Use for 'are there more like this' / 'is this spreading'. Read-only.

| argument | type | required | description |
| --- | --- | --- | --- |
| `ring_key` | string | no | 'top' (default), a member id, or a 1-based cluster index |
| `min_signals` | integer | no | candidate threshold, default 1 (loose, to find look-alikes) |
| `top` | integer | no | candidate groups to consider, default 8 |
| `limit` | integer | no | similar groups to return, default 3, max 6 |

### `page_rank`

**Read-only.** PageRank over the whole graph: which entities sit at the centre of the flow. Use it to rank suspects/accounts by structural influence before investigating one. Pass label='Account' to rank only accounts.

| argument | type | required | description |
| --- | --- | --- | --- |
| `top` | integer | no | how many ranked entities, default 10 |
| `label` | string | no | optional node label filter, e.g. 'Account' |

### `connected_components`

**Read-only.** Run FalkorDB's WCC algorithm: which parts of the graph are separate islands? Isolated clusters around an alert are a strong 'ring' signal; size>=2 only.

| argument | type | required | description |
| --- | --- | --- | --- |
| `top` | integer | no | components to return, default 8 |
| `min_size` | integer | no | ignore components smaller than this, default 2 |

### `community_detection`

**Read-only.** Run FalkorDB's label-propagation algorithm: dense communities in the graph — who clusters with whom regardless of my assumptions about devices or beneficiaries.

| argument | type | required | description |
| --- | --- | --- | --- |
| `top` | integer | no | communities to return, default 8 |
| `min_size` | integer | no | ignore communities smaller than this, default 2 |

### `centrality`

**Read-only.** Graph centrality via FalkorDB: kind=betweenness (which nodes bridge other pairs — the account every path runs through) or harmonic (closest to everyone). Use it to find the hub of a mule ring.

| argument | type | required | description |
| --- | --- | --- | --- |
| `kind` | string | no | — |
| `top` | integer | no | default 10 |
| `label` | string | no | optional node label filter, e.g. 'Account' |

### `list_alerts`

**Read-only.** Read the alert queue: accounts flagged before, newest first. Start a triage pass with this to decide what deserves investigation.

| argument | type | required | description |
| --- | --- | --- | --- |
| `status` | string | no | filter, e.g. 'open' or 'under_review' |
| `severity` | string | no | — |
| `limit` | integer | no | default 10 |

### `create_handoff`

**Read-write.** ACTION: hand the current investigation to another agent (role: investigator / triage / analyst). Writes a Handoff node into the shared graph — context, case and entities — so the next agent picks it up with list_handoffs without you pasting anything.

| argument | type | required | description |
| --- | --- | --- | --- |
| `to_role` | string | yes | — |
| `context` | string | yes | why you are handing off + what you found so far |
| `from_role` | string | no | your role, default 'triage' |
| `case_id` | string | no | case this belongs to, if any |
| `entity_ids` | array<string> | no | — |

### `list_handoffs`

**Read-only.** Read handoffs waiting to be picked up (default status=pending), with their case and linked entities. Call this first when you are picking up someone else's work.

| argument | type | required | description |
| --- | --- | --- | --- |
| `status` | string | no | 'pending', 'accepted', or '' for all |
| `limit` | integer | no | default 5 |

### `accept_handoff`

**Read-write.** ACTION: claim a handoff for this session — marks it accepted and returns the full context, case and entities the previous agent left for you.

| argument | type | required | description |
| --- | --- | --- | --- |
| `handoff_id` | string | yes | — |

### `run_cypher`

**Read-only.** Read-only Cypher escape hatch for questions the other tools cannot answer. Only MATCH / OPTIONAL MATCH / RETURN / WITH / CALL / UNWIND / SHOW / EXPLAIN. Return a single column aliased AS row when possible.

| argument | type | required | description |
| --- | --- | --- | --- |
| `query` | string | yes | read-only Cypher |
| `purpose` | string | no | why this query is needed |

### `flag_account`

**Read-write.** ACTION: raise an alert on an account and set its status to under_review. Only do this when the graph evidence supports it.

| argument | type | required | description |
| --- | --- | --- | --- |
| `account_id` | string | yes | — |
| `severity` | string | yes | — |
| `reason` | string | yes | evidence-based reason, quote the signals |
| `case_id` | string | no | optional case to attach the alert to |

### `open_case`

**Read-write.** ACTION: open an investigation case and link the entities involved, so any agent can pick it up later.

| argument | type | required | description |
| --- | --- | --- | --- |
| `title` | string | yes | — |
| `summary` | string | no | — |
| `entity_ids` | array<string> | no | — |

### `add_case_note`

**Read-write.** ACTION: append a note with findings/evidence to an existing case.

| argument | type | required | description |
| --- | --- | --- | --- |
| `case_id` | string | yes | — |
| `text` | string | yes | — |

### `remember`

**Read-write.** ACTION: store a durable memory note so future sessions know it. kind=episodic (what happened), semantic (a durable fact), procedural (how to work). Link it to the entities it is about.

| argument | type | required | description |
| --- | --- | --- | --- |
| `kind` | string | yes | — |
| `text` | string | yes | — |
| `about_ids` | array<string> | no | — |

### `recall`

**Read-only.** Read long-term memory: earlier findings, playbooks and open cases, optionally filtered by text or kind. Use it before concluding, in case this was investigated before.

| argument | type | required | description |
| --- | --- | --- | --- |
| `query` | string | no | entity id or free text |
| `kind` | string | no | — |

## Company-brain toolbelt (6 tools)

Track 03 (`python -m graphsleuth company-chat`): the same agent loop over an organisation's
people, teams, systems, docs, tickets and decisions.

### `who_knows`

**Read-only.** Track 03: who knows about a system. People who worked on projects that USE the system, ranked by how many such projects, plus people who explicitly declared expertise. Use this to answer 'who knows / who should I ask about <system>'.

| argument | type | required | description |
| --- | --- | --- | --- |
| `system_id` | string | yes | system id, e.g. SYS-ledger-db |

### `owners_of`

**Read-only.** Track 03: who owns a system or project — the owning person/team, plus the teammates reachable from that owner. Use for 'who is responsible for / who owns <thing>'.

| argument | type | required | description |
| --- | --- | --- | --- |
| `entity_id` | string | yes | system or project id, e.g. SYS-ledger-db |

### `blast_radius`

**Read-only.** Track 03: what breaks if a service goes down. The reverse transitive :DEPENDS_ON closure — affected systems, projects at risk, and the teams that own them. Use for 'what depends on <system>' / 'impact if <system> fails'.

| argument | type | required | description |
| --- | --- | --- | --- |
| `system_id` | string | yes | system id, e.g. SYS-ledger-db |

### `decision_trail`

**Read-only.** Track 03: why a decision was made — who decided it, what it affects, the doc/ticket/chat it was sourced from, and which earlier decision it supersedes / was superseded by. Use for 'why did we decide X' / 'what is the story behind <decision>'.

| argument | type | required | description |
| --- | --- | --- | --- |
| `decision_id` | string | yes | decision id, e.g. ADR-002 |

### `knowledge_sources`

**Read-only.** Track 03: everything written about a system — docs, tickets and chats in one graph, each with a named author. Use to cite where an answer comes from.

| argument | type | required | description |
| --- | --- | --- | --- |
| `system_id` | string | yes | system id, e.g. SYS-ledger-db |

### `stale_knowledge`

**Read-only.** Track 03: what knowledge is going stale — docs not updated in N days, superseded decisions, and bus-factor risk (systems with <=1 declared expert that projects depend on). Use for 'what is out of date / risky to lose'.

| argument | type | required | description |
| --- | --- | --- | --- |
| `stale_days` | integer | no | age threshold in days, default 180 |

