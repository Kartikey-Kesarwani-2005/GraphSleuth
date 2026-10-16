"""The agent's toolbelt.

Every tool is a window onto the graph. Nothing here is a black box: each tool
returns the entity ids and relationship types that produced the answer, so the
agent can explain *how* it got there.

Two families:

* **read tools**  — entity_search, graph_neighbors, trace_money_flow,
  find_shared_links, detect_suspicious_cluster, shortest_connection, run_cypher
* **write tools** — flag_account, open_case, add_case_note, remember, recall
  (these are the *actions* the agent takes; they are written back into the graph)
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from . import db, memory

MAX_ROWS = 60


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _shrink(value: Any, max_str: int = 160, max_items: int = 25) -> Any:
    """Keep tool results small enough to sit in an LLM context window."""
    if isinstance(value, str):
        return value if len(value) <= max_str else value[:max_str] + "…"
    if isinstance(value, dict):
        return {k: _shrink(v, max_str, max_items) for k, v in list(value.items())[:max_items]}
    if isinstance(value, (list, tuple)):
        return [_shrink(v, max_str, max_items) for v in value[:max_items]]
    if isinstance(value, float):
        return round(value, 2)
    return value


def _node_summary(node: Any) -> dict:
    data = db.to_json(node)
    if isinstance(data, dict):
        return {
            "id": data.get("id"),
            "labels": data.get("_labels", []),
            "props": _shrink({k: v for k, v in data.items() if k not in {"_labels", "id"}}),
        }
    return {"id": str(data)}


# --------------------------------------------------------------------------- #
# read tools
# --------------------------------------------------------------------------- #
def entity_search(query: str, labels: list[str] | None = None, limit: int = 10) -> dict:
    """Find graph entities by id or name, optionally restricted to node labels."""
    labels = [str(x) for x in (labels or [])]
    params = {"q": query, "labels": labels, "limit": max(1, min(int(limit), 25))}
    try:
        rows = db.query(
            """
            MATCH (n)
            WHERE (n.id = $q
                   OR toLower(coalesce(n.id, '')) CONTAINS toLower($q)
                   OR toLower(coalesce(n.name, '')) CONTAINS toLower($q))
              AND (size($labels) = 0 OR any(l IN labels(n) WHERE l IN $labels))
            RETURN n AS row
            LIMIT $limit
            """,
            params,
        )
    except Exception:
        rows = db.query(
            """
            MATCH (n)
            WHERE n.id = $q
               OR toLower(coalesce(n.id, '')) CONTAINS toLower($q)
               OR toLower(coalesce(n.name, '')) CONTAINS toLower($q)
            RETURN n AS row
            LIMIT $limit
            """,
            params,
        )
    return {"matches": [_node_summary(r) for r in rows], "count": len(rows)}


def graph_neighbors(entity_id: str, depth: int = 1, limit: int = 40) -> dict:
    """Walk outward from an entity, hop by hop, and return the connected subgraph."""
    depth = max(1, min(int(depth), 3))
    seen_ids = {entity_id}
    frontier = [entity_id]
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    for _ in range(depth):
        next_frontier: list[str] = []
        for current in frontier[:20]:
            rows = db.query(
                """
                MATCH (c {id: $id})-[r]-(n)
                RETURN [type(r), n, r] AS row
                LIMIT $limit
                """,
                {"id": current, "limit": max(1, min(int(limit), MAX_ROWS))},
            )
            for rel_type, neighbour, relationship in rows:
                summary = _node_summary(neighbour)
                neighbour_id = summary.get("id")
                if not neighbour_id:
                    continue
                nodes[neighbour_id] = summary
                rel_props = db.to_json(relationship)
                if isinstance(rel_props, dict):
                    rel_props.pop("_type", None)
                edges.append(
                    {
                        "from": current,
                        "to": neighbour_id,
                        "type": rel_type,
                        "props": _shrink(rel_props or {}),
                    }
                )
                if neighbour_id not in seen_ids:
                    seen_ids.add(neighbour_id)
                    next_frontier.append(neighbour_id)
        frontier = next_frontier
        if not frontier:
            break

    return {
        "start": entity_id,
        "depth": depth,
        "nodes": list(nodes.values())[:MAX_ROWS],
        "edges": edges[: MAX_ROWS * 2],
        "node_count": len(nodes),
        "edge_count": len(edges),
    }


def trace_money_flow(account_id: str, depth: int = 4) -> dict:
    """Follow funds hop by hop from an account in both directions."""
    depth = max(1, min(int(depth), 6))

    def hop(source: str, forward: bool) -> list[list]:
        if forward:
            cypher = """
                MATCH (a:Account {id: $id})-[:ORIGINATED]->(t:Transaction)-[:DESTINED]->(b:Account)
                RETURN [a.id, b.id, t.id, t.amount, t.timestamp] AS row
                LIMIT $limit
            """
        else:
            cypher = """
                MATCH (x:Account)-[:ORIGINATED]->(t:Transaction)-[:DESTINED]->(a:Account {id: $id})
                RETURN [x.id, a.id, t.id, t.amount, t.timestamp] AS row
                LIMIT $limit
            """
        return db.query(cypher, {"id": source, "limit": MAX_ROWS})

    def walk(start: str, forward: bool) -> dict:
        levels: list[dict] = []
        visited = {start}
        frontier = [start]
        for level in range(depth):
            hops: list[dict] = []
            next_frontier: list[str] = []
            for source in frontier[:15]:
                for src, dst, tx_id, amount, ts in hop(source, forward):
                    neighbour = dst if forward else src
                    hops.append(
                        {
                            "from": src,
                            "to": dst,
                            "tx": tx_id,
                            "amount": amount,
                            "timestamp": ts,
                        }
                    )
                    if neighbour not in visited:
                        visited.add(neighbour)
                        next_frontier.append(neighbour)
            if hops:
                levels.append({"level": level + 1, "hops": hops[:MAX_ROWS]})
            frontier = next_frontier
            if not frontier:
                break
        return {"levels": levels, "reached": sorted(visited - {start})}

    outgoing = walk(account_id, forward=True)
    incoming = walk(account_id, forward=False)

    def total(levels: list[dict]) -> float:
        return round(sum(h["amount"] or 0 for level in levels for h in level["hops"]), 2)

    return {
        "account": account_id,
        "depth": depth,
        "outgoing": outgoing,
        "incoming": incoming,
        "total_out": total(outgoing["levels"]),
        "total_in": total(incoming["levels"]),
    }


def find_shared_links(account_id: str) -> dict:
    """Find accounts that share devices or a beneficiary with this account."""
    device_rows = db.query(
        """
        MATCH (a:Account {id: $id})-[:USED_DEVICE]->(d:Device)<-[:USED_DEVICE]-(b:Account)
        WHERE b.id <> $id
        RETURN [b.id, d.id, d.fingerprint] AS row
        LIMIT $limit
        """,
        {"id": account_id, "limit": MAX_ROWS},
    )
    beneficiary_rows = db.query(
        """
        MATCH (a:Account {id: $id})-[:ORIGINATED]->(:Transaction)-[:DESTINED]->(ben)
              <-[:DESTINED]-(:Transaction)<-[:ORIGINATED]-(other:Account)
        WHERE other.id <> $id
        RETURN [other.id, ben.id] AS row
        LIMIT $limit
        """,
        {"id": account_id, "limit": MAX_ROWS},
    )

    shared_devices: dict[str, list[str]] = {}
    for other, device, _fp in device_rows:
        shared_devices.setdefault(other, []).append(device)

    shared_beneficiaries: dict[str, list[str]] = {}
    for other, beneficiary in beneficiary_rows:
        shared_beneficiaries.setdefault(other, []).append(beneficiary)

    linked = sorted(set(shared_devices) | set(shared_beneficiaries))

    alerts: dict[str, list[dict]] = {}
    for other in linked[:20]:
        rows = db.query(
            """
            MATCH (b:Account {id: $id})-[:FLAGGED_BY]->(al:Alert)
            RETURN [al.id, al.rule, al.severity] AS row
            LIMIT 10
            """,
            {"id": other},
        )
        if rows:
            alerts[other] = [{"alert": r[0], "rule": r[1], "severity": r[2]} for r in rows]

    scored: list[dict] = []
    for other in linked:
        score = 0.0
        score += 1.0 * len(shared_devices.get(other, []))
        score += 1.5 * len(shared_beneficiaries.get(other, []))
        score += 1.0 * len(alerts.get(other, []))
        scored.append(
            {
                "account": other,
                "score": round(score, 2),
                "shared_devices": shared_devices.get(other, []),
                "shared_beneficiaries": shared_beneficiaries.get(other, []),
                "alerts": [a["alert"] for a in alerts.get(other, [])],
            }
        )
    scored.sort(key=lambda item: item["score"], reverse=True)

    return {
        "account": account_id,
        "linked_accounts": len(linked),
        "links": scored[:20],
        "interpretation": (
            "shared_devices + shared_beneficiaries + prior alerts are the three mule-ring "
            "signals; two or more on the same account is a strong escalation trigger"
        ),
    }


def detect_suspicious_cluster(min_signals: int = 2, top: int = 5) -> dict:
    """Cluster the graph into suspect groups using shared-device and shared-beneficiary
    links plus prior alerts (a union-find community pass over the graph)."""
    min_signals = max(1, int(min_signals))

    device_pairs = db.query(
        """
        MATCH (a:Account)-[:USED_DEVICE]->(d:Device)<-[:USED_DEVICE]-(b:Account)
        WHERE a.id < b.id
        RETURN [a.id, b.id, d.id] AS row
        """
    )
    beneficiary_pairs = db.query(
        """
        MATCH (x:Account)-[:ORIGINATED]->(:Transaction)-[:DESTINED]->(ben)
              <-[:DESTINED]-(:Transaction)<-[:ORIGINATED]-(y:Account)
        WHERE x.id < y.id
        RETURN [x.id, y.id, ben.id] AS row
        """
    )
    alerts = db.query(
        "MATCH (a:Account)-[:FLAGGED_BY]->(al:Alert) RETURN [a.id, al.severity] AS row"
    )
    risk_rows = db.query("MATCH (a:Account) RETURN [a.id, a.risk_score] AS row")
    risk = {row[0]: row[1] or 0 for row in risk_rows}

    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    signals: dict[str, dict] = {}

    def bump(account: str, key: str, value: Any) -> None:
        entry = signals.setdefault(
            account, {"devices": set(), "beneficiaries": set(), "alerts": [], "risk": risk.get(account, 0)}
        )
        if key == "alerts":
            entry["alerts"].append(value)
        else:
            entry[key].add(value)

    for a, b, device in device_pairs:
        union(a, b)
        bump(a, "devices", device)
        bump(b, "devices", device)
    for x, y, beneficiary in beneficiary_pairs:
        union(x, y)
        bump(x, "beneficiaries", beneficiary)
        bump(y, "beneficiaries", beneficiary)
    for account, severity in alerts:
        bump(account, "alerts", severity)

    clusters: dict[str, list[str]] = {}
    for account in list(signals):
        clusters.setdefault(find(account), []).append(account)

    results: list[dict] = []
    for members in clusters.values():
        details = []
        total_score = 0.0
        for account in sorted(members):
            entry = signals[account]
            score = (
                2.0 * len(entry["devices"])
                + 2.0 * len(entry["beneficiaries"])
                + 1.0 * len(entry["alerts"])
                + 1.0 * float(entry["risk"] or 0)
            )
            signal_count = (
                (1 if entry["devices"] else 0)
                + (1 if entry["beneficiaries"] else 0)
                + (1 if entry["alerts"] else 0)
            )
            if signal_count < min_signals and len(members) < 3:
                continue
            total_score += score
            details.append(
                {
                    "account": account,
                    "risk_score": round(float(entry["risk"] or 0), 2),
                    "shared_devices": sorted(entry["devices"]),
                    "shared_beneficiaries": sorted(entry["beneficiaries"]),
                    "alerts": entry["alerts"],
                    "score": round(score, 2),
                }
            )
        if len(details) >= 2:
            details.sort(key=lambda item: item["score"], reverse=True)
            results.append(
                {
                    "cluster_score": round(total_score, 2),
                    "size": len(details),
                    "members": details,
                }
            )

    results.sort(key=lambda item: item["cluster_score"], reverse=True)
    return {
        "algorithm": "union-find clustering over shared-device + shared-beneficiary + alert signals",
        "min_signals": min_signals,
        "clusters_found": len(results),
        "clusters": results[: max(1, int(top))],
    }


def shortest_connection(id_a: str, id_b: str, max_depth: int = 6) -> dict:
    """Breadth-first search for the shortest chain of relationships between two entities."""
    max_depth = max(1, min(int(max_depth), 8))
    queue = [(id_a, None)]
    parents: dict[str, tuple[str | None, str | None]] = {id_a: (None, None)}
    found = False

    for _ in range(max_depth):
        next_queue: list[tuple[str, str | None]] = []
        for current, _ in queue:
            rows = db.query(
                "MATCH (c {id: $id})-[r]-(n) RETURN [type(r), n] AS row LIMIT $limit",
                {"id": current, "limit": MAX_ROWS},
            )
            for rel_type, neighbour in rows:
                summary = _node_summary(neighbour)
                neighbour_id = summary.get("id")
                if not neighbour_id or neighbour_id in parents:
                    continue
                parents[neighbour_id] = (current, rel_type)
                if neighbour_id == id_b:
                    found = True
                    break
                next_queue.append((neighbour_id, rel_type))
            if found:
                break
        if found:
            break
        queue = next_queue
        if not queue:
            break

    if not found:
        return {
            "from": id_a,
            "to": id_b,
            "connected": False,
            "note": f"no chain found within {max_depth} hops",
        }

    chain: list[dict] = []
    cursor = id_b
    while cursor is not None:
        parent, rel_type = parents[cursor]
        chain.append({"node": cursor, "via": rel_type})
        cursor = parent
    chain.reverse()

    parts = [str(chain[0]["node"])]
    for step in chain[1:]:
        parts.append(f"-[{step['via']}]->")
        parts.append(str(step["node"]))
    narrative = " ".join(parts)

    return {
        "from": id_a,
        "to": id_b,
        "connected": True,
        "hops": len(chain) - 1,
        "path": chain,
        "narrative": narrative,
    }


READ_ONLY_BLOCKLIST = re.compile(
    r"\b(CREATE|DELETE|DETACH|SET|MERGE|REMOVE|DROP|ALTER|LOAD)\b", re.IGNORECASE
)
ALLOWED_START = ("MATCH", "OPTIONAL", "RETURN", "WITH", "CALL", "UNWIND", "SHOW", "EXPLAIN")


def _strip_string_literals(cypher: str) -> str:
    return re.sub(r"'([^'\\]|\\.)*'|\"([^\"\\]|\\.)*\"", "''", cypher)


def run_cypher(query: str, purpose: str = "") -> dict:
    """Run a read-only Cypher query against the graph (the escape hatch tool)."""
    stripped = _strip_string_literals(query).strip()
    first_word = stripped.split(None, 1)[0].upper() if stripped else ""

    if first_word not in ALLOWED_START:
        return {
            "error": "query must start with one of: " + ", ".join(ALLOWED_START),
            "hint": "read-only queries only; use flag_account / open_case / add_case_note to write",
        }
    if READ_ONLY_BLOCKLIST.search(stripped):
        return {
            "error": "read-only tool: CREATE/DELETE/SET/MERGE/REMOVE/DROP/ALTER/LOAD are not allowed",
            "hint": "use flag_account, open_case, add_case_note or remember for writes",
        }

    try:
        rows = db.query(query)
    except Exception as exc:  # surface the database error so the agent can fix the query
        return {"error": str(exc)[:400]}
    return {"purpose": purpose, "rows": _shrink(rows), "row_count": len(rows)}


# --------------------------------------------------------------------------- #
# write tools — the agent's actions
# --------------------------------------------------------------------------- #
def flag_account(account_id: str, severity: str, reason: str, case_id: str = "") -> dict:
    """Action: raise an alert on an account and mark it under review."""
    alert_id = f"AL-{uuid.uuid4().hex[:6]}"
    severity = severity if severity in {"low", "medium", "high", "critical"} else "medium"
    rows = db.query(
        """
        MATCH (a:Account {id: $id})
        CREATE (a)-[:FLAGGED_BY]->(al:Alert {id: $aid, rule: 'analyst_flag', severity: $sev,
                reason: $reason, created_at: $ts, status: 'open'})
        SET a.status = 'under_review'
        RETURN a.id AS row
        """,
        {"id": account_id, "aid": alert_id, "sev": severity, "reason": reason, "ts": _now()},
    )
    if not rows:
        return {"error": f"account {account_id} not found", "flagged": False}

    linked_case = ""
    if case_id:
        case_rows = db.query(
            "MATCH (al:Alert {id: $aid}), (c:Case {id: $cid}) MERGE (al)-[:PART_OF]->(c) "
            "RETURN c.id AS row",
            {"aid": alert_id, "cid": case_id},
        )
        linked_case = case_rows[0] if case_rows else ""
    return {
        "flagged": True,
        "account": account_id,
        "alert_id": alert_id,
        "severity": severity,
        "case": linked_case,
        "status": "under_review",
    }


def open_case(title: str, summary: str = "", entity_ids: list[str] | None = None) -> dict:
    """Action: open an investigation case in the graph and attach the entities involved."""
    rows = db.query("MATCH (c:Case) RETURN count(c) AS row")
    existing = int(rows[0]) if rows else 0
    case_id = f"CASE-{existing + 1:04d}"

    db.query(
        "CREATE (c:Case {id: $id, title: $title, summary: $summary, status: 'open', "
        "created_at: $ts}) RETURN c.id AS row",
        {"id": case_id, "title": title, "summary": summary, "ts": _now()},
    )
    linked = []
    for entity_id in entity_ids or []:
        linked_rows = db.query(
            "MATCH (c:Case {id: $cid}), (e {id: $eid}) MERGE (c)-[:INVOLVES]->(e) "
            "RETURN e.id AS row",
            {"cid": case_id, "eid": entity_id},
        )
        if linked_rows:
            linked.append(entity_id)
    return {"case_id": case_id, "title": title, "involves": linked, "status": "open"}


def add_case_note(case_id: str, text: str) -> dict:
    """Action: attach a note to a case (permanent, readable by every agent)."""
    note_id = f"NOTE-{uuid.uuid4().hex[:6]}"
    rows = db.query(
        """
        MATCH (c:Case {id: $cid})
        CREATE (n:Note {id: $nid, kind: 'case', text: $text, created_at: $ts})-[:ON_CASE]->(c)
        RETURN c.id AS row
        """,
        {"cid": case_id, "nid": note_id, "text": text, "ts": _now()},
    )
    if not rows:
        return {"error": f"case {case_id} not found"}
    return {"case_id": case_id, "note_id": note_id, "saved": True}


def remember(kind: str, text: str, about_ids: list[str] | None = None) -> dict:
    """Action: store a durable memory note (episodic / semantic / procedural)."""
    return memory.remember(kind, text, about_ids or [])


def recall(query: str = "", kind: str = "") -> dict:
    """Read the agent's own long-term memory (survives across sessions)."""
    notes = memory.recall(query, kind)
    return {
        "query": query,
        "kind": kind or "any",
        "notes": _shrink(notes),
        "count": len(notes),
        "open_cases": _shrink(memory.open_cases()),
    }


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
HANDLERS: dict[str, Callable[..., dict]] = {
    "entity_search": entity_search,
    "graph_neighbors": graph_neighbors,
    "trace_money_flow": trace_money_flow,
    "find_shared_links": find_shared_links,
    "detect_suspicious_cluster": detect_suspicious_cluster,
    "shortest_connection": shortest_connection,
    "run_cypher": run_cypher,
    "flag_account": flag_account,
    "open_case": open_case,
    "add_case_note": add_case_note,
    "remember": remember,
    "recall": recall,
}


def _spec(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


TOOL_SPECS: list[dict] = [
    _spec(
        "entity_search",
        "Find entities (accounts, people, devices, merchants, transactions, alerts, cases) by id or name. "
        "Use this first to resolve whatever id or name the investigation starts from.",
        {
            "query": {"type": "string", "description": "id or name, full or partial (e.g. ACC-9003)"},
            "labels": {
                "type": "array",
                "items": {"type": "string"},
                "description": "optional label filter, e.g. ['Account']",
            },
            "limit": {"type": "integer", "description": "max matches, default 10"},
        },
        ["query"],
    ),
    _spec(
        "graph_neighbors",
        "Expand an entity's neighbourhood hop by hop. Returns the connected subgraph (nodes + typed edges). "
        "Use it to see how an account is wired to devices, people and other accounts. depth 1-3.",
        {
            "entity_id": {"type": "string"},
            "depth": {"type": "integer", "description": "hops to expand, default 1, max 3"},
            "limit": {"type": "integer"},
        },
        ["entity_id"],
    ),
    _spec(
        "trace_money_flow",
        "Follow funds from an account in both directions, hop by hop, with transaction ids, amounts and timestamps. "
        "Use it to find pass-through chains, round-tripping and the final beneficiary.",
        {
            "account_id": {"type": "string"},
            "depth": {"type": "integer", "description": "hops, default 4, max 6"},
        },
        ["account_id"],
    ),
    _spec(
        "find_shared_links",
        "Given an account, find the accounts it shares devices or a beneficiary with, plus any alerts on them, "
        "ranked by combined mule-ring signal score.",
        {"account_id": {"type": "string"}},
        ["account_id"],
    ),
    _spec(
        "detect_suspicious_cluster",
        "Run the ring-detection algorithm over the whole graph: union-find clustering on shared-device links, "
        "shared-beneficiary links and prior alerts. Use it when you need to look beyond one account.",
        {
            "min_signals": {"type": "integer", "description": "minimum distinct signals per member, default 2"},
            "top": {"type": "integer", "description": "how many clusters to return, default 5"},
        },
        [],
    ),
    _spec(
        "shortest_connection",
        "Breadth-first search for the shortest chain of relationships between two entities. "
        "Use it to prove how two suspects are linked and to narrate the path.",
        {
            "id_a": {"type": "string"},
            "id_b": {"type": "string"},
            "max_depth": {"type": "integer", "description": "default 6"},
        },
        ["id_a", "id_b"],
    ),
    _spec(
        "run_cypher",
        "Read-only Cypher escape hatch for questions the other tools cannot answer. "
        "Only MATCH / OPTIONAL MATCH / RETURN / WITH / CALL / UNWIND / SHOW / EXPLAIN. "
        "Return a single column aliased AS row when possible.",
        {
            "query": {"type": "string", "description": "read-only Cypher"},
            "purpose": {"type": "string", "description": "why this query is needed"},
        },
        ["query"],
    ),
    _spec(
        "flag_account",
        "ACTION: raise an alert on an account and set its status to under_review. "
        "Only do this when the graph evidence supports it.",
        {
            "account_id": {"type": "string"},
            "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
            "reason": {"type": "string", "description": "evidence-based reason, quote the signals"},
            "case_id": {"type": "string", "description": "optional case to attach the alert to"},
        },
        ["account_id", "severity", "reason"],
    ),
    _spec(
        "open_case",
        "ACTION: open an investigation case and link the entities involved, so any agent can pick it up later.",
        {
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "entity_ids": {"type": "array", "items": {"type": "string"}},
        },
        ["title"],
    ),
    _spec(
        "add_case_note",
        "ACTION: append a note with findings/evidence to an existing case.",
        {"case_id": {"type": "string"}, "text": {"type": "string"}},
        ["case_id", "text"],
    ),
    _spec(
        "remember",
        "ACTION: store a durable memory note so future sessions know it. "
        "kind=episodic (what happened), semantic (a durable fact), procedural (how to work). "
        "Link it to the entities it is about.",
        {
            "kind": {"type": "string", "enum": ["episodic", "semantic", "procedural"]},
            "text": {"type": "string"},
            "about_ids": {"type": "array", "items": {"type": "string"}},
        },
        ["kind", "text"],
    ),
    _spec(
        "recall",
        "Read long-term memory: earlier findings, playbooks and open cases, optionally filtered by text or kind. "
        "Use it before concluding, in case this was investigated before.",
        {
            "query": {"type": "string", "description": "entity id or free text"},
            "kind": {"type": "string", "enum": ["episodic", "semantic", "procedural", ""]},
        },
        [],
    ),
]


def execute(name: str, arguments: dict[str, Any]) -> dict:
    """Dispatch one tool call and always return JSON-safe data (errors included)."""
    handler = HANDLERS.get(name)
    if handler is None:
        return {"error": f"unknown tool '{name}'", "available": sorted(HANDLERS)}
    try:
        return handler(**arguments)
    except TypeError as exc:
        return {"error": f"bad arguments for {name}: {exc}"}
    except Exception as exc:  # noqa: BLE001 - tool failures must not kill the agent
        return {"error": f"{type(exc).__name__}: {exc}"[:400]}
