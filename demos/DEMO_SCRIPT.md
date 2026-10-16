# GraphSleuth — 3-minute demo script for judges

Runs entirely offline (scripted brain, real graph, real tools).

## 0. Setup (once, ~1 minute)

```powershell
docker compose up -d
.\.venv\Scripts\python.exe -m graphsleuth seed
.\.venv\Scripts\python.exe -m graphsleuth status
```

`status` prints node/relationship counts — proof the context layer is live.

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
| 6 | `detect_suspicious_cluster` | union-find ranks the 5-account cluster top |
| 7 | `shortest_connection` | narrates the exact chain EXT → ... → ACC-9100 |
| 8-11 | `open_case`, `add_case_note`, `flag_account`, `remember` | acts on the graph + writes memory |

## 3. Show the path (30s)

Open `demos/out/report.md`: verdict, numbered evidence chain, **Mermaid graph of the traversed
path**, tools used, actions taken, confidence.

> "Every hop in the answer maps to a tool call. If the graph didn't show it, the agent doesn't
> claim it."

## 4. Prove memory across sessions (30s)

```powershell
.\.venv\Scripts\python.exe -m graphsleuth chat
you> What was the last thing you concluded about ACC-9003?
```

The answer comes from the `remember` step of the previous run (and the open case), not from the
conversation — restart the process, even the machine, and it still knows.

Second proof — a different agent, same graph:

```powershell
.\.venv\Scripts\python.exe -m graphsleuth chat --tenant acme
you> What open fraud cases exist?
```

## 5. MCP (optional, 20s)

Show the config in `README.md` — the same twelve tools over MCP (stdio), so Claude Desktop or a
teammate's agent works against the same FalkorDB graph with no extra wiring.

## Talking points

* **The graph shapes the agent.** The system prompt teaches a graph workflow (resolve → expand →
  trace → cluster → prove → act) and every tool returns typed relationships.
* **Actions are graph writes.** Flagging an account creates an `Alert` node and links it to a
  `Case` — auditable, shared, persistent.
* **Separate memory per tenant** (`--tenant`), so one graph can serve several teams.
* **No dead ends.** `run_cypher` is a guarded read-only escape hatch for questions the twelve
  tools don't cover.
