"""Command line interface.

    python -m graphsleuth status                    # FalkorDB + multigraph + LLM check
    python -m graphsleuth seed [--tenant acme]      # build the synthetic fraud graph
    python -m graphsleuth demo [--target ACC-9003] [--save report.md]
    python -m graphsleuth pipeline [--target ACC-9003] [--verify]   # triage -> handoff -> investigator [-> verifier]
    python -m graphsleuth bench [--writers 4 --readers 16]
    python -m graphsleuth chat [--tenant acme] [--session SES-1234]
    python -m graphsleuth serve [--port 8765]          # interactive web console
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import bench as bench_module
from . import db, explain, memory, seed as seed_module
from .agent import GraphSleuth, build_demo_brain
from .agents import pipeline_markdown, run_pipeline
from .config import config


def cmd_status(_args: argparse.Namespace) -> int:
    try:
        info = db.ping()
    except Exception as exc:  # noqa: BLE001
        print(f"FalkorDB NOT reachable at {config.falkordb_host}:{config.falkordb_port}")
        print(f"  reason: {type(exc).__name__}: {exc}")
        print("  start it with:  docker compose up -d")
        return 1
    print(f"FalkorDB OK  ->  {info['host']}  (multigraph: one graph per tenant)")
    print(f"  active graph: '{info['graph']}'  (tenant '{info['tenant']}')")
    others = [name for name in info["graphs"] if name != info["graph"]]
    if others:
        print(f"  other graphs: {', '.join(others)}")
    print(f"  nodes: {info['nodes']}   relationships: {info['relationships']}")
    if config.use_live_llm():
        chain = config.llm_models
        print(f"  LLM: LIVE ({config.llm_provider})")
        if len(chain) > 1:
            print(f"  fallback chain: {' -> '.join(chain)}")
    else:
        print("  LLM: offline fallback (scripted brain) — set GROQ_API_KEY for the real model")
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    db.set_tenant(args.tenant)
    stats = seed_module.seed(reset=not args.keep)
    print(
        f"Done. {stats['nodes']} nodes / {stats['relationships']} relationships "
        f"in graph '{db.graph_name_for(args.tenant)}' (tenant '{db.resolve_tenant()}')"
    )
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    question = f"Investigate {args.target} — is this account part of a fraud ring?"
    print(f"Question: {question}\n")
    agent = GraphSleuth(
        tenant=args.tenant,
        brain=build_demo_brain(args.target) if args.offline else None,
        scripted=build_demo_brain(args.target),
        model=args.model,
    )
    result = agent.ask(question)
    print("\n" + result["answer"])
    print("\n" + "-" * 78)
    print("EVIDENCE TRAIL")
    print("-" * 78)
    print(explain.render_trace(result["trace"]))
    if args.save:
        path = explain.save_evidence(args.save, question, result)
        print(f"\nSaved report to {path}")
    return 0


def cmd_pipeline(args: argparse.Namespace) -> int:
    print(f"Pipeline: triage -> handoff -> investigator | target={args.target} tenant={args.tenant}\n")
    result = run_pipeline(
        args.target,
        tenant=args.tenant,
        verbose=not args.quiet,
        offline=args.offline,
        model=args.model,
        verify=args.verify,
    )
    print("\n" + "=" * 78)
    print("TRIAGE")
    print("=" * 78)
    print(result["triage"]["answer"])
    print("\n" + "=" * 78)
    print("HANDOFF (state shared through the graph)")
    print("=" * 78)
    created = result["handoff"]["created"] or "none"
    accepted = result["handoff"]["accepted"] or "not picked up"
    print(f"  created : {created}\n  accepted: {accepted} ({result['handoff']['status']})")
    print("\n" + "=" * 78)
    print("INVESTIGATOR")
    print("=" * 78)
    print(result["investigation"]["answer"])
    if result.get("verification"):
        print("\n" + "=" * 78)
        print("VERIFIER (independent re-check)")
        print("=" * 78)
        print(result["verification"]["answer"])
    if args.save:
        path = Path(args.save)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(pipeline_markdown(result), encoding="utf-8")
        print(f"\nSaved pipeline report to {path}")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from . import evaluate

    report = evaluate.run(tenant=args.tenant)
    print(evaluate.render(report))
    if args.save:
        path = Path(args.save)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Saved scorecard to {path}")
    return 0 if report["passed"] == report["total"] else 1


def cmd_memory(args: argparse.Namespace) -> int:
    db.set_tenant(args.tenant)
    tenant = db.resolve_tenant()
    books = memory.playbooks(tenant, limit=args.limit)
    notes = memory.recall("", args.kind, tenant, limit=args.limit)
    cases = memory.open_cases(tenant, limit=args.limit)

    print(f"Memory — tenant '{tenant}' (graph '{db.graph_name_for(tenant)}')")
    print("\nPROCEDURAL — learned playbooks")
    if books:
        for book in books:
            print(f"  * {book['note_id']}  {book['text'][:160]}")
    else:
        print("  (none yet — run an investigation to learn one)")

    print(f"\nNOTES — durable memory{' (kind=' + args.kind + ')' if args.kind else ''}")
    if notes:
        for note in notes:
            about = f" -> {', '.join(note['about'])}" if note["about"] else ""
            print(f"  * [{note['kind']}] {note['note_id']}  {note['text'][:140]}{about}")
    else:
        print("  (none)")

    print("\nOPEN CASES")
    if cases:
        for case in cases:
            print(f"  * {case['case_id']} | {case['title']} | {case['summary'][:120]}")
    else:
        print("  (none)")
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    report = bench_module.run(
        tenant=args.tenant, writers=args.writers, readers=args.readers, seconds=args.seconds
    )
    print(bench_module.render(report))
    if args.save:
        path = Path(args.save)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Saved raw numbers to {path}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    serve(host=args.host, port=args.port)
    return 0


def cmd_company(args: argparse.Namespace) -> int:
    from . import company

    db.set_tenant(args.tenant)
    tenant = db.resolve_tenant()
    if not args.keep:
        company.seed(reset=True, verbose=not args.quiet)
    print(f"Company brain — tenant '{tenant}' (graph '{db.graph_name_for(tenant)}')\n")
    report = company.run_demo(
        system_id=args.system, decision_id=args.decision, verbose=not args.quiet
    )
    if args.save:
        path = Path(args.save)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding="utf-8")
        print(f"\nSaved company-brain report to {path}")
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    agent = GraphSleuth(tenant=args.tenant, session_id=args.session, model=args.model)
    print(f"GraphSleuth ready. session={agent.session_id} tenant={agent.tenant}")
    print("Type a question, /evidence for the last trail, /new for a fresh session, /exit to quit.\n")
    last: dict | None = None
    while True:
        try:
            question = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not question:
            continue
        if question in {"/exit", "/quit"}:
            return 0
        if question == "/new":
            agent = GraphSleuth(tenant=args.tenant)
            print(f"New session {agent.session_id}\n")
            continue
        if question == "/evidence":
            print(explain.render_trace(agent.history) if agent.history else "No tool calls yet.")
            continue
        result = agent.ask(question)
        last = result
        print("\n" + result["answer"] + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="graphsleuth", description="Fraud investigation agent on FalkorDB")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="check FalkorDB connectivity")
    p_status.set_defaults(func=cmd_status)

    p_seed = sub.add_parser("seed", help="build the synthetic fraud graph")
    p_seed.add_argument("--keep", action="store_true", help="append instead of wiping first")
    p_seed.add_argument("--tenant", default=memory.DEFAULT_TENANT, help="seed this tenant's graph")
    p_seed.set_defaults(func=cmd_seed)

    p_demo = sub.add_parser("demo", help="run the end-to-end investigation (live LLM if configured)")
    p_demo.add_argument("--target", default="ACC-9003", help="account or entity id to investigate")
    p_demo.add_argument("--tenant", default=memory.DEFAULT_TENANT)
    p_demo.add_argument("--save", default="", help="write a markdown report to this path")
    p_demo.add_argument("--offline", action="store_true", help="force the scripted fallback brain")
    p_demo.add_argument("--model", default=None, help="pin the live brain to one model (skip the fallback chain)")
    p_demo.set_defaults(func=cmd_demo)

    p_pipe = sub.add_parser("pipeline", help="two agents: triage -> handoff -> investigator")
    p_pipe.add_argument("--target", default="ACC-9003", help="account or entity id to triage")
    p_pipe.add_argument("--tenant", default=memory.DEFAULT_TENANT)
    p_pipe.add_argument("--save", default="", help="write the combined markdown report here")
    p_pipe.add_argument("--quiet", action="store_true", help="hide per-tool-call logs")
    p_pipe.add_argument("--offline", action="store_true", help="force the scripted fallback brains")
    p_pipe.add_argument("--verify", action="store_true", help="add a third adversarial agent that re-checks the verdict")
    p_pipe.add_argument("--model", default=None, help="pin the live brain to one model (skip the fallback chain)")
    p_pipe.set_defaults(func=cmd_pipeline)

    p_bench = sub.add_parser("bench", help="high-concurrency read/write benchmark")
    p_bench.add_argument("--writers", type=int, default=4)
    p_bench.add_argument("--readers", type=int, default=16)
    p_bench.add_argument("--seconds", type=float, default=6.0)
    p_bench.add_argument("--tenant", default=memory.DEFAULT_TENANT)
    p_bench.add_argument("--save", default="", help="write raw JSON numbers here")
    p_bench.set_defaults(func=cmd_bench)

    p_mem = sub.add_parser("memory", help="inspect agent memory: playbooks, notes, open cases")
    p_mem.add_argument("--tenant", default=memory.DEFAULT_TENANT)
    p_mem.add_argument("--kind", default="", help="filter notes: episodic | semantic | procedural")
    p_mem.add_argument("--limit", type=int, default=10)
    p_mem.set_defaults(func=cmd_memory)

    p_eval = sub.add_parser("eval", help="run the reproducible evaluation scorecard")
    p_eval.add_argument("--tenant", default=memory.DEFAULT_TENANT)
    p_eval.add_argument("--save", default="", help="write the scorecard JSON here")
    p_eval.set_defaults(func=cmd_eval)

    p_chat = sub.add_parser("chat", help="interactive investigation session")
    p_chat.add_argument("--tenant", default=memory.DEFAULT_TENANT)
    p_chat.add_argument("--session", default=None, help="reuse an existing session id")
    p_chat.add_argument("--model", default=None, help="pin the live brain to one model (skip the fallback chain)")
    p_chat.set_defaults(func=cmd_chat)

    p_serve = sub.add_parser("serve", help="interactive web console: stream the investigation + graph")
    p_serve.add_argument("--host", default="127.0.0.1", help="bind address (default localhost only)")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.set_defaults(func=cmd_serve)

    from . import company as _company

    p_company = sub.add_parser(
        "company", help="Company Brain (Track 03): who knows what, ownership, blast radius, decisions"
    )
    p_company.add_argument("--tenant", default=_company.DEFAULT_TENANT, help="company brain tenant graph")
    p_company.add_argument("--system", default="SYS-ledger-db", help="system to focus the tour on")
    p_company.add_argument("--decision", default="ADR-002", help="decision to trace back to its source")
    p_company.add_argument("--keep", action="store_true", help="use the existing graph instead of re-seeding")
    p_company.add_argument("--save", default="", help="write the markdown tour to this path")
    p_company.add_argument("--quiet", action="store_true", help="only write the report, do not print it")
    p_company.set_defaults(func=cmd_company)

    return parser


def _use_utf8_output() -> None:
    """Make console output survive any model-authored text on Windows.

    Live models emit typography the Windows console (cp1252) cannot encode —
    non-breaking hyphens, arrows, emoji — which would crash a print() mid-report.
    Reconfigure the streams once, replacing the few un-encodable glyphs.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - never let cosmetics break a run
            pass


def main(argv: list[str] | None = None) -> int:
    _use_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
