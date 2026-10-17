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


def test_llm_model_chain_primary_first() -> None:
    import os

    from graphsleuth.config import config

    saved_model, saved_env = config.groq_model, os.environ.get("LLM_MODELS")
    config.groq_model = "primary-model"
    os.environ["LLM_MODELS"] = "primary-model, second ,third,second"  # dupes + spaces
    try:
        assert config.llm_models == ["primary-model", "second", "third"]
    finally:
        config.groq_model = saved_model
        if saved_env is None:
            os.environ.pop("LLM_MODELS", None)
        else:
            os.environ["LLM_MODELS"] = saved_env


def test_daily_cap_detection() -> None:
    from graphsleuth.llm import _is_daily_cap

    class R:
        def __init__(self, text: str) -> None:
            self.text = text

    assert _is_daily_cap(R("Rate limit reached on tokens per day (TPD): Limit 200000"))
    assert not _is_daily_cap(R("Please try again in 5.9025s"))  # burst limit, not daily


def test_livebrain_walks_fallback_on_daily_cap() -> None:
    """A model that hits its per-day cap must not stop the run — the next model does it."""
    import os

    from graphsleuth import llm as llm_module
    from graphsleuth.config import config

    class FakeResponse:
        def __init__(self, status: int, text: str = "", body: dict | None = None) -> None:
            self.status_code = status
            self.text = text
            self.headers: dict = {}
            self._body = body or {}

        def json(self) -> dict:
            return self._body

    seen: list[str] = []

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002
        model = json["model"]
        seen.append(model)
        if model == "exhausted-model":
            return FakeResponse(429, "Rate limit reached on tokens per day (TPD): Limit 200000")
        return FakeResponse(200, body={"choices": [{"message": {"content": "ok", "tool_calls": []}}]})

    saved = (config.groq_api_key, config.llm_mode, config.groq_model, os.environ.get("LLM_MODELS"))
    config.groq_api_key, config.llm_mode, config.groq_model = "test-key", "live", "exhausted-model"
    os.environ["LLM_MODELS"] = "exhausted-model,fallback-model"
    original_post = llm_module.requests.post
    llm_module.requests.post = fake_post
    try:
        brain = llm_module.LiveBrain()
        assert brain._chain == ["exhausted-model", "fallback-model"]
        reply = brain.chat([{"role": "user", "content": "hi"}], [])
        assert reply.content == "ok"
        assert brain.model == "fallback-model"  # advanced
        assert seen[:2] == ["exhausted-model", "fallback-model"]
    finally:
        llm_module.requests.post = original_post
        config.groq_api_key, config.llm_mode, config.groq_model, saved_env = saved[0], saved[1], saved[2], saved[3]
        if saved_env is None:
            os.environ.pop("LLM_MODELS", None)
        else:
            os.environ["LLM_MODELS"] = saved_env


def test_distill_playbook_orders_tools_and_drops_bookkeeping() -> None:
    from graphsleuth import memory

    trace = [
        {"tool": "recall", "arguments": {}, "result": {}},
        {"tool": "recall", "arguments": {}, "result": {}},   # repeated -> collapse
        {"tool": "entity_search", "arguments": {"query": "ACC-9003"}, "result": {}},
        {"tool": "trace_money_flow", "arguments": {"account_id": "ACC-9003"}, "result": {}},
        {"tool": "remember", "arguments": {}, "result": {}},  # bookkeeping -> dropped
        {"tool": "flag_account", "arguments": {"account_id": "ACC-9003"}, "result": {}},
    ]
    playbook = memory.distill_playbook(trace, "investigate ACC-9003")
    assert playbook is not None
    assert playbook["steps"] == ["recall", "entity_search", "trace_money_flow", "flag_account"]
    assert " > " in playbook["signature"]
    assert "ACC-9003" in playbook["entities"]
    # too few tools is not a procedure
    assert memory.distill_playbook([{"tool": "recall", "arguments": {}, "result": {}}]) is None
    assert memory.distill_playbook([]) is None


def test_facts_from_entry_extracts_nodes_and_edges() -> None:
    from graphsleuth import explain

    nodes, edges = explain.facts_from_entry(
        {
            "tool": "find_shared_links",
            "arguments": {},
            "result": {
                "account": "ACC-9003",
                "links": [{"account": "ACC-9002", "shared_devices": ["DEV-9001"], "shared_beneficiaries": []}],
            },
        }
    )
    assert {"ACC-9003", "ACC-9002", "DEV-9001"} <= set(nodes)
    assert ("ACC-9002", "DEV-9001", "SHARED_DEVICE") in edges

    nodes2, edges2 = explain.facts_from_entry(
        {"tool": "trace_money_flow", "arguments": {}, "result": {"outgoing": {"levels": [{"hops": [{"from": "ACC-9001", "to": "ACC-9002", "tx": "TX-9001"}]}]}, "incoming": {"levels": []}}}
    )
    assert {"ACC-9001", "ACC-9002"} <= set(nodes2)
    assert ("ACC-9001", "ACC-9002", "TX-9001") in edges2


def test_web_console_assets_are_shipped() -> None:
    from graphsleuth import server

    assert (server.WEB_DIR / "index.html").exists()
    assert (server.WEB_DIR / "app.js").exists()
    assert (server.WEB_DIR / "app.css").exists()


def test_eval_scorecard_is_registered_and_renders() -> None:
    from graphsleuth import evaluate

    assert len(evaluate.CHECKS) >= 10
    names = [name for name, _ in evaluate.CHECKS]
    assert "mule ring detected" in names and "tenant isolation" in names
    # render() is pure: build a fake report and check the table + score line
    fake = {
        "tenant": "default",
        "checks": [
            {"check": "mule ring detected", "passed": True, "detail": "6/6"},
            {"check": "tenant isolation", "passed": False, "detail": "leak"},
        ],
        "passed": 1,
        "total": 2,
        "score": 50,
    }
    out = evaluate.render(fake)
    assert "[PASS] mule ring detected" in out and "[FAIL] tenant isolation" in out
    assert "1/2 checks passed (50%)" in out


def test_compose_verifier_final_reports_independently() -> None:
    from graphsleuth.agents import compose_verifier_final

    history = [
        {"tool": "find_shared_links", "arguments": {}, "result": {"links": [{"account": "ACC-9002", "shared_devices": ["DEV-9001"], "shared_beneficiaries": []}]}},
        {"tool": "detect_suspicious_cluster", "arguments": {}, "result": {"clusters": [{"size": 6, "cluster_score": 41.05, "members": [{"account": "ACC-9003"}]}]}},
        {"tool": "list_alerts", "arguments": {}, "result": {"alerts": [{"alert": "AL-9001"}, {"alert": "AL-9002"}]}},
        {"tool": "shortest_connection", "arguments": {}, "result": {"path": [{"node": "ACC-EXT-01"}, {"node": "ACC-9001"}, {"node": "ACC-9003"}]}},
        {"tool": "add_case_note", "arguments": {}, "result": {"note_id": "NOTE-1", "case_id": "CASE-0001"}},
        {"tool": "remember", "arguments": {}, "result": {"note_id": "MEM-1", "linked_to": []}},
    ]
    text = compose_verifier_final("Verify ACC-9003 against case CASE-0001: re-check", history)
    assert "**Verification** — CONFIRMED" in text
    assert "ACC-9003" in text
    assert "**Re-checked**" in text and "**Discrepancies**" in text
    assert "NOTE-1" in text and "MEM-1" in text


def test_mcp_exposes_every_tool_with_matching_parameters() -> None:
    """Track 01: "tool selection through MCP". The MCP toolbelt must be the same
    one the built-in agent uses — same names, same parameters. The two can drift
    silently (shortest_connection was missing `via` over MCP until this test),
    which would make an MCP client unable to reproduce the agent's money-only
    path.
    """
    import importlib
    import inspect

    try:
        mcp_server = importlib.import_module("graphsleuth.mcp_server")
    except (ImportError, SystemExit) as exc:  # MCP package is optional here
        print(f"(skipping MCP parity: {exc})")
        return

    specs = {
        spec["function"]["name"]: set(spec["function"]["parameters"]["properties"])
        for spec in tools.TOOL_SPECS
    }
    assert len(specs) == 20
    for name, spec_params in specs.items():
        exported = getattr(mcp_server, name, None)
        assert callable(exported), f"{name} is not exposed over MCP"
        mcp_params = set(inspect.signature(exported).parameters)
        assert mcp_params == spec_params, (
            f"{name}: MCP parameters {sorted(mcp_params)} != agent toolbelt {sorted(spec_params)}"
        )


def test_company_brain_dataset_is_internally_consistent() -> None:
    """Track 03: the company graph is data-correct before it ever touches FalkorDB.

    Every edge endpoint must resolve to a declared node, every decision must be
    traceable to at least one doc/ticket/chat source, and the dataset must contain
    the shape the tour relies on (a deep dependency chain and a superseded decision).
    """
    from graphsleuth import company

    data = company.build()
    ids = set(data["nodes"])

    # every edge endpoint exists, and no self-edges sneak in
    for rel, src, dst in data["edges"]:
        assert src in ids, f"{rel} source {src} is not a declared node"
        assert dst in ids, f"{rel} target {dst} is not a declared node"
        assert src != dst, f"{rel} self-edge on {src}"

    # every decision cites its sources (the "trace decisions to their source" requirement)
    sourced = {src for rel, src, _ in data["edges"] if rel == "SOURCED_FROM"}
    for decision in data["decisions"]:
        assert decision["id"] in sourced, f"{decision['id']} has no SOURCED_FROM"

    # docs, tickets and chats all attach to systems (docs/tickets/chats in one graph)
    for rel, kind in (("ABOUT", "Doc"), ("ABOUT", "Ticket"), ("MENTIONS", "Chat")):
        assert any(
            rel == r and data["nodes"][s]["label"] == kind for r, s, _ in data["edges"]
        ), f"no {kind} linked via {rel}"

    # a dependency chain exists deep enough for a transitive blast radius
    depends = {}
    for rel, src, dst in data["edges"]:
        if rel == "DEPENDS_ON":
            depends.setdefault(src, set()).add(dst)

    def depth(node: str, seen: frozenset[str]) -> int:
        children = depends.get(node, set())
        if not children or node in seen:
            return 0
        return 1 + max(depth(child, seen | {node}) for child in children)

    assert max(depth(s["id"], frozenset()) for s in data["systems"]) >= 2

    # and a decision that was later reversed (knowledge kept current)
    assert any(rel == "SUPERSEDES" for rel, _, _ in data["edges"])


def test_company_build_is_deterministic() -> None:
    from graphsleuth import company

    assert company.build() == company.build()
    # the brief's front-page query shape is preserved verbatim
    assert "(p:Person)-[:WORKED_ON]->(pr:Project)-[:USES]->(s:System" in company.WHO_KNOWS_QUERY
    assert "count(pr)" in company.WHO_KNOWS_QUERY  # not count(*): see the quirk note


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
