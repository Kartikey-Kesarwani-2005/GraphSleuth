"""FalkorDB connection helpers — multigraph, one graph per tenant.

Everything the agent knows lives in the graph. This module is the only place
that talks to FalkorDB directly; the rest of the code goes through `query()`.

Multigraph topology (Track 01/02 brief: "leverage multigraph topology",
"separate memory per user or tenant"):

    every tenant gets its OWN FalkorDB graph:  <FALKORDB_GRAPH>:<tenant>
    e.g.  graphsleuth:default, graphsleuth:acme, graphsleuth:bank-x

Isolation is therefore topological, not a WHERE-clause convention: agent A
physically cannot read agent B's memory or fraud records, while both run on
one FalkorDB instance (concurrent reads/writes across graphs).

The active tenant travels through a `ContextVar`, so `db.query(...)` anywhere
in the code base runs against the right graph without threading a tenant
argument through every call site. `db.tenant_scope(...)` switches it explicitly
(the agent loop and the MCP tools use it).

Convention used by this project: internal queries return **one column** aliased
`AS row`, so the code never has to depend on FalkorDB's result header parsing.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from falkordb import FalkorDB

from .config import config

DEFAULT_TENANT = "default"

_client: FalkorDB | None = None
_graphs: dict[str, Any] = {}
_tenant: ContextVar[str] = ContextVar("graphsleuth_tenant", default=DEFAULT_TENANT)


# --------------------------------------------------------------------------- #
# multigraph: tenant -> graph name
# --------------------------------------------------------------------------- #
def graph_name_for(tenant: str) -> str:
    """Name of the FalkorDB graph that belongs to one tenant."""
    tenant = (tenant or DEFAULT_TENANT).strip().lower() or DEFAULT_TENANT
    return f"{config.graph_name}:{tenant}"


def resolve_tenant(tenant: str | None = None) -> str:
    """Explicit tenant wins, else the tenant of the current context."""
    if tenant:
        return tenant.strip().lower() or DEFAULT_TENANT
    return _tenant.get()


def set_tenant(tenant: str) -> None:
    """Pin the current context (thread / async task) to a tenant graph."""
    _tenant.set(resolve_tenant(tenant))


@contextmanager
def tenant_scope(tenant: str | None) -> Iterator[str]:
    """Run a block against one tenant's graph, then restore the previous one."""
    if not tenant:
        yield _tenant.get()
        return
    token = _tenant.set(resolve_tenant(tenant))
    try:
        yield _tenant.get()
    finally:
        _tenant.reset(token)


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
    """Select a graph by name, or the graph of the active tenant."""
    graph_name = name or graph_name_for(resolve_tenant())
    handle = _graphs.get(graph_name)
    if handle is None:
        handle = client().select_graph(graph_name)
        _graphs[graph_name] = handle
    return handle


def list_graphs() -> list[str]:
    """Every graph on this FalkorDB instance (one per tenant, plus leftovers)."""
    try:
        return sorted(str(name) for name in client().list_graphs())
    except Exception:  # pragma: no cover - admin helper, never fatal
        return []


def drop_graph(name: str) -> bool:
    """Delete a whole tenant graph (GRAPH.DELETE). Used by seed --wipe-tenant."""
    try:
        client().execute_command("GRAPH.DELETE", name)
        _graphs.pop(name, None)
        return True
    except Exception:  # pragma: no cover - admin helper, never fatal
        return False


def drop_tenant(tenant: str) -> bool:
    return drop_graph(graph_name_for(tenant))


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #
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


def query(
    cypher: str,
    params: dict[str, Any] | None = None,
    tenant: str | None = None,
) -> list[Any]:
    """Run a Cypher query on the active tenant's graph (or an explicit one).

    Queries should `RETURN <expr> AS row`. If the result has several columns we
    return positional dicts, which keeps ad-hoc Cypher usable from the agent.
    """
    result = graph(graph_name_for(resolve_tenant(tenant))).query(cypher, params or {})
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


def insert_rows(cypher: str, rows: list[dict], size: int = 400) -> None:
    """Write rows in batches with one parameterised ``UNWIND`` statement.

    Both seeders (fraud graph and company graph) load through this: FalkorDB
    handles a few hundred rows per round trip far better than one giant write.
    """
    for start in range(0, len(rows), size):
        query(cypher, {"rows": rows[start : start + size]})


def wipe_graph() -> None:
    """Delete every node and relationship of the active graph (a seeding reset)."""
    query("MATCH (n) DETACH DELETE n RETURN count(n) AS row")


def create_indexes(labels: tuple[str, ...] | list[str]) -> None:
    """Range indexes on the ``id`` property of each label (DDL: no RETURN clause).

    An already-existing index is not an error, so failures are swallowed — the
    seeders only care that lookups on ids are fast when they happen.
    """
    for label in labels:
        try:
            query(f"CREATE INDEX FOR (n:{label}) ON (n.id)")
        except Exception:  # noqa: BLE001 - pre-existing index or older syntax
            pass


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
    tenant = resolve_tenant()
    name = graph_name_for(tenant)
    rows = query("RETURN 1 AS row")
    counts = query(
        "MATCH (n) WITH count(n) AS nodes "
        "OPTIONAL MATCH ()-[r]->() WITH nodes, count(r) AS rels "
        "RETURN [nodes, rels] AS row"
    )
    return {
        "connected": rows == [1],
        "host": f"{config.falkordb_host}:{config.falkordb_port}",
        "tenant": tenant,
        "graph": name,
        "graphs": list_graphs(),
        "nodes": counts[0][0] if counts else 0,
        "relationships": counts[0][1] if counts else 0,
    }
