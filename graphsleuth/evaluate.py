"""Reproducible evaluation scorecard for GraphSleuth.

    python -m graphsleuth eval [--tenant default] [--save evals/scorecard.json]

A judged demo should not be "trust me, it works". This runs a fixed battery of
checks against the *live* FalkorDB graph and the agent's own code, and prints a
pass/fail table with the evidence for each line. The seed graph is deterministic
(`random.Random(42)`), so the scores are reproducible run to run.

Checks span the whole brief: ring detection and precision, money-path tracing,
native graph algorithms, episodic/semantic/procedural memory round-trips, tenant
isolation, the explainability layer (verified actions) and the read-only guard.
The isolation and memory checks run in a throwaway tenant (`eval-harness`) that
is dropped afterwards, so `eval` never leaves residue in your real graphs.
"""

from __future__ import annotations

from typing import Callable

from . import db, explain, memory
from .tools import HANDLERS, run_cypher

HARNESS_TENANT = "eval-harness"

EXPECTED_RING = {"ACC-9001", "ACC-9002", "ACC-9003", "ACC-9004", "ACC-9005", "ACC-9100"}

Check = tuple[str, Callable[[str], tuple[bool, str]]]


# --------------------------------------------------------------------------- #
# checks: each returns (passed, evidence)
# --------------------------------------------------------------------------- #
def check_graph_present(tenant: str) -> tuple[bool, str]:
    info = db.ping()
    ok = info["nodes"] > 500 and info["relationships"] > 500
    return ok, f"{info['nodes']} nodes / {info['relationships']} relationships"


def check_ring_detected(tenant: str) -> tuple[bool, str]:
    result = HANDLERS["detect_suspicious_cluster"](min_signals=2)
    clusters = result.get("clusters") or []
    if not clusters:
        return False, "no cluster detected"
    top = clusters[0]
    members = {m["account"] for m in top["members"]}
    hit = EXPECTED_RING & members
    ok = len(hit) >= 5 and float(top.get("cluster_score") or 0) >= 30
    return ok, (
        f"top cluster {top.get('size')} members, score {top.get('cluster_score')}, "
        f"expected {len(hit)}/6 present"
    )


def check_noise_filtered(tenant: str) -> tuple[bool, str]:
    """Precision: the flagged set must be a small slice of ~200 ordinary accounts."""
    rows = db.query("MATCH (a:Account) RETURN count(a) AS row")
    total = int(rows[0]) if rows else 0
    result = HANDLERS["detect_suspicious_cluster"](min_signals=2, top=50)
    flagged = sum(c.get("size", 0) for c in (result.get("clusters") or []))
    ok = total > 100 and flagged <= max(6, total // 8)
    return ok, f"{flagged} accounts flagged out of {total} ({100 * flagged // max(1, total)}%)"


def check_money_path(tenant: str) -> tuple[bool, str]:
    result = HANDLERS["shortest_connection"](
        id_a="ACC-EXT-01", id_b="ACC-9100", max_depth=8, via=["ORIGINATED", "DESTINED"]
    )
    path = result.get("path") or []
    if len(path) < 2:
        return False, "no path found"
    endpoints_ok = path[0]["node"] == "ACC-EXT-01" and path[-1]["node"] == "ACC-9100"
    money_only = all(str(step.get("via")) in {"ORIGINATED", "DESTINED"} for step in path[1:])
    chain = " -> ".join(str(step["node"]) for step in path)
    return endpoints_ok and money_only, f"{len(path) - 1} hops: {chain}"


def check_shared_device_signal(tenant: str) -> tuple[bool, str]:
    result = HANDLERS["find_shared_links"](account_id="ACC-9003")
    links = result.get("links") or []
    with_device = [link for link in links if link.get("shared_devices")]
    ok = len(with_device) >= 2
    return ok, f"{len(links)} linked accounts, {len(with_device)} share a device"


def check_native_algorithms(tenant: str) -> tuple[bool, str]:
    components = HANDLERS["connected_components"](top=5, min_size=2)
    centrality = HANDLERS["centrality"](kind="betweenness", top=3)
    ok = (components.get("components_found", 0) >= 1) and (centrality.get("nodes_ranked", 0) > 0)
    return ok, (
        f"{components.get('components_found')} components, "
        f"{centrality.get('nodes_ranked')} nodes ranked (betweenness)"
    )


def check_memory_roundtrip(tenant: str) -> tuple[bool, str]:
    db.set_tenant(HARNESS_TENANT)
    # a minimal entity so the ABOUT link is exercised too (harness starts empty)
    db.query("MERGE (a:Account {id: 'ACC-9003'}) RETURN a.id AS row", tenant=HARNESS_TENANT)
    note = memory.remember("semantic", "EVAL round-trip canary 99887766", about_ids=["ACC-9003"], tenant=HARNESS_TENANT)
    hits = memory.recall("99887766", "", HARNESS_TENANT)
    ok = any(hit["note_id"] == note["note_id"] for hit in hits) and bool(note["linked_to"])
    return ok, f"wrote {note['note_id']}, recalled {len(hits)}, linked {note['linked_to']}"


def check_tenant_isolation(tenant: str) -> tuple[bool, str]:
    db.set_tenant(HARNESS_TENANT)
    note = memory.remember("semantic", "EVAL isolation canary ISOCANARY42", tenant=HARNESS_TENANT)
    leaked = [hit for hit in memory.recall("ISOCANARY42", "", tenant) if hit["note_id"] == note["note_id"]]
    ok = not leaked
    return ok, f"canary in '{HARNESS_TENANT}' visible in '{tenant}': {len(leaked)}"


def check_procedural_learning(tenant: str) -> tuple[bool, str]:
    trace = [
        {"tool": name, "arguments": {}, "result": {}}
        for name in ["recall", "entity_search", "graph_neighbors", "trace_money_flow", "find_shared_links"]
    ]
    playbook = memory.distill_playbook(trace, "synthetic")
    ok = bool(playbook) and len(playbook["steps"]) == 5
    return ok, playbook["signature"] if playbook else "no playbook distilled"


def check_verified_actions(tenant: str) -> tuple[bool, str]:
    trace = [
        {"tool": "recall", "arguments": {}, "result": {}},
        {"tool": "flag_account", "arguments": {"account_id": "ACC-9003"}, "result": {}},
    ]
    actions = explain.verified_actions(trace)
    ok = len(actions) == 1 and "flag_account" in actions[0]
    return ok, f"read-only ignored, actions={actions}"


def check_readonly_guard(tenant: str) -> tuple[bool, str]:
    result = run_cypher("MATCH (n) DETACH DELETE n")
    ok = "error" in result
    return ok, (result.get("error") or "write was NOT blocked")[:90]


CHECKS: list[Check] = [
    ("graph is seeded", check_graph_present),
    ("mule ring detected", check_ring_detected),
    ("noise filtered (precision)", check_noise_filtered),
    ("money path traced", check_money_path),
    ("shared-device signal fires", check_shared_device_signal),
    ("native graph algorithms", check_native_algorithms),
    ("memory round-trip", check_memory_roundtrip),
    ("tenant isolation", check_tenant_isolation),
    ("procedural learning", check_procedural_learning),
    ("verified actions (no over-claim)", check_verified_actions),
    ("read-only Cypher guard", check_readonly_guard),
]


def run(tenant: str = memory.DEFAULT_TENANT) -> dict:
    db.set_tenant(tenant)
    results = []
    for name, check in CHECKS:
        try:
            passed, detail = check(tenant)
        except Exception as exc:  # noqa: BLE001 - a broken check is a failed check, not a crash
            passed, detail = False, f"{type(exc).__name__}: {exc}"
        results.append({"check": name, "passed": bool(passed), "detail": detail})
    try:
        db.drop_tenant(HARNESS_TENANT)
    except Exception:  # noqa: BLE001
        pass
    passed_count = sum(1 for r in results if r["passed"])
    return {
        "tenant": tenant,
        "checks": results,
        "passed": passed_count,
        "total": len(results),
        "score": round(100 * passed_count / len(results)) if results else 0,
    }


def render(report: dict) -> str:
    width = max(len(r["check"]) for r in report["checks"]) + 2
    lines = [
        f"GraphSleuth evaluation — tenant '{report['tenant']}'",
        "=" * 78,
    ]
    for result in report["checks"]:
        mark = "PASS" if result["passed"] else "FAIL"
        lines.append(f"  [{mark}] {result['check']:<{width}} {result['detail']}")
    lines.append("=" * 78)
    lines.append(f"  score: {report['passed']}/{report['total']} checks passed ({report['score']}%)")
    return "\n".join(lines)
