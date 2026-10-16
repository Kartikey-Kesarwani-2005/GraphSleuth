"""Command line interface.

    python -m graphsleuth status                    # FalkorDB + multigraph + LLM check
    python -m graphsleuth seed [--tenant acme]      # build the synthetic fraud graph
    python -m graphsleuth demo [--target ACC-9003] [--save report.md]
    python -m graphsleuth pipeline [--target ACC-9003]   # triage -> handoff -> investigator
    python -m graphsleuth bench [--writers 4 --readers 16]
    python -m graphsleuth chat [--tenant acme] [--session SES-1234]
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
        print(f"  LLM: LIVE ({config.llm_provider})")
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
    result = run_pipeline(args.target, tenant=args.tenant, verbose=not args.quiet, offline=args.offline)
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
    if args.save:
        path = Path(args.save)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(pipeline_markdown(result), encoding="utf-8")
        print(f"\nSaved pipeline report to {path}")
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


def cmd_chat(args: argparse.Namespace) -> int:
    agent = GraphSleuth(tenant=args.tenant, session_id=args.session)
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
    p_demo.set_defaults(func=cmd_demo)

    p_pipe = sub.add_parser("pipeline", help="two agents: triage -> handoff -> investigator")
    p_pipe.add_argument("--target", default="ACC-9003", help="account or entity id to triage")
    p_pipe.add_argument("--tenant", default=memory.DEFAULT_TENANT)
    p_pipe.add_argument("--save", default="", help="write the combined markdown report here")
    p_pipe.add_argument("--quiet", action="store_true", help="hide per-tool-call logs")
    p_pipe.add_argument("--offline", action="store_true", help="force the scripted fallback brains")
    p_pipe.set_defaults(func=cmd_pipeline)

    p_bench = sub.add_parser("bench", help="high-concurrency read/write benchmark")
    p_bench.add_argument("--writers", type=int, default=4)
    p_bench.add_argument("--readers", type=int, default=16)
    p_bench.add_argument("--seconds", type=float, default=6.0)
    p_bench.add_argument("--tenant", default=memory.DEFAULT_TENANT)
    p_bench.add_argument("--save", default="", help="write raw JSON numbers here")
    p_bench.set_defaults(func=cmd_bench)

    p_chat = sub.add_parser("chat", help="interactive investigation session")
    p_chat.add_argument("--tenant", default=memory.DEFAULT_TENANT)
    p_chat.add_argument("--session", default=None, help="reuse an existing session id")
    p_chat.set_defaults(func=cmd_chat)

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
