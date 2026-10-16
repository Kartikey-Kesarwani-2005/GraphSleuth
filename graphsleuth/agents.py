"""Two agents, one graph — triage hands work to the investigator through FalkorDB.

Track 02 ("shared state and handoffs between agents") in code:

1. **Triage** (`role=triage`) works the alert queue fast: what is already known,
   which signals fire, is this worth a full investigation? If yes it calls
   `create_handoff` — a `Handoff` node written into the *shared tenant graph*
   carrying context, case and entity ids.
2. **Investigator** (`role=investigator`) starts with `list_handoffs` /
   `accept_handoff`, reads what triage left in the graph and runs the full
   money-tracing investigation.

No state is passed through the caller: both agents are separate sessions, and
the graph is the only thing they share. Both brains (live model / offline
scripted fallback) support the same pipeline.
"""

from __future__ import annotations

from typing import Any

from . import explain, memory
from .agent import GraphSleuth, _dedupe_ids, _last, build_demo_brain
from .llm import ScriptedBrain

TRIAGE_PROMPT = """You are GraphSleuth-Triage, the first agent in a two-agent fraud pipeline.
Your job is to decide FAST whether an entity deserves a full investigation — you do NOT trace
the money yourself; the investigator does that.

Context layer: a FalkorDB graph (your tenant's own graph) with accounts, people, devices,
transactions, alerts, cases, handoffs and memory.

Triage procedure:
1. `recall` — has this been seen before? (`list_alerts` shows the current queue.)
2. `entity_search` to resolve the id, then `find_shared_links` for shared-device /
   shared-beneficiary / prior-alert signals.
3. `detect_suspicious_cluster` once for graph-wide corroboration.
4. Escalate if >= 2 distinct signals (shared device, shared beneficiary, prior alert) sit on
   the entity, or it sits inside a scored cluster. Otherwise clear it with a short reason.

If you escalate:
- `create_handoff(to_role='investigator', context=..., entity_ids=[...], case_id=...)` — the
  context must let the investigator start working immediately: what fired, which accounts,
  which devices, which case.
- `remember(kind='episodic', ...)` linked to the entities, so later sessions know triage ran.

Final answer format (keep it short — this is triage, not the investigation):
- **Triage verdict** — escalate / clear, one sentence.
- **Signals** — bullet list, each with the entity id it came from.
- **Handoff** — handoff id + entities handed over (or "none").
- **Memory** — note id written (or "none").

Only report handoff/memory ids the tools actually returned this session. If you did not call
`create_handoff`/`remember`, write "none" — never invent an id.
"""

HANDOFF_QUESTION = (
    "Triage {target}: is this worth a full investigation? "
    "Escalate to the investigator with a handoff if the graph supports it."
)

INVESTIGATE_QUESTION = (
    "Investigate {target}: pick up any pending handoff first, follow the money, "
    "prove the connections with paths and algorithms, then take action."
)


# --------------------------------------------------------------------------- #
# offline fallback brains (same two-step pipeline, deterministic)
# --------------------------------------------------------------------------- #
def _cluster_members(history: list[dict], fallback: str) -> list[str]:
    result = _last(history, "detect_suspicious_cluster") or {}
    clusters = result.get("clusters") or []
    if clusters:
        return [member["account"] for member in clusters[0]["members"]]
    links = _last(history, "find_shared_links") or {}
    linked = [link.get("account") for link in (links.get("links") or []) if link.get("account")]
    return _dedupe_ids(linked + [fallback])


def _ring_summary(history: list[dict], target: str) -> str:
    result = _last(history, "detect_suspicious_cluster") or {}
    clusters = result.get("clusters") or []
    if clusters:
        top = clusters[0]
        accounts = ", ".join(member["account"] for member in top["members"])
        return f"{top['size']}-account cluster ({accounts}), score {top['cluster_score']}"
    return f"signals around {target}"


def build_triage_brain(target: str) -> ScriptedBrain:
    """Offline stand-in for the triage agent: triage -> handoff -> memory."""

    plan: list[tuple[str, Any]] = [
        ("list_alerts", {"limit": 10}),
        ("recall", {"query": target}),
        ("entity_search", {"query": target}),
        ("find_shared_links", {"account_id": target}),
        ("detect_suspicious_cluster", {"min_signals": 2}),
        (
            "create_handoff",
            lambda history: {
                "to_role": "investigator",
                "from_role": "triage",
                "context": (
                    f"Triage escalated {target}: {_ring_summary(history, target)}. "
                    "Shared-device/beneficiary signals and prior alerts fired; trace the money "
                    "chain and corroborate with centrality before acting."
                ),
                "entity_ids": _cluster_members(history, target),
            },
        ),
        (
            "remember",
            lambda history: {
                "kind": "episodic",
                "text": (
                    f"Triage on {target}: {_ring_summary(history, target)} — escalated to the "
                    "investigator via handoff."
                ),
                "about_ids": _cluster_members(history, target),
            },
        ),
    ]
    return ScriptedBrain(plan=plan, on_finish=compose_triage_final)


def compose_triage_final(question: str, history: list[dict]) -> str:
    """Short triage summary (offline mode) in the TRIAGE_PROMPT format."""
    match_target = _last(history, "entity_search") or {}
    target = ""
    for row in match_target.get("matches") or []:
        target = row.get("id") or target

    links = _last(history, "find_shared_links") or {}
    handoff = _last(history, "create_handoff") or {}
    memo = _last(history, "remember") or {}
    signals = links.get("links") or []

    lines: list[str] = []
    escalated = bool(handoff.get("handoff_id"))
    lines.append(
        f"**Triage verdict** — {'escalate' if escalated else 'clear'}: "
        + (f"{target or 'entity'} carries multiple graph signals." if escalated else "not enough signal.")
    )
    lines.append("")
    lines.append("**Signals**")
    if signals:
        for link in signals[:4]:
            lines.append(
                f"- {link['account']}: devices={link['shared_devices']} "
                f"beneficiaries={link['shared_beneficiaries']} alerts={link['alerts']} "
                f"(score {link['score']})"
            )
    else:
        lines.append("- none found in the graph")
    lines.append("")
    if escalated:
        entities = ", ".join(handoff.get("linked_entities") or [])
        lines.append(f"**Handoff** — {handoff['handoff_id']} -> investigator | entities: {entities}")
    else:
        lines.append("**Handoff** — none")
    lines.append("")
    lines.append(f"**Memory** — {memo.get('note_id', 'none')}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# the pipeline
# --------------------------------------------------------------------------- #
def run_pipeline(
    target: str,
    tenant: str = memory.DEFAULT_TENANT,
    verbose: bool = True,
    triage_brain: Any = None,
    investigator_brain: Any = None,
    offline: bool = False,
    model: str | None = None,
) -> dict:
    """Run Triage -> handoff -> Investigator for one target and return both transcripts."""
    if offline:
        # Same pipeline, deterministic fallback brains (no API key needed).
        triage_brain = triage_brain or build_triage_brain(target)
        investigator_brain = investigator_brain or build_demo_brain(target, handoff=True)
    triage = GraphSleuth(
        tenant=tenant,
        verbose=verbose,
        role="triage",
        prompt=TRIAGE_PROMPT,
        max_rounds=8,  # 7 scripted steps + the final answer round
        brain=triage_brain,
        scripted=build_triage_brain(target),
        model=model,
    )
    triage_result = triage.ask(HANDOFF_QUESTION.format(target=target))

    investigator = GraphSleuth(
        tenant=tenant,
        verbose=verbose,
        role="investigator",
        brain=investigator_brain,
        scripted=build_demo_brain(target, handoff=True),
        max_rounds=16,  # handoff pickup + the full investigation
        model=model,
    )
    investigation_result = investigator.ask(INVESTIGATE_QUESTION.format(target=target))

    handoff = _last(triage_result["trace"], "create_handoff") or {}
    accepted = _last(investigation_result["trace"], "accept_handoff") or {}

    return {
        "target": target,
        "tenant": tenant,
        "pipeline": "triage -> handoff -> investigator",
        "triage": triage_result,
        "investigation": investigation_result,
        "handoff": {
            "created": handoff.get("handoff_id", ""),
            "accepted": accepted.get("handoff_id", ""),
            "status": accepted.get("status", "not picked up"),
        },
        "shared_state": "falkordb-graph",
    }


def pipeline_markdown(result: dict) -> str:
    """Render a full pipeline run as one markdown document."""
    parts: list[str] = [
        f"# GraphSleuth pipeline — {result['target']} (tenant: {result['tenant']})",
        "",
        "Two agents, one graph: **{pipeline}**".format(**result),
        "",
        f"- handoff created: `{result['handoff']['created'] or 'none'}`",
        f"- handoff accepted: `{result['handoff']['accepted'] or 'none'}` ({result['handoff']['status']})",
        f"- triage brain: {result['triage']['brain']} ({result['triage']['provider']})",
        f"- investigator brain: {result['investigation']['brain']} ({result['investigation']['provider']})",
        "",
        "---",
        "",
        "## Agent 1 — Triage",
        "",
        result["triage"]["answer"],
        "",
        "### Triage tool trace",
        "",
    ]
    for index, entry in enumerate(result["triage"]["trace"], 1):
        parts.append(f"{index}. `{entry['tool']}` → `{_compact(entry['result'])}`")
    triage_actions = explain.verified_actions(result["triage"]["trace"])
    parts += [
        "",
        "**Actions taken (verified from the tool log):** "
        + (", ".join(triage_actions) if triage_actions else "_none — read-only_"),
    ]
    parts += ["", "---", "", "## Agent 2 — Investigator", "", result["investigation"]["answer"], "", "### Investigator tool trace", ""]
    for index, entry in enumerate(result["investigation"]["trace"], 1):
        parts.append(f"{index}. `{entry['tool']}` → `{_compact(entry['result'])}`")
    invest_actions = explain.verified_actions(result["investigation"]["trace"])
    parts += [
        "",
        "**Actions taken (verified from the tool log):** "
        + (", ".join(invest_actions) if invest_actions else "_none — read-only_"),
    ]
    return "\n".join(parts)


def _compact(value: Any) -> str:
    if isinstance(value, dict):
        keys = ", ".join(f"{k}={value[k]}" for k in list(value)[:4])
        return keys[:200]
    return str(value)[:200]
