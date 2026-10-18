"""GraphSleuth as an MCP server (Track 01: "tool selection through MCP").

Any MCP-capable client — Claude Desktop, an IDE agent, another agent in your
team — can connect to this server and get the whole graph toolbelt: the same
thirty tools the built-in agents use (24 fraud-investigation tools plus 6
company-brain tools for Track 03), backed by the same FalkorDB graph and the
same memory. That is what makes the graph a *shared* context layer rather than
one agent's private data.

Run it:
    python -m graphsleuth.mcp_server            # stdio transport

Tenancy: the server works against one tenant's graph per process. Pick the
tenant with GRAPHSLEUTH_TENANT (default: `default`), so a client config gives
each user/agent its own graph while everything shares one FalkorDB instance.

Client configuration (example for a stdio MCP client):

    {
      "mcpServers": {
        "graphsleuth": {
          "command": "<path to>/.venv/Scripts/python.exe",
          "args": ["-m", "graphsleuth.mcp_server"],
          "env": {
            "FALKORDB_HOST": "localhost",
            "FALKORDB_PORT": "6379",
            "GRAPHSLEUTH_TENANT": "default"
          }
        }
      }
    }
"""

from __future__ import annotations

import json
import os
from typing import Any

try:
    from mcp.server.mcpserver import MCPServer  # mcp >= 2.0 (FastMCP was renamed)
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "This MCP server needs the 'mcp' package (pip install mcp). "
        f"Import failed with: {exc}"
    ) from exc

from . import db
from .tools import HANDLERS

# One MCP server process = one tenant's graph (multigraph per tenant).
db.set_tenant(os.getenv("GRAPHSLEUTH_TENANT", "") or db.DEFAULT_TENANT)

server = MCPServer(
    name="graphsleuth",
    title="GraphSleuth — fraud investigation graph",
    instructions=(
        f"Fraud investigation tools backed by tenant '{db.resolve_tenant()}' "
        f"(graph '{db.graph_name_for(db.resolve_tenant())}') in FalkorDB: accounts, people, "
        "devices, transactions, alerts, cases, agent handoffs and long-term agent memory. "
        "Start with entity_search or recall, follow connections with graph_neighbors / "
        "trace_money_flow / find_shared_links, rank with page_rank / centrality / "
        "connected_components / community_detection, hand work over with create_handoff / "
        "list_handoffs / accept_handoff, and use run_cypher for anything else (read-only). "
        "For company-brain (Track 03) questions — who knows a system, who owns it, what breaks "
        "if it goes down, why a decision was made, what we know, what's stale — use who_knows / "
        "owners_of / blast_radius / decision_trail / knowledge_sources / stale_knowledge against "
        "an org graph (tenant 'company')."
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


@server.tool(description="Shortest chain of relationships between two entities (BFS), for explaining how they are linked. Pass `via` to follow only certain relationship types (e.g. ['ORIGINATED','DESTINED'] for a money-only chain).")
def shortest_connection(
    id_a: str, id_b: str, max_depth: int = 6, via: list[str] | None = None
) -> str:
    return _call("shortest_connection", id_a=id_a, id_b=id_b, max_depth=max_depth, via=via or [])


@server.tool(description="Counterfactual explainability: pretend one entity does not exist and re-check the conclusion — does the ring still form, do its neighbours still reach each other, and does a chain still exist without it? Read-only.")
def what_if_removed(
    entity_id: str, from_id: str = "", to_id: str = "", max_depth: int = 6
) -> str:
    return _call(
        "what_if_removed",
        entity_id=entity_id,
        from_id=from_id,
        to_id=to_id,
        max_depth=max_depth,
    )


@server.tool(description="Batch counterfactual: stress-test every member of the suspected ring, rank by load-bearing, and return the ring's resilience verdict (resilient / mostly_resilient / bottlenecked / fragile) with articulation points. Read-only.")
def assess_ring_resilience(
    ring_key: str = "top",
    min_signals: int = 2,
    top: int = 5,
    depth: int = 2,
    from_id: str = "ACC-EXT-01",
    to_id: str = "ACC-9100",
) -> str:
    return _call(
        "assess_ring_resilience",
        ring_key=ring_key,
        min_signals=min_signals,
        top=top,
        depth=depth,
        from_id=from_id,
        to_id=to_id,
    )


@server.tool(description="Reconstruct when the suspected ring acted: timestamp-ordered transfers (in/out/internal), merchant spends and alerts, with first/last activity, value moved, busiest one-hour burst, member activation order and whether alerts led or followed the money. Read-only.")
def ring_timeline(
    ring_key: str = "top",
    min_signals: int = 2,
    top: int = 5,
    limit: int = 30,
) -> str:
    return _call(
        "ring_timeline",
        ring_key=ring_key,
        min_signals=min_signals,
        top=top,
        limit=limit,
    )


@server.tool(description="Pattern generalisation: fingerprint the suspected ring (size, collector shape, shared devices, alerts, risk) and rank every other candidate group by structural similarity, including money cross-links between them. Answers 'is this playbook running anywhere else / is it spreading'. Read-only.")
def find_similar_rings(
    ring_key: str = "top",
    min_signals: int = 1,
    top: int = 8,
    limit: int = 3,
) -> str:
    return _call(
        "find_similar_rings",
        ring_key=ring_key,
        min_signals=min_signals,
        top=top,
        limit=limit,
    )


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


@server.tool(description="PageRank: which entities sit at the centre of the flow through the graph.")
def page_rank(top: int = 10, label: str = "") -> str:
    return _call("page_rank", top=top, label=label)


@server.tool(description="Weakly connected components (algo.WCC): which parts of the graph are separate islands?")
def connected_components(top: int = 8, min_size: int = 2) -> str:
    return _call("connected_components", top=top, min_size=min_size)


@server.tool(description="Label propagation: dense communities in the graph — who clusters with whom.")
def community_detection(top: int = 8, min_size: int = 2) -> str:
    return _call("community_detection", top=top, min_size=min_size)


@server.tool(description="Centrality: betweenness (bridges) or harmonic (closest to everyone) via FalkorDB.")
def centrality(kind: str = "betweenness", top: int = 10, label: str = "") -> str:
    return _call("centrality", kind=kind, top=top, label=label)


@server.tool(description="Read the alert queue: accounts flagged before, newest first.")
def list_alerts(status: str = "", severity: str = "", limit: int = 10) -> str:
    return _call("list_alerts", status=status, severity=severity, limit=limit)


@server.tool(description="ACTION: hand the investigation to another agent — writes a Handoff node into the shared graph.")
def create_handoff(
    to_role: str,
    context: str,
    from_role: str = "",
    case_id: str = "",
    entity_ids: list[str] | None = None,
) -> str:
    return _call(
        "create_handoff",
        to_role=to_role,
        context=context,
        from_role=from_role,
        case_id=case_id,
        entity_ids=entity_ids or [],
    )


@server.tool(description="Read handoffs waiting to be picked up (default status=pending), with case and entities.")
def list_handoffs(status: str = "pending", limit: int = 5) -> str:
    return _call("list_handoffs", status=status, limit=limit)


@server.tool(description="ACTION: claim a handoff — marks it accepted and returns the full context.")
def accept_handoff(handoff_id: str) -> str:
    return _call("accept_handoff", handoff_id=handoff_id)


# --- Track 03 company-brain tools (org graph: people/teams/projects/systems/docs/tickets/chats/decisions) ---


@server.tool(description="Track 03: who knows about a system — people who worked on projects that use it (ranked), plus declared experts.")
def who_knows(system_id: str) -> str:
    return _call("who_knows", system_id=system_id)


@server.tool(description="Track 03: who owns a system or project, and the teammates reachable from that owner.")
def owners_of(entity_id: str) -> str:
    return _call("owners_of", entity_id=entity_id)


@server.tool(description="Track 03: what breaks if a system goes down — reverse transitive :DEPENDS_ON closure (systems, projects, teams).")
def blast_radius(system_id: str) -> str:
    return _call("blast_radius", system_id=system_id)


@server.tool(description="Track 03: why a decision was made — who decided, what it affects, the doc/ticket/chat source, and what it supersedes.")
def decision_trail(decision_id: str) -> str:
    return _call("decision_trail", decision_id=decision_id)


@server.tool(description="Track 03: every doc, ticket and chat about a system, each with a named author.")
def knowledge_sources(system_id: str) -> str:
    return _call("knowledge_sources", system_id=system_id)


@server.tool(description="Track 03: what knowledge is going stale — ageing docs, superseded decisions, and bus-factor risk.")
def stale_knowledge(stale_days: int = 180) -> str:
    return _call("stale_knowledge", stale_days=stale_days)


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
