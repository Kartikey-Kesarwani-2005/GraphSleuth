"""GraphSleuth as an MCP server (Track 01: "tool selection through MCP").

Any MCP-capable client — Claude Desktop, an IDE agent, another agent in your
team — can connect to this server and get the whole graph toolbelt: the same
twelve tools the built-in agent uses, backed by the same FalkorDB graph and the
same memory. That is what makes the graph a *shared* context layer rather than
one agent's private data.

Run it:
    python -m graphsleuth.mcp_server            # stdio transport

Client configuration (example for a stdio MCP client):

    {
      "mcpServers": {
        "graphsleuth": {
          "command": "<path to>/.venv/Scripts/python.exe",
          "args": ["-m", "graphsleuth.mcp_server"],
          "env": { "FALKORDB_HOST": "localhost", "FALKORDB_PORT": "6379" }
        }
      }
    }
"""

from __future__ import annotations

import json
from typing import Any

try:
    from mcp.server.mcpserver import MCPServer  # mcp >= 2.0 (FastMCP was renamed)
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "This MCP server needs the 'mcp' package (pip install mcp). "
        f"Import failed with: {exc}"
    ) from exc

from .tools import HANDLERS

server = MCPServer(
    name="graphsleuth",
    title="GraphSleuth — fraud investigation graph",
    instructions=(
        "Fraud investigation tools backed by a FalkorDB graph of accounts, people, devices, "
        "transactions, alerts, cases and long-term agent memory. Start with entity_search or "
        "recall, follow connections with graph_neighbors / trace_money_flow / find_shared_links, "
        "and use run_cypher for anything else (read-only)."
    ),
)


def _call(tool: str, **arguments: Any) -> str:
    """Dispatch to the shared toolbelt and return JSON text for the client."""
    result = HANDLERS[tool](**arguments)
    return json.dumps(result, default=str, ensure_ascii=False)


@server.tool(description="Find graph entities (accounts, people, devices, merchants, transactions, alerts, cases) by id or name.")
def entity_search(query: str, labels: list[str] | None = None, limit: int = 10) -> str:
    return _call("entity_search", query=query, labels=labels or [], limit=limit)


@server.tool(description="Expand an entity's neighbourhood hop by hop and return the connected subgraph.")
def graph_neighbors(entity_id: str, depth: int = 1, limit: int = 40) -> str:
    return _call("graph_neighbors", entity_id=entity_id, depth=depth, limit=limit)


@server.tool(description="Follow funds from an account in both directions, hop by hop, with transaction ids and amounts.")
def trace_money_flow(account_id: str, depth: int = 4) -> str:
    return _call("trace_money_flow", account_id=account_id, depth=depth)


@server.tool(description="Find accounts sharing devices or a beneficiary with this account, plus alerts on them.")
def find_shared_links(account_id: str) -> str:
    return _call("find_shared_links", account_id=account_id)


@server.tool(description="Ring detection: union-find clustering over shared devices, shared beneficiaries and prior alerts.")
def detect_suspicious_cluster(min_signals: int = 2, top: int = 5) -> str:
    return _call("detect_suspicious_cluster", min_signals=min_signals, top=top)


@server.tool(description="Shortest chain of relationships between two entities (BFS), for explaining how they are linked.")
def shortest_connection(id_a: str, id_b: str, max_depth: int = 6) -> str:
    return _call("shortest_connection", id_a=id_a, id_b=id_b, max_depth=max_depth)


@server.tool(description="Read-only Cypher escape hatch. Only MATCH/OPTIONAL MATCH/RETURN/WITH/CALL/UNWIND/SHOW/EXPLAIN.")
def run_cypher(query: str, purpose: str = "") -> str:
    return _call("run_cypher", query=query, purpose=purpose)


@server.tool(description="ACTION: raise an alert on an account and mark it under review.")
def flag_account(account_id: str, severity: str, reason: str, case_id: str = "") -> str:
    return _call("flag_account", account_id=account_id, severity=severity, reason=reason, case_id=case_id)


@server.tool(description="ACTION: open an investigation case and link the entities involved.")
def open_case(title: str, summary: str = "", entity_ids: list[str] | None = None) -> str:
    return _call("open_case", title=title, summary=summary, entity_ids=entity_ids or [])


@server.tool(description="ACTION: append a note with findings to an existing case.")
def add_case_note(case_id: str, text: str) -> str:
    return _call("add_case_note", case_id=case_id, text=text)


@server.tool(description="ACTION: store a durable memory note (episodic/semantic/procedural) linked to entities.")
def remember(kind: str, text: str, about_ids: list[str] | None = None) -> str:
    return _call("remember", kind=kind, text=text, about_ids=about_ids or [])


@server.tool(description="Read long-term memory (earlier findings, playbooks, open cases).")
def recall(query: str = "", kind: str = "") -> str:
    return _call("recall", query=query, kind=kind)


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
