"""FalkorDB connection helpers.

Everything the agent knows lives in the graph. This module is the only place
that talks to FalkorDB directly; the rest of the code goes through `query()`.

Convention used by this project: internal queries return **one column** aliased
`AS row`, so the code never has to depend on FalkorDB's result header parsing.
"""

from __future__ import annotations

from typing import Any

from falkordb import FalkorDB

from .config import config

_client: FalkorDB | None = None


def client() -> FalkorDB:
    """Return a lazily created FalkorDB client."""
    global _client
    if _client is None:
        kwargs: dict[str, Any] = {
            "host": config.falkordb_host,
            "port": config.falkordb_port,
        }
        if config.falkordb_password:
            kwargs["password"] = config.falkordb_password
        _client = FalkorDB(**kwargs)
    return _client


def graph(name: str | None = None):
    """Select the graph (database) that holds the fraud network + agent memory."""
    return client().select_graph(name or config.graph_name)


def to_json(value: Any) -> Any:
    """Convert FalkorDB values (nodes, relationships, paths) into plain JSON data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_json(v) for v in value]

    labels = getattr(value, "labels", None)
    properties = getattr(value, "properties", None)

    # Node: has labels + properties
    if labels is not None and isinstance(properties, dict):
        data = dict(properties)
        data["_labels"] = sorted(str(x) for x in labels)
        return to_json(data)

    # Relationship / Edge: has a type + properties
    rel_type = getattr(value, "relation", None) or getattr(value, "type", None)
    if rel_type is not None and isinstance(properties, dict):
        data = dict(properties)
        data["_type"] = str(rel_type)
        return to_json(data)

    # Path: expose its nodes and relationships
    nodes = getattr(value, "nodes", None)
    if nodes is not None:
        return {"nodes": to_json(nodes), "relationships": to_json(getattr(value, "edges", None))}

    return str(value)


def query(cypher: str, params: dict[str, Any] | None = None, g=None) -> list[Any]:
    """Run a Cypher query and return a list of JSON-safe `row` values.

    Queries should `RETURN <expr> AS row`. If the result has several columns we
    return positional dicts, which keeps ad-hoc Cypher usable from the agent.
    """
    result = (g or graph()).query(cypher, params or {})
    raw_rows = list(getattr(result, "result_set", []) or [])
    headers = _header_names(result)

    rows: list[Any] = []
    for record in raw_rows:
        values = list(record) if isinstance(record, (list, tuple)) else [record]
        if headers and len(headers) == len(values) and len(values) > 1:
            rows.append({headers[i]: to_json(v) for i, v in enumerate(values)})
        elif len(values) == 1:
            rows.append(to_json(values[0]))
        else:
            rows.append([to_json(v) for v in values])
    return rows


def _header_names(result: Any) -> list[str] | None:
    try:
        header = getattr(result, "header", None)
        if not header:
            return None
        names = []
        for column in header:
            name = getattr(column, "name", None) or getattr(column, "alias", None)
            names.append(str(name) if name else "")
        return names
    except Exception:  # pragma: no cover - header parsing is best effort
        return None


def ping() -> dict[str, Any]:
    """Connectivity check used by `python -m graphsleuth status`."""
    rows = query("RETURN 1 AS row")
    counts = query(
        "MATCH (n) WITH count(n) AS nodes "
        "OPTIONAL MATCH ()-[r]->() WITH nodes, count(r) AS rels "
        "RETURN [nodes, rels] AS row"
    )
    return {
        "connected": rows == [1],
        "host": f"{config.falkordb_host}:{config.falkordb_port}",
        "graph": config.graph_name,
        "nodes": counts[0][0] if counts else 0,
        "relationships": counts[0][1] if counts else 0,
    }
