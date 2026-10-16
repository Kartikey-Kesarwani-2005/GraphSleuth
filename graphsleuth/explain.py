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

    def add_node(node_id: str, label: str = "") -> None:
        if node_id:
            nodes.setdefault(node_id, label or node_id.split("-")[0])

    for entry in trace:
        tool, result = entry.get("tool"), entry.get("result")
        if not isinstance(result, dict):
            continue

        if tool == "graph_neighbors":
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

        elif tool == "shortest_connection":
            path = result.get("path") or []
            for step in path:
                add_node(str(step.get("node")))
            for previous, step in zip(path, path[1:]):
                edges.append((str(previous.get("node")), str(step.get("node")), str(step.get("via"))))

        elif tool == "detect_suspicious_cluster":
            for cluster in result.get("clusters", []) or []:
                for member in cluster.get("members", []) or []:
                    add_node(str(member.get("account")), "Account")

    return nodes, _dedupe(edges)


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
