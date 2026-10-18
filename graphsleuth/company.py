"""Company Brain — a second, self-contained graph for Track 03.

The fraud graph answers *"is this account part of a ring?"*. This module answers the
Track 03 questions about a company instead:

* **who knows about a system** — the brief's exact `Person-[:WORKED_ON]->Project-[:USES]->System`
  query, plus explicitly declared expertise (`:KNOWS`);
* **who owns what** — teams own projects, people own systems;
* **what breaks if a service goes down** — the reverse transitive closure of `:DEPENDS_ON`;
* **why a decision was made** — traced back to the doc / ticket / chat it was sourced from;
* **what knowledge is going stale** — superseded decisions, ageing docs, and bus-factor risk.

Everything lives in its own FalkorDB graph (`graphsleuth:company`), so the company brain and
the fraud demo never mix — the same one-graph-per-tenant multigraph pattern the brief asks for.

    python -m graphsleuth company                       # seed + run the whole tour
    python -m graphsleuth company --system SYS-ledger-db
    python -m graphsleuth company --save demos/COMPANY_BRAIN.md

The dataset is hand-authored (no RNG), so every number in the tour is reproducible.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db

# The company brain lives in its own tenant graph, never mixed with the fraud demo.
DEFAULT_TENANT = "company"

# A fixed "now" so staleness is deterministic (the fraud seed pins its day the same way).
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)

# The question the hackathon brief puts on the front page, kept verbatim as a tool query.
# (Note `count(pr)`, not `count(*)`: on this FalkorDB build `count(*)` collapses the rows and
#  reports 1 even when a person matched several projects — counting a bound variable is correct.)
WHO_KNOWS_QUERY = """
MATCH (p:Person)-[:WORKED_ON]->(pr:Project)-[:USES]->(s:System {id: $system})
WITH p, count(pr) AS projects
ORDER BY projects DESC, p.id
RETURN [p.id, p.name, projects] AS row
""".strip()


def _ts(days: float) -> str:
    return (NOW + timedelta(days=days)).isoformat(timespec="seconds")


# --- the company -------------------------------------------------------------
# Hand-authored so the graph has *shape* worth querying: a critical service with a
# bus factor of one, a deep dependency chain, and a decision that was later reversed.

PEOPLE = [
    {"id": "EMP-001", "name": "Aarav Sharma", "role": "Staff Backend Engineer"},
    {"id": "EMP-002", "name": "Isha Verma", "role": "Backend Engineer"},
    {"id": "EMP-003", "name": "Rohan Patel", "role": "Site Reliability Engineer"},
    {"id": "EMP-004", "name": "Meera Nair", "role": "Data Engineer"},
    {"id": "EMP-005", "name": "Kabir Khan", "role": "Mobile Engineer"},
    {"id": "EMP-006", "name": "Anaya Reddy", "role": "Platform Engineer"},
    {"id": "EMP-007", "name": "Vihaan Singh", "role": "Engineering Manager"},
    {"id": "EMP-008", "name": "Diya Gupta", "role": "Product Manager"},
    {"id": "EMP-009", "name": "Arjun Rao", "role": "Security Engineer"},
    {"id": "EMP-010", "name": "Saanvi Iyer", "role": "Backend Engineer"},
    {"id": "EMP-011", "name": "Dev Mehta", "role": "Data Engineer"},
    {"id": "EMP-012", "name": "Nisha Kapoor", "role": "Engineering Manager"},
]

TEAMS = [
    {"id": "TEAM-payments", "name": "Payments Platform"},
    {"id": "TEAM-platform", "name": "Platform & Reliability"},
    {"id": "TEAM-identity", "name": "Identity & Access"},
    {"id": "TEAM-growth", "name": "Growth & Insights"},
]

PROJECTS = [
    {"id": "PROJ-payments-core", "name": "Payments Core"},
    {"id": "PROJ-checkout", "name": "Checkout Revamp"},
    {"id": "PROJ-ledger", "name": "Ledger Migration"},
    {"id": "PROJ-observability", "name": "Observability Uplift"},
    {"id": "PROJ-auth", "name": "SSO Rollout"},
    {"id": "PROJ-analytics", "name": "Analytics Pipelines"},
]

SYSTEMS = [
    {"id": "SYS-payments-api", "name": "Payments API", "criticality": "critical"},
    {"id": "SYS-ledger-db", "name": "Ledger DB", "criticality": "critical"},
    {"id": "SYS-auth-service", "name": "Auth Service", "criticality": "high"},
    {"id": "SYS-mobile-bff", "name": "Mobile BFF", "criticality": "high"},
    {"id": "SYS-kafka-bus", "name": "Event Bus", "criticality": "critical"},
    {"id": "SYS-notifications", "name": "Notification Service", "criticality": "medium"},
    {"id": "SYS-analytics-etl", "name": "Analytics ETL", "criticality": "medium"},
    {"id": "SYS-observability-stack", "name": "Observability Stack", "criticality": "high"},
]

DOCS = [
    {"id": "DOC-001", "title": "Payments API design", "updated_at": _ts(-540)},
    {"id": "DOC-002", "title": "Ledger schema reference", "updated_at": _ts(-45)},
    {"id": "DOC-003", "title": "Event bus topic catalog", "updated_at": _ts(-120)},
    {"id": "DOC-004", "title": "Auth threat model", "updated_at": _ts(-30)},
    {"id": "DOC-005", "title": "Mobile BFF runbook", "updated_at": _ts(-400)},
    {"id": "DOC-006", "title": "Observability onboarding", "updated_at": _ts(-60)},
    {"id": "DOC-007", "title": "Analytics pipeline map", "updated_at": _ts(-210)},
    {"id": "DOC-008", "title": "Notification templates", "updated_at": _ts(-15)},
]

TICKETS = [
    {"id": "TKT-101", "title": "Payments API 2x latency at peak", "status": "open", "opened_at": _ts(-20)},
    {"id": "TKT-102", "title": "Ledger DB migration to schema v9", "status": "in_progress", "opened_at": _ts(-60)},
    {"id": "TKT-103", "title": "SSO for the mobile app", "status": "open", "opened_at": _ts(-35)},
    {"id": "TKT-104", "title": "Kafka broker upgrade", "status": "closed", "opened_at": _ts(-150)},
    {"id": "TKT-105", "title": "Analytics backfill job", "status": "open", "opened_at": _ts(-25)},
    {"id": "TKT-106", "title": "Observability cost reduction", "status": "open", "opened_at": _ts(-12)},
]

CHATS = [
    {"id": "CHAT-201", "text": "we should rate-limit payments-api before the sale", "ts": _ts(-18)},
    {"id": "CHAT-202", "text": "ledger-db failover drill is green again", "ts": _ts(-9)},
    {"id": "CHAT-203", "text": "legacy auth token rotation is painful, revisit SSO", "ts": _ts(-40)},
    {"id": "CHAT-204", "text": "kafka consumer lag alert keeps firing", "ts": _ts(-130)},
    {"id": "CHAT-205", "text": "mobile BFF cache is doing nothing, revisit", "ts": _ts(-22)},
    {"id": "CHAT-206", "text": "observability dashboards shipped for all services", "ts": _ts(-70)},
]

DECISIONS = [
    {"id": "ADR-001", "title": "Adopt the event bus for async workflows", "status": "accepted", "decided_at": _ts(-140)},
    {"id": "ADR-002", "title": "Migrate the ledger to schema v9", "status": "accepted", "decided_at": _ts(-50)},
    {"id": "ADR-003", "title": "Replace in-house login with SSO", "status": "accepted", "decided_at": _ts(-28)},
    {"id": "ADR-004", "title": "Keep the in-house login service", "status": "superseded", "decided_at": _ts(-365)},
    {"id": "ADR-005", "title": "Rate-limit the Payments API", "status": "accepted", "decided_at": _ts(-16)},
]

# Edge list: (relationship, source id, target id). Endpoints are validated by tests.
EDGES = [
    # membership + ownership
    ("MEMBER_OF", "EMP-001", "TEAM-payments"),
    ("MEMBER_OF", "EMP-002", "TEAM-payments"),
    ("MEMBER_OF", "EMP-010", "TEAM-payments"),
    ("MEMBER_OF", "EMP-003", "TEAM-platform"),
    ("MEMBER_OF", "EMP-007", "TEAM-platform"),
    ("MEMBER_OF", "EMP-011", "TEAM-platform"),
    ("MEMBER_OF", "EMP-005", "TEAM-identity"),
    ("MEMBER_OF", "EMP-009", "TEAM-identity"),
    ("MEMBER_OF", "EMP-004", "TEAM-growth"),
    ("MEMBER_OF", "EMP-006", "TEAM-growth"),
    ("MEMBER_OF", "EMP-008", "TEAM-growth"),
    ("MEMBER_OF", "EMP-012", "TEAM-growth"),
    ("OWNS", "TEAM-payments", "PROJ-payments-core"),
    ("OWNS", "TEAM-payments", "PROJ-checkout"),
    ("OWNS", "TEAM-platform", "PROJ-ledger"),
    ("OWNS", "TEAM-platform", "PROJ-observability"),
    ("OWNS", "TEAM-identity", "PROJ-auth"),
    ("OWNS", "TEAM-growth", "PROJ-analytics"),
    ("OWNS", "EMP-001", "SYS-payments-api"),
    ("OWNS", "EMP-003", "SYS-ledger-db"),
    ("OWNS", "EMP-003", "SYS-kafka-bus"),
    ("OWNS", "EMP-009", "SYS-auth-service"),
    ("OWNS", "EMP-005", "SYS-mobile-bff"),
    ("OWNS", "EMP-006", "SYS-notifications"),
    ("OWNS", "EMP-004", "SYS-analytics-etl"),
    ("OWNS", "EMP-007", "SYS-observability-stack"),
    # who worked on what, and what those projects use
    ("WORKED_ON", "EMP-001", "PROJ-payments-core"),
    ("WORKED_ON", "EMP-002", "PROJ-payments-core"),
    ("WORKED_ON", "EMP-010", "PROJ-payments-core"),
    ("WORKED_ON", "EMP-005", "PROJ-checkout"),
    ("WORKED_ON", "EMP-008", "PROJ-checkout"),
    ("WORKED_ON", "EMP-010", "PROJ-checkout"),
    ("WORKED_ON", "EMP-001", "PROJ-ledger"),
    ("WORKED_ON", "EMP-003", "PROJ-ledger"),
    ("WORKED_ON", "EMP-007", "PROJ-observability"),
    ("WORKED_ON", "EMP-003", "PROJ-observability"),
    ("WORKED_ON", "EMP-009", "PROJ-auth"),
    ("WORKED_ON", "EMP-005", "PROJ-auth"),
    ("WORKED_ON", "EMP-004", "PROJ-analytics"),
    ("WORKED_ON", "EMP-011", "PROJ-analytics"),
    ("USES", "PROJ-payments-core", "SYS-payments-api"),
    ("USES", "PROJ-payments-core", "SYS-ledger-db"),
    ("USES", "PROJ-checkout", "SYS-payments-api"),
    ("USES", "PROJ-checkout", "SYS-mobile-bff"),
    ("USES", "PROJ-ledger", "SYS-ledger-db"),
    ("USES", "PROJ-ledger", "SYS-kafka-bus"),
    ("USES", "PROJ-observability", "SYS-observability-stack"),
    ("USES", "PROJ-auth", "SYS-auth-service"),
    ("USES", "PROJ-analytics", "SYS-analytics-etl"),
    ("USES", "PROJ-analytics", "SYS-kafka-bus"),
    # explicit expertise: who *knows* a system
    ("KNOWS", "EMP-001", "SYS-payments-api"),
    ("KNOWS", "EMP-002", "SYS-payments-api"),
    ("KNOWS", "EMP-010", "SYS-payments-api"),
    ("KNOWS", "EMP-003", "SYS-ledger-db"),
    ("KNOWS", "EMP-001", "SYS-ledger-db"),
    ("KNOWS", "EMP-009", "SYS-auth-service"),
    ("KNOWS", "EMP-005", "SYS-mobile-bff"),
    ("KNOWS", "EMP-003", "SYS-kafka-bus"),
    ("KNOWS", "EMP-006", "SYS-kafka-bus"),
    ("KNOWS", "EMP-006", "SYS-notifications"),
    ("KNOWS", "EMP-004", "SYS-analytics-etl"),
    ("KNOWS", "EMP-011", "SYS-analytics-etl"),
    ("KNOWS", "EMP-007", "SYS-observability-stack"),
    # runtime dependencies (what breaks if a service goes down)
    ("DEPENDS_ON", "SYS-payments-api", "SYS-ledger-db"),
    ("DEPENDS_ON", "SYS-payments-api", "SYS-kafka-bus"),
    ("DEPENDS_ON", "SYS-auth-service", "SYS-ledger-db"),
    ("DEPENDS_ON", "SYS-mobile-bff", "SYS-payments-api"),
    ("DEPENDS_ON", "SYS-mobile-bff", "SYS-auth-service"),
    ("DEPENDS_ON", "SYS-notifications", "SYS-kafka-bus"),
    ("DEPENDS_ON", "SYS-analytics-etl", "SYS-kafka-bus"),
    ("DEPENDS_ON", "SYS-observability-stack", "SYS-kafka-bus"),
    # knowledge sources
    ("ABOUT", "DOC-001", "SYS-payments-api"),
    ("ABOUT", "DOC-002", "SYS-ledger-db"),
    ("ABOUT", "DOC-003", "SYS-kafka-bus"),
    ("ABOUT", "DOC-004", "SYS-auth-service"),
    ("ABOUT", "DOC-005", "SYS-mobile-bff"),
    ("ABOUT", "DOC-006", "SYS-observability-stack"),
    ("ABOUT", "DOC-007", "SYS-analytics-etl"),
    ("ABOUT", "DOC-008", "SYS-notifications"),
    ("AUTHORED_BY", "DOC-001", "EMP-001"),
    ("AUTHORED_BY", "DOC-002", "EMP-002"),
    ("AUTHORED_BY", "DOC-003", "EMP-003"),
    ("AUTHORED_BY", "DOC-004", "EMP-009"),
    ("AUTHORED_BY", "DOC-005", "EMP-005"),
    ("AUTHORED_BY", "DOC-006", "EMP-007"),
    ("AUTHORED_BY", "DOC-007", "EMP-004"),
    ("AUTHORED_BY", "DOC-008", "EMP-006"),
    ("ABOUT", "TKT-101", "SYS-payments-api"),
    ("ABOUT", "TKT-102", "SYS-ledger-db"),
    ("ABOUT", "TKT-103", "SYS-auth-service"),
    ("ABOUT", "TKT-104", "SYS-kafka-bus"),
    ("ABOUT", "TKT-105", "SYS-analytics-etl"),
    ("ABOUT", "TKT-106", "SYS-observability-stack"),
    ("ASSIGNED_TO", "TKT-101", "EMP-002"),
    ("ASSIGNED_TO", "TKT-102", "EMP-001"),
    ("ASSIGNED_TO", "TKT-103", "EMP-009"),
    ("ASSIGNED_TO", "TKT-104", "EMP-003"),
    ("ASSIGNED_TO", "TKT-105", "EMP-004"),
    ("ASSIGNED_TO", "TKT-106", "EMP-007"),
    ("MENTIONS", "CHAT-201", "SYS-payments-api"),
    ("MENTIONS", "CHAT-202", "SYS-ledger-db"),
    ("MENTIONS", "CHAT-203", "SYS-auth-service"),
    ("MENTIONS", "CHAT-204", "SYS-kafka-bus"),
    ("MENTIONS", "CHAT-205", "SYS-mobile-bff"),
    ("MENTIONS", "CHAT-206", "SYS-observability-stack"),
    ("BY", "CHAT-201", "EMP-007"),
    ("BY", "CHAT-202", "EMP-003"),
    ("BY", "CHAT-203", "EMP-009"),
    ("BY", "CHAT-204", "EMP-006"),
    ("BY", "CHAT-205", "EMP-005"),
    ("BY", "CHAT-206", "EMP-007"),
    ("AFFECTS", "ADR-001", "SYS-kafka-bus"),
    ("AFFECTS", "ADR-002", "SYS-ledger-db"),
    ("AFFECTS", "ADR-003", "SYS-auth-service"),
    ("AFFECTS", "ADR-004", "SYS-auth-service"),
    ("AFFECTS", "ADR-005", "SYS-payments-api"),
    ("DECIDED_BY", "ADR-001", "EMP-007"),
    ("DECIDED_BY", "ADR-002", "EMP-001"),
    ("DECIDED_BY", "ADR-003", "EMP-009"),
    ("DECIDED_BY", "ADR-004", "EMP-007"),
    ("DECIDED_BY", "ADR-005", "EMP-007"),
    ("SOURCED_FROM", "ADR-001", "DOC-003"),
    ("SOURCED_FROM", "ADR-001", "TKT-104"),
    ("SOURCED_FROM", "ADR-002", "DOC-002"),
    ("SOURCED_FROM", "ADR-002", "TKT-102"),
    ("SOURCED_FROM", "ADR-003", "DOC-004"),
    ("SOURCED_FROM", "ADR-003", "TKT-103"),
    ("SOURCED_FROM", "ADR-004", "CHAT-203"),
    ("SOURCED_FROM", "ADR-005", "CHAT-201"),
    ("SOURCED_FROM", "ADR-005", "TKT-101"),
    ("SUPERSEDES", "ADR-003", "ADR-004"),
]


def build() -> dict[str, Any]:
    """Return the whole dataset as plain data (pure — no database needed)."""
    nodes: dict[str, dict] = {}
    for bucket, label in (
        (PEOPLE, "Person"),
        (TEAMS, "Team"),
        (PROJECTS, "Project"),
        (SYSTEMS, "System"),
        (DOCS, "Doc"),
        (TICKETS, "Ticket"),
        (CHATS, "Chat"),
        (DECISIONS, "Decision"),
    ):
        for item in bucket:
            nodes[item["id"]] = {"label": label, **item}
    return {
        "people": PEOPLE,
        "teams": TEAMS,
        "projects": PROJECTS,
        "systems": SYSTEMS,
        "docs": DOCS,
        "tickets": TICKETS,
        "chats": CHATS,
        "decisions": DECISIONS,
        "edges": EDGES,
        "nodes": nodes,
    }


# --- loading -----------------------------------------------------------------

# Labels indexed for id lookups, and the label -> rows map the loader walks.
INDEX_LABELS = ("Person", "Team", "Project", "System", "Doc", "Ticket", "Chat", "Decision")


def seed(reset: bool = True, verbose: bool = True) -> dict:
    """Write the company graph into the *active* tenant graph (caller sets the tenant)."""
    if reset:
        db.wipe_graph()
    db.create_indexes(INDEX_LABELS)

    def load(label: str, items: list[dict]) -> None:
        db.insert_rows(
            f"UNWIND $rows AS r MERGE (n:{label} {{id: r.id}}) SET n += r RETURN count(n) AS row",
            [dict(item) for item in items],  # copy: a re-seed never mutates the constants
        )

    load("Person", PEOPLE)
    load("Team", TEAMS)
    load("Project", PROJECTS)
    load("System", SYSTEMS)
    load("Doc", DOCS)
    load("Ticket", TICKETS)
    load("Chat", CHATS)
    load("Decision", DECISIONS)

    edges: dict[str, list[dict]] = {}
    for rel, src, dst in EDGES:
        edges.setdefault(rel, []).append({"src": src, "dst": dst})
    for rel, rows in edges.items():
        db.insert_rows(
            f"UNWIND $rows AS r MATCH (a {{id: r.src}}), (b {{id: r.dst}}) "
            f"MERGE (a)-[:{rel}]->(b) RETURN count(*) AS row",
            rows,
        )

    stats = db.query(
        "MATCH (n) WITH count(n) AS nodes OPTIONAL MATCH ()-[r]->() "
        "WITH nodes, count(r) AS rels RETURN [nodes, rels] AS row"
    )[0]
    summary = {"nodes": stats[0], "relationships": stats[1], "people": len(PEOPLE)}
    if verbose:
        print(f"Seeded company brain: {summary['nodes']} nodes, {summary['relationships']} relationships")
    return summary


_SEED_LOCK = threading.Lock()


def ensure_seeded() -> bool:
    """Seed the demo org graph only if the company tenant is empty.

    The web console calls this so a judge can switch to Company mode and ask a
    question without first running the CLI. Returns True if it seeded.
    """
    with _SEED_LOCK:
        with db.tenant_scope(DEFAULT_TENANT):
            found = db.query("MATCH (s:System) RETURN count(s) AS row")
        if found and found[0]:
            return False
        with db.tenant_scope(DEFAULT_TENANT):
            seed(reset=True, verbose=False)
        return True


# --- the five Track 03 questions --------------------------------------------

def who_knows(system_id: str) -> dict:
    """The brief's query: who has actually worked on things that use this system."""
    by_project = db.query(WHO_KNOWS_QUERY, {"system": system_id})
    experts = [
        {"person": row[0], "name": row[1], "projects": int(row[2])} for row in by_project
    ]
    direct = db.query(
        "MATCH (p:Person)-[:KNOWS]->(s:System {id: $id}) "
        "WITH p ORDER BY p.id RETURN [p.id, p.name] AS row",
        {"id": system_id},
    )
    direct_ids = {row[0] for row in direct}
    for expert in experts:
        expert["declared_expert"] = expert["person"] in direct_ids
    return {
        "system": system_id,
        "people": experts,
        "declared_experts": [{"person": r[0], "name": r[1]} for r in direct],
    }


def owners_of(entity_id: str) -> dict:
    """Who owns what: the owning team(s)/person(s) of a project or system."""
    owners = db.query(
        "MATCH (o)-[:OWNS]->(e {id: $id}) "
        "WITH o ORDER BY o.id RETURN [o.id, o.name, labels(o)[0]] AS row",
        {"id": entity_id},
    )
    team_members = db.query(
        "MATCH (o)-[:OWNS]->(e {id: $id}) "
        "OPTIONAL MATCH (o)-[:MEMBER_OF]->(t:Team) "
        "WITH coalesce(t, o) AS team "
        "MATCH (p:Person)-[:MEMBER_OF]->(team) "
        "WITH DISTINCT p ORDER BY p.id RETURN [p.id, p.name] AS row",
        {"id": entity_id},
    )
    return {
        "entity": entity_id,
        "owners": [{"id": r[0], "name": r[1], "kind": r[2]} for r in owners],
        "reachable_people": [{"person": r[0], "name": r[1]} for r in team_members],
    }


def blast_radius(system_id: str) -> dict:
    """What breaks if this service goes down: reverse transitive dependency closure."""
    affected = db.query(
        "MATCH (down:System {id: $id})<-[:DEPENDS_ON*1..6]-(other:System) "
        "RETURN DISTINCT [other.id, other.name, other.criticality] AS row",
        {"id": system_id},
    )
    projects = db.query(
        "MATCH (down:System {id: $id})<-[:DEPENDS_ON*0..6]-(:System)<-[:USES]-(pr:Project) "
        "RETURN DISTINCT [pr.id, pr.name] AS row",
        {"id": system_id},
    )
    teams = db.query(
        "MATCH (down:System {id: $id})<-[:DEPENDS_ON*0..6]-(:System)<-[:USES]-(pr:Project)"
        "<-[:OWNS]-(t:Team) "
        "RETURN DISTINCT [t.id, t.name] AS row",
        {"id": system_id},
    )
    return {
        "system": system_id,
        "affected_systems": [{"id": r[0], "name": r[1], "criticality": r[2]} for r in affected],
        "projects_at_risk": [{"project": r[0], "name": r[1]} for r in projects],
        "teams_at_risk": [{"team": r[0], "name": r[1]} for r in teams],
    }


def decision_trail(decision_id: str) -> dict:
    """Trace a decision back to its source, and forward to whatever replaced it."""
    head = db.query(
        "MATCH (d:Decision {id: $id}) RETURN [d.id, d.title, d.status, d.decided_at] AS row",
        {"id": decision_id},
    )
    if not head:
        return {"decision": decision_id, "found": False}
    decider = db.query(
        "MATCH (d:Decision {id: $id})-[:DECIDED_BY]->(p:Person) "
        "WITH p ORDER BY p.id RETURN [p.id, p.name] AS row",
        {"id": decision_id},
    )
    affects = db.query(
        "MATCH (d:Decision {id: $id})-[:AFFECTS]->(s:System) "
        "WITH s ORDER BY s.id RETURN [s.id, s.name] AS row",
        {"id": decision_id},
    )
    sources = db.query(
        "MATCH (d:Decision {id: $id})-[:SOURCED_FROM]->(src) "
        "WITH src ORDER BY src.id RETURN [src.id, labels(src)[0], coalesce(src.title, src.text, '')] AS row",
        {"id": decision_id},
    )
    superseded = db.query(
        "MATCH (d:Decision {id: $id})-[:SUPERSEDES]->(old:Decision) "
        "WITH old ORDER BY old.id RETURN [old.id, old.title, old.status] AS row",
        {"id": decision_id},
    )
    superseded_by = db.query(
        "MATCH (newer:Decision)-[:SUPERSEDES]->(d:Decision {id: $id}) "
        "WITH newer ORDER BY newer.id RETURN [newer.id, newer.title, newer.status] AS row",
        {"id": decision_id},
    )
    return {
        "found": True,
        "decision": head[0][0],
        "title": head[0][1],
        "status": head[0][2],
        "decided_at": head[0][3],
        "decided_by": [{"person": r[0], "name": r[1]} for r in decider],
        "affects": [{"system": r[0], "name": r[1]} for r in affects],
        "sources": [{"id": r[0], "kind": r[1], "summary": r[2]} for r in sources],
        "supersedes": [{"decision": r[0], "title": r[1], "status": r[2]} for r in superseded],
        "superseded_by": [{"decision": r[0], "title": r[1], "status": r[2]} for r in superseded_by],
    }


def knowledge_sources(system_id: str) -> dict:
    """Everything written about a system, by source type: docs, tickets, chats.

    This is the *"docs, tickets, and chats in one graph"* requirement: the same
    traversal returns all three, each with a named author, so an answer can always
    cite where it came from.
    """
    docs = db.query(
        "MATCH (d:Doc)-[:ABOUT]->(s:System {id: $id}) "
        "OPTIONAL MATCH (d)-[:AUTHORED_BY]->(a:Person) "
        "WITH d, a ORDER BY d.id RETURN [d.id, d.title, d.updated_at, a.name] AS row",
        {"id": system_id},
    )
    tickets = db.query(
        "MATCH (t:Ticket)-[:ABOUT]->(s:System {id: $id}) "
        "OPTIONAL MATCH (t)-[:ASSIGNED_TO]->(p:Person) "
        "WITH t, p ORDER BY t.id RETURN [t.id, t.title, t.status, p.name] AS row",
        {"id": system_id},
    )
    chats = db.query(
        "MATCH (c:Chat)-[:MENTIONS]->(s:System {id: $id}) "
        "OPTIONAL MATCH (c)-[:BY]->(p:Person) "
        "WITH c, p ORDER BY c.id RETURN [c.id, c.text, c.ts, p.name] AS row",
        {"id": system_id},
    )
    return {
        "system": system_id,
        "docs": [{"id": r[0], "title": r[1], "updated_at": r[2], "author": r[3]} for r in docs],
        "tickets": [{"id": r[0], "title": r[1], "status": r[2], "assignee": r[3]} for r in tickets],
        "chats": [{"id": r[0], "text": r[1], "ts": r[2], "author": r[3]} for r in chats],
    }


def stale_knowledge(stale_days: int = 180) -> dict:
    """Keeping knowledge current: ageing docs, superseded decisions, and bus-factor risk."""
    cutoff = _ts(-stale_days)
    stale_docs = db.query(
        "MATCH (d:Doc) WHERE d.updated_at < $cutoff "
        "WITH d ORDER BY d.updated_at RETURN [d.id, d.title, d.updated_at] AS row",
        {"cutoff": cutoff},
    )
    superseded = db.query(
        "MATCH (newer:Decision)-[:SUPERSEDES]->(old:Decision) "
        "WITH newer, old ORDER BY old.id "
        "RETURN [old.id, old.title, newer.id, newer.title] AS row"
    )
    bus_factor = db.query(
        "MATCH (s:System) "
        "OPTIONAL MATCH (s)<-[:KNOWS]-(p:Person) "
        "WITH s, count(p) AS experts "
        "WHERE experts <= 1 "
        "OPTIONAL MATCH (s)<-[:USES]-(pr:Project) "
        "WITH s, experts, count(DISTINCT pr) AS projects "
        "WHERE projects > 0 "
        "OPTIONAL MATCH (s)<-[:OWNS]-(owner) "
        "WITH s, experts, projects, collect(DISTINCT owner.id) AS owners "
        "RETURN [s.id, s.name, experts, projects, owners] AS row"
    )
    return {
        "stale_days": stale_days,
        "stale_docs": [{"id": r[0], "title": r[1], "updated_at": r[2]} for r in stale_docs],
        "superseded_decisions": [
            {"old": r[0], "old_title": r[1], "replaced_by": r[2], "new_title": r[3]} for r in superseded
        ],
        "bus_factor_risks": [
            {"system": r[0], "name": r[1], "experts": r[2], "projects": r[3], "owners": r[4]}
            for r in bus_factor
        ],
    }


# --- the tour ----------------------------------------------------------------

def _fmt_people(people: list[dict]) -> str:
    if not people:
        return "  _(none)_"
    return "\n".join(
        f"  - {p['name']} (`{p['person']}`) — {p.get('projects', '?')} project(s)"
        + ("  ·declared expert" if p.get("declared_expert") else "")
        for p in people
    )


def run_demo(
    system_id: str = "SYS-ledger-db",
    decision_id: str = "ADR-002",
    *,
    verbose: bool = True,
) -> str:
    """Run the Track 03 questions and return (optionally print) a markdown report."""
    knows = who_knows(system_id)
    owns = owners_of(system_id)
    blast = blast_radius(system_id)
    trail = decision_trail(decision_id)
    sources = knowledge_sources(system_id)
    stale = stale_knowledge()

    parts: list[str] = [
        "# GraphSleuth — Company Brain (Track 03)",
        "",
        f"One FalkorDB graph, six Track 03 questions. System in focus: `{system_id}`; "
        f"decision in focus: `{decision_id}`.",
        "",
        "## 1. Who knows about this system?",
        "",
        f"```cypher\n{WHO_KNOWS_QUERY}\n```",
        "",
        _fmt_people(knows["people"]),
        "",
        "Declared experts (`:KNOWS`): "
        + (", ".join(f"{e['name']} (`{e['person']}`)" for e in knows["declared_experts"]) or "_(none)_"),
        "",
        "## 2. Who owns it?",
        "",
        "\n".join(
            f"  - {o['name']} (`{o['id']}`, {o['kind']})" for o in owns["owners"]
        ) or "  _(none)_",
        "",
        "Reachable owners' teammates: "
        + (", ".join(p["name"] for p in owns["reachable_people"]) or "_(none)_"),
        "",
        "## 3. What breaks if it goes down?",
        "",
        "Affected systems (reverse `:DEPENDS_ON` closure):",
        "\n".join(
            f"  - {s['name']} (`{s['id']}`, {s['criticality']})" for s in blast["affected_systems"]
        ) or "  _(none)_",
        "",
        "Projects at risk: "
        + (", ".join(f"{p['name']} (`{p['project']}`)" for p in blast["projects_at_risk"]) or "_(none)_"),
        "",
        "Teams at risk: "
        + (", ".join(f"{t['name']} (`{t['team']}`)" for t in blast["teams_at_risk"]) or "_(none)_"),
        "",
        "## 4. Why was this decision made? (traced to source)",
        "",
        f"**{trail.get('title', '?')}** (`{decision_id}`, status: {trail.get('status', '?')})",
        "",
        "Decided by: " + (", ".join(d["name"] for d in trail.get("decided_by", [])) or "_(unknown)_"),
        "",
        "Affects: "
        + (", ".join(f"{s['name']} (`{s['system']}`)" for s in trail.get("affects", [])) or "_(none)_"),
        "",
        "Sourced from:",
        "\n".join(
            f"  - [{s['kind']}] `{s['id']}` — {s['summary'][:100]}" for s in trail.get("sources", [])
        ) or "  _(no source recorded)_",
        "",
        "Supersedes: "
        + (
            ", ".join(f"{s['title']} (`{s['decision']}`, {s['status']})" for s in trail.get("supersedes", []))
            or "_(nothing)_"
        ),
        "",
        "Superseded by: "
        + (
            ", ".join(f"{s['title']} (`{s['decision']}`)" for s in trail.get("superseded_by", []))
            or "_(still current)_"
        ),
        "",
        "## 5. What do we know, and where from? (docs · tickets · chats)",
        "",
        "Docs:",
        "\n".join(
            f"  - `{d['id']}` {d['title']} — by {d['author']}, last {d['updated_at'][:10]}"
            for d in sources["docs"]
        )
        or "  _(none)_",
        "",
        "Tickets:",
        "\n".join(
            f"  - `{t['id']}` {t['title']} [{t['status']}] — {t['assignee']}"
            for t in sources["tickets"]
        )
        or "  _(none)_",
        "",
        "Chats:",
        "\n".join(
            f"  - `{c['id']}` \"{c['text']}\" — {c['author']}" for c in sources["chats"]
        )
        or "  _(none)_",
        "",
        "## 6. What knowledge is going stale?",
        "",
        f"Docs not updated in {stale['stale_days']} days:",
        "\n".join(f"  - {d['title']} (`{d['id']}`, last {d['updated_at'][:10]})" for d in stale["stale_docs"])
        or "  _(none)_",
        "",
        "Superseded decisions:",
        "\n".join(
            f"  - `{s['old']}` {s['old_title']} → replaced by `{s['replaced_by']}` {s['new_title']}"
            for s in stale["superseded_decisions"]
        )
        or "  _(none)_",
        "",
        "Bus-factor risk (≤1 declared expert but in use):",
        "\n".join(
            f"  - {b['name']} (`{b['system']}`): {b['experts']} expert(s), used by {b['projects']} project(s)"
            for b in stale["bus_factor_risks"]
        )
        or "  _(none)_",
        "",
        "---",
        "",
        "_Every answer above is a graph traversal, and every claim names its source nodes._",
    ]
    report = "\n".join(parts)
    if verbose:
        print(report)
    return report


# --- Track 03 as an agent ---------------------------------------------------- #
# The same loop the fraud investigator uses, but with the six company-brain tools
# and an org-focused prompt, running against the `company` tenant. Live, the model
# chooses which question to answer with which tool; offline, a scripted brain runs
# the same six questions so the command works with no API key.

COMPANY_SYSTEM_PROMPT = """You are GraphSleuth's Company Brain. Your context layer is a FalkorDB graph
that holds one organisation: people, teams, projects, systems, documents, tickets, chat
messages, and the architectural decisions the company has made. You answer questions about
WHO KNOWS WHAT, ownership, risk, and why things are the way they are — by traversing the
graph, never by guessing. (Each tenant has its own graph; everything you see is this tenant's.)

Available tools (Track 03):
- `who_knows(system_id)` — who has worked on projects that use a system, ranked, plus declared experts.
- `owners_of(entity_id)` — who owns a system/project, and the teammates reachable from that owner.
- `blast_radius(system_id)` — what breaks if a system goes down (reverse transitive :DEPENDS_ON).
- `decision_trail(decision_id)` — why a decision was made: who, what it affects, its doc/ticket/chat
  source, and which earlier decision it supersedes / was superseded by.
- `knowledge_sources(system_id)` — every doc, ticket and chat about a system, each with its author.
- `stale_knowledge(stale_days)` — ageing docs, superseded decisions, and bus-factor risk.
- `recall` / `remember` — your long-term memory of past questions in this tenant.

Working rules:
1. Evidence only. Every claim must come from a tool result, and you must cite the node ids
   (e.g. `EMP-003`, `DOC-002`, `TKT-102`, `CHAT-202`). If the graph does not show it, say so.
2. Pick the right tool for the question. "Who knows X" -> `who_knows`. "Who owns X" -> `owners_of`.
   "What breaks / what depends on X" -> `blast_radius`. "Why did we decide X" -> `decision_trail`.
   "What do we know about X / where is it documented" -> `knowledge_sources`.
   "What's out of date / risky" -> `stale_knowledge`. Ask more than one when the question needs it.
3. Start with `recall` in case this tenant already answered a related question.
4. Finish by calling `remember` with the conclusion and the ids it is about, so the next session
   starts smarter.

Final answer format (always, in this order):
- **Answer** — direct, one or two sentences.
- **Evidence** — the graph facts you used, each with its node id(s).
- **Sources** — the docs/tickets/chats/decisions you cite (`ID` — short note).
- **Confidence** — high/medium/low, and what is missing or stale.
"""

_SYS_RE = re.compile(r"SYS-[A-Za-z0-9-]+")
_ADR_RE = re.compile(r"ADR-[A-Za-z0-9-]+")


def _focus(question: str) -> tuple[str, str]:
    """Pick the system / decision a free-text question is about (sensible defaults)."""
    system = _SYS_RE.search(question)
    decision = _ADR_RE.search(question)
    return (
        system.group(0) if system else "SYS-ledger-db",
        decision.group(0) if decision else "ADR-002",
    )


def build_company_brain(question: str) -> Any:
    """Offline fallback: the six Track 03 questions, hand-scripted, answered from the graph."""
    from .llm import ScriptedBrain

    system, decision = _focus(question)
    plan: list[tuple[str, Any]] = [
        ("recall", {"query": system}),
        ("who_knows", {"system_id": system}),
        ("owners_of", {"entity_id": system}),
        ("blast_radius", {"system_id": system}),
        ("decision_trail", {"decision_id": decision}),
        ("knowledge_sources", {"system_id": system}),
        ("stale_knowledge", {}),
        (
            "remember",
            {
                "kind": "semantic",
                "text": f"Answered: who knows / owns / depends on {system}, and why {decision} was made.",
                "about_ids": [system, decision],
            },
        ),
    ]
    return ScriptedBrain(plan=plan, on_finish=compose_company_final)


def compose_company_final(question: str, history: list[dict]) -> str:
    """Turn the company tool trail into the required Answer/Evidence/Sources format."""

    def last(tool: str) -> dict:
        for entry in reversed(history):
            if entry["tool"] == tool and isinstance(entry["result"], dict):
                return entry["result"]
        return {}

    knows = last("who_knows")
    owns = last("owners_of")
    blast = last("blast_radius")
    trail = last("decision_trail")
    sources = last("knowledge_sources")
    stale = last("stale_knowledge")

    system = knows.get("system") or owns.get("entity") or blast.get("system") or "the system"
    decision = trail.get("decision", "")

    people = knows.get("people", [])
    top = people[0] if people else None
    answer_bits = []
    if top:
        answer_bits.append(
            f"**{top['name']}** (`{top['person']}`) knows `{system}` best "
            f"({top['projects']} project(s))"
        )
    if owns.get("owners"):
        owner = owns["owners"][0]
        answer_bits.append(f"it is owned by **{owner['name']}** (`{owner['id']}`)")
    if blast.get("affected_systems"):
        names = ", ".join(s["name"] for s in blast["affected_systems"])
        answer_bits.append(f"if it goes down it takes out {names}")
    answer = ("; ".join(answer_bits) + ".") if answer_bits else f"No company-brain evidence for {system}."

    evidence: list[str] = []
    for p in people[:6]:
        flag = " *(declared expert)*" if p.get("declared_expert") else ""
        evidence.append(f"  - `who_knows`: {p['name']} (`{p['person']}`) — {p['projects']} project(s){flag}")
    for s in blast.get("affected_systems", []):
        evidence.append(f"  - `blast_radius`: {s['name']} (`{s['id']}`, {s['criticality']})")
    for d in trail.get("decided_by", []):
        evidence.append(f"  - `decision_trail`: decided by {d['name']} (`{d['person']}`)")
    for s in trail.get("supersedes", []):
        evidence.append(f"  - `decision_trail`: `{decision}` supersedes `{s['decision']}` ({s['title']})")
    for b in stale.get("bus_factor_risks", [])[:4]:
        evidence.append(f"  - `stale_knowledge`: {b['name']} (`{b['system']}`) has {b['experts']} expert(s)")

    cites: list[str] = []
    seen_cites: set[str] = set()

    def add_cite(cite_id: str, line: str) -> None:
        if cite_id in seen_cites:
            return
        seen_cites.add(cite_id)
        cites.append(line)

    for s in trail.get("sources", []):
        add_cite(s["id"], f"  - [{s['kind']}] `{s['id']}` — {s['summary'][:80]}")
    for d in sources.get("docs", []):
        add_cite(d["id"], f"  - [Doc] `{d['id']}` {d['title']} — by {d['author']}")
    for t in sources.get("tickets", []):
        add_cite(t["id"], f"  - [Ticket] `{t['id']}` {t['title']} [{t['status']}]")
    for c in sources.get("chats", []):
        add_cite(c["id"], f"  - [Chat] `{c['id']}` \"{c['text'][:70]}\" — {c['author']}")

    tools = [e["tool"] for e in history]
    return "\n".join(
        [
            "**Answer**",
            "",
            answer,
            "",
            f"**Evidence** (tools: {', '.join(dict.fromkeys(tools))})",
            "",
            "\n".join(evidence) or "  _(none)_",
            "",
            "**Sources**",
            "",
            "\n".join(cites) or "  _(none)_",
            "",
            "**Confidence** — high: every claim above is a graph traversal with named node ids. "
            "Re-check against `stale_knowledge` before acting, since some docs and experts may be out of date.",
        ]
    )


def chat(
    question: str,
    *,
    tenant: str = DEFAULT_TENANT,
    verbose: bool = True,
    model: str | None = None,
    on_event: Any = None,
    offline: bool = False,
) -> dict:
    """Answer one company question with the agent (live model, or scripted offline)."""
    from .agent import GraphSleuth
    from .tools import COMPANY_TOOL_SPECS

    scripted = build_company_brain(question)
    agent = GraphSleuth(
        tenant=tenant,
        prompt=COMPANY_SYSTEM_PROMPT,
        tool_specs=COMPANY_TOOL_SPECS,
        scripted=scripted,
        brain=scripted if offline else None,
        model=model,
        verbose=verbose,
        on_event=on_event,
        role="company-brain",
    )
    return agent.ask(question)
