"""GraphSleuth — the investigator.

The agent loop is deliberately small and transparent:

    question -> memory briefing -> LLM chooses a tool -> graph runs it
             -> result goes back into the LLM's context -> ... -> verdict + actions

Two brains can drive the loop (see `llm.py`): a live OpenAI-compatible model, or
a scripted brain that performs the same investigation offline. Either way the
*evidence trail* is recorded, and the agent writes its findings back into the
graph as memory, alerts and case notes.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import memory, tools
from .config import config
from .llm import LLMResponse, ScriptedBrain, get_brain
from .tools import TOOL_SPECS, execute

SYSTEM_PROMPT = """You are GraphSleuth, a fraud investigation agent. Your context layer is a
FalkorDB graph that contains accounts, people, devices, merchants, transactions, alerts,
cases, and your own long-term memory. You reason by following relationships, never by guessing.

Working rules:
1. Evidence only. Every claim in your final answer must come from a tool result. If the graph
   does not show it, say so instead of inventing it.
2. Start with memory. Call `recall` before investigating — a previous session may already know
   something about this entity or beneficiary.
3. Resolve ids with `entity_search`, then expand with `graph_neighbors` (depth 1-3).
4. For suspicious-transaction questions: `trace_money_flow` (where did the money go?), then
   `find_shared_links` (who else is on this device / beneficiary?).
5. To look past a single account use `detect_suspicious_cluster` (union-find clustering over
   shared devices, shared beneficiaries and prior alerts).
6. Prove relationships with `shortest_connection` so the path can be shown.
7. `run_cypher` is the read-only escape hatch when no other tool fits.
8. Two or more of {shared device, shared beneficiary, prior alert} on the same account is a
   strong mule-ring signal and justifies escalation.
9. Act when the evidence supports it: `open_case`, `add_case_note`, `flag_account` (quote the
   signals in the reason). These are real graph writes — they persist for other agents.
10. Finish by calling `remember` with what you concluded, linked to the entities involved, so
    the next session starts smarter than this one did.

Final answer format (always, in this order):
- **Verdict** — one or two sentences.
- **Evidence chain** — numbered hops with entity ids and relationship types.
- **Graph path** — a compact chain like `ACC-EXT-01 -[TX-9000]-> ACC-9001 -[TX-9001]-> ACC-9002`.
- **Tools & queries used** — names of the tools you called.
- **Actions taken** — cases opened, alerts raised, memory written.
- **Confidence** — high/medium/low, plus what evidence is still missing.
"""


class GraphSleuth:
    """One investigation agent bound to a tenant and a session."""

    def __init__(
        self,
        tenant: str = memory.DEFAULT_TENANT,
        session_id: str | None = None,
        brain: Any = None,
        verbose: bool = True,
    ) -> None:
        self.tenant = tenant
        self.session_id = session_id or memory.new_session_id()
        self.verbose = verbose
        self.history: list[dict] = []
        self._brain_override = brain

    # -- internals ---------------------------------------------------------- #
    def _brain(self):
        if self._brain_override is not None:
            return self._brain_override
        if config.use_live_llm():
            return get_brain()
        return get_brain(scripted=build_demo_brain())

    def _log(self, message: str) -> None:
        if self.verbose:
            print(message)

    def _briefing(self, question: str) -> str:
        digest = memory.context_digest(question, self.tenant)
        return f"{SYSTEM_PROMPT}\n\n# Memory briefing (from the graph, tenant={self.tenant})\n{digest}"

    @staticmethod
    def _assistant_message(response: LLMResponse) -> dict:
        message: dict[str, Any] = {"role": "assistant", "content": response.content or ""}
        if response.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments, default=str)},
                }
                for call in response.tool_calls
            ]
        return message

    # -- public API --------------------------------------------------------- #
    def ask(self, question: str) -> dict:
        """Investigate a question and return the verdict plus the full evidence trail."""
        memory.append_message(self.session_id, "user", question, self.tenant)
        messages: list[dict] = [
            {"role": "system", "content": self._briefing(question)},
            {"role": "user", "content": question},
        ]
        brain = self._brain()
        answer: str | None = None
        rounds = 0

        while rounds < config.max_tool_rounds:
            rounds += 1
            response = brain.chat(messages, TOOL_SPECS, history=self.history)
            messages.append(self._assistant_message(response))

            if response.tool_calls:
                for call in response.tool_calls:
                    self._log(f"  [{rounds}] {call.name}({_short_args(call.arguments)})")
                    result = execute(call.name, call.arguments)
                    self.history.append(
                        {"tool": call.name, "arguments": call.arguments, "result": result}
                    )
                    self._log(f"      -> {_summarise_result(result)}")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "name": call.name,
                            "content": json.dumps(result, default=str, ensure_ascii=False)[:6000],
                        }
                    )
                continue

            answer = response.content
            if answer:
                break

        if not answer:
            answer = (
                f"Investigation stopped after {rounds} rounds without reaching a conclusion. "
                "The graph did not yield enough evidence for a verdict."
            )

        memory.append_message(self.session_id, "assistant", answer, self.tenant)
        memory.set_session_summary(self.session_id, answer[:600])

        return {
            "question": question,
            "answer": answer,
            "trace": self.history,
            "session_id": self.session_id,
            "tenant": self.tenant,
            "rounds": rounds,
            "brain": "live" if config.use_live_llm() else "demo",
        }


def _short_args(arguments: dict) -> str:
    parts = []
    for key, value in arguments.items():
        text = str(value)
        parts.append(f"{key}={text[:60]}")
    return ", ".join(parts)


def _summarise_result(result: Any) -> str:
    if not isinstance(result, dict):
        return str(result)[:100]
    if "error" in result:
        return f"error: {result['error'][:80]}"
    keys = ", ".join(f"{k}={_compact(result[k])}" for k in list(result)[:5])
    return keys[:160]


def _compact(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return f"[{len(value)}]"
    if isinstance(value, dict):
        return "{" + ",".join(list(value)[:3]) + "}"
    text = str(value)
    return text if len(text) <= 40 else text[:37] + "…"


# --------------------------------------------------------------------------- #
# offline / demo brain
# --------------------------------------------------------------------------- #
def build_demo_brain(target: str = "ACC-9003") -> ScriptedBrain:
    """The investigation a well-behaved model would run, hand-scripted.

    Keeps the project fully demoable without an API key, and doubles as the
    script for the judged walkthrough.
    """

    def from_result(tool: str, key: str, default: Any = "") -> Any:
        def resolve(history: list[dict]) -> Any:
            for entry in reversed(history):
                if entry["tool"] == tool and isinstance(entry["result"], dict):
                    return entry["result"].get(key, default)
            return default

        return resolve

    def cluster_members(history: list[dict]) -> list[str]:
        for entry in reversed(history):
            if entry["tool"] == "detect_suspicious_cluster" and isinstance(entry["result"], dict):
                clusters = entry["result"].get("clusters") or []
                if clusters:
                    return [member["account"] for member in clusters[0]["members"]]
        return [target]

    def ring_summary(history: list[dict]) -> str:
        for entry in reversed(history):
            if entry["tool"] == "detect_suspicious_cluster" and isinstance(entry["result"], dict):
                clusters = entry["result"].get("clusters") or []
                if clusters:
                    top = clusters[0]
                    accounts = ", ".join(member["account"] for member in top["members"])
                    return f"{top['size']}-account cluster ({accounts}), score {top['cluster_score']}"
        return f"cluster around {target}"

    plan: list[tuple[str, Any]] = [
        ("recall", {"query": target}),
        ("entity_search", {"query": target}),
        ("graph_neighbors", {"entity_id": target, "depth": 2}),
        ("trace_money_flow", {"account_id": target, "depth": 4}),
        ("find_shared_links", {"account_id": target}),
        ("detect_suspicious_cluster", {"min_signals": 2}),
        ("shortest_connection", {"id_a": "ACC-EXT-01", "id_b": "ACC-9100", "max_depth": 8}),
        (
            "open_case",
            lambda history: {
                "title": f"Mule ring around {target}",
                "summary": ring_summary(history),
                "entity_ids": cluster_members(history) + ["ACC-9100", "DEV-9001", "DEV-9002"],
            },
        ),
        (
            "add_case_note",
            lambda history: {
                "case_id": from_result("open_case", "case_id", "")(history),
                "text": (
                    f"Evidence: {ring_summary(history)}. Money chain traced from ACC-EXT-01 to "
                    "ACC-9100 via the shared devices DEV-9001/DEV-9002; prior alerts on "
                    "ACC-9001, ACC-9002 and ACC-9004."
                ),
            },
        ),
        (
            "flag_account",
            lambda history: {
                "account_id": target,
                "severity": "high",
                "reason": (
                    "Shares device DEV-9001/DEV-9002 with flagged accounts, is a pass-through hop "
                    "in a same-day chain ending at ACC-9100, and sits in the top-ranked cluster."
                ),
                "case_id": from_result("open_case", "case_id", "")(history),
            },
        ),
        (
            "remember",
            lambda history: {
                "kind": "episodic",
                "text": (
                    f"Investigated {target}: {ring_summary(history)}. Confirmed same-day mule chain "
                    "ACC-EXT-01 -> ACC-9001 -> ... -> ACC-9005 -> ACC-9100 through shared devices "
                    "DEV-9001/DEV-9002; account flagged and case opened."
                ),
                "about_ids": cluster_members(history) + ["ACC-9100"],
            },
        ),
    ]
    return ScriptedBrain(plan=plan, on_finish=compose_final)


def _last(history: list[dict], tool: str) -> dict | None:
    for entry in reversed(history):
        if entry["tool"] == tool and isinstance(entry["result"], dict):
            return entry["result"]
    return None


def compose_final(question: str, history: list[dict]) -> str:
    """Turn the evidence trail into the required verdict format (offline mode)."""
    target_match = re.search(r"(ACC-\d+|TX-\d+|AL-\d+|CASE-\d+)", question)
    target = target_match.group(1) if target_match else "ACC-9003"

    cluster = _last(history, "detect_suspicious_cluster") or {}
    shared = _last(history, "find_shared_links") or {}
    flow = _last(history, "trace_money_flow") or {}
    path = _last(history, "shortest_connection") or {}
    prior = _last(history, "recall") or {}

    clusters = cluster.get("clusters") or []
    top = clusters[0] if clusters else {}
    members = top.get("members", [])

    lines: list[str] = []
    lines.append(f"**Verdict** — {target} is a confirmed hop in a {top.get('size', 0)}-account mule ring "
                 "that pools same-day transfers into beneficiary account ACC-9100. Escalated.")

    lines.append("")
    lines.append("**Evidence chain**")
    steps = 0
    if prior.get("notes"):
        latest = prior["notes"][0]
        steps += 1
        lines.append(f"{steps}. Memory: [{latest.get('kind')}] {str(latest.get('text'))[:150]}")
    for level in (flow.get("incoming", {}).get("levels") or [])[:1]:
        for hop in level["hops"][:2]:
            steps += 1
            lines.append(f"{steps}. Inbound: {hop['from']} -> {hop['to']} via {hop['tx']} "
                         f"(INR {hop['amount']:,.0f}, {hop['timestamp']})")
    for level in (flow.get("outgoing", {}).get("levels") or [])[:2]:
        for hop in level["hops"][:3]:
            steps += 1
            lines.append(f"{steps}. Outbound: {hop['from']} -> {hop['to']} via {hop['tx']} "
                         f"(INR {hop['amount']:,.0f}, {hop['timestamp']})")
    for link in (shared.get("links") or [])[:3]:
        steps += 1
        lines.append(
            f"{steps}. Shared links: {link['account']} (score {link['score']}) "
            f"devices={link['shared_devices']} beneficiaries={link['shared_beneficiaries']} "
            f"alerts={link['alerts']}"
        )
    if top:
        steps += 1
        lines.append(
            f"{steps}. Clustering: top cluster score {top.get('cluster_score')} over "
            f"{', '.join(member['account'] for member in members)}"
        )

    lines.append("")
    lines.append("**Graph path**")
    lines.append(path.get("narrative") or "(no single path found)")

    lines.append("")
    lines.append("**Members flagged by the clustering algorithm**")
    for member in members[:6]:
        lines.append(
            f"- {member['account']} | risk {member['risk_score']} | devices "
            f"{member['shared_devices']} | beneficiaries {member['shared_beneficiaries']} | "
            f"alerts {member['alerts']}"
        )

    lines.append("")
    tools_used = []
    for entry in history:
        if entry["tool"] not in tools_used:
            tools_used.append(entry["tool"])
    lines.append("**Tools & queries used** — " + ", ".join(tools_used))

    actions: list[str] = []
    case = _last(history, "open_case") or {}
    if case.get("case_id"):
        actions.append(f"opened {case['case_id']} involving {', '.join(case.get('involves', []))}")
    note = _last(history, "add_case_note") or {}
    if note.get("note_id"):
        actions.append(f"added evidence note {note['note_id']} to {note['case_id']}")
    flagged = _last(history, "flag_account") or {}
    if flagged.get("alert_id"):
        actions.append(
            f"flagged {flagged['account']} as {flagged['severity']} ({flagged['alert_id']}), "
            "status under_review"
        )
    memo = _last(history, "remember") or {}
    if memo.get("note_id"):
        actions.append(f"wrote memory {memo['note_id']} linked to {memo.get('linked_to')}")
    lines.append("")
    lines.append("**Actions taken** — " + ("; ".join(actions) if actions else "none"))

    lines.append("")
    lines.append(
        "**Confidence** — high. Signals agree (shared devices + shared beneficiary + prior alerts + "
        "same-day pass-through). Still missing: beneficial-owner confirmation for ACC-9100 and the "
        "source of funds behind ACC-EXT-01."
    )
    return "\n".join(lines)
