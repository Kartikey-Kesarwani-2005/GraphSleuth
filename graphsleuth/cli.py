"""Command line interface.

    python -m graphsleuth status                 # is FalkorDB reachable?
    python -m graphsleuth seed [--keep]          # build the synthetic fraud graph
    python -m graphsleuth demo [--target ACC-9003] [--save report.md]
    python -m graphsleuth chat [--tenant acme] [--session SES-1234]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import db, explain, memory, seed as seed_module
from .agent import GraphSleuth, build_demo_brain
from .config import config


def cmd_status(_args: argparse.Namespace) -> int:
    try:
        info = db.ping()
    except Exception as exc:  # noqa: BLE001
        print(f"FalkorDB NOT reachable at {config.falkordb_host}:{config.falkordb_port}")
        print(f"  reason: {type(exc).__name__}: {exc}")
        print("  start it with:  docker compose up -d")
        return 1
    print(f"FalkorDB OK  ->  {info['host']}  (graph '{info['graph']}')")
    print(f"  nodes: {info['nodes']}   relationships: {info['relationships']}")
    print(f"  LLM: {'live (' + config.openai_model + ')' if config.use_live_llm() else 'scripted demo brain'}")
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    stats = seed_module.seed(reset=not args.keep)
    print(f"Done. {stats['nodes']} nodes / {stats['relationships']} relationships.")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    question = f"Investigate {args.target} — is this account part of a fraud ring?"
    print(f"Question: {question}\n")
    agent = GraphSleuth(tenant=args.tenant, brain=build_demo_brain(args.target))
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
    p_seed.set_defaults(func=cmd_seed)

    p_demo = sub.add_parser("demo", help="run the scripted end-to-end investigation")
    p_demo.add_argument("--target", default="ACC-9003", help="account or entity id to investigate")
    p_demo.add_argument("--tenant", default=memory.DEFAULT_TENANT)
    p_demo.add_argument("--save", default="", help="write a markdown report to this path")
    p_demo.set_defaults(func=cmd_demo)

    p_chat = sub.add_parser("chat", help="interactive investigation session")
    p_chat.add_argument("--tenant", default=memory.DEFAULT_TENANT)
    p_chat.add_argument("--session", default=None, help="reuse an existing session id")
    p_chat.set_defaults(func=cmd_chat)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
