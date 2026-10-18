"""GraphSleuth web console — a zero-dependency investigation UI.

    python -m graphsleuth serve            # then open http://127.0.0.1:8765

Why this exists: the agent's whole value is *the path behind the answer*, and a
terminal transcript hides that. This serves a small single-page app that:

* streams the agent's live reasoning (every tool call and result) as it happens,
* grows a force-directed graph of the entities it touches, and
* shows the verdict, the verified write-actions and the tenant's memory.

No new dependencies. The HTTP server is Python's own `http.server`; the front
end is plain HTML/CSS/JS (no CDN, so it works offline on a judge's laptop).
The stream is newline-delimited JSON over a chunked response, so the browser
sees each tool call the moment the agent makes it.

Multigraph: every request carries a `tenant`, and the whole request runs inside
`db.tenant_scope(tenant)`, so the console can show two tenants side by side and
neither can read the other's graph.

**Trust model** — there is no authentication: the tenant is chosen by the caller
and every endpoint can write to the graph. It binds loopback (`127.0.0.1`) by
default for exactly that reason; keep it there or front it with something that
authenticates.
"""

from __future__ import annotations

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from . import db
from . import explain
from . import llm
from . import memory
from . import tools
from .agent import GraphSleuth, build_demo_brain
from .config import config

WEB_DIR = Path(__file__).resolve().parent / "web"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}


# --------------------------------------------------------------------------- #
# request handlers
# --------------------------------------------------------------------------- #
def _status() -> dict:
    info = db.ping()
    return {
        "falkordb": info,
        "llm": {
            "live": config.use_live_llm(),
            "provider": config.llm_provider,
            "chain": config.llm_models,
            "fallback_chain": getattr(llm, "FALLBACK_CHAIN", []),
        },
        "mcp": {"tools": len(tools.ALL_TOOL_SPECS)},
        "version": __version__,
    }


def _graph(target: str, tenant: str, depth: int = 2) -> dict:
    """A snapshot subgraph around one entity, for the initial canvas."""
    from .tools import graph_neighbors

    with db.tenant_scope(tenant):
        data = graph_neighbors(target, depth=depth)
    nodes, edges = {}, []
    nodes[target] = target.split("-")[0]
    for node in data.get("nodes", []):
        labels = node.get("labels") or []
        nodes[str(node.get("id"))] = labels[0] if labels else str(node.get("id")).split("-")[0]
    for edge in data.get("edges", []):
        edges.append({"from": str(edge.get("from")), "to": str(edge.get("to")), "type": str(edge.get("type"))})
    return {"nodes": _node_list(nodes, target), "edges": edges, "focal": target, "ring": []}


def _memory_view(tenant: str) -> dict:
    with db.tenant_scope(tenant):
        notes = memory.recall("", "", tenant=tenant, limit=15)
        cases = memory.open_cases(tenant=tenant, limit=8)
        from .tools import list_alerts

        alerts = list_alerts(limit=10).get("alerts", [])
    return {"notes": notes, "cases": cases, "alerts": alerts}


def _stress(entity: str, tenant: str, from_id: str = "", to_id: str = "") -> dict:
    """Live counterfactual: mentally remove one entity and re-check the case.

    Read-only (what_if_removed never writes); runs inside the tenant's own graph.
    """
    from .tools import what_if_removed

    with db.tenant_scope(tenant):
        return what_if_removed(
            entity,
            from_id=from_id or "ACC-EXT-01",
            to_id=to_id or "ACC-9100",
            max_depth=8,
        )


def _node_list(nodes: dict[str, str], focal: str, ring: list[str] | None = None) -> list[dict]:
    focal = focal or ""
    ring = set(ring or [])
    out = []
    for node_id, label in nodes.items():
        if not node_id or node_id == "__ring__":
            continue
        kind = "focal" if node_id == focal else ("ring" if node_id in ring else "normal")
        out.append({"id": node_id, "label": label, "kind": kind})
    return out


def _derive_ring(trace: list[dict]) -> list[str]:
    for entry in reversed(trace):
        if entry.get("tool") == "detect_suspicious_cluster":
            clusters = (entry.get("result") or {}).get("clusters") or []
            if clusters:
                return [m.get("account") for m in clusters[0].get("members", [])]
    return []


def _result_payload(result: dict) -> dict:
    nodes, edges = explain.collect_graph_facts(result["trace"])
    ring = _derive_ring(result["trace"])
    return {
        "type": "done",
        "answer": result["answer"],
        "rounds": result["rounds"],
        "session_id": result["session_id"],
        "tenant": result["tenant"],
        "provider": result["provider"],
        "brain": result["brain"],
        "actions": explain.verified_actions(result["trace"]),
        "graph": {
            "nodes": _node_list(nodes, "", ring),
            "edges": [{"from": a, "to": b, "type": t} for a, b, t in edges],
            "focal": "",
            "ring": ring,
        },
    }


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "GraphSleuth"
    protocol_version = "HTTP/1.1"

    # -- helpers ----------------------------------------------------------- #
    def log_message(self, fmt: str, *args: Any) -> None:  # quieter console
        return

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self._send_json({"error": "not found"}, 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        # The console is a dev tool: never let the browser cache app.js/index.html,
        # or a stale copy silently hides new behaviour (e.g. the company mode).
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def _sse_stream(self, target: str, tenant: str, question: str, offline: bool, mode: str = "fraud") -> None:
        """Stream the agent's live tool calls as newline-delimited JSON."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        events: queue.Queue = queue.Queue()

        def on_event(event: dict) -> None:
            events.put(event)

        def worker() -> None:
            try:
                with db.tenant_scope(tenant):
                    if mode == "company":
                        # Track 03: the company-brain agent (org tools, org prompt).
                        from . import company

                        company.ensure_seeded()  # a judge can ask without running the CLI
                        result = company.chat(
                            question,
                            tenant=tenant,
                            verbose=False,
                            on_event=on_event,
                            offline=offline,
                        )
                    else:
                        agent = GraphSleuth(
                            tenant=tenant,
                            verbose=False,
                            on_event=on_event,
                            brain=build_demo_brain(target) if offline else None,
                            scripted=build_demo_brain(target),
                        )
                        result = agent.ask(question)
                events.put(_result_payload(result))
            except Exception as exc:  # noqa: BLE001 - stream the error to the UI
                events.put({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
            finally:
                events.put(None)

        threading.Thread(target=worker, daemon=True).start()

        while True:
            event = events.get()
            if event is None:
                break
            try:
                self.wfile.write((json.dumps(event, default=str) + "\n").encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

    # -- routing ----------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        route = parsed.path
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if route in ("/", "/index.html"):
                self._send_file(WEB_DIR / "index.html")
            elif route in ("/app.css", "/app.js"):
                self._send_file(WEB_DIR / route.lstrip("/"))
            elif route == "/api/status":
                self._send_json(_status())
            elif route == "/api/graph":
                self._send_json(_graph(params.get("target", "ACC-9003"), params.get("tenant", ""), int(params.get("depth", "2") or 2)))
            elif route == "/api/memory":
                self._send_json(_memory_view(params.get("tenant", "")))
            elif route == "/api/stress":
                self._send_json(
                    _stress(
                        params.get("entity", ""),
                        params.get("tenant", ""),
                        params.get("from", ""),
                        params.get("to", ""),
                    )
                )
            elif route == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send_json({"error": "not found", "route": route}, 404)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {}
        if urlparse(self.path).path != "/api/investigate":
            self._send_json({"error": "not found"}, 404)
            return
        mode = str(body.get("mode") or "fraud").strip().lower()
        if mode == "company":
            target = str(body.get("target") or "SYS-ledger-db").strip()
            tenant = str(body.get("tenant") or "company").strip()
            question = str(body.get("question") or f"Who knows {target}, and what breaks if it goes down?")
        else:
            target = str(body.get("target") or "ACC-9003").strip()
            tenant = str(body.get("tenant") or "").strip()
            question = str(body.get("question") or f"Investigate {target} — is this account part of a fraud ring?")
        try:
            self._sse_stream(target, tenant, question, bool(body.get("offline")), mode)
        except (BrokenPipeError, ConnectionResetError):
            return


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Serve the console on `host:port` (loopback by default).

    There is no authentication anywhere in this server: whoever can reach the
    port can read *and write* any tenant graph, and the tenant is chosen by the
    caller (`?tenant=` / the request body). That is fine on loopback and nowhere
    else, so binding a routable interface warns.
    """
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            f"WARNING: the console has NO authentication. Anyone who can reach {host}:{port} can "
            "read and write every tenant graph. Bind 127.0.0.1 (or put it behind a proxy that "
            "authenticates) unless that exposure is intentional."
        )
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"GraphSleuth console  ->  http://{host}:{port}")
    print("  Ctrl+C to stop. The graph is FalkorDB; the agent picks its own tools.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="graphsleuth-serve", description="GraphSleuth web console")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="interface to bind (loopback by default — this server has no authentication)",
    )
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
