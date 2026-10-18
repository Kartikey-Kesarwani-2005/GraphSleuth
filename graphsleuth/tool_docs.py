"""Generate `docs/TOOLS.md` from the tool specs.

    python -m graphsleuth tools           # print the reference
    python -m graphsleuth tools --write   # refresh docs/TOOLS.md

The reference is generated, not hand-written, so it cannot drift: adding, renaming
or changing a tool without regenerating the file fails `tests/test_offline.py`.
"""

from __future__ import annotations

from pathlib import Path

from . import tools

DOC_PATH = Path(__file__).resolve().parent.parent / "docs" / "TOOLS.md"


def _type_of(schema: dict) -> str:
    kind = str(schema.get("type") or "any")
    if kind == "array":
        items = schema.get("items") or {}
        return f"array<{items.get('type', 'any')}>"
    return kind


def _render_tool(spec: dict) -> list[str]:
    function = spec["function"]
    name = function["name"]
    parameters = function.get("parameters") or {}
    properties = parameters.get("properties") or {}
    required = set(parameters.get("required") or [])
    writes = name in tools.MUTATING_TOOLS
    lines = [f"### `{name}`", ""]
    lines.append(f"**{'Read-write' if writes else 'Read-only'}.** {function.get('description', '').strip()}")
    lines.append("")
    if properties:
        lines.append("| argument | type | required | description |")
        lines.append("| --- | --- | --- | --- |")
        for arg, schema in properties.items():
            description = " ".join(str(schema.get("description", "")).split()).replace("|", "\\|")
            lines.append(
                f"| `{arg}` | {_type_of(schema)} | {'yes' if arg in required else 'no'} | "
                f"{description or '—'} |"
            )
    else:
        lines.append("_No arguments._")
    lines.append("")
    return lines


def render_tools_markdown() -> str:
    """The full tool reference, in the order the agents see the tools."""
    company_specs = tools.COMPANY_TOOL_SPECS

    lines = [
        "# Tool reference",
        "",
        f"All {len(tools.ALL_TOOL_SPECS)} tools the agents, the CLI and the MCP server can call. This",
        "file is generated from the tool specs (`graphsleuth/tools.py`) — run",
        "`python -m graphsleuth tools --write` after changing a tool, and the offline tests will fail",
        "if it is out of date.",
        "",
        "**Read vs write.** Six tools mutate the graph — `remember`, `open_case`, `add_case_note`,",
        "`flag_account`, `create_handoff`, `accept_handoff` — and they are the only entries in",
        "`tools.MUTATING_TOOLS`, which is what the report uses to list *verified* actions. Everything",
        "else reads. `run_cypher` is the one raw-Cypher entry point and refuses writes",
        "(`CREATE`, `DELETE`, `SET`, `MERGE`, `REMOVE`, `DROP`, `ALTER`, `LOAD`) via `READ_ONLY_BLOCKLIST`.",
        "",
        "**Every agent call is traced.** A tool result goes back into the model's context and into the",
        "session's evidence trail, which is what `demo --save`, the console's evidence pane and",
        "`explain.to_markdown` render — so the report can show the payload a claim came from.",
        "",
        f"## Fraud-investigation toolbelt ({len(tools.TOOL_SPECS)} tools)",
        "",
    ]
    for spec in tools.TOOL_SPECS:
        lines += _render_tool(spec)
    lines += [
        f"## Company-brain toolbelt ({len(company_specs)} tools)",
        "",
        "Track 03 (`python -m graphsleuth company-chat`): the same agent loop over an organisation's",
        "people, teams, systems, docs, tickets and decisions.",
        "",
    ]
    for spec in company_specs:
        lines += _render_tool(spec)
    return "\n".join(lines) + "\n"
