"""Explainability: turn an investigation's evidence trail into something a human
(or a judge) can audit — numbered steps, the Cypher/tools used, and a Mermaid
diagram of the path the agent followed through the graph.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def render_trace(trace: list[dict]) -> str:
    """Plain-text evidence trail: what the agent asked, and what came back."""
    lines: list[str] = []
    for index, entry in enumerate(trace, start=1):
        result = entry.get("result")
        status = "ok"
        if isinstance(result, dict) and "error" in result:
            status = "error"
        lines.append(f"{index:>2}. {entry['tool']}  [{status}]")
        args = entry.get("arguments") or {}
        if args:
            lines.append(f"    args: {args}")
        lines.append(f"    {_digest(result)}")
    return "\n".join(lines)


def _digest(result: Any) -> str:
    if not isinstance(result, dict):
        return str(result)[:200]
    if "error" in result:
        return f"error: {result['error'][:200]}"
    parts = []
    for key, value in result.items():
        if isinstance(value, list):
            parts.append(f"{key}: {len(value)} items")
        elif isinstance(value, dict):
            inner = ", ".join(
                f"{k}: {len(v) if isinstance(v, (list, tuple, dict)) else v}"
                for k, v in list(value.items())[:4]
            )
            parts.append(f"{key}: {{{inner}}}")
        else:
            parts.append(f"{key}: {str(value)[:80]}")
    return " | ".join(parts)[:400]


def collect_graph_facts(trace: list[dict]) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """Extract nodes and typed edges from tool results for diagramming."""
    nodes: dict[str, str] = {}
    edges: list[tuple[str, str, str]] = []
    for entry in trace:
        _facts_into(entry, nodes, edges)
    return nodes, _dedupe(edges)


def facts_from_entry(entry: dict) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """Nodes and edges contributed by a single tool call.

    Used by the web UI to grow the graph live as the agent investigates.
    """
    nodes: dict[str, str] = {}
    edges: list[tuple[str, str, str]] = []
    _facts_into(entry, nodes, edges)
    return nodes, edges


def _facts_into(entry: dict, nodes: dict[str, str], edges: list[tuple[str, str, str]]) -> None:
    def add_node(node_id: str, label: str = "") -> None:
        if node_id:
            nodes.setdefault(node_id, label or node_id.split("-")[0])

    tool, result = entry.get("tool"), entry.get("result")
    if not isinstance(result, dict):
        return

    if tool == "graph_neighbors":
        add_node(str(result.get("start") or ""))
        for node in result.get("nodes", []) or []:
            if isinstance(node, dict):
                labels = node.get("labels") or []
                add_node(str(node.get("id")), labels[0] if labels else "")
        for edge in result.get("edges", []) or []:
            if isinstance(edge, dict):
                add_node(str(edge.get("from")))
                add_node(str(edge.get("to")))
                edges.append((str(edge.get("from")), str(edge.get("to")), str(edge.get("type"))))

    elif tool == "trace_money_flow":
        for direction in ("outgoing", "incoming"):
            for level in (result.get(direction) or {}).get("levels", []) or []:
                for hop in level.get("hops", []) or []:
                    add_node(str(hop.get("from")), "Account")
                    add_node(str(hop.get("to")), "Account")
                    edges.append((str(hop.get("from")), str(hop.get("to")), str(hop.get("tx"))))

    elif tool == "find_shared_links":
        add_node(str(result.get("account") or ""), "Account")
        for link in result.get("links", []) or []:
            account = str(link.get("account") or "")
            add_node(account, "Account")
            for device in link.get("shared_devices") or []:
                add_node(str(device), "Device")
                edges.append((account, str(device), "SHARED_DEVICE"))

    elif tool == "shortest_connection":
        path = result.get("path") or []
        for step in path:
            add_node(str(step.get("node")))
        for previous, step in zip(path, path[1:]):
            edges.append((str(previous.get("node")), str(step.get("node")), str(step.get("via"))))

    elif tool == "what_if_removed":
        entity = str(result.get("entity") or "")
        add_node(entity, str(result.get("label") or ""))
        for neighbour in result.get("neighbours", []) or []:
            other = str(neighbour.get("id") or "")
            if not other:
                continue
            add_node(other, str(neighbour.get("label") or ""))
            edges.append((other, entity, str(neighbour.get("via") or "LINKED")))
        steps = (result.get("path") or {}).get("money_path_before") or []
        for step in steps:
            add_node(str(step.get("node")))
        for previous, step in zip(steps, steps[1:]):
            edges.append((str(previous.get("node")), str(step.get("node")), str(step.get("via"))))

    elif tool == "detect_suspicious_cluster":
        for cluster in result.get("clusters", []) or []:
            for member in cluster.get("members", []) or []:
                account = str(member.get("account") or "")
                add_node(account, "Account")

    # --- Track 03 company-brain tools (org graph) ------------------------- #
    elif tool == "who_knows":
        system = str(result.get("system") or "")
        add_node(system, "System")
        for person in result.get("people", []) or []:
            pid = str(person.get("person") or "")
            add_node(pid, "Person")
            edges.append((pid, system, "WORKED_ON"))
        for expert in result.get("declared_experts", []) or []:
            pid = str(expert.get("person") or "")
            add_node(pid, "Person")
            edges.append((pid, system, "KNOWS"))

    elif tool == "owners_of":
        entity = str(result.get("entity") or "")
        add_node(entity)
        first_owner = ""
        for owner in result.get("owners", []) or []:
            owner_id = str(owner.get("id") or "")
            add_node(owner_id, owner.get("kind") or "Team")
            edges.append((owner_id, entity, "OWNS"))
            first_owner = first_owner or owner_id
        for person in result.get("reachable_people", []) or []:
            pid = str(person.get("person") or "")
            add_node(pid, "Person")
            if first_owner:
                edges.append((pid, first_owner, "MEMBER_OF"))

    elif tool == "blast_radius":
        system = str(result.get("system") or "")
        add_node(system, "System")
        for affected in result.get("affected_systems", []) or []:
            sid = str(affected.get("id") or "")
            add_node(sid, "System")
            edges.append((sid, system, "DEPENDS_ON"))
        for project in result.get("projects_at_risk", []) or []:
            pr_id = str(project.get("project") or "")
            add_node(pr_id, "Project")
            edges.append((pr_id, system, "AT_RISK"))
        for team in result.get("teams_at_risk", []) or []:
            t_id = str(team.get("team") or "")
            add_node(t_id, "Team")
            edges.append((t_id, system, "OWNS"))

    elif tool == "decision_trail":
        if result.get("found"):
            decision = str(result.get("decision") or "")
            add_node(decision, "Decision")
            for person in result.get("decided_by", []) or []:
                pid = str(person.get("person") or "")
                add_node(pid, "Person")
                edges.append((pid, decision, "DECIDED_BY"))
            for item in result.get("affects", []) or []:
                sid = str(item.get("system") or "")
                add_node(sid, "System")
                edges.append((decision, sid, "AFFECTS"))
            for source in result.get("sources", []) or []:
                src = str(source.get("id") or "")
                add_node(src, source.get("kind") or "Source")
                edges.append((decision, src, "SOURCED_FROM"))
            for old in result.get("supersedes", []) or []:
                old_id = str(old.get("decision") or "")
                add_node(old_id, "Decision")
                edges.append((decision, old_id, "SUPERSEDES"))

    elif tool == "knowledge_sources":
        system = str(result.get("system") or "")
        add_node(system, "System")
        for doc in result.get("docs", []) or []:
            did = str(doc.get("id") or "")
            add_node(did, "Doc")
            edges.append((did, system, "ABOUT"))
        for ticket in result.get("tickets", []) or []:
            tid = str(ticket.get("id") or "")
            add_node(tid, "Ticket")
            edges.append((tid, system, "ABOUT"))
        for chat in result.get("chats", []) or []:
            cid = str(chat.get("id") or "")
            add_node(cid, "Chat")
            edges.append((cid, system, "MENTIONS"))

    elif tool == "stale_knowledge":
        for doc in result.get("stale_docs", []) or []:
            add_node(str(doc.get("id") or ""), "Doc")
        for pair in result.get("superseded_decisions", []) or []:
            new_id = str(pair.get("replaced_by") or "")
            old_id = str(pair.get("old") or "")
            add_node(new_id, "Decision")
            add_node(old_id, "Decision")
            edges.append((new_id, old_id, "SUPERSEDES"))
        for risk in result.get("bus_factor_risks", []) or []:
            add_node(str(risk.get("system") or ""), "System")



def _short(value: Any) -> str:
    text = str(value)
    return text if len(text) <= 40 else text[:37] + "..."


def verified_actions(trace: list[dict]) -> list[str]:
    """Write actions that were actually executed, read straight from the tool log.

    A small model's prose is not trustworthy for this: it will happily claim it
    opened a case it never opened. This list comes from the trace, so the report
    can state what really happened regardless of what the answer text says.
    """
    from .tools import MUTATING_TOOLS

    actions: list[str] = []
    for entry in trace:
        tool = entry.get("tool")
        if tool in MUTATING_TOOLS:
            args = entry.get("arguments") or {}
            rendered = ", ".join(f"{key}={_short(value)}" for key, value in list(args.items())[:3])
            actions.append(f"`{tool}({rendered})`")
    return actions


def _dedupe(edges: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[tuple[str, str, str]] = []
    for edge in edges:
        if edge not in seen and edge[0] and edge[1]:
            seen.add(edge)
            unique.append(edge)
    return unique[:60]


def mermaid(nodes: dict[str, str], edges: list[tuple[str, str, str]]) -> str:
    lines = ["```mermaid", "graph LR"]
    for node_id, label in nodes.items():
        safe_id = _safe_id(node_id)
        lines.append(f'    {safe_id}["{node_id}<br/><i>{label}</i>"]')
    for source, target, label in edges:
        lines.append(f'    {_safe_id(source)} -->|{label}| {_safe_id(target)}')
    lines.append("```")
    return "\n".join(lines)


def _safe_id(raw: str) -> str:
    return "n" + "".join(ch if ch.isalnum() else "_" for ch in str(raw))


def to_markdown(question: str, result: dict) -> str:
    """A complete, shareable investigation report (answer + evidence + diagram)."""
    trace = result.get("trace", [])
    nodes, edges = collect_graph_facts(trace)
    actions = verified_actions(trace)

    parts = [
        "# GraphSleuth investigation report",
        "",
        f"- **Question:** {question}",
        f"- **Session:** {result.get('session_id')}   **Tenant:** {result.get('tenant')}",
        f"- **Brain:** {result.get('brain')}   **Rounds:** {result.get('rounds')}",
        "",
        "## Verdict",
        "",
        result.get("answer", ""),
        "",
        "## Actions taken (verified from the tool log)",
        "",
        *([f"- {action}" for action in actions] or ["- _None — read-only investigation._"]),
        "",
        "## Evidence trail",
        "",
        "```text",
        render_trace(trace),
        "```",
        "",
        "## Path through the graph",
        "",
        mermaid(nodes, edges) if nodes else "_No graph path was traversed._",
        "",
    ]
    return "\n".join(parts)


def save_evidence(path: str | Path, question: str, result: dict) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(to_markdown(question, result), encoding="utf-8")
    return target
