"""Unit tests for the pieces that do not need a live FalkorDB.

Run:  .venv/Scripts/python.exe -m pytest tests -q      (or plain `python tests/test_offline.py`)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from graphsleuth import explain, tools  # noqa: E402
from graphsleuth.agent import compose_final  # noqa: E402


def test_tool_specs_are_openai_shaped() -> None:
    assert len(tools.TOOL_SPECS) == 20
    for spec in tools.TOOL_SPECS:
        function = spec["function"]
        assert spec["type"] == "function"
        assert function["name"] in tools.HANDLERS
        assert function["parameters"]["type"] == "object"
        # every declared tool must have a handler and vice versa
    assert set(tools.HANDLERS) == {s["function"]["name"] for s in tools.TOOL_SPECS}


def test_graph_algorithm_and_handoff_tools_registered() -> None:
    """Track 01/02 requirements are actual agent tools, not hidden helpers."""
    for name in (
        "page_rank",
        "connected_components",
        "community_detection",
        "centrality",  # graph algorithms as agent tools
        "create_handoff",
        "list_handoffs",
        "accept_handoff",  # shared state + handoffs between agents
        "list_alerts",
    ):
        assert name in tools.HANDLERS, name
        assert name in {s["function"]["name"] for s in tools.TOOL_SPECS}


def test_multigraph_one_graph_per_tenant() -> None:
    from graphsleuth import db  # noqa: E402

    assert db.graph_name_for("default") == "graphsleuth:default"
    assert db.graph_name_for("Acme") == "graphsleuth:acme"  # normalised
    assert db.graph_name_for("  ") == db.graph_name_for(None)  # fallback to default
    assert db.graph_name_for("a") != db.graph_name_for("b")  # isolation by topology


def test_handoff_tool_validates_roles() -> None:
    result = tools.execute("create_handoff", {"to_role": "wizard", "context": "hi"})
    assert "error" in result
    result = tools.execute("create_handoff", {"to_role": "investigator", "context": "   "})
    assert "error" in result


def test_unknown_tool_returns_error() -> None:
    result = tools.execute("nope", {})
    assert "error" in result
    assert "available" in result


def test_readonly_guard_blocks_writes() -> None:
    assert "error" in tools.run_cypher("CREATE (n:Evil) RETURN n AS row")
    assert "error" in tools.run_cypher("MATCH (n) DETACH DELETE n RETURN n AS row")
    assert "error" in tools.run_cypher("MATCH (n) SET n.pwned = true RETURN n AS row")
    assert "error" in tools.run_cypher("DELETE FROM graph")  # bad start phrase
    # literals containing write words should not trip the guard
    assert "query must start" not in tools.run_cypher("MATCH (n) WHERE n.text = 'CREATE CASE' RETURN n AS row")


def test_guard_reports_start_phrase() -> None:
    # EXPLAIN and other allowed verbs must pass the guard and reach the database
    # (here it fails only because FalkorDB is not running in this offline test).
    result = tools.run_cypher("EXPLAIN MATCH (n) RETURN n AS row")
    message = str(result)
    assert "query must start" not in message
    assert "read-only tool" not in message


def test_explain_collects_path_from_trace() -> None:
    trace = [
        {
            "tool": "trace_money_flow",
            "arguments": {"account_id": "ACC-9003"},
            "result": {
                "outgoing": {
                    "levels": [
                        {
                            "level": 1,
                            "hops": [
                                {"from": "ACC-9003", "to": "ACC-9004", "tx": "TX-9003", "amount": 1.0, "timestamp": "t"}
                            ],
                        }
                    ]
                },
                "incoming": {"levels": []},
            },
        },
        {
            "tool": "shortest_connection",
            "arguments": {},
            "result": {
                "path": [{"node": "ACC-9003", "via": None}, {"node": "ACC-9004", "via": "ORIGINATED"}],
                "narrative": "ACC-9003 -[ORIGINATED]-> ACC-9004",
            },
        },
    ]
    nodes, edges = explain.collect_graph_facts(trace)
    assert "ACC-9003" in nodes and "ACC-9004" in nodes
    assert ("ACC-9003", "ACC-9004", "ORIGINATED") in edges or ("ACC-9003", "ACC-9004", "TX-9003") in edges
    diagram = explain.mermaid(nodes, edges)
    assert diagram.startswith("```mermaid")
    assert "-->" in diagram


def test_report_markdown_has_sections() -> None:
    result = {
        "answer": "**Verdict** — fraud ring.",
        "trace": [],
        "session_id": "SES-test",
        "tenant": "default",
        "brain": "demo",
        "rounds": 1,
    }
    markdown = explain.to_markdown("Investigate ACC-9003", result)
    for heading in ("# GraphSleuth investigation report", "## Verdict", "## Evidence trail"):
        assert heading in markdown


def test_compose_final_uses_real_results() -> None:
    history = [
        {"tool": "recall", "arguments": {}, "result": {"notes": [{"kind": "episodic", "text": "prior finding"}]}},
        {
            "tool": "trace_money_flow",
            "arguments": {},
            "result": {
                "outgoing": {
                    "levels": [
                        {
                            "level": 1,
                            "hops": [
                                {
                                    "from": "ACC-9003",
                                    "to": "ACC-9004",
                                    "tx": "TX-9003",
                                    "amount": 1790000.0,
                                    "timestamp": "2026-10-12",
                                }
                            ],
                        }
                    ]
                },
                "incoming": {"levels": []},
            },
        },
        {
            "tool": "detect_suspicious_cluster",
            "arguments": {},
            "result": {
                "clusters": [
                    {
                        "size": 2,
                        "cluster_score": 12.5,
                        "members": [
                            {
                                "account": "ACC-9003",
                                "risk_score": 0.77,
                                "shared_devices": ["DEV-9001"],
                                "shared_beneficiaries": [],
                                "alerts": [],
                                "score": 6.0,
                            },
                            {
                                "account": "ACC-9004",
                                "risk_score": 0.85,
                                "shared_devices": ["DEV-9002"],
                                "shared_beneficiaries": [],
                                "alerts": ["AL-9002"],
                                "score": 6.5,
                            },
                        ],
                    }
                ]
            },
        },
        {
            "tool": "shortest_connection",
            "arguments": {},
            "result": {"narrative": "ACC-EXT-01 -[TX-9000]-> ACC-9001"},
        },
        {"tool": "open_case", "arguments": {}, "result": {"case_id": "CASE-0002", "involves": ["ACC-9003"]}},
        {"tool": "add_case_note", "arguments": {}, "result": {"case_id": "CASE-0002", "note_id": "NOTE-abc"}},
        {
            "tool": "flag_account",
            "arguments": {},
            "result": {"flagged": True, "account": "ACC-9003", "alert_id": "AL-abc", "severity": "high"},
        },
        {"tool": "remember", "arguments": {}, "result": {"note_id": "MEM-abc", "linked_to": ["ACC-9003"]}},
    ]
    answer = compose_final("Investigate ACC-9003 — is this account part of a fraud ring?", history)
    assert "ACC-9003" in answer
    assert "TX-9003" in answer
    assert "CASE-0002" in answer
    assert "1,790,000" in answer  # real amount from the tool result
    assert "**Verdict**" in answer
    assert "**Graph path**" in answer


def test_shrink_keeps_results_small() -> None:
    payload = {"text": "x" * 5000, "items": list(range(100))}
    shrunk = tools._shrink(payload)
    assert len(shrunk["text"]) < 200
    assert len(shrunk["items"]) <= 25


def test_spec_descriptions_mention_usage() -> None:
    descriptions = {s["function"]["name"]: s["function"]["description"] for s in tools.TOOL_SPECS}
    assert "ACTION" in descriptions["flag_account"]
    assert "read-only" in descriptions["run_cypher"].lower()
    assert "memory" in descriptions["recall"].lower()


if __name__ == "__main__":
    failures = 0
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            try:
                function()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(json.dumps({"failures": failures}))
    raise SystemExit(1 if failures else 0)
