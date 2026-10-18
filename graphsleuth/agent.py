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

from . import db, memory, tools
from .config import config
from .llm import LLMResponse, ScriptedBrain, get_brain
from .tools import TOOL_SPECS, execute

SYSTEM_PROMPT = """You are GraphSleuth, a fraud investigation agent. Your context layer is a
FalkorDB graph that contains accounts, people, devices, merchants, transactions, alerts,
cases, agent handoffs, and your own long-term memory. You reason by following relationships,
never by guessing. (Each tenant has its own graph — everything you see is your tenant's data.)

Working rules:
1. Evidence only. Every claim in your final answer must come from a tool result. If the graph
   does not show it, say so instead of inventing it — this includes actions: only list a tool
   under "Actions taken" if you actually called it in this session. A false action in the report
   is worse than an incomplete one.
2. Start with memory. Call `recall` before investigating — a previous session may already know
   something about this entity or beneficiary.
3. Resolve ids with `entity_search`, then expand with `graph_neighbors` (depth 1-3).
4. For suspicious-transaction questions: `trace_money_flow` (where did the money go?), then
   `find_shared_links` (who else is on this device / beneficiary?).
5. To look past a single account use `detect_suspicious_cluster` (union-find over shared
   devices, corroborated by shared beneficiaries and prior alerts).
6. Prove relationships with `shortest_connection` so the path can be shown.
7. Before you escalate — and before you claim **high** confidence — stress-test the conclusion:
   `what_if_removed` on the decisive device or edge (does the verdict survive losing it?) and
   `assess_ring_resilience` on the top cluster (which members are load-bearing?). If you did not
   run them, your confidence is at most **medium** and you must say the counterfactual is missing.
   `ring_timeline` (when did it happen?) and `find_similar_rings` (is the playbook repeating
   elsewhere?) are cheap read-only checks worth running on a confirmed ring.
8. For whole-graph structure use the graph algorithms: `centrality` (which node bridges every
   path), `page_rank` (structural hubs), `connected_components` (isolated islands),
   `community_detection` (who clusters with whom).
9. `run_cypher` is the read-only escape hatch when no other tool fits.
10. Two or more of {shared device, shared beneficiary, prior alert} on the same account is a
    strong mule-ring signal and justifies escalation.
11. Act when the evidence supports it: `open_case`, `add_case_note`, `flag_account` (quote the
    signals in the reason). These are real graph writes — they persist for other agents.
12. If a handoff is waiting (`list_handoffs`), call `accept_handoff` first and continue the
    previous agent's work instead of starting from scratch.
13. If you are the triage agent: triage quickly (`list_alerts`, `find_shared_links`,
    `detect_suspicious_cluster`), then either `create_handoff(to_role='investigator', ...)`
    with the full context, or close it out as non-escalation.
14. Finish by calling `remember` with what you concluded, linked to the entities involved, so
    the next session starts smarter than this one did.

Final answer format (always, in this order):
- **Verdict** — one or two sentences.
- **Evidence chain** — numbered hops with entity ids and relationship types.
- **Graph path** — a compact chain like `ACC-EXT-01 -[TX-9000]-> ACC-9001 -[TX-9001]-> ACC-9002`.
- **Tools & queries used** — names of the tools you called.
- **Actions taken** — the write tools you really called this session (`open_case`,
  `add_case_note`, `flag_account`, `create_handoff`, `accept_handoff`, `remember`) with their ids.
  If you only read, write exactly: `None — read-only investigation.`
- **Confidence** — high/medium/low, plus what evidence is still missing. "high" requires the
  counterfactual stress test from rule 7 to have run in this session; without it, say what is
  missing and answer at most "medium".
"""


class GraphSleuth:
    """One investigation agent bound to a tenant and a session."""

    def __init__(
        self,
        tenant: str = memory.DEFAULT_TENANT,
        session_id: str | None = None,
        brain: Any = None,
        verbose: bool = True,
        role: str = "investigator",
        prompt: str | None = None,
        max_rounds: int | None = None,
        scripted: Any = None,
        model: str | None = None,
        on_event: Any = None,
        tool_specs: list[dict] | None = None,
    ) -> None:
        self.tenant = tenant
        self.session_id = session_id or memory.new_session_id()
        self.verbose = verbose
        self.history: list[dict] = []
        self.role = role
        self.system_prompt = prompt or SYSTEM_PROMPT
        self.max_rounds = int(max_rounds or config.max_tool_rounds)
        self._brain_override = brain
        self._scripted = scripted
        self._model = model
        self._on_event = on_event
        # The fraud agent gets the 24 fraud tools; a company agent passes its own
        # subset (see company.COMPANY_TOOL_SPECS). Same loop either way.
        self.tool_specs = tool_specs if tool_specs is not None else TOOL_SPECS

    # -- internals ---------------------------------------------------------- #
    def _brain(self):
        if self._brain_override is not None:
            return self._brain_override
        if config.use_live_llm():
            return get_brain(model=self._model)
        return get_brain(scripted=self._scripted or build_demo_brain())

    def _log(self, message: str) -> None:
        if self.verbose:
            print(message)

    def _emit(self, event: dict) -> None:
        """Publish a live event to an optional listener (the web UI / SSE stream).

        A broken listener must never break an investigation, so failures are
        swallowed. Events are: `start`, `tool_call`, `tool_result`, `skipped`,
        `answer`.
        """
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception:  # noqa: BLE001 - listeners are best-effort telemetry
            pass

    def _learn_playbook(self, question: str, complete: bool = True) -> None:
        """Distil this investigation into procedural memory (a reusable playbook).

        Only substantial investigations teach: a 1-2 tool lookup is not a
        procedure. Identical tool sequences are de-duplicated, and the tenant
        keeps at most `memory.save_playbook` playbooks, so memory stays useful
        instead of becoming a log.

        Two runs teach nothing, because distilling them would file a procedure as
        if it had produced the verdict: one the round budget cut short, and one
        whose probes all came back empty (an empty or wrong graph).
        """
        if not complete:
            self._log("  [memory] no playbook learned — the run hit its round budget")
            return
        if _probes_ran_empty(self.history):
            self._log("  [memory] no playbook learned — the investigation found no evidence")
            return
        try:
            playbook = memory.distill_playbook(self.history, question, self.tenant)
            if not playbook or memory.playbook_exists(playbook["signature"], self.tenant):
                return
            saved = memory.save_playbook(playbook, self.tenant)
            if saved.get("saved"):
                self._emit({"type": "learned", "playbook": saved})
                self._log(f"  [memory] learned playbook {saved['note_id']}: {playbook['signature']}")
        except Exception:  # noqa: BLE001 - learning must never fail an investigation
            pass

    def _briefing(self, question: str) -> str:
        digest = memory.context_digest(question, self.tenant)
        return (
            f"{self.system_prompt}\n\n"
            f"# Memory briefing (from the graph, tenant={self.tenant}, "
            f"session={self.session_id}, role={self.role})\n{digest}"
        )

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

    def _prune(self, messages: list[dict], keep: int = 8) -> None:
        """Trim old tool exchanges out of the live prompt.

        Keeps the system + user turn plus the most recent `keep` messages. The
        complete evidence trail still lives in `self.history` (that is what the
        report and the memory write are built from), so a free-tier token cap
        only ever costs the model *chat* recall, never evidence.
        """
        if len(messages) <= keep + 2:
            return
        head = messages[:2]
        tail = messages[2:]
        # Drop whole rounds so a tool reply never loses its assistant message.
        while len(tail) > keep:
            dropped = tail.pop(0)
            if dropped.get("role") == "assistant":
                while tail and tail[0].get("role") == "tool":
                    tail.pop(0)
        messages[:] = head + tail

    # -- public API --------------------------------------------------------- #
    def ask(self, question: str) -> dict:
        """Investigate a question and return the verdict plus the full evidence trail."""
        # Everything below runs against THIS tenant's graph, and tool actions are
        # stamped with THIS session (handoffs, memory writes).
        db.set_tenant(self.tenant)
        tools.bind_session(self.session_id)

        memory.append_message(self.session_id, "user", question, self.tenant)
        messages: list[dict] = [
            {"role": "system", "content": self._briefing(question)},
            {"role": "user", "content": question},
        ]
        brain = self._brain()
        self._emit(
            {
                "type": "start",
                "question": question,
                "session_id": self.session_id,
                "tenant": self.tenant,
                "role": self.role,
                "provider": getattr(brain, "provider", "offline fallback"),
            }
        )
        answer: str | None = None
        rounds = 0
        seen_calls: set[str] = set()
        forced_verdict = False

        while rounds < self.max_rounds:
            rounds += 1
            # The final round is reserved for the verdict so the agent can never
            # run out of budget mid-investigation.
            force_answer = rounds >= self.max_rounds
            forced_verdict = force_answer
            response = brain.chat(
                messages, self.tool_specs, history=self.history, force_answer=force_answer
            )
            messages.append(self._assistant_message(response))

            if response.tool_calls:
                for call in response.tool_calls:
                    self._log(f"  [{rounds}] {call.name}({_short_args(call.arguments)})")
                    self._emit(
                        {
                            "type": "tool_call",
                            "round": rounds,
                            "tool": call.name,
                            "arguments": call.arguments,
                        }
                    )
                    signature = f"{call.name}:{json.dumps(call.arguments, sort_keys=True, default=str)}"
                    if signature in seen_calls:
                        # Same tool, same arguments, again: don't pay for it twice
                        # (free tiers cap tokens) and nudge the model to move on.
                        self._log("      -> (duplicate call skipped)")
                        self._emit({"type": "skipped", "round": rounds, "tool": call.name})
                        content = json.dumps(
                            {
                                "note": "duplicate call skipped — you already have this exact "
                                "result above; use it and continue (or write the final report)."
                            },
                            ensure_ascii=False,
                        )
                    else:
                        seen_calls.add(signature)
                        result = execute(call.name, call.arguments)
                        self.history.append(
                            {"tool": call.name, "arguments": call.arguments, "result": result}
                        )
                        self._log(f"      -> {_summarise_result(result)}")
                        self._emit(
                            {
                                "type": "tool_result",
                                "round": rounds,
                                "tool": call.name,
                                "arguments": call.arguments,
                                "result": result,
                                "summary": _summarise_result(result),
                            }
                        )
                        content = json.dumps(result, default=str, ensure_ascii=False)[:1200]
                    messages.append(
                        {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": content}
                    )
                self._prune(messages)
                continue

            answer = response.content
            if answer:
                break

        if not answer:
            answer = (
                f"Investigation stopped after {rounds} rounds without reaching a conclusion. "
                "The graph did not yield enough evidence for a verdict."
            )

        # Was the plan cut short? A scripted brain knows how much of the plan never
        # ran; a live brain can only be judged by "the verdict round was forced".
        # Either way the reader is told, in the answer itself, so the caveat travels
        # with the report, the console and the saved evidence file.
        remaining = getattr(brain, "remaining_steps", None)
        truncated = forced_verdict if remaining is None else remaining > 0
        if truncated:
            if remaining:
                note = (
                    f"{remaining} planned tool call(s) did not run, so any write actions among "
                    "them (case note, flag, memory) never happened."
                )
            else:
                note = "the answer was produced on the final round, so the investigation may be incomplete."
            answer += (
                f"\n\n> ⚠ **Round budget reached** (`MAX_TOOL_ROUNDS={self.max_rounds}`, "
                f"{rounds} rounds used): {note} Re-run with a larger `MAX_TOOL_ROUNDS` "
                "for a complete plan."
            )
            self._log(f"  [!] round budget reached after {rounds} rounds — {note}")
            self._emit(
                {
                    "type": "truncated",
                    "rounds": rounds,
                    "max_rounds": self.max_rounds,
                    "remaining_steps": remaining,
                }
            )

        # The same discipline for confidence: the prompt asks for the counterfactual
        # stress test before a "high" claim, but a small model forgets — so the claim is
        # checked against the trace and corrected when the evidence does not support it.
        corrected = _downgrade_confidence(answer, self.history)
        if corrected:
            answer = corrected + (
                "\n\n> ⚠ **Confidence downgraded** — the answer claimed high confidence, but no "
                "counterfactual stress test (`what_if_removed` / `assess_ring_resilience`) ran in "
                "this session, so how much the conclusion leans on a single link is unknown."
            )
            self._log("  [!] confidence downgraded — no counterfactual ran this session")
            self._emit({"type": "confidence_downgraded", "reason": "no counterfactual"})

        memory.append_message(self.session_id, "assistant", answer, self.tenant)
        memory.set_session_summary(self.session_id, answer[:600])
        self._learn_playbook(question, complete=not truncated)

        result = {
            "question": question,
            "answer": answer,
            "trace": self.history,
            "session_id": self.session_id,
            "tenant": self.tenant,
            "role": self.role,
            "rounds": rounds,
            "truncated": truncated,
            "max_rounds": self.max_rounds,
            "brain": "live" if getattr(brain, "provider", None) else "offline-scripted",
            "provider": getattr(brain, "provider", "offline fallback"),
        }
        self._emit({"type": "answer", "answer": answer, "rounds": rounds})
        return result


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
        inner = ", ".join(
            f"{k}:{len(v) if isinstance(v, (list, tuple, dict)) else v}"
            for k, v in list(value.items())[:3]
        )
        return "{" + inner + "}"
    text = str(value)
    return text if len(text) <= 40 else text[:37] + "…"


# --------------------------------------------------------------------------- #
# history readers + shared case spine (every offline brain uses these)
# --------------------------------------------------------------------------- #
def _last(history: list[dict], tool: str) -> dict | None:
    for entry in reversed(history):
        if entry["tool"] == tool and isinstance(entry["result"], dict):
            return entry["result"]
    return None


def _pending_handoff(history: list[dict]) -> str:
    """Handoff id of the newest pending handoff seen by `list_handoffs`."""
    handoffs = (_last(history, "list_handoffs") or {}).get("handoffs") or []
    return handoffs[0]["handoff_id"] if handoffs else ""


def _dedupe_ids(ids: list[str]) -> list[str]:
    """Order-preserving de-duplication for entity id lists."""
    seen: set[str] = set()
    unique: list[str] = []
    for entity_id in ids:
        if entity_id not in seen:
            seen.add(entity_id)
            unique.append(entity_id)
    return unique


def _cluster_members(history: list[dict], fallback: str) -> list[str]:
    """Members of the top detected cluster, else the linked accounts (+ the target)."""
    clusters = (_last(history, "detect_suspicious_cluster") or {}).get("clusters") or []
    if clusters:
        return [member["account"] for member in clusters[0]["members"]]
    links = _last(history, "find_shared_links") or {}
    linked = [link.get("account") for link in (links.get("links") or []) if link.get("account")]
    return _dedupe_ids(linked + [fallback])


def _ring_summary(history: list[dict], target: str) -> str:
    """One-line description of the top cluster for case titles and notes."""
    clusters = (_last(history, "detect_suspicious_cluster") or {}).get("clusters") or []
    if clusters:
        top = clusters[0]
        accounts = ", ".join(member["account"] for member in top["members"])
        return f"{top['size']}-account cluster ({accounts}), score {top['cluster_score']}"
    return f"signals around {target}"


def _flow_hops(flow: dict) -> list[dict]:
    """Every transaction hop a `trace_money_flow` result contains (both directions)."""
    hops: list[dict] = []
    for direction in ("incoming", "outgoing"):
        for level in (flow.get(direction) or {}).get("levels") or []:
            hops.extend(level.get("hops") or [])
    return hops


def _evidence_seen(history: list[dict]) -> bool:
    """Did the tools actually find anything in this graph?

    False means the tenant is empty or wrong (`python -m graphsleuth seed` builds
    the demo network), not that the target is clean. Every offline brain asks this
    before claiming a finding — and before writing one.
    """
    searches = _last(history, "entity_search") or {}
    clusters = (_last(history, "detect_suspicious_cluster") or {}).get("clusters") or []
    links = (_last(history, "find_shared_links") or {}).get("links") or []
    path = (_last(history, "shortest_connection") or {}).get("path") or []
    neighbours = (_last(history, "graph_neighbors") or {}).get("node_count") or 0
    return bool(
        searches.get("matches")
        or clusters
        or links
        or _flow_hops(_last(history, "trace_money_flow") or {})
        or path
        or neighbours
    )


def _counterfactual_ran(history: list[dict]) -> bool:
    """Did a counterfactual stress test actually run (and produce numbers)?

    `what_if_removed` reports a robustness score; `assess_ring_resilience` reports a
    ring verdict. An errored call counts as "did not run".
    """
    counter = _last(history, "what_if_removed") or {}
    resilience = _last(history, "assess_ring_resilience") or {}
    return counter.get("robustness") is not None or bool(resilience.get("found_ring"))


def _downgrade_confidence(answer: str, history: list[dict]) -> str | None:
    """Return `answer` with an unsupported "high" confidence claim corrected, or None.

    Prompt rules drift — a small live model will assert "confidence high, nothing
    missing" without ever running the counterfactual stress test. The claim is
    deterministic, so it is checked deterministically: if the answer says high and
    no counterfactual ran, the line is rewritten to medium (the rest of the line is
    kept verbatim) and the caller appends the reason.
    """
    if _counterfactual_ran(history):
        return None
    match = re.search(
        r"(\*\*Confidence\*\*\s*[—:-]\s*)(?:\*\*)?high(?:\*\*)?(?!\w)", answer, re.IGNORECASE
    )
    if not match:
        return None
    return (
        f"{answer[: match.start()]}{match.group(1)}"
        f"medium (downgraded — no counterfactual ran){answer[match.end():]}"
    )


def _tool_names(history: list[dict]) -> list[str]:
    """Distinct tool names in call order."""
    names: list[str] = []
    for entry in history:
        if entry["tool"] not in names:
            names.append(entry["tool"])
    return names


_EVIDENCE_PROBES = {
    "entity_search",
    "graph_neighbors",
    "find_shared_links",
    "detect_suspicious_cluster",
    "trace_money_flow",
    "shortest_connection",
}


def _probes_ran_empty(history: list[dict]) -> bool:
    """The standard probes ran AND every one of them came back empty.

    The signature of an empty or wrong tenant — as opposed to a run that simply
    used other tools, which must not be treated as "found nothing".
    """
    ran = [entry for entry in history if entry.get("tool") in _EVIDENCE_PROBES]
    return bool(ran) and not _evidence_seen(history)


def _actions_taken(history: list[dict]) -> list[str]:
    """The write actions a trace really contains, with the ids the graph returned."""
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
    return actions


# The concrete case every offline brain verifies: the same money path, and the same
# counterfactual stress test the verdict must survive. Shared so the demo walkthrough
# and the independent verifier agent can never drift apart.
CASE_MONEY_PATH: tuple[str, dict] = (
    "shortest_connection",
    {"id_a": "ACC-EXT-01", "id_b": "ACC-9100", "max_depth": 8, "via": ["ORIGINATED", "DESTINED"]},
)
CASE_STRESS_TEST: tuple[str, dict] = (
    "what_if_removed",
    {"entity_id": "DEV-9001", "from_id": "ACC-EXT-01", "to_id": "ACC-9100", "max_depth": 8},
)


# --------------------------------------------------------------------------- #
# offline fallback brain (used only when no LLM key is configured)
# --------------------------------------------------------------------------- #
def build_demo_brain(target: str = "ACC-9003", handoff: bool = False) -> ScriptedBrain:
    """The investigation a well-behaved model would run, hand-scripted.

    Keeps the project fully runnable without an API key, and doubles as the
    script for the judged walkthrough. With `handoff=True` the plan starts by
    picking up a pending handoff (the two-agent pipeline).
    """

    def from_result(tool: str, key: str, default: Any = "") -> Any:
        def resolve(history: list[dict]) -> Any:
            for entry in reversed(history):
                if entry["tool"] == tool and isinstance(entry["result"], dict):
                    return entry["result"].get(key, default)
            return default

        return resolve

    plan: list[tuple[str, Any]] = []
    if handoff:
        plan += [
            ("list_handoffs", {"status": "pending", "limit": 3}),
            ("accept_handoff", lambda history: {"handoff_id": _pending_handoff(history)}),
        ]
    plan += [
        ("recall", {"query": target}),
        ("entity_search", {"query": target}),
        ("graph_neighbors", {"entity_id": target, "depth": 2}),
        ("trace_money_flow", {"account_id": target, "depth": 4}),
        ("find_shared_links", {"account_id": target}),
        ("detect_suspicious_cluster", {"min_signals": 2}),
        CASE_MONEY_PATH,
        # Counterfactual stress tests (read-only): the verdict must survive losing
        # the shared device it leans on, and every member is ranked by how
        # load-bearing it is (articulation points).
        CASE_STRESS_TEST,
        (
            "assess_ring_resilience",
            {"ring_key": "top"},
        ),
        (
            # When did it actually happen? Timestamp-ordered story of the ring's
            # activity, burst window and alert lead/lag (read-only).
            "ring_timeline",
            {"ring_key": "top"},
        ),
        (
            # Is the playbook repeating anywhere else in the graph? (read-only).
            "find_similar_rings",
            {"ring_key": "top"},
        ),
        # Writes are gated on evidence: on an empty or wrong graph (or an unseeded
        # tenant) these steps resolve to None and ScriptedBrain skips them, so the
        # fallback never opens a case, flags an account or writes "confirmed mule
        # chain" about an account the graph has never seen.
        (
            "open_case",
            lambda history: None if not _evidence_seen(history) else {
                "title": f"Mule ring around {target}",
                "summary": _ring_summary(history, target),
                "entity_ids": _dedupe_ids(
                    _cluster_members(history, target) + ["ACC-9100", "DEV-9001", "DEV-9002"]
                ),
            },
        ),
        (
            "add_case_note",
            lambda history: None
            if not _evidence_seen(history)
            else {
                "case_id": from_result("open_case", "case_id", "")(history),
                "text": (
                    f"Evidence: {_ring_summary(history, target)}. Money chain traced from ACC-EXT-01 to "
                    "ACC-9100 via the shared devices DEV-9001/DEV-9002; prior alerts on "
                    "ACC-9001, ACC-9002 and ACC-9004."
                ),
            },
        ),
        (
            "flag_account",
            lambda history: None
            if not _evidence_seen(history)
            else {
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
            lambda history: None
            if not _evidence_seen(history)
            else {
                "kind": "episodic",
                "text": (
                    f"Investigated {target}: {_ring_summary(history, target)}. Confirmed same-day mule chain "
                    "ACC-EXT-01 -> ACC-9001 -> ... -> ACC-9005 -> ACC-9100 through shared devices "
                    "DEV-9001/DEV-9002; account flagged and case opened."
                ),
                "about_ids": _dedupe_ids(_cluster_members(history, target) + ["ACC-9100"]),
            },
        ),
    ]
    return ScriptedBrain(plan=plan, on_finish=compose_final)


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

    # No evidence: say exactly that. An empty or wrong tenant must never be dressed
    # up as a finding — the fallback used to invent a ring, a beneficiary and a
    # "high confidence" line out of nothing.
    if not _evidence_seen(history):
        tenant = db.resolve_tenant()
        seen = []
        for tool, message in (
            (
                "entity_search",
                f"entity_search matched "
                f"{len((_last(history, 'entity_search') or {}).get('matches') or [])} entities "
                f"for {target}",
            ),
            ("find_shared_links", "find_shared_links returned no shared device, beneficiary or alert"),
            ("detect_suspicious_cluster", "detect_suspicious_cluster returned no cluster"),
            ("trace_money_flow", "trace_money_flow followed no transaction"),
            ("shortest_connection", "shortest_connection found no path"),
        ):
            if _last(history, tool) is not None:
                seen.append(message)
        actions = _actions_taken(history)
        return "\n".join(
            [
                f"**Verdict** — none reached: the tools found no evidence about {target} in tenant "
                f"`{tenant}`, so there is nothing here to confirm or clear.",
                "",
                "**What the tools saw**",
                *[f"- {item}" for item in seen],
                "",
                "**Why** — this is an empty or wrong graph, not a clean account. Check the active "
                "tenant with `python -m graphsleuth status`, and build the demo network with "
                "`python -m graphsleuth seed` (`--tenant <name>` for a non-default tenant).",
                "",
                "**Tools & queries used** — " + ", ".join(_tool_names(history)),
                "",
                "**Actions taken** — "
                + ("; ".join(actions) if actions else "none — nothing was written to the graph."),
                "",
                "**Confidence** — n/a: no verdict was reached.",
            ]
        )

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

    counter = _last(history, "what_if_removed") or {}
    if counter.get("conclusion"):
        lines.append("")
        lines.append("**Counterfactual check**")
        lines.append(str(counter["conclusion"]))

    resilience = _last(history, "assess_ring_resilience") or {}
    arts = resilience.get("articulation_points") or []
    if resilience.get("summary"):
        lines.append("")
        lines.append("**Ring resilience**")
        lines.append(str(resilience["summary"]))
        if arts:
            lines.append("")
            lines.append(
                "**Disruption guidance** — the ring's structural bottlenecks are "
                + ", ".join(str(a) for a in arts)
                + "; containing them first breaks the flow fastest."
            )

    timeline = _last(history, "ring_timeline") or {}
    if timeline.get("summary"):
        lines.append("")
        lines.append("**Timeline (reconstructed from graph timestamps)**")
        lines.append(str(timeline["summary"]))
        order = timeline.get("activation_order") or []
        if order:
            lines.append(
                "- activation order: "
                + " → ".join(
                    f"{step['member']} ({str(step['first_touch'])[11:16]})"
                    for step in order[:6]
                )
            )

    similar = _last(history, "find_similar_rings") or {}
    if similar.get("summary"):
        lines.append("")
        lines.append("**Pattern check — is this playbook running elsewhere?**")
        lines.append(str(similar["summary"]))

    lines.append("")
    lines.append("**Members flagged by the clustering algorithm**")
    for member in members[:6]:
        role = member.get("role")
        label = f" ({role})" if role and role != "linked_account" else ""
        lines.append(
            f"- {member['account']}{label} | risk {member['risk_score']} | devices "
            f"{member['shared_devices']} | beneficiaries {member['shared_beneficiaries']} | "
            f"alerts {member['alerts']}"
        )

    lines.append("")
    lines.append("**Tools & queries used** — " + ", ".join(_tool_names(history)))

    actions = _actions_taken(history)
    lines.append("")
    lines.append("**Actions taken** — " + ("; ".join(actions) if actions else "none"))

    lines.append("")
    # Self-consistency guard: the stated confidence can never exceed what the
    # counterfactual + resilience numbers actually support. If the case is a single
    # point of failure, say so instead of claiming "high".
    robustness = counter.get("robustness")
    resilience_verdict = str(resilience.get("verdict") or "")
    if not counter or not isinstance(robustness, (int, float)):
        # No counterfactual ran (or it produced no usable numbers): the run cannot
        # claim the verdict survives losing its key link, so it must not claim
        # "high" — and it must never print "robustness n/a/100".
        lines.append(
            "**Confidence** — medium, NOT high: no counterfactual stress test ran in this trace, "
            "so how much the conclusion leans on a single link is unknown. Missing: a "
            "`what_if_removed` check on the decisive device/path, beneficial-owner confirmation "
            "for ACC-9100 and the source of funds behind ACC-EXT-01."
        )
    elif robustness <= 40:
        lines.append(
            "**Confidence** — medium, NOT high: the counterfactual shows the conclusion "
            f"leans on a single link (robustness {robustness}/100). Corroborate before acting."
        )
    elif resilience_verdict in {"bottlenecked", "fragile"} and arts:
        lines.append(
            "**Confidence** — high on membership, conditional on structure: signals agree "
            f"(shared devices + collector + alerts + same-day chain), but the ring is "
            f"`{resilience_verdict}` (articulation: {', '.join(str(a) for a in arts)}). "
            "Still missing: beneficial-owner confirmation for ACC-9100 and source of funds "
            "behind ACC-EXT-01."
        )
    else:
        lines.append(
            "**Confidence** — high. Signals agree (shared devices + shared beneficiary + prior "
            "alerts + same-day pass-through), the verdict survives the counterfactual "
            f"(robustness {robustness}/100). Still missing: beneficial-owner confirmation for "
            "ACC-9100 and the source of funds behind ACC-EXT-01."
        )
    return "\n".join(lines)
