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
    assert len(tools.TOOL_SPECS) == 24  # the fraud-investigation toolbelt
    assert len(tools.COMPANY_TOOL_SPECS) == 6  # Track 03 company-brain tools
    assert len(tools.ALL_TOOL_SPECS) == 30  # everything the agents/MCP can call
    for spec in tools.ALL_TOOL_SPECS:
        function = spec["function"]
        assert spec["type"] == "function"
        assert function["name"] in tools.HANDLERS
        assert function["parameters"]["type"] == "object"
        # every declared tool must have a handler and vice versa
    assert set(tools.HANDLERS) == {s["function"]["name"] for s in tools.ALL_TOOL_SPECS}


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


def test_counterfactual_tool_is_registered_and_read_only() -> None:
    """Explainability beyond "here is the path": the agent can *prove* its verdict does not
    hinge on one link by mentally removing a device/account and re-running the analysis.

    The graph must never be mutated by it (it is a mental removal), and it must be part of
    the agent toolbelt and the MCP toolbelt like every other tool."""
    import inspect

    assert "what_if_removed" in tools.HANDLERS
    assert "what_if_removed" in {s["function"]["name"] for s in tools.ALL_TOOL_SPECS}
    assert "what_if_removed" not in tools.MUTATING_TOOLS  # read-only counterfactual

    spec = next(s for s in tools.ALL_TOOL_SPECS if s["function"]["name"] == "what_if_removed")
    params = set(spec["function"]["parameters"]["properties"])
    sig = set(inspect.signature(tools.what_if_removed).parameters)
    assert sig == params == {"entity_id", "from_id", "to_id", "max_depth"}

    # the shared BFS/cluster cores it reuses must exist (same logic, one implementation)
    assert callable(tools._bfs_path) and callable(tools._detect_cluster)

    # missing/unknown ids degrade gracefully instead of raising
    # (no database needed: these return before touching the graph only when the query
    #  finds nothing, so here we only assert the guard for an empty id)
    result = tools.what_if_removed("")
    assert "error" in result


def test_ring_resilience_tool_is_registered_and_read_only() -> None:
    """The batch counterfactual (whole-ring stress test) must be part of the toolbelt,
    read-only, and its MCP/handler/spec signatures must stay in sync — it is what turns
    single-node explainability into a full resilience verdict (articulation points)."""
    import inspect

    assert "assess_ring_resilience" in tools.HANDLERS
    assert "assess_ring_resilience" in {s["function"]["name"] for s in tools.ALL_TOOL_SPECS}
    assert "assess_ring_resilience" not in tools.MUTATING_TOOLS

    spec = next(
        s for s in tools.ALL_TOOL_SPECS if s["function"]["name"] == "assess_ring_resilience"
    )
    params = set(spec["function"]["parameters"]["properties"])
    sig = set(inspect.signature(tools.assess_ring_resilience).parameters)
    assert sig == params

    try:
        mcp_server = __import__("importlib").import_module("graphsleuth.mcp_server")
    except (ImportError, SystemExit):
        return
    mcp_params = set(
        inspect.signature(getattr(mcp_server, "assess_ring_resilience")).parameters
    )
    assert mcp_params == params  # MCP clients see the exact same tool


def test_timeline_and_pattern_tools_are_registered_and_read_only() -> None:
    """Two questions judges always ask: *when* did this happen, and *is it happening
    elsewhere*? `ring_timeline` answers the first from the graph's own timestamps and
    `find_similar_rings` the second via structural fingerprints. Both must be in every
    toolbelt (handler + spec + MCP), read-only, and signature-exact."""
    import inspect

    for name in ("ring_timeline", "find_similar_rings"):
        assert name in tools.HANDLERS
        assert name in {s["function"]["name"] for s in tools.ALL_TOOL_SPECS}
        assert name not in tools.MUTATING_TOOLS

        spec = next(s for s in tools.ALL_TOOL_SPECS if s["function"]["name"] == name)
        params = set(spec["function"]["parameters"]["properties"])
        sig = set(inspect.signature(getattr(tools, name)).parameters)
        assert sig == params, f"{name}: signature/spec drift"

    # pure helpers stay importable and side-effect free
    assert tools._iso_minutes("2026-10-12T11:14:00+00:00") is not None
    assert tools._iso_minutes("not-a-date") is None
    similarity, reasons = tools._profile_similarity(
        {"size": 6, "devices": ["D1", "D2"], "collectors": 1, "alerts": 3, "mean_risk": 0.5, "score": 40, "members": []},
        {"size": 6, "devices": ["D1"], "collectors": 1, "alerts": 3, "mean_risk": 0.5, "score": 12, "members": []},
    )
    assert 0 <= similarity <= 100 and similarity >= 60 and reasons


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


def test_verified_actions_ignore_failed_calls() -> None:
    """A write that errored is not an action the agent took.

    The report states "Actions taken (verified from the tool log)", so a rejected
    call (read-only guard, bad case id, missing entity) must not be listed there as
    if it had happened.
    """
    trace = [
        {"tool": "entity_search", "arguments": {"query": "ACC-9003"}, "result": {"matches": []}},
        {"tool": "flag_account", "arguments": {"account_id": "ACC-9003"}, "result": {"flagged": True}},
        {"tool": "open_case", "arguments": {"title": "x"}, "result": {"error": "no such entity"}},
        {"tool": "remember", "arguments": {"text": "y"}, "result": {"error": "cypher write blocked"}},
    ]
    actions = explain.verified_actions(trace)
    assert len(actions) == 1
    assert "flag_account" in actions[0]


def test_confidence_downgraded_without_counterfactual() -> None:
    """A live model's "high" claim is corrected when no counterfactual ran.

    The prompt asks for `what_if_removed` / `assess_ring_resilience` before claiming
    high confidence; the answer is checked against the trace so the rule cannot be
    ignored silently.
    """
    from graphsleuth.agent import _downgrade_confidence

    history = [{"tool": "entity_search", "arguments": {}, "result": {"matches": [{"id": "ACC-9003"}]}}]
    answer = "**Verdict** — escalated.\n\n**Confidence** — high. Nothing else is missing."
    corrected = _downgrade_confidence(answer, history)
    assert corrected is not None
    assert "medium (downgraded" in corrected
    assert "**Confidence** — high" not in corrected
    # A trace that actually ran the stress test keeps the claim, and so does an
    # answer that never claimed "high".
    with_counterfactual = history + [
        {"tool": "what_if_removed", "arguments": {}, "result": {"robustness": 100}}
    ]
    assert _downgrade_confidence(answer, with_counterfactual) is None
    assert _downgrade_confidence("**Confidence** — low: thin evidence.", history) is None
    assert _downgrade_confidence("**Confidence** — medium, not high: no cf.", history) is None


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

    saved_model = config.groq_model
    saved_key = config.groq_api_key
    saved_env = os.environ.get("LLM_MODELS")
    # Force the groq path so the configured primary is deterministic: without a
    # key (e.g. CI, no .env) `llm_model` would fall back to the openai default.
    config.groq_api_key = "test-key"
    config.groq_model = "primary-model"
    os.environ["LLM_MODELS"] = "primary-model, second ,third,second"  # dupes + spaces
    try:
        assert config.llm_models == ["primary-model", "second", "third"]
    finally:
        config.groq_model = saved_model
        config.groq_api_key = saved_key
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
        for spec in tools.ALL_TOOL_SPECS
    }
    assert len(specs) == len(tools.ALL_TOOL_SPECS)  # MCP mirrors the full toolbelt (27)
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


def test_company_tools_are_registered_and_mcp_exposed() -> None:
    """Track 03 must be agent-callable, not only a scripted tour: the six company
    questions are real tools in the shared registry, and an MCP client can call them."""
    names = {"who_knows", "owners_of", "blast_radius", "decision_trail", "knowledge_sources", "stale_knowledge"}
    assert names <= set(tools.HANDLERS)
    assert names == {s["function"]["name"] for s in tools.COMPANY_TOOL_SPECS}
    assert names.isdisjoint({s["function"]["name"] for s in tools.TOOL_SPECS})
    # the company tools are read-only, so they must never be counted as actions
    assert names.isdisjoint(tools.MUTATING_TOOLS)

    import importlib

    try:
        mcp_server = importlib.import_module("graphsleuth.mcp_server")
    except (ImportError, SystemExit) as exc:
        print(f"(skipping company MCP check: {exc})")
        return
    for name in names:
        assert callable(getattr(mcp_server, name, None)), f"{name} not exposed over MCP"


def test_company_agent_offline_brain_answers_with_citations() -> None:
    """The offline company brain must pick the right system/decision from free text
    and compose an Answer/Evidence/Sources report from the tool trail."""
    from graphsleuth import company

    system, decision = company._focus("who knows SYS-payments-api and why ADR-005")
    assert system == "SYS-payments-api"
    assert decision == "ADR-005"
    # defaults when the question names nothing
    assert company._focus("what is going stale?") == ("SYS-ledger-db", "ADR-002")

    brain = company.build_company_brain("who knows SYS-ledger-db")
    tools_called = [name for name, _ in brain._queue]
    assert tools_called[:2] == ["recall", "who_knows"]
    assert {"owners_of", "blast_radius", "decision_trail", "knowledge_sources", "stale_knowledge"} <= set(
        tools_called
    )

    # a synthetic trail composes the required sections with node ids
    history = [
        {"tool": "who_knows", "arguments": {}, "result": {
            "system": "SYS-ledger-db",
            "people": [{"person": "EMP-001", "name": "Aarav Sharma", "projects": 2, "declared_expert": True}],
            "declared_experts": [{"person": "EMP-001", "name": "Aarav Sharma"}],
        }},
        {"tool": "decision_trail", "arguments": {}, "result": {
            "found": True, "decision": "ADR-002", "title": "Migrate the ledger to schema v9",
            "status": "accepted", "decided_by": [{"person": "EMP-001", "name": "Aarav Sharma"}],
            "affects": [], "sources": [{"id": "DOC-002", "kind": "Doc", "summary": "Ledger schema reference"}],
            "supersedes": [], "superseded_by": [],
        }},
    ]
    report = company.compose_company_final("who knows SYS-ledger-db", history)
    for section in ("**Answer**", "**Evidence**", "**Sources**", "**Confidence**"):
        assert section in report
    assert "EMP-001" in report
    assert "DOC-002" in report


def test_company_result_produces_graph_facts() -> None:
    """The console grows the org graph from company tool results, so the explainability
    layer must turn them into nodes/edges (Track 03 in the UI, not only in the CLI)."""
    from graphsleuth import explain

    trace = [
        {"tool": "who_knows", "arguments": {}, "result": {
            "system": "SYS-ledger-db",
            "people": [{"person": "EMP-001", "name": "Aarav Sharma", "projects": 2, "declared_expert": True}],
            "declared_experts": [{"person": "EMP-001", "name": "Aarav Sharma"}],
        }},
        {"tool": "blast_radius", "arguments": {}, "result": {
            "system": "SYS-ledger-db",
            "affected_systems": [{"id": "SYS-payments-api", "name": "Payments API", "criticality": "critical"}],
            "projects_at_risk": [], "teams_at_risk": [],
        }},
        {"tool": "decision_trail", "arguments": {}, "result": {
            "found": True, "decision": "ADR-002", "title": "x", "status": "accepted",
            "decided_by": [{"person": "EMP-001", "name": "Aarav Sharma"}],
            "affects": [{"system": "SYS-ledger-db", "name": "Ledger DB"}],
            "sources": [{"id": "DOC-002", "kind": "Doc", "summary": "Ledger schema"}],
            "supersedes": [], "superseded_by": [],
        }},
        {"tool": "knowledge_sources", "arguments": {}, "result": {
            "system": "SYS-ledger-db",
            "docs": [{"id": "DOC-002", "title": "Ledger", "updated_at": "", "author": "Aarav"}],
            "tickets": [], "chats": [],
        }},
    ]
    nodes, edges = explain.collect_graph_facts(trace)
    assert {"EMP-001", "SYS-ledger-db", "SYS-payments-api", "ADR-002", "DOC-002"} <= set(nodes)
    assert ("EMP-001", "SYS-ledger-db", "WORKED_ON") in edges
    assert ("SYS-payments-api", "SYS-ledger-db", "DEPENDS_ON") in edges
    assert ("ADR-002", "DOC-002", "SOURCED_FROM") in edges


def test_web_console_offers_company_mode() -> None:
    """The console must expose the org brain (Track 03) as a first-class mode, and the
    server must route `mode: company` to the company agent."""
    from graphsleuth import server

    html = (server.WEB_DIR / "index.html").read_text(encoding="utf-8")
    js = (server.WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert 'value="company"' in html  # the mode selector exists
    assert 'id="question"' in html  # free-text org question box ships with it
    assert "mode" in js and "who_knows" in js  # org tool results grow the canvas
    assert "question" in js  # the console sends the company question
    import inspect

    src = inspect.getsource(server.Handler._sse_stream)
    assert 'mode == "company"' in src  # the server actually branches on it
    post = inspect.getsource(server.Handler.do_POST)
    assert 'body.get("question")' in post  # the server honours a free-text org question


def test_console_click_to_stress_test_is_wired() -> None:
    """The judge-facing differentiator: click any node in the console and the server runs the
    read-only counterfactual (`what_if_removed`) live, ghosting the removed node on the canvas
    and printing robustness/impact. The route, the helper and the front-end must all agree."""
    import inspect

    from graphsleuth import server

    src = inspect.getsource(server)
    assert '"/api/stress"' in src          # the route exists
    assert "def _stress" in src            # and its handler
    assert "what_if_removed" in inspect.getsource(server._stress)  # it reuses the real tool

    html = (server.WEB_DIR / "index.html").read_text(encoding="utf-8")
    js = (server.WEB_DIR / "app.js").read_text(encoding="utf-8")
    css = (server.WEB_DIR / "app.css").read_text(encoding="utf-8")
    assert 'id="stress"' in html and 'id="stressrestore"' in html
    assert "/api/stress" in js and "runStressTest" in js
    assert "stress.entity" in js           # the canvas ghosts the removed node
    assert ".stress-critical" in css       # impact badge styling ships too


def test_proof_pack_export_is_wired() -> None:
    """The judge-facing artifact: after a run the console must offer *Copy verdict*,
    *Download report (.md)* and *Download proof pack (.html)* — a standalone HTML with the
    verdict, evidence trail, canvas snapshot and reproducibility commands. No server round
    trip, no CDN: it must work offline in the browser."""
    from graphsleuth import server

    html = (server.WEB_DIR / "index.html").read_text(encoding="utf-8")
    js = (server.WEB_DIR / "app.js").read_text(encoding="utf-8")
    css = (server.WEB_DIR / "app.css").read_text(encoding="utf-8")

    for element in ('id="exports"', 'id="copyverdict"', 'id="downloadmd"', 'id="downloadproof"'):
        assert element in html

    for marker in ("buildProofPack", "buildMarkdown", "downloadFile", "toDataURL"):
        assert marker in js
    assert "Reproduce" in js and "python -m graphsleuth eval" in js  # provenance is embedded
    assert "canvas.toDataURL" in js  # the graph snapshot travels with the pack
    assert ".exports" in css  # and it is styled like the rest of the console


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
