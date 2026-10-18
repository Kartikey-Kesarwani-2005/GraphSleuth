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


def _detect_cluster(min_signals: int = 2, top: int = 5, exclude: frozenset[str] = frozenset()) -> dict:
    """Core clustering pass (union-find). `exclude` drops nodes so a counterfactual
    "what if this entity did not exist?" check reuses the *same* algorithm."""
    skip = set(exclude)
    min_signals = max(1, int(min_signals))

    device_pairs = [
        row
        for row in db.query(
            """
            MATCH (a:Account)-[:USED_DEVICE]->(d:Device)<-[:USED_DEVICE]-(b:Account)
            WHERE a.id < b.id
            RETURN [a.id, b.id, d.id] AS row
            """
        )
        if not (skip & {str(row[0]), str(row[1]), str(row[2])})
    ]
    beneficiary_pairs = [
        row
        for row in db.query(
            """
            MATCH (x:Account)-[:ORIGINATED]->(:Transaction)-[:DESTINED]->(ben)
                  <-[:DESTINED]-(:Transaction)<-[:ORIGINATED]-(y:Account)
            WHERE x.id < y.id
            RETURN [x.id, y.id, ben.id] AS row
            """
        )
        if not (skip & {str(row[0]), str(row[1]), str(row[2])})
    ]
    alerts = [
        row
        for row in db.query(
            "MATCH (a:Account)-[:FLAGGED_BY]->(al:Alert) RETURN [a.id, al.severity] AS row"
        )
        if str(row[0]) not in skip
    ]
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
    payload = {
        "algorithm": "union-find over shared devices + beneficiary collectors + alerts",
        "min_signals": min_signals,
        "clusters_found": len(results),
        "clusters_filtered_out": filtered,
        "clusters": results[: max(1, int(top))],
    }
    if skip:
        payload["excluded"] = sorted(skip)
    return payload


def detect_suspicious_cluster(min_signals: int = 2, top: int = 5) -> dict:
    """Cluster the graph into suspect groups, with the evidence behind each member.

    A shared **device** is the strong link: two accounts seen on the same device
    is rare and hard to fake, so clusters are grown from those links (union-find).
    A shared **beneficiary** is deliberately NOT a link on its own — random
    transfers produce countless shared destinations — it only corroborates a
    cluster: a destination that collects from 2+ members is attached to the
    cluster as a collector account."""
    return _detect_cluster(min_signals, top)


def _cluster_members(cluster: dict | None) -> list[str]:
    """Sorted, deduplicated member account ids of a detected cluster."""
    return sorted(
        {
            str(member.get("account"))
            for member in ((cluster or {}).get("members") or [])
            if member.get("account")
        }
    )


def _pick_cluster(
    ring_key: str,
    min_signals: int,
    top: int,
    empty_note: str = "no cluster with enough signals at this threshold",
) -> tuple[dict | None, list[dict], str]:
    """Resolve which cluster a `ring_*` tool should inspect (and run the detector).

    `ring_key` accepts ``"top"`` (default), a 1-based cluster index, or any member
    account id. Returns ``(target, clusters, error)`` where ``error`` is empty on
    success — one line for callers:

        target, clusters, error = _pick_cluster(ring_key, min_signals, top, "...")
    """
    clusters = list(
        _detect_cluster(max(1, int(min_signals)), max(1, int(top))).get("clusters") or []
    )
    if not clusters:
        return None, [], empty_note
    key = str(ring_key or "top").strip()
    if key.isdigit():
        index = min(int(key), len(clusters)) - 1
        return clusters[max(0, index)], clusters, ""
    if key != "top":
        for cluster in clusters:
            if key in {str(m.get("account")) for m in cluster.get("members", [])}:
                return cluster, clusters, ""
        return None, clusters, f"'{key}' is not a member of any detected cluster"
    return clusters[0], clusters, ""


def _bfs_path(
    id_a: str,
    id_b: str,
    max_depth: int,
    allowed: set[str],
    skip: frozenset[str] = frozenset(),
) -> dict:
    """Breadth-first search shared by `shortest_connection` and the counterfactual
    `what_if_removed` (same walk, with `skip` nodes treated as if they did not exist)."""
    skip = set(skip)
    max_depth = max(1, min(int(max_depth), 8))
    if id_a in skip or id_b in skip:
        return {
            "from": id_a,
            "to": id_b,
            "connected": False,
            "via": sorted(allowed),
            "note": f"endpoint {id_a if id_a in skip else id_b} is the removed entity",
        }

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
                if not neighbour_id or neighbour_id in parents or neighbour_id in skip:
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


def shortest_connection(id_a: str, id_b: str, max_depth: int = 6, via: Any = None) -> dict:
    """Breadth-first search for the shortest chain of relationships between two entities.

    `via` optionally restricts the walk to specific relationship types, e.g.
    ``["ORIGINATED", "DESTINED"]`` to follow money only instead of letting the
    path cut through unrelated nodes such as cases or alerts.

    Agent-internal nodes (`Handoff`, `MemoryNote`, `Session`, `Message`) are
    never used as stepping stones: the agent's own bookkeeping must not become
    a shortcut through the fraud network it is investigating.
    """
    if isinstance(via, str):
        allowed = {part.strip().upper() for part in via.split(",") if part.strip()}
    elif isinstance(via, (list, tuple)):
        allowed = {str(part).strip().upper() for part in via if str(part).strip()}
    else:
        allowed = set()
    return _bfs_path(id_a, id_b, max_depth, allowed)


def what_if_removed(entity_id: str, from_id: str = "", to_id: str = "", max_depth: int = 6) -> dict:
    """Counterfactual explainability: pretend `entity_id` does not exist and re-check the
    conclusion the agent would otherwise draw.

    Three graph-native checks, all read-only (the graph is never modified):

    1. **Cluster** — does the mule ring still form without this device/account?
       (`detect_suspicious_cluster` re-run with the node excluded).
    2. **Bridge** — do the neighbours of this node still reach each other without it?
       If not, this node is a bridge and its removal severs paths the evidence leaned on.
    3. **Path** — with `from_id`/`to_id`, does the chain still exist when the node is skipped?
       Checked twice: over any relationship, and over the money trail only
       (`ORIGINATED`/`DESTINED`), because a fraud conclusion leans on the money path.

    Use it before escalating: a verdict that survives the removal of every single link
    is far stronger than one that depends on exactly one device or one account.
    """
    max_depth = max(1, min(int(max_depth), 8))
    entity_id = str(entity_id or "").strip()
    if not entity_id:
        return {"error": "entity_id is required", "example": {"entity_id": "DEV-9001"}}

    found = db.query("MATCH (n {id: $id}) RETURN labels(n) AS row", {"id": entity_id})
    if not found:
        return {"found": False, "entity": entity_id, "note": "no node carries that id"}
    labels = found[0] or []
    label = str(labels[0]) if isinstance(labels, list) and labels else entity_id.split("-")[0]

    neighbours_rows = db.query(
        "MATCH (n {id: $id})-[r]-(m) RETURN [type(r), m] AS row LIMIT $limit",
        {"id": entity_id, "limit": MAX_ROWS},
    )
    neighbours: list[dict] = []
    for rel_type, neighbour in neighbours_rows:
        summary = _node_summary(neighbour)
        if summary.get("id"):
            neighbours.append({"id": summary["id"], "label": (summary.get("labels") or [""])[0], "via": str(rel_type)})

    # --- 1. bridge: can this node's neighbours still reach each other without it? ---
    # Two probes count as "still connected" when their hop-limited worlds (computed
    # with the entity deleted) overlap — the overlap is itself a witness path that
    # avoids the removed node.
    bridge = {"is_bridge": False, "components": 1, "pairs_separated": 0, "reach_depth": 3}
    probe = [n["id"] for n in neighbours if n["id"] != entity_id][:10]
    if len(probe) >= 2:
        worlds = {start: _reachable(start, frozenset({entity_id}), depth=3) for start in probe}
        root: dict[str, str] = {start: start for start in probe}

        def _find(x: str) -> str:
            while root[x] != x:
                root[x] = root[root[x]]
                x = root[x]
            return x

        for index, a in enumerate(probe):
            for b in probe[index + 1 :]:
                if worlds[a] & worlds[b] and _find(a) != _find(b):
                    root[_find(b)] = _find(a)
        sizes: dict[str, int] = {}
        for start in probe:
            group = _find(start)
            sizes[group] = sizes.get(group, 0) + 1
        total_pairs = len(probe) * (len(probe) - 1) // 2
        kept_pairs = sum(size * (size - 1) // 2 for size in sizes.values())
        bridge = {
            "is_bridge": len(sizes) > 1,
            "components": len(sizes),
            "pairs_separated": max(0, total_pairs - kept_pairs),
            "reach_depth": 3,
        }

    # --- 2. cluster: does the ring survive without this node? ---
    before = _detect_cluster(2, 5)
    after = _detect_cluster(2, 5, exclude=frozenset({entity_id}))
    top_before = before["clusters"][0] if before["clusters"] else None
    top_after = after["clusters"][0] if after["clusters"] else None
    cluster = {
        "top_before": {"size": top_before["size"], "score": top_before["cluster_score"]} if top_before else None,
        "top_after": {"size": top_after["size"], "score": top_after["cluster_score"]} if top_after else None,
        "members_dropped": sorted(
            set(_cluster_members(top_before)) - set(_cluster_members(top_after))
        ),
    }

    # --- 3. path: does a requested chain still exist without this node? ---
    path: dict | None = None
    if from_id and to_id:
        def _check(allowed: set[str]) -> tuple[dict, dict]:
            base = _bfs_path(str(from_id), str(to_id), max_depth, allowed)
            counter = _bfs_path(
                str(from_id), str(to_id), max_depth, allowed, skip=frozenset({entity_id})
            )
            return base, counter

        base, counter = _check(set())
        money_before, money_after = _check({"ORIGINATED", "DESTINED"})
        path = {
            "from": str(from_id),
            "to": str(to_id),
            "connected_before": bool(base.get("connected")),
            "connected_after": bool(counter.get("connected")),
            "hops_before": base.get("hops"),
            "hops_after": counter.get("hops"),
            "before": base.get("narrative") or base.get("note"),
            "after": counter.get("narrative") or counter.get("note"),
            "money_before": bool(money_before.get("connected")),
            "money_after": bool(money_after.get("connected")),
            "money_hops_before": money_before.get("hops"),
            "money_hops_after": money_after.get("hops"),
            "money_trail_before": money_before.get("narrative") or money_before.get("note"),
            "money_trail_after": money_after.get("narrative") or money_after.get("note"),
            "money_path_before": money_before.get("path") or [],
            "money_path_after": money_after.get("path") or [],
        }

    # --- verdict ---
    impact = "minor"
    reasons: list[str] = []

    if path:
        if path["connected_before"] and not path["connected_after"]:
            impact = "critical"
            reasons.append(f"every {path['from']} -> {path['to']} chain runs through {entity_id}")
        elif path["money_before"] and not path["money_after"]:
            impact = "critical"
            reasons.append(
                f"the money trail {path['from']} -> {path['to']} runs only through {entity_id}"
            )
        elif path["money_before"] and path["money_after"]:
            reasons.append(
                f"the money trail {path['from']} -> {path['to']} still runs without {entity_id} "
                f"({path['money_hops_before']} -> {path['money_hops_after']} hops)"
            )

    if bridge["is_bridge"]:
        if impact != "critical":
            impact = "high"
        reasons.append(
            f"removing {entity_id} splits its {len(probe)} probed neighbours into "
            f"{bridge['components']} components ({bridge['pairs_separated']} pair(s) lose every path)"
        )

    dropped = (top_before["size"] - top_after["size"]) if (top_before and top_after) else 0
    if top_before and not top_after:
        impact = "critical"
        reasons.append("the top cluster disappears entirely without this node")
    elif top_before and top_after and dropped >= 2:
        if impact == "minor":
            impact = "high"
        reasons.append(
            f"the top cluster shrinks from {top_before['size']} to {top_after['size']} member(s), "
            f"score {top_before['cluster_score']} -> {top_after['cluster_score']}"
        )
    elif top_after:
        reasons.append(
            f"the ring still holds without {entity_id} "
            f"({top_after['size']} of {top_before['size']} members remain, "
            f"score {top_before['cluster_score']} -> {top_after['cluster_score']})"
            if dropped
            else f"the ring still holds without {entity_id} "
            f"({top_after['size']} members, score {top_after['cluster_score']})"
        )
    if not reasons:
        reasons.append(f"{entity_id} is not load-bearing for the current conclusion")

    return {
        "found": True,
        "entity": entity_id,
        "label": label,
        "removed_edges": len(neighbours_rows),
        "neighbours": neighbours[:20],
        "bridge": bridge,
        "cluster": cluster,
        "path": path,
        "impact": impact,
        **_robustness_metrics(entity_id, top_before, top_after, path, bridge, probe),
        "conclusion": f"Removing {entity_id} ({impact}): " + "; ".join(reasons) + ".",
        "note": "read-only counterfactual — the graph was NOT modified.",
    }


def _robustness_metrics(
    entity_id: str,
    top_before: dict | None,
    top_after: dict | None,
    path: dict | None,
    bridge: dict,
    probe: list[str],
) -> dict:
    path_survives = True
    money_survives = True
    ring_survives = True
    if path:
        if path.get("connected_before") and not path.get("connected_after"):
            path_survives = False
        if path.get("money_before") and not path.get("money_after"):
            money_survives = False
    if top_before and top_after:
        dropped = top_before["size"] - top_after["size"]
        if dropped >= 2 or top_after["size"] < 3:
            ring_survives = False
    elif top_before and not top_after:
        ring_survives = False
    elif not top_before:
        ring_survives = True

    score = 100
    if not money_survives or not path_survives:
        score = 0
    elif bridge.get("is_bridge"):
        penalty = min(bridge.get("pairs_separated") or 0, 20)
        score = max(0, 90 - penalty)
    if not ring_survives:
        score = min(score, 40)
    score = max(0, min(100, int(score)))

    if score >= 95:
        verdict = "robust"
    elif score >= 60:
        verdict = "mostly_robust"
    elif score >= 30:
        verdict = "fragile"
    else:
        verdict = "unstable"

    load_bearing = (100 - score) / 100.0
    if load_bearing < 0:
        load_bearing = 0.0
    if load_bearing > 1.0:
        load_bearing = 1.0

    candidates: list[dict] = []
    for nid in probe[:6]:
        candidates.append(
            {
                "entity_id": nid,
                "label": str(nid).split("-")[0],
                "why": "bridge-probe neighbour (alternate/redundancy candidate)",
            }
        )
    candidates = candidates[:3]

    return {
        "robustness": score,
        "verdict": verdict,
        "load_bearing_score": round(load_bearing, 3),
        "minimal_cut_candidates": candidates,
    }


def assess_ring_resilience(
    ring_key: str = "top",
    min_signals: int = 2,
    top: int = 5,
    depth: int = 2,
    from_id: str = "ACC-EXT-01",
    to_id: str = "ACC-9100",
) -> dict:
    """Batch counterfactual: stress-test **every** member of the suspected ring.

    For each member the same checks `what_if_removed` runs are repeated (cluster,
    bridge, money trail), and the members are ranked by how load-bearing they are.
    The result is a *resilience verdict* for the whole ring:

    - ``resilient``          — no single member is a bottleneck (the ring survives
                               losing any one account/device),
    - ``mostly_resilient``   — one weak link, the ring largely survives,
    - ``bottlenecked``       — ≥1 articulation points: remove them and the money
                               trail or the ring itself collapses,
    - ``fragile``             — several members are load-bearing.

    `articulation_points` lists the members whose removal breaks the conclusion —
    the graph-theory answer to "where is this network actually vulnerable?".
    Read-only; safe to run after every investigation.
    """
    depth = max(1, min(int(depth), 4))
    target_cluster, _, error = _pick_cluster(
        ring_key,
        min_signals,
        top,
        "no cluster with enough signals — nothing to stress-test",
    )
    if error:
        return {
            "found_ring": False,
            "note": error,
            "algorithm": "batch counterfactual over the top cluster",
        }

    members = _cluster_members(target_cluster)[:12]
    if not members:
        return {
            "found_ring": False,
            "note": "top cluster has no member accounts to test",
            "algorithm": "batch counterfactual over the top cluster",
        }

    rows: list[dict] = []
    articulations: list[str] = []
    total_before = target_cluster.get("size") or len(members)
    score_before = target_cluster.get("cluster_score") or 0

    for member in members:
        probe = what_if_removed(
            member, from_id=from_id, to_id=to_id, max_depth=depth + 2
        )
        if probe.get("error") or probe.get("found") is False:
            rows.append({"entity": member, "skipped": True})
            continue
        path = probe.get("path") or {}
        bridge = probe.get("bridge") or {}
        cluster = probe.get("cluster") or {}
        after = cluster.get("top_after") or {}
        members_lost = after.get("size") is not None and total_before - after["size"] >= 2
        money_breaks = bool(path.get("money_before")) and not bool(path.get("money_after"))
        any_breaks = bool(path.get("connected_before")) and not bool(path.get("connected_after"))
        is_articulation = bool(money_breaks or any_breaks or (after.get("size") == 0))
        rows.append(
            {
                "entity": member,
                "impact": probe.get("impact"),
                "robustness": probe.get("robustness"),
                "verdict": probe.get("verdict"),
                "load_bearing_score": probe.get("load_bearing_score"),
                "money_trail_breaks": money_breaks,
                "any_trail_breaks": any_breaks,
                "ring_loses_2plus": bool(members_lost),
                "cluster_size_after": after.get("size"),
                "cluster_score_after": after.get("score"),
                "is_articulation": is_articulation,
            }
        )
        if is_articulation:
            articulations.append(member)

    scored = [r for r in rows if not r.get("skipped")]
    mean_robustness = (
        round(sum(r.get("robustness") or 0 for r in scored) / len(scored))
        if scored
        else 0
    )
    mean_load_bearing = (
        round(sum(r.get("load_bearing_score") or 0 for r in scored) / len(scored), 3)
        if scored
        else 0.0
    )

    if not scored:
        verdict = "untestable"
    elif not articulations and mean_robustness >= 90:
        verdict = "resilient"
    elif not articulations and mean_robustness >= 75:
        verdict = "mostly_resilient"
    elif 1 <= len(articulations) <= 2:
        verdict = "bottlenecked"
    else:
        verdict = "fragile"

    rows.sort(key=lambda row: (row.get("is_articulation") is not True, row.get("robustness") or 0))

    summary = (
        f"Ring of {len(scored)} member(s) is {verdict}: "
        f"mean robustness {mean_robustness}/100, mean load-bearing {mean_load_bearing}. "
        + (
            f"Articulation points (remove these and the case collapses): {', '.join(articulations)}."
            if articulations
            else "No single member is load-bearing — the ring survives losing any one of them."
        )
    )

    return {
        "found_ring": True,
        "ring": sorted(members),
        "cluster_size_before": total_before,
        "cluster_score_before": score_before,
        "endpoints": {"from": from_id, "to": to_id},
        "members": rows,
        "articulation_points": articulations,
        "resilience": verdict,
        "mean_robustness": mean_robustness,
        "mean_load_bearing_score": mean_load_bearing,
        "summary": summary,
        "note": "read-only batch counterfactual — the graph was NOT modified.",
    }


def _cluster_profile(cluster: dict | None) -> dict:
    """Structural fingerprint of one cluster: size, collector shape, shared devices,
    alert count and mean risk — the features that separate a real ring from an
    innocent device coincidence."""
    members = list((cluster or {}).get("members") or [])
    devices: set[str] = set()
    beneficiaries: set[str] = set()
    alerts = 0
    collectors = 0
    risks: list[float] = []
    for member in members:
        if member.get("role") == "shared_beneficiary" or member.get("shared_beneficiaries"):
            collectors += 1
        devices.update(str(d) for d in (member.get("shared_devices") or []))
        beneficiaries.update(str(b) for b in (member.get("shared_beneficiaries") or []))
        alerts += len(member.get("alerts") or [])
        try:
            risks.append(float(member.get("risk_score") or 0))
        except (TypeError, ValueError):
            continue
    return {
        "size": int((cluster or {}).get("size") or len(members)),
        "members": sorted(str(m.get("account")) for m in members if m.get("account")),
        "devices": sorted(devices),
        "collectors": collectors,
        "alerts": alerts,
        "mean_risk": round(sum(risks) / len(risks), 3) if risks else 0.0,
        "score": float((cluster or {}).get("cluster_score") or 0),
    }


def _profile_similarity(target: dict, other: dict) -> tuple[int, list[str]]:
    """0-100 structural similarity between two cluster fingerprints, plus the features
    that matched (so the agent can *explain* the comparison, not just score it)."""
    reasons: list[str] = []

    # size: how close are the group sizes?
    biggest = max(target["size"], other["size"], 1)
    size_sim = 1.0 - abs(target["size"] - other["size"]) / biggest
    if size_sim >= 0.75:
        reasons.append(f"similar size ({other['size']} vs {target['size']})")

    # shape: does it funnel into a shared collector like ours does?
    if target["collectors"] and other["collectors"]:
        shape_sim = 1.0
        reasons.append("also funnels into a shared collector")
    elif not target["collectors"] and not other["collectors"]:
        shape_sim = 0.6
    else:
        shape_sim = 0.2

    # shared devices: the strong (hard-to-fake) link
    union = set(target["devices"]) | set(other["devices"])
    overlap = set(target["devices"]) & set(other["devices"])
    device_sim = (len(overlap) / len(union)) if union else 0.5
    if overlap:
        reasons.append(f"shares devices {sorted(overlap)}")

    # alert density: does it trip the same rules?
    if target["alerts"] and other["alerts"]:
        alert_sim = 1.0 - abs(target["alerts"] - other["alerts"]) / max(target["alerts"], other["alerts"])
        if alert_sim >= 0.75:
            reasons.append(f"similar alert load ({other['alerts']} vs {target['alerts']})")
    elif not target["alerts"] and not other["alerts"]:
        alert_sim = 0.6
        reasons.append("no alerts on either side")
    else:
        alert_sim = 0.2

    # risk: are the members equally suspicious on average?
    risk_gap = abs(target["mean_risk"] - other["mean_risk"])
    risk_sim = max(0.0, 1.0 - risk_gap / max(target["mean_risk"], other["mean_risk"], 0.1))
    if risk_sim >= 0.85:
        reasons.append(f"comparable mean risk ({other['mean_risk']} vs {target['mean_risk']})")

    score = 100 * (0.25 * size_sim + 0.25 * shape_sim + 0.25 * device_sim + 0.15 * alert_sim + 0.10 * risk_sim)
    return int(round(score)), reasons


def find_similar_rings(
    ring_key: str = "top",
    min_signals: int = 1,
    top: int = 8,
    limit: int = 3,
) -> dict:
    """Pattern generalisation: "is this playbook running anywhere else in the graph?"

    Detects every candidate group (loose threshold), fingerprints each one, and ranks
    the *other* groups by structural similarity to the suspected ring — size, collector
    shape, shared devices, alert load, risk. Also checks for money flowing between the
    ring and each look-alike (cross-links). The verdict separates a genuine repeat of
    the playbook from innocent coincidences (a bare shared device with no collector,
    no alerts and low risk). Read-only.
    """
    top = max(2, int(top))
    limit = max(1, min(int(limit), 6))
    target, clusters, error = _pick_cluster(
        ring_key,
        min_signals,
        top,
        "no candidate groups at this threshold — nothing to compare against",
    )
    if error:
        return {"found": False, "note": error}

    target_profile = _cluster_profile(target)
    target_members = set(target_profile["members"])

    # Money crossing between the ring and each look-alike (one query for all rows).
    cross_rows: list = []
    candidate_members: dict[str, set[str]] = {}
    for cluster in clusters:
        if cluster is target:
            continue
        for member in cluster.get("members", []):
            account = str(member.get("account") or "")
            if account:
                candidate_members.setdefault(account, set()).add(str(id(cluster)))
    if candidate_members:
        cross_rows = db.query(
            """
            MATCH (x:Account)-[:ORIGINATED]->(t:Transaction)-[:DESTINED]->(y:Account)
            WHERE (x.id IN $a AND y.id IN $b) OR (x.id IN $b AND y.id IN $a)
            RETURN [x.id, y.id, t.id, t.timestamp, t.amount] AS row LIMIT 40
            """,
            {"a": sorted(target_members), "b": sorted(candidate_members)},
        )

    comparisons: list[dict] = []
    for cluster in clusters:
        if cluster is target:
            continue
        profile = _cluster_profile(cluster)
        similarity, reasons = _profile_similarity(target_profile, profile)
        cross = [
            {
                "from": str(row[0]),
                "to": str(row[1]),
                "tx": str(row[2]),
                "timestamp": str(row[3] or ""),
                "amount": float(row[4] or 0),
            }
            for row in cross_rows
            if str(row[0]) in set(profile["members"]) or str(row[1]) in set(profile["members"])
        ]
        if cross:
            reasons.append(f"{len(cross)} transfer(s) link the two groups directly")
        comparisons.append(
            {
                "members": profile["members"],
                "size": profile["size"],
                "cluster_score": profile["score"],
                "collectors": profile["collectors"],
                "alerts": profile["alerts"],
                "mean_risk": profile["mean_risk"],
                "similarity": similarity,
                "matches": reasons,
                "cross_links": cross[:5],
            }
        )

    comparisons.sort(key=lambda row: row["similarity"], reverse=True)
    best = comparisons[:limit]
    top_similarity = best[0]["similarity"] if best else 0
    lookalikes = [row for row in best if row["similarity"] >= 60 and not row["cross_links"]]

    if top_similarity >= 60 and any(row["cross_links"] for row in best):
        verdict = "playbook_repeats_and_is_spreading"
        verdict_text = (
            "A structurally similar group exists AND money already flows between the two — "
            "the playbook is repeating, not isolated."
        )
    elif top_similarity >= 60:
        verdict = "playbook_repeats"
        verdict_text = (
            "A structurally similar group exists but shows no money link to this ring — "
            "treat as a possible copycat / second cell and check it independently."
        )
    elif top_similarity >= 40:
        verdict = "loose_resemblance_only"
        verdict_text = (
            "Look-alikes are superficial: they match on shape but lack the collector + alerts + "
            "risk combination. Coincidence is more likely than a second cell."
        )
    else:
        verdict = "unique_playbook"
        verdict_text = (
            "No other group in this graph shares the ring's fingerprint — the combination of "
            "shared devices, a collector funnel and alerts is unique to this case."
        )

    summary = (
        f"Compared the ring against {len(comparisons)} other candidate group(s): "
        f"closest match {top_similarity}/100. Verdict: {verdict}. {verdict_text}"
    )
    return {
        "found": True,
        "target": {
            "members": target_profile["members"],
            "size": target_profile["size"],
            "collectors": target_profile["collectors"],
            "alerts": target_profile["alerts"],
            "mean_risk": target_profile["mean_risk"],
            "cluster_score": target_profile["score"],
        },
        "compared": len(comparisons),
        "closest_similarity": top_similarity,
        "lookalike_count": len(lookalikes),
        "candidates": best,
        "verdict": verdict,
        "verdict_text": verdict_text,
        "summary": summary,
        "note": "read-only — similarity is structural (size/shape/devices/alerts/risk), "
                "not a claim of shared ownership.",
    }


def ring_timeline(
    ring_key: str = "top",
    min_signals: int = 2,
    top: int = 5,
    limit: int = 30,
) -> dict:
    """When did the ring actually act? Reconstructs the minute-by-minute story from the
    graph's own timestamps: every transfer touching the cluster (in, out and internal),
    merchant spends and alerts, merged into one ordered event list.

    Returns first/last activity, total value moved, the busiest burst window, per-member
    first-touch order and whether alerts fired before or after the money moved. This is
    the "narrative timeline" judges can verify against the raw rows. Read-only.
    """
    min_signals = max(1, int(min_signals))
    limit = max(5, min(int(limit), 60))
    target, _, error = _pick_cluster(
        ring_key, min_signals, top, "no cluster at this threshold — no timeline to build"
    )
    if error:
        return {"found": False, "note": error}

    members = _cluster_members(target)
    if not members:
        return {"found": False, "note": "top cluster has no member accounts"}

    # Every hop that touches a member: internal, inbound and outbound (one pass).
    transfer_rows = db.query(
        """
        MATCH (a:Account)-[:ORIGINATED]->(t:Transaction)-[:DESTINED]->(b:Account)
        WHERE a.id IN $m OR b.id IN $m
        RETURN [t.id, a.id, b.id, t.timestamp, t.amount, t.channel] AS row
        LIMIT $limit
        """,
        {"m": members, "limit": limit},
    )
    payment_rows = db.query(
        """
        MATCH (a:Account)-[:ORIGINATED]->(t:Transaction)-[:PAID]->(merch:Merchant)
        WHERE a.id IN $m
        RETURN [t.id, a.id, merch.id, t.timestamp, t.amount, t.category] AS row
        LIMIT $limit
        """,
        {"m": members, "limit": limit},
    )
    alert_rows = db.query(
        """
        MATCH (a:Account)-[:FLAGGED_BY]->(al:Alert)
        WHERE a.id IN $m
        RETURN [al.id, a.id, al.created_at, al.severity, al.rule] AS row
        LIMIT $limit
        """,
        {"m": members, "limit": limit},
    )

    events: list[dict] = []
    total_value = 0.0
    for row in transfer_rows:
        tx_id, src, dst, ts, amount, channel = (list(row) + [None] * 6)[:6]
        src, dst = str(src), str(dst)
        direction = "internal" if (src in members and dst in members) else (
            "inbound" if dst in members else "outbound"
        )
        value = float(amount or 0)
        total_value += value
        events.append(
            {
                "at": str(ts or ""),
                "kind": "transfer",
                "direction": direction,
                "tx": str(tx_id),
                "from": src,
                "to": dst,
                "amount": round(value, 2),
                "channel": str(channel or ""),
            }
        )
    for row in payment_rows:
        tx_id, account, merchant, ts, amount, category = (list(row) + [None] * 6)[:6]
        value = float(amount or 0)
        total_value += value
        events.append(
            {
                "at": str(ts or ""),
                "kind": "merchant_spend",
                "direction": "outbound",
                "tx": str(tx_id),
                "from": str(account),
                "to": str(merchant),
                "amount": round(value, 2),
                "category": str(category or ""),
            }
        )
    for row in alert_rows:
        alert_id, account, created, severity, rule = (list(row) + [None] * 5)[:5]
        events.append(
            {
                "at": str(created or ""),
                "kind": "alert",
                "direction": "detection",
                "alert": str(alert_id),
                "account": str(account),
                "severity": str(severity or ""),
                "rule": str(rule or ""),
            }
        )

    events = [e for e in events if e.get("at")]
    events.sort(key=lambda e: e["at"])
    if not events:
        return {
            "found": False,
            "note": "cluster members have no timestamped activity to reconstruct",
            "members": members,
        }

    transfers = [e for e in events if e["kind"] == "transfer"]
    alerts = [e for e in events if e["kind"] == "alert"]
    first_at, last_at = events[0]["at"], events[-1]["at"]

    # Burst detection: densest `window`-minute slice (default 60 minutes).
    window_minutes = 60
    burst = {"start": "", "end": "", "count": 0, "value": 0.0}
    for start in range(len(events)):
        window_value = 0.0
        count = 0
        start_ts = _iso_minutes(events[start]["at"])
        if start_ts is None:
            continue
        for other in events[start:]:
            other_ts = _iso_minutes(other["at"])
            if other_ts is None or other_ts - start_ts > window_minutes:
                break
            count += 1
            if other["kind"] == "transfer":
                window_value += other.get("amount") or 0
        if count > burst["count"]:
            burst = {
                "start": events[start]["at"],
                "end": events[min(start + count, len(events)) - 1]["at"],
                "count": count,
                "value": round(window_value, 2),
            }

    # First touch per member (order the ring activated in).
    first_touch: dict[str, str] = {}
    for event in events:
        if event["kind"] != "transfer":
            continue
        for side in (event["from"], event["to"]):
            if side in members and side not in first_touch:
                first_touch[side] = event["at"]
    activation_order = [
        {"member": member, "first_touch": first_touch[member]}
        for member in sorted(first_touch, key=lambda m: first_touch[m])
    ]

    # Did detection lead or follow the money?
    detection_lag = ""
    if alerts and transfers:
        first_alert = alerts[0]["at"]
        first_tx = transfers[0]["at"]
        if first_alert <= first_tx:
            detection_lag = (
                f"alerts fired BEFORE the first traced transfer ({first_alert} <= {first_tx}) — "
                "detection led the money"
            )
        else:
            lag_min = (_iso_minutes(first_alert) or 0) - (_iso_minutes(first_tx) or 0)
            detection_lag = (
                f"first alert {lag_min:.0f} minutes after the first traced transfer — "
                "money moved before detection"
            )

    inbound = sum(1 for e in transfers if e["direction"] == "inbound")
    internal = sum(1 for e in transfers if e["direction"] == "internal")
    outbound = sum(1 for e in transfers if e["direction"] == "outbound")
    spends = [e for e in events if e["kind"] == "merchant_spend"]

    summary = (
        f"Activity from {first_at} to {last_at}: {len(transfers)} transfer(s) moved "
        f"INR {total_value:,.0f} ({inbound} in / {internal} internal / {outbound} out), "
        f"{len(spends)} merchant spend(s), {len(alerts)} alert(s). "
        + (
            f"Burst: {burst['count']} events in one hour from {burst['start']} "
            f"(INR {burst['value']:,.0f}). "
            if burst["count"] >= 3
            else ""
        )
        + (detection_lag[:1].upper() + detection_lag[1:] + "." if detection_lag else "")
    )

    return {
        "found": True,
        "members": members,
        "window": {"first": first_at, "last": last_at},
        "totals": {
            "events": len(events),
            "transfers": len(transfers),
            "inbound": inbound,
            "internal": internal,
            "outbound": outbound,
            "merchant_spends": len(spends),
            "alerts": len(alerts),
            "value_moved": round(total_value, 2),
        },
        "burst": burst,
        "activation_order": activation_order,
        "detection_lag": detection_lag,
        "events": events[:limit],
        "summary": summary,
        "note": "read-only reconstruction from graph timestamps — nothing was written.",
    }


def _iso_minutes(stamp: str) -> float | None:
    """Minutes since the epoch for an ISO-8601 string (best effort, None if unparseable)."""
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp() / 60.0
    except (TypeError, ValueError):
        return None


def _reachable(start: str, skip: frozenset[str], depth: int = 3, budget: int = 300) -> set[str]:
    """Nodes reachable from `start` within `depth` hops, treating `skip` as absent.

    `budget` caps how many nodes one walk may visit so a hub's counterfactual never
    turns into a full-graph traversal on a large tenant.
    """
    blocked = set(skip)
    seen = {start}
    frontier = [start]
    for _ in range(max(1, depth)):
        nxt: list[str] = []
        for current in frontier:
            rows = db.query(
                "MATCH (c {id: $id})-[r]-(n) RETURN n AS row LIMIT $limit",
                {"id": current, "limit": MAX_ROWS},
            )
            for row in rows:
                # `RETURN n AS row` arrives as the node itself (already JSON), but
                # tolerate a wrapped cell so the walk never depends on that detail.
                cell = row[0] if isinstance(row, (list, tuple)) and row else row
                summary = _node_summary(cell)
                neighbour = summary.get("id")
                if not neighbour or neighbour in seen or neighbour in blocked:
                    continue
                if INTERNAL_LABELS & set(summary.get("labels") or []):
                    continue
                seen.add(neighbour)
                if len(seen) >= budget:
                    return seen
                nxt.append(neighbour)
        frontier = nxt
        if not frontier:
            break
    return seen


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


def _ranked_result(algorithm: str, engine: str, label: str, rows: list[Any], top: int) -> dict:
    """The common result shape of every node-ranking algorithm tool."""
    return {
        "algorithm": algorithm,
        "engine": engine,
        "label": label or "any",
        "nodes_ranked": len(rows),
        "top": _ranked_rows(rows, label, top),
    }


def _group_result(rows: list[Any], top: int, min_size: int) -> tuple[int, list[dict]]:
    """Shape `[members]` rows from a grouping algorithm (WCC / label propagation).

    Returns `(groups_found, top_n)` — the total number of groups at this size
    threshold, and the largest `top_n` shaped for the tool result.
    """
    groups = [sorted(str(member) for member in row) for row in rows if isinstance(row, list)]
    groups = [group for group in groups if len(group) >= max(1, int(min_size))]
    groups.sort(key=len, reverse=True)
    shaped = [{"size": len(group), "members": group[:25]} for group in groups[: max(1, int(top))]]
    return len(groups), shaped


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

    Uses FalkorDB's native `algo.pageRank(<label|null>, <reltype|null>)`. If a build
    does not expose the procedure, the tool falls back to an in-process power
    iteration over the same graph — the `engine` field always says which one
    produced the answer.
    """
    top = max(1, min(int(top), 50))

    rows: list[Any] = []
    try:
        # FalkorDB's native procedure is algo.pageRank(<node label|null>, <relationship type|null>).
        # (Some docs show an (iterations, damping) form; on this build those two numbers are read
        # as a label/type filter and match nothing — which is why this used to fall back.) We rank
        # the whole graph and filter to `label` afterwards, so `label='Account'` still means
        # "accounts that are central to the whole flow", not "accounts ranked inside accounts only".
        rows = db.query(
            "CALL algo.pageRank(null, null) YIELD node, score "
            "RETURN [coalesce(node.id, node.name), score] AS row"
        )
        engine = "falkordb:algo.pageRank"
    except Exception:
        rows = []
    if rows:
        return _ranked_result("pagerank", engine, label, rows, top)

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
    found, top_groups = _group_result(rows, top, min_size)
    return {
        "algorithm": "wcc (weakly connected components)",
        "engine": "falkordb:algo.WCC",
        "components_found": found,
        "components": top_groups,
    }


def community_detection(top: int = 8, min_size: int = 2) -> dict:
    """Label propagation: dense communities in the graph (who clusters with whom)."""
    rows = db.query(
        "CALL algo.labelPropagation() YIELD node, communityId "
        "WITH communityId, collect(coalesce(node.id, node.name)) AS members "
        "RETURN members AS row"
    )
    found, top_groups = _group_result(rows, top, min_size)
    return {
        "algorithm": "label propagation",
        "engine": "falkordb:algo.labelPropagation",
        "communities_found": found,
        "communities": top_groups,
    }


def centrality(kind: str = "betweenness", top: int = 10, label: str = "") -> dict:
    """Centrality: which nodes bridge (betweenness) or sit closest to all others
    (harmonic). Use it to find the account every path in the ring runs through.

    FalkorDB's betweenness samples 32 source nodes with a *time-based* seed by
    default, so the ranking changes between runs. We pin `samplingSeed` and set
    `samplingSize` past the node count, which makes the procedure use every
    source node — an exact, reproducible Brandes ranking (still <0.5s here).
    """
    top = max(1, min(int(top), 50))
    if kind == "harmonic":
        rows = db.query(
            "CALL algo.HarmonicCentrality() YIELD node, score "
            "RETURN [coalesce(node.id, node.name), score] AS row"
        )
        engine = "falkordb:algo.HarmonicCentrality"
    else:
        kind = "betweenness"
        try:
            rows = db.query(
                "CALL algo.betweenness($config) YIELD node, score "
                "RETURN [coalesce(node.id, node.name), score] AS row",
                {"config": {"samplingSize": 1_000_000, "samplingSeed": 42}},
            )
            engine = "falkordb:algo.betweenness (exact, samplingSeed=42)"
        except Exception:
            # Older builds only accept the bare call (sampled, non-reproducible).
            rows = db.query(
                "CALL algo.betweenness() YIELD node, score "
                "RETURN [coalesce(node.id, node.name), score] AS row"
            )
            engine = "falkordb:algo.betweenness (default sampling)"
    return _ranked_result(kind, engine, label, rows, top)


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
                coalesce(al.reason, al.rule, ''), al.created_at] AS row
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
HANDOFF_ROLES = {"triage", "investigator", "analyst", "verifier"}


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


def _next_case_id() -> str:
    """Allocate the next free case id, safely under concurrency.

    A plain `count(cases) + 1` is racy: two concurrent agents can read the same
    count and both claim the same id (Track 02 cares about high-concurrency
    writes). FalkorDB executes one statement atomically, so the id comes from a
    counter node incremented there. The counter bootstraps itself from the number
    of existing cases the first time it runs, so numbering continues exactly as
    before (after the seed's `CASE-0001`, the next case is `CASE-0002`). The
    existence check afterwards only matters if case ids are non-contiguous (a
    deleted case); it never fires in the normal path.
    """
    for _ in range(50):
        rows = db.query(
            """
            MATCH (c:Case)
            WITH count(c) AS existing
            MERGE (k:Counter {name: 'case_id'})
            ON CREATE SET k.value = existing
            SET k.value = k.value + 1
            RETURN k.value AS row
            """
        )
        candidate = f"CASE-{int(rows[0]):04d}" if rows else "CASE-0001"
        taken = db.query("MATCH (c:Case {id: $id}) RETURN c.id AS row LIMIT 1", {"id": candidate})
        if not taken:
            return candidate
    return f"CASE-{uuid.uuid4().hex[:6]}"  # vanishingly unlikely fallback


def open_case(title: str, summary: str = "", entity_ids: list[str] | None = None) -> dict:
    """Action: open an investigation case in the graph and attach the entities involved."""
    case_id = _next_case_id()

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
# Track 03 — company brain tools
# --------------------------------------------------------------------------- #
# Same toolbelt, same tenant model: these read the *org* graph (people, teams,
# projects, systems, docs, tickets, chats, decisions) of whatever tenant is
# active. Run `python -m graphsleuth company` to create the demo org graph, and
# set the tenant to 'company' (the `company-chat` command does this for you).
# The hackathon's Track 03 questions — who knows / who owns / what breaks / why
# decided / what do we know / what's stale — are each one graph traversal.

_EMPTY_HINT = (
    "no company-brain data in this tenant - this tool reads the org graph "
    "(Person/System/Doc/Ticket/Chat/Decision). Run `python -m graphsleuth company` "
    "and use tenant 'company'."
)


def _company():
    from . import company  # local import keeps the fraud toolbelt import-light

    return company


def who_knows(system_id: str) -> dict:
    """Who has worked on things that use this system (ranked) + declared experts."""
    company = _company()
    result = company.who_knows(system_id)
    result["query"] = company.WHO_KNOWS_QUERY
    if not result.get("people"):
        result["hint"] = _EMPTY_HINT
    return result


def owners_of(entity_id: str) -> dict:
    """Who owns a system or project, and the teammates reachable from that owner."""
    result = _company().owners_of(entity_id)
    if not result.get("owners"):
        result["hint"] = _EMPTY_HINT
    return result


def blast_radius(system_id: str) -> dict:
    """What breaks if this service goes down: reverse transitive :DEPENDS_ON closure."""
    result = _company().blast_radius(system_id)
    if not result.get("affected_systems"):
        result["hint"] = _EMPTY_HINT
    return result


def decision_trail(decision_id: str) -> dict:
    """Why a decision was made, traced to its doc/ticket/chat source, and what it replaced."""
    result = _company().decision_trail(decision_id)
    if not result.get("found"):
        result["hint"] = _EMPTY_HINT
    return result


def knowledge_sources(system_id: str) -> dict:
    """Everything written about a system — docs, tickets and chats — each with its author."""
    result = _company().knowledge_sources(system_id)
    if not (result.get("docs") or result.get("tickets") or result.get("chats")):
        result["hint"] = _EMPTY_HINT
    return result


def stale_knowledge(stale_days: int = 180) -> dict:
    """Keeping knowledge current: ageing docs, superseded decisions, and bus-factor risk."""
    return _company().stale_knowledge(stale_days)


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
    "what_if_removed": what_if_removed,
    "assess_ring_resilience": assess_ring_resilience,
    "ring_timeline": ring_timeline,
    "find_similar_rings": find_similar_rings,
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
    "who_knows": who_knows,
    "owners_of": owners_of,
    "blast_radius": blast_radius,
    "decision_trail": decision_trail,
    "knowledge_sources": knowledge_sources,
    "stale_knowledge": stale_knowledge,
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
        "what_if_removed",
        "Counterfactual explainability: mentally delete one entity (a device, account or person) "
        "and re-check the conclusion — does the mule ring still form, do its neighbours still "
        "reach each other, and do a chain and the money trail still exist without it? Call it "
        "before you escalate, so the verdict provably does not hinge on a single link. Read-only.",
        {
            "entity_id": {"type": "string", "description": "node id to remove mentally, e.g. 'DEV-9001' or 'ACC-9100'"},
            "from_id": {"type": "string", "description": "optional: start of a chain to re-check, e.g. 'ACC-EXT-01'"},
            "to_id": {"type": "string", "description": "optional: end of a chain to re-check, e.g. 'ACC-9100'"},
            "max_depth": {"type": "integer", "description": "hops for the chain check, default 6"},
        },
        ["entity_id"],
    ),
    _spec(
        "assess_ring_resilience",
        "Batch counterfactual: stress-test every member of the suspected ring at once and rank "
        "them by how load-bearing they are. Returns per-member robustness, the articulation "
        "points (remove those and the case collapses) and a resilience verdict for the whole "
        "ring: resilient / mostly_resilient / bottlenecked / fragile. Read-only.",
        {
            "ring_key": {"type": "string", "description": "'top' (default) or a member account id to locate its cluster"},
            "min_signals": {"type": "integer", "description": "cluster sensitivity, default 2"},
            "top": {"type": "integer", "description": "cluster selection size, default 5"},
            "depth": {"type": "integer", "description": "reach depth for each probe, default 2, max 4"},
            "from_id": {"type": "string", "description": "chain start for the money-trail probe, default 'ACC-EXT-01'"},
            "to_id": {"type": "string", "description": "chain end for the money-trail probe, default 'ACC-9100'"},
        },
        [],
    ),
    _spec(
        "ring_timeline",
        "Reconstruct when the suspected ring actually acted: every transfer touching the "
        "cluster (inbound/internal/outbound), merchant spends and alerts merged into one "
        "timestamp-ordered event list. Returns first/last activity, value moved, the busiest "
        "one-hour burst, per-member activation order and whether alerts led or followed the "
        "money. Use for 'when did this happen' / 'build the sequence of events'. Read-only.",
        {
            "ring_key": {"type": "string", "description": "'top' (default) or a member account id"},
            "min_signals": {"type": "integer", "description": "cluster sensitivity, default 2"},
            "top": {"type": "integer", "description": "cluster selection size, default 5"},
            "limit": {"type": "integer", "description": "max events, default 30, max 60"},
        },
        [],
    ),
    _spec(
        "find_similar_rings",
        "Pattern generalisation: is this playbook running anywhere else? Fingerprints the "
        "suspected ring (size, collector shape, shared devices, alert load, mean risk) and "
        "ranks every other candidate group by structural similarity, checking for money "
        "flowing between them (cross-links). Separates a repeat cell from innocent device "
        "coincidences. Use for 'are there more like this' / 'is this spreading'. Read-only.",
        {
            "ring_key": {"type": "string", "description": "'top' (default), a member id, or a 1-based cluster index"},
            "min_signals": {"type": "integer", "description": "candidate threshold, default 1 (loose, to find look-alikes)"},
            "top": {"type": "integer", "description": "candidate groups to consider, default 8"},
            "limit": {"type": "integer", "description": "similar groups to return, default 3, max 6"},
        },
        [],
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


# --- Track 03 company-brain tools ------------------------------------------- #
# Exposed to the agent and (via mcp_server) to any MCP client. They read the org
# graph of the active tenant; the `company-chat` agent runs with tenant='company'.
COMPANY_TOOL_SPECS: list[dict] = [
    _spec(
        "who_knows",
        "Track 03: who knows about a system. People who worked on projects that USE the system, "
        "ranked by how many such projects, plus people who explicitly declared expertise. "
        "Use this to answer 'who knows / who should I ask about <system>'.",
        {"system_id": {"type": "string", "description": "system id, e.g. SYS-ledger-db"}},
        ["system_id"],
    ),
    _spec(
        "owners_of",
        "Track 03: who owns a system or project — the owning person/team, plus the teammates "
        "reachable from that owner. Use for 'who is responsible for / who owns <thing>'.",
        {"entity_id": {"type": "string", "description": "system or project id, e.g. SYS-ledger-db"}},
        ["entity_id"],
    ),
    _spec(
        "blast_radius",
        "Track 03: what breaks if a service goes down. The reverse transitive :DEPENDS_ON closure "
        "— affected systems, projects at risk, and the teams that own them. "
        "Use for 'what depends on <system>' / 'impact if <system> fails'.",
        {"system_id": {"type": "string", "description": "system id, e.g. SYS-ledger-db"}},
        ["system_id"],
    ),
    _spec(
        "decision_trail",
        "Track 03: why a decision was made — who decided it, what it affects, the doc/ticket/chat "
        "it was sourced from, and which earlier decision it supersedes / was superseded by. "
        "Use for 'why did we decide X' / 'what is the story behind <decision>'.",
        {"decision_id": {"type": "string", "description": "decision id, e.g. ADR-002"}},
        ["decision_id"],
    ),
    _spec(
        "knowledge_sources",
        "Track 03: everything written about a system — docs, tickets and chats in one graph, each "
        "with a named author. Use to cite where an answer comes from.",
        {"system_id": {"type": "string", "description": "system id, e.g. SYS-ledger-db"}},
        ["system_id"],
    ),
    _spec(
        "stale_knowledge",
        "Track 03: what knowledge is going stale — docs not updated in N days, superseded decisions, "
        "and bus-factor risk (systems with <=1 declared expert that projects depend on). "
        "Use for 'what is out of date / risky to lose'.",
        {"stale_days": {"type": "integer", "description": "age threshold in days, default 180"}},
        [],
    ),
]

# The full toolbelt: the 24 fraud tools plus the 6 company-brain tools.
ALL_TOOL_SPECS: list[dict] = TOOL_SPECS + COMPANY_TOOL_SPECS


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
