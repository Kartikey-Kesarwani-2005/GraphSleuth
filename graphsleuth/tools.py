"""The agent's toolbelt.

Every tool is a window onto the graph. Nothing here is a black box: each tool
returns the entity ids and relationship types that produced the answer, so the
agent can explain *how* it got there.

Two families:

* **read tools**  — entity_search, graph_neighbors, trace_money_flow,
  find_shared_links, detect_suspicious_cluster, shortest_connection,
  page_rank, connected_components, community_detection, centrality,
  list_alerts, list_handoffs, run_cypher
* **write tools** — flag_account, open_case, add_case_note, create_handoff,
  accept_handoff, remember, recall
  (these are the *actions* the agent takes; they are written back into the graph)

The four algorithm tools call FalkorDB's **native graph procedures**
(`algo.WCC`, `algo.labelPropagation`, `algo.betweenness`, `algo.pageRank`) —
graph algorithms are first-class agent tools, not Python helpers bolted on.
"""

from __future__ import annotations

import re
import threading
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Callable

from . import db, memory

MAX_ROWS = 60

# Nodes the agent writes about itself. They are useful context but must never be
# traversed as if they were part of the fraud network (see shortest_connection).
INTERNAL_LABELS = {"Handoff", "MemoryNote", "Session", "Message"}

# Session that is currently executing tool calls (stamped onto handoffs).
_session: ContextVar[str] = ContextVar("graphsleuth_session", default="")


def bind_session(session_id: str) -> None:
    """Bind tool calls in this context to an agent session."""
    _session.set(session_id or "")


def current_session() -> str:
    return _session.get()


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
    """Cluster the graph into suspect groups, with the evidence behind each member.

    A shared **device** is the strong link: two accounts seen on the same device
    is rare and hard to fake, so clusters are grown from those links (union-find).
    A shared **beneficiary** is deliberately NOT a link on its own — random
    transfers produce countless shared destinations — it only corroborates a
    cluster: a destination that collects from 2+ members is attached to the
    cluster as a collector account."""
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

    # Strong link: seen on the same device. Only these links merge accounts into a
    # cluster, because device identity is rare and hard to fake.
    for a, b, device in device_pairs:
        union(a, b)
        bump(a, "devices", device)
        bump(b, "devices", device)

    # Shared beneficiaries are recorded per account but deliberately do NOT merge
    # clusters on their own (random transfers share destinations all the time).
    # They corroborate a cluster: see "collectors" below.
    for x, y, beneficiary in beneficiary_pairs:
        bump(x, "beneficiaries", beneficiary)
        bump(y, "beneficiaries", beneficiary)

    for account, severity in alerts:
        bump(account, "alerts", severity)

    # Only accounts carrying a strong signal (a shared device, or a prior alert)
    # can seed a cluster.
    clusters: dict[str, list[str]] = {}
    for account, entry in signals.items():
        if entry["devices"] or entry["alerts"]:
            clusters.setdefault(find(account), []).append(account)

    results: list[dict] = []
    filtered = 0
    for members in clusters.values():
        member_set = set(members)

        # A beneficiary that collects from 2+ members corroborates the cluster.
        shared_ben: dict[str, int] = {}
        for account in members:
            for beneficiary in signals[account]["beneficiaries"]:
                shared_ben[beneficiary] = shared_ben.get(beneficiary, 0) + 1
        collectors = {
            beneficiary: count
            for beneficiary, count in shared_ben.items()
            if count >= 2 and beneficiary not in member_set
        }

        details = []
        total_score = 0.0
        for account in sorted(members):
            entry = signals[account]
            mine = sorted(b for b in entry["beneficiaries"] if shared_ben.get(b, 0) >= 2)
            score = (
                2.0 * len(entry["devices"])
                + 2.0 * len(mine)
                + 1.0 * len(entry["alerts"])
                + 1.0 * float(entry["risk"] or 0)
            )
            signal_count = (
                (1 if entry["devices"] else 0)
                + (1 if mine else 0)
                + (1 if entry["alerts"] else 0)
            )
            if signal_count < min_signals and len(members) < 3:
                continue
            total_score += score
            details.append(
                {
                    "account": account,
                    "role": "linked_account",
                    "risk_score": round(float(entry["risk"] or 0), 2),
                    "shared_devices": sorted(entry["devices"]),
                    "shared_beneficiaries": mine,
                    "alerts": entry["alerts"],
                    "score": round(score, 2),
                }
            )

        # The collector(s) join the cluster: the money sink tying the members together.
        for beneficiary, count in sorted(collectors.items(), key=lambda kv: -kv[1]):
            score = 2.0 * count
            total_score += score
            details.append(
                {
                    "account": beneficiary,
                    "role": "shared_beneficiary",
                    "risk_score": round(float(risk.get(beneficiary, 0) or 0), 2),
                    "shared_devices": [],
                    "shared_beneficiaries": sorted(
                        m for m in members if beneficiary in signals[m]["beneficiaries"]
                    ),
                    "alerts": (signals.get(beneficiary) or {}).get("alerts", []),
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
        else:
            filtered += 1

    results.sort(key=lambda item: item["cluster_score"], reverse=True)
    return {
        "algorithm": "union-find over shared devices + beneficiary collectors + alerts",
        "min_signals": min_signals,
        "clusters_found": len(results),
        "clusters_filtered_out": filtered,
        "clusters": results[: max(1, int(top))],
    }


def shortest_connection(id_a: str, id_b: str, max_depth: int = 6, via: Any = None) -> dict:
    """Breadth-first search for the shortest chain of relationships between two entities.

    `via` optionally restricts the walk to specific relationship types, e.g.
    ``["ORIGINATED", "DESTINED"]`` to follow money only instead of letting the
    path cut through unrelated nodes such as cases or alerts.

    Agent-internal nodes (`Handoff`, `MemoryNote`, `Session`, `Message`) are
    never used as stepping stones: the agent's own bookkeeping must not become
    a shortcut through the fraud network it is investigating.
    """
    max_depth = max(1, min(int(max_depth), 8))
    if isinstance(via, str):
        allowed = {part.strip().upper() for part in via.split(",") if part.strip()}
    elif isinstance(via, (list, tuple)):
        allowed = {str(part).strip().upper() for part in via if str(part).strip()}
    else:
        allowed = set()

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
                if allowed and str(rel_type).upper() not in allowed:
                    continue
                summary = _node_summary(neighbour)
                neighbour_id = summary.get("id")
                if not neighbour_id or neighbour_id in parents:
                    continue
                if neighbour_id != id_b and INTERNAL_LABELS & set(summary.get("labels") or []):
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
            "via": sorted(allowed),
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
        "via": sorted(allowed),
        "path": chain,
        "narrative": narrative,
    }


READ_ONLY_BLOCKLIST = re.compile(
    r"\b(CREATE|DELETE|DETACH|SET|MERGE|REMOVE|DROP|ALTER|LOAD)\b", re.IGNORECASE
)
ALLOWED_START = ("MATCH", "OPTIONAL", "RETURN", "WITH", "CALL", "UNWIND", "SHOW", "EXPLAIN")


# --------------------------------------------------------------------------- #
# graph algorithms as agent tools (FalkorDB native procedures)
# --------------------------------------------------------------------------- #
def _ranked_rows(rows: list[Any], label: str, top: int, score_index: int = 1) -> list[dict]:
    """Shape `[id, score]` rows from an algorithm into a ranked top-N list."""
    entries: list[dict] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        entity, score = row[0], row[score_index]
        if label and not _has_label(entity, label):
            continue
        entries.append({"entity": str(entity), "score": round(float(score), 5)})
    entries.sort(key=lambda item: item["score"], reverse=True)
    for index, entry in enumerate(entries[:top]):
        entry["rank"] = index + 1
    return entries[:top]


_LABEL_CACHE: dict[str, set[str]] = {}
_LABEL_LOCK = threading.Lock()


def _has_label(entity: Any, label: str) -> bool:
    """Does this entity id carry the given label? Cached per call context."""
    with _LABEL_LOCK:
        known = _LABEL_CACHE.get(label)
    if known is None:
        known = _labelled_ids(label)
        with _LABEL_LOCK:
            _LABEL_CACHE[label] = known
    return str(entity) in known


def page_rank(top: int = 10, label: str = "") -> dict:
    """PageRank: which entities sit at the centre of the flow through the graph.

    Tries FalkorDB's native `algo.pageRank` first. Some falkordb:latest builds
    yield zero rows from that procedure (verified on this dataset), so the tool
    falls back to an in-process power iteration over the same graph — the
    `engine` field always says which one produced the answer.
    """
    top = max(1, min(int(top), 50))

    rows: list[Any] = []
    try:
        rows = db.query(
            "CALL algo.pageRank(10, 0.85) YIELD node, score "
            "RETURN [coalesce(node.id, node.name), score] AS row"
        )
        engine = "falkordb:algo.pageRank"
    except Exception:
        rows = []
    if rows:
        return {
            "algorithm": "pagerank",
            "engine": engine,
            "label": label or "any",
            "nodes_ranked": len(rows),
            "top": _ranked_rows(rows, label, top),
        }

    return {
        "algorithm": "pagerank",
        "engine": "in-process power iteration (algo.pageRank yielded 0 rows)",
        "label": label or "any",
        **_pagerank_python(label, top),
    }


def _pagerank_python(label: str, top: int, iterations: int = 20, damping: float = 0.85) -> dict:
    edges = db.query(
        "MATCH (a)-[r]->(b) "
        "RETURN [coalesce(a.id, a.name, toString(id(a))), "
        "        coalesce(b.id, b.name, toString(id(b)))] AS row"
    )
    pairs = [(row[0], row[1]) for row in edges if isinstance(row, (list, tuple)) and len(row) == 2]
    nodes: set[str] = {str(node) for pair in pairs for node in pair}
    if label:
        nodes |= _labelled_ids(label)
    if not nodes:
        return {"nodes_ranked": 0, "top": []}

    outgoing: dict[str, list[str]] = {node: [] for node in nodes}
    for source, target in pairs:
        if source in nodes and target in nodes:
            outgoing[source].append(target)

    rank = {node: 1.0 / len(nodes) for node in nodes}
    for _ in range(iterations):
        dangling = sum(rank[node] for node in nodes if not outgoing[node])
        base = (1.0 - damping) / len(nodes) + damping * dangling / len(nodes)
        updated = {node: base for node in nodes}
        for source, targets in outgoing.items():
            if not targets or not rank[source]:
                continue
            share = damping * rank[source] / len(targets)
            for target in targets:
                updated[target] += share
        rank = updated

    rows = [[node, score] for node, score in rank.items()]
    return {"nodes_ranked": len(nodes), "top": _ranked_rows(rows, label, top)}


def _labelled_ids(label: str) -> set[str]:
    rows = db.query(
        "MATCH (n) WHERE $label IN labels(n) "
        "RETURN coalesce(n.id, n.name, toString(id(n))) AS row",
        {"label": label},
    )
    return {str(row) for row in rows}


def connected_components(top: int = 8, min_size: int = 2) -> dict:
    """Weakly connected components (`algo.WCC`): which parts of the graph form
    islands? Small islands around an alert are a strong 'isolated ring' signal."""
    rows = db.query(
        "CALL algo.WCC() YIELD node, componentId "
        "WITH componentId, collect(coalesce(node.id, node.name)) AS members "
        "RETURN members AS row"
    )
    groups = [sorted(str(member) for member in row) for row in rows if isinstance(row, list)]
    groups = [group for group in groups if len(group) >= max(1, int(min_size))]
    groups.sort(key=len, reverse=True)
    top_groups = [
        {"size": len(group), "members": group[:25]} for group in groups[: max(1, int(top))]
    ]
    return {
        "algorithm": "wcc (weakly connected components)",
        "engine": "falkordb:algo.WCC",
        "components_found": len(groups),
        "components": top_groups,
    }


def community_detection(top: int = 8, min_size: int = 2) -> dict:
    """Label propagation: dense communities in the graph (who clusters with whom)."""
    rows = db.query(
        "CALL algo.labelPropagation() YIELD node, communityId "
        "WITH communityId, collect(coalesce(node.id, node.name)) AS members "
        "RETURN members AS row"
    )
    groups = [sorted(str(member) for member in row) for row in rows if isinstance(row, list)]
    groups = [group for group in groups if len(group) >= max(1, int(min_size))]
    groups.sort(key=len, reverse=True)
    top_groups = [
        {"size": len(group), "members": group[:25]} for group in groups[: max(1, int(top))]
    ]
    return {
        "algorithm": "label propagation",
        "engine": "falkordb:algo.labelPropagation",
        "communities_found": len(groups),
        "communities": top_groups,
    }


def centrality(kind: str = "betweenness", top: int = 10, label: str = "") -> dict:
    """Centrality: which nodes bridge (betweenness) or sit closest to all others
    (harmonic). Use it to find the account every path in the ring runs through."""
    procedures = {
        "betweenness": "algo.betweenness",
        "harmonic": "algo.HarmonicCentrality",
    }
    kind = kind if kind in procedures else "betweenness"
    rows = db.query(
        f"CALL {procedures[kind]}() YIELD node, score "
        "RETURN [coalesce(node.id, node.name), score] AS row"
    )
    return {
        "algorithm": kind,
        "engine": f"falkordb:{procedures[kind]}",
        "label": label or "any",
        "nodes_ranked": len(rows),
        "top": _ranked_rows(rows, label, max(1, min(int(top), 50))),
    }


def list_alerts(status: str = "", severity: str = "", limit: int = 10) -> dict:
    """Read alerts raised on accounts — the queue a triage agent works through."""
    rows = db.query(
        """
        MATCH (a:Account)-[:FLAGGED_BY]->(al:Alert)
        WHERE ($status = '' OR coalesce(al.status, '') = $status)
          AND ($sev = '' OR coalesce(al.severity, '') = $sev)
        WITH al, a
        ORDER BY al.created_at DESC
        LIMIT $limit
        RETURN [al.id, al.severity, coalesce(al.status, ''), a.id,
                coalesce(al.reason, ''), al.created_at] AS row
        """,
        {"status": (status or "").strip().lower(), "sev": (severity or "").strip().lower(), "limit": max(1, int(limit))},
    )
    alerts = [
        {
            "alert_id": row[0],
            "severity": row[1],
            "status": row[2],
            "account": row[3],
            "reason": row[4],
            "created_at": row[5],
        }
        for row in rows
        if isinstance(row, (list, tuple)) and len(row) == 6
    ]
    return {"alerts": alerts, "count": len(alerts)}


# --------------------------------------------------------------------------- #
# multi-agent handoffs — state shared between agents through the graph
# --------------------------------------------------------------------------- #
HANDOFF_ROLES = {"triage", "investigator", "analyst"}


def create_handoff(
    to_role: str,
    context: str,
    from_role: str = "",
    case_id: str = "",
    entity_ids: list[str] | None = None,
) -> dict:
    """ACTION: hand the current investigation to another agent.

    The handoff is a `Handoff` node in the *shared* graph — the receiving agent
    reads it with `list_handoffs`, so state passes between agents through
    FalkorDB rather than through a caller (Track 02: shared state + handoffs).
    """
    to_role = (to_role or "").strip().lower()
    if to_role not in HANDOFF_ROLES:
        return {"error": f"to_role must be one of {sorted(HANDOFF_ROLES)}"}
    if not context.strip():
        return {"error": "context is required — the next agent needs to know why"}

    handoff_id = f"HANDOFF-{uuid.uuid4().hex[:6]}"
    from_role = (from_role or "triage").strip().lower()
    session = current_session()
    db.query(
        """
        CREATE (h:Handoff {id: $id, from_role: $from, to_role: $to, status: 'pending',
                context: $ctx, created_by: $sid, created_at: $ts})
        RETURN h.id AS row
        """,
        {"id": handoff_id, "from": from_role, "to": to_role, "ctx": context[:4000],
         "sid": session, "ts": _now()},
    )

    linked: list[str] = []
    for entity_id in entity_ids or []:
        rows = db.query(
            "MATCH (h:Handoff {id: $hid}), (e {id: $eid}) MERGE (h)-[:ABOUT]->(e) RETURN e.id AS row",
            {"hid": handoff_id, "eid": entity_id},
        )
        if rows:
            linked.append(entity_id)

    case_linked = ""
    if case_id:
        rows = db.query(
            "MATCH (h:Handoff {id: $hid}), (c:Case {id: $cid}) MERGE (h)-[:HANDLED_FOR]->(c) "
            "RETURN c.id AS row",
            {"hid": handoff_id, "cid": case_id},
        )
        case_linked = rows[0] if rows else ""

    return {
        "handoff_id": handoff_id,
        "from_role": from_role,
        "to_role": to_role,
        "status": "pending",
        "case": case_linked,
        "linked_entities": linked,
        "created_by": session,
        "context": context[:300],
    }


def list_handoffs(status: str = "pending", limit: int = 5) -> dict:
    """Read handoffs waiting for this kind of agent (plus their case + entities)."""
    rows = db.query(
        """
        MATCH (h:Handoff)
        WHERE ($status = '' OR h.status = $status)
        WITH h
        ORDER BY h.created_at DESC
        LIMIT $limit
        OPTIONAL MATCH (h)-[:ABOUT]->(e)
        OPTIONAL MATCH (h)-[:HANDLED_FOR]->(c:Case)
        WITH h, collect(DISTINCT e.id) AS entities, collect(DISTINCT c.id) AS cases
        RETURN [h.id, h.from_role, h.to_role, h.status, h.context, entities, cases,
                h.created_at, coalesce(h.created_by, '')] AS row
        """,
        {"status": (status or "").strip().lower(), "limit": max(1, int(limit))},
    )
    handoffs = [
        {
            "handoff_id": row[0],
            "from_role": row[1],
            "to_role": row[2],
            "status": row[3],
            "context": row[4],
            "entities": [x for x in (row[5] or []) if x],
            "cases": [x for x in (row[6] or []) if x],
            "created_at": row[7],
            "created_by": row[8],
        }
        for row in rows
        if isinstance(row, (list, tuple)) and len(row) == 9
    ]
    return {"handoffs": handoffs, "count": len(handoffs)}


def accept_handoff(handoff_id: str) -> dict:
    """ACTION: claim a handoff — marks it accepted and returns the full context."""
    rows = db.query(
        """
        MATCH (h:Handoff {id: $id})
        SET h.status = 'accepted', h.accepted_by = $sid, h.accepted_at = $ts
        RETURN [h.id, h.from_role, h.to_role, h.context, h.status] AS row
        """,
        {"id": handoff_id, "sid": current_session(), "ts": _now()},
    )
    if not rows or not isinstance(rows[0], (list, tuple)):
        return {"error": f"handoff {handoff_id} not found"}
    detail = rows[0]
    linked = db.query(
        "MATCH (h:Handoff {id: $id}) OPTIONAL MATCH (h)-[:ABOUT]->(e) "
        "OPTIONAL MATCH (h)-[:HANDLED_FOR]->(c:Case) "
        "RETURN [collect(DISTINCT e.id), collect(DISTINCT c.id)] AS row",
        {"id": handoff_id},
    )
    entities: list[str] = []
    cases: list[str] = []
    if linked and isinstance(linked[0], (list, tuple)):
        entities = [x for x in (linked[0][0] or []) if x]
        cases = [x for x in (linked[0][1] or []) if x]
    return {
        "handoff_id": detail[0],
        "from_role": detail[1],
        "to_role": detail[2],
        "context": detail[3],
        "status": detail[4],
        "entities": entities,
        "cases": cases,
        "accepted_by": current_session(),
    }


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
# Tools that change state. Reports use this to print a machine-verified
# "actions taken" list instead of trusting whatever the model wrote in prose.
MUTATING_TOOLS = {
    "flag_account",
    "open_case",
    "add_case_note",
    "create_handoff",
    "accept_handoff",
    "remember",
}

HANDLERS: dict[str, Callable[..., dict]] = {
    "entity_search": entity_search,
    "graph_neighbors": graph_neighbors,
    "trace_money_flow": trace_money_flow,
    "find_shared_links": find_shared_links,
    "detect_suspicious_cluster": detect_suspicious_cluster,
    "shortest_connection": shortest_connection,
    "page_rank": page_rank,
    "connected_components": connected_components,
    "community_detection": community_detection,
    "centrality": centrality,
    "list_alerts": list_alerts,
    "list_handoffs": list_handoffs,
    "run_cypher": run_cypher,
    "flag_account": flag_account,
    "open_case": open_case,
    "add_case_note": add_case_note,
    "create_handoff": create_handoff,
    "accept_handoff": accept_handoff,
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
        "Use it to prove how two suspects are linked and to narrate the path. "
        "Pass `via` to follow only certain relationship types (e.g. a money-only chain).",
        {
            "id_a": {"type": "string"},
            "id_b": {"type": "string"},
            "max_depth": {"type": "integer", "description": "default 6"},
            "via": {
                "type": "array",
                "items": {"type": "string"},
                "description": "optional relationship types to restrict the path, e.g. "
                "['ORIGINATED','DESTINED'] for a money-only chain",
            },
        },
        ["id_a", "id_b"],
    ),
    _spec(
        "page_rank",
        "PageRank over the whole graph: which entities sit at the centre of the flow. "
        "Use it to rank suspects/accounts by structural influence before investigating one. "
        "Pass label='Account' to rank only accounts.",
        {
            "top": {"type": "integer", "description": "how many ranked entities, default 10"},
            "label": {"type": "string", "description": "optional node label filter, e.g. 'Account'"},
        },
        [],
    ),
    _spec(
        "connected_components",
        "Run FalkorDB's WCC algorithm: which parts of the graph are separate islands? "
        "Isolated clusters around an alert are a strong 'ring' signal; size>=2 only.",
        {
            "top": {"type": "integer", "description": "components to return, default 8"},
            "min_size": {"type": "integer", "description": "ignore components smaller than this, default 2"},
        },
        [],
    ),
    _spec(
        "community_detection",
        "Run FalkorDB's label-propagation algorithm: dense communities in the graph — "
        "who clusters with whom regardless of my assumptions about devices or beneficiaries.",
        {
            "top": {"type": "integer", "description": "communities to return, default 8"},
            "min_size": {"type": "integer", "description": "ignore communities smaller than this, default 2"},
        },
        [],
    ),
    _spec(
        "centrality",
        "Graph centrality via FalkorDB: kind=betweenness (which nodes bridge other pairs — "
        "the account every path runs through) or harmonic (closest to everyone). "
        "Use it to find the hub of a mule ring.",
        {
            "kind": {"type": "string", "enum": ["betweenness", "harmonic"]},
            "top": {"type": "integer", "description": "default 10"},
            "label": {"type": "string", "description": "optional node label filter, e.g. 'Account'"},
        },
        [],
    ),
    _spec(
        "list_alerts",
        "Read the alert queue: accounts flagged before, newest first. "
        "Start a triage pass with this to decide what deserves investigation.",
        {
            "status": {"type": "string", "description": "filter, e.g. 'open' or 'under_review'"},
            "severity": {"type": "string", "enum": ["low", "medium", "high", "critical", ""]},
            "limit": {"type": "integer", "description": "default 10"},
        },
        [],
    ),
    _spec(
        "create_handoff",
        "ACTION: hand the current investigation to another agent (role: investigator / triage / "
        "analyst). Writes a Handoff node into the shared graph — context, case and entities — "
        "so the next agent picks it up with list_handoffs without you pasting anything.",
        {
            "to_role": {"type": "string", "enum": ["investigator", "triage", "analyst"]},
            "context": {"type": "string", "description": "why you are handing off + what you found so far"},
            "from_role": {"type": "string", "description": "your role, default 'triage'"},
            "case_id": {"type": "string", "description": "case this belongs to, if any"},
            "entity_ids": {"type": "array", "items": {"type": "string"}},
        },
        ["to_role", "context"],
    ),
    _spec(
        "list_handoffs",
        "Read handoffs waiting to be picked up (default status=pending), with their case and "
        "linked entities. Call this first when you are picking up someone else's work.",
        {
            "status": {"type": "string", "description": "'pending', 'accepted', or '' for all"},
            "limit": {"type": "integer", "description": "default 5"},
        },
        [],
    ),
    _spec(
        "accept_handoff",
        "ACTION: claim a handoff for this session — marks it accepted and returns the full "
        "context, case and entities the previous agent left for you.",
        {"handoff_id": {"type": "string"}},
        ["handoff_id"],
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
    _LABEL_CACHE.clear()  # label lookups are per-call so flags show up immediately
    handler = HANDLERS.get(name)
    if handler is None:
        return {"error": f"unknown tool '{name}'", "available": sorted(HANDLERS)}
    try:
        return handler(**arguments)
    except TypeError as exc:
        return {"error": f"bad arguments for {name}: {exc}"}
    except Exception as exc:  # noqa: BLE001 - tool failures must not kill the agent
        return {"error": f"{type(exc).__name__}: {exc}"[:400]}
