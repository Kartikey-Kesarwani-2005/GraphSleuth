"""Agent memory — stored in the same FalkorDB graph as the fraud network.

Three kinds of memory live in the graph:

* **episodic**  — what happened in past sessions (`Session` + `Message` nodes)
* **semantic**  — durable facts the agent learned (`MemoryNote` kind=semantic)
* **procedural** — how the agent should work, playbooks (kind=procedural)

Notes are written with `remember()` and can be linked to any entity in the
fraud graph (`:ABOUT`), which is what makes recall graph-aware rather than a
flat vector lookup: the agent can ask "what do I already know about ACC-9003?"
and follow edges to the note.

Multigraph tenancy: every note/session/case lives in its **tenant's own graph**
(`graphsleuth:<tenant>`), so memory is isolated by topology — one agent cannot
read another tenant's notes even with raw Cypher (`run_cypher` runs against the
active tenant's graph only).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from . import db

DEFAULT_TENANT = "default"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_session_id() -> str:
    return f"SES-{uuid.uuid4().hex[:8]}"


def append_message(session_id: str, role: str, content: str, tenant: str | None = None) -> None:
    """Persist one conversation turn — episodic memory that survives restarts.

    `tenant=None` writes into the graph of the active tenant (see `db.tenant_scope`).
    """
    tenant = db.resolve_tenant(tenant)
    db.query(
        """
        MERGE (s:Session {id: $sid})
          ON CREATE SET s.started_at = $ts, s.tenant = $tenant
        CREATE (m:Message {id: $mid, role: $role, content: $content, created_at: $ts, tenant: $tenant})
        CREATE (s)-[:SAID]->(m)
        RETURN m.id AS row
        """,
        {
            "sid": session_id,
            "mid": f"MSG-{uuid.uuid4().hex[:8]}",
            "role": role,
            "content": content[:8000],
            "ts": _now(),
            "tenant": tenant,
        },
        tenant=tenant,
    )


def set_session_summary(session_id: str, summary: str, tenant: str | None = None) -> None:
    tenant = db.resolve_tenant(tenant)
    db.query(
        "MATCH (s:Session {id: $sid}) SET s.summary = $summary, s.closed_at = $ts RETURN s.id AS row",
        {"sid": session_id, "summary": summary[:2000], "ts": _now()},
        tenant=tenant,
    )


def remember(
    kind: str,
    text: str,
    about_ids: list[str] | None = None,
    tenant: str | None = None,
    note_id: str | None = None,
) -> dict:
    """Write a memory note, optionally linked to entities in the graph."""
    tenant = db.resolve_tenant(tenant)
    kind = kind if kind in {"episodic", "semantic", "procedural"} else "semantic"
    note_id = note_id or f"MEM-{uuid.uuid4().hex[:8]}"

    db.query(
        """
        MERGE (n:MemoryNote {id: $id})
        ON CREATE SET n.kind = $kind, n.text = $text, n.created_at = $ts, n.tenant = $tenant
        RETURN n.id AS row
        """,
        {"id": note_id, "kind": kind, "text": text[:4000], "ts": _now(), "tenant": tenant},
        tenant=tenant,
    )

    linked: list[str] = []
    for entity_id in about_ids or []:
        rows = db.query(
            """
            MATCH (n:MemoryNote {id: $nid}), (e {id: $eid})
            MERGE (n)-[:ABOUT]->(e)
            RETURN e.id AS row
            """,
            {"nid": note_id, "eid": entity_id},
            tenant=tenant,
        )
        if rows:
            linked.append(entity_id)

    return {"note_id": note_id, "kind": kind, "linked_to": linked, "text": text[:200]}


def recall(query_text: str = "", kind: str = "", tenant: str | None = None, limit: int = 8) -> list[dict]:
    """Search memory notes; `query_text` can be an entity id or free text."""
    tenant = db.resolve_tenant(tenant)
    rows = db.query(
        """
        MATCH (n:MemoryNote)
        WHERE n.tenant = $tenant
          AND ($kind = '' OR n.kind = $kind)
          AND ($q = '' OR toLower(n.text) CONTAINS toLower($q) OR n.id CONTAINS $q)
        OPTIONAL MATCH (n)-[:ABOUT]->(e)
        WITH n, collect(e.id) AS about
        ORDER BY n.created_at DESC
        LIMIT $limit
        RETURN [n.id, n.kind, n.text, n.created_at, about] AS row
        """,
        {"q": query_text, "kind": kind, "tenant": tenant, "limit": limit},
        tenant=tenant,
    )
    return [
        {"note_id": r[0], "kind": r[1], "text": r[2], "created_at": r[3], "about": r[4]}
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# procedural memory: learned playbooks
# --------------------------------------------------------------------------- #
def distill_playbook(trace: list[dict], question: str = "", tenant: str | None = None) -> dict | None:
    """Turn a finished investigation into a reusable procedure.

    Procedural memory = *how* the agent worked, not what it found. We keep the
    ordered sequence of tools that actually produced a verdict, plus the entities
    they touched, so a later session gets a head start instead of rediscovering
    the routine. Bookkeeping tools (`remember`) are dropped: they record, they do
    not investigate.
    """
    if not trace:
        return None
    steps: list[str] = []
    for entry in trace:
        name = entry.get("tool")
        if not name or name in {"remember"}:
            continue
        if not steps or steps[-1] != name:
            steps.append(name)
    if len(steps) < 4:  # a one- or two-tool lookup is not a procedure worth keeping
        return None
    entities = _entities_from_trace(trace)
    signature = " > ".join(steps)
    return {
        "steps": steps,
        "signature": signature,
        "question": question[:200],
        "entities": entities,
        "text": (
            f"Playbook: {signature}. Learned from an investigation that produced a verdict"
            + (f" ({question[:120]})" if question else "")
            + (f". First entities: {', '.join(entities[:6])}." if entities else "")
        ),
    }


def playbook_exists(signature: str, tenant: str | None = None) -> bool:
    tenant = db.resolve_tenant(tenant)
    rows = db.query(
        "MATCH (n:MemoryNote {kind: 'procedural'}) WHERE n.signature = $sig RETURN n.id AS row LIMIT 1",
        {"sig": signature},
        tenant=tenant,
    )
    return bool(rows)


def count_playbooks(tenant: str | None = None) -> int:
    tenant = db.resolve_tenant(tenant)
    rows = db.query(
        "MATCH (n:MemoryNote {kind: 'procedural'}) RETURN count(n) AS row",
        tenant=tenant,
    )
    return int(rows[0]) if rows else 0


def save_playbook(playbook: dict, tenant: str | None = None, max_playbooks: int = 12) -> dict:
    """Persist a distilled playbook as procedural memory, linked to its entities."""
    tenant = db.resolve_tenant(tenant)
    if count_playbooks(tenant) >= max_playbooks:
        return {"saved": False, "reason": f"playbook cap ({max_playbooks}) reached"}
    note_id = f"MEM-{uuid.uuid4().hex[:8]}"
    db.query(
        """
        MERGE (n:MemoryNote {id: $id})
        ON CREATE SET n.kind = 'procedural', n.text = $text, n.signature = $sig,
                      n.steps = $steps, n.created_at = $ts, n.tenant = $tenant
        RETURN n.id AS row
        """,
        {
            "id": note_id,
            "text": playbook["text"][:4000],
            "sig": playbook["signature"],
            "steps": playbook["steps"],
            "ts": _now(),
            "tenant": tenant,
        },
        tenant=tenant,
    )
    linked: list[str] = []
    for entity_id in playbook.get("entities") or []:
        rows = db.query(
            "MATCH (n:MemoryNote {id: $nid}), (e {id: $eid}) MERGE (n)-[:ABOUT]->(e) RETURN e.id AS row",
            {"nid": note_id, "eid": entity_id},
            tenant=tenant,
        )
        if rows:
            linked.append(entity_id)
    return {"saved": True, "note_id": note_id, "steps": playbook["steps"], "linked_to": linked}


def playbooks(tenant: str | None = None, limit: int = 5) -> list[dict]:
    """The learned procedures for a tenant, newest first."""
    tenant = db.resolve_tenant(tenant)
    rows = db.query(
        """
        MATCH (n:MemoryNote {kind: 'procedural'})
        WHERE n.tenant = $tenant
        WITH n
        ORDER BY n.created_at DESC
        LIMIT $limit
        RETURN [n.id, n.text, n.signature, n.created_at] AS row
        """,
        {"tenant": tenant, "limit": limit},
        tenant=tenant,
    )
    return [{"note_id": r[0], "text": r[1], "signature": r[2], "created_at": r[3]} for r in rows]


def _entities_from_trace(trace: list[dict]) -> list[str]:
    """Entity ids the tools actually operated on (for linking a playbook)."""
    ids: list[str] = []
    for entry in trace:
        args = entry.get("arguments") or {}
        for key in ("account_id", "entity_id", "target", "id_a", "id_b"):
            value = args.get(key)
            if isinstance(value, str) and value:
                ids.append(value)
        for value in args.get("entity_ids") or []:
            if isinstance(value, str):
                ids.append(value)
        result = entry.get("result")
        if isinstance(result, dict):
            for key in ("account", "start"):
                value = result.get(key)
                if isinstance(value, str) and value:
                    ids.append(value)
    seen: set[str] = set()
    unique: list[str] = []
    for entity_id in ids:
        if entity_id and entity_id not in seen:
            seen.add(entity_id)
            unique.append(entity_id)
    return unique[:20]


def open_cases(tenant: str | None = None, limit: int = 5) -> list[dict]:
    tenant = db.resolve_tenant(tenant)  # cases live in the tenant's own graph
    rows = db.query(
        """
        MATCH (c:Case)
        WHERE coalesce(c.status, 'open') = 'open'
        WITH c
        ORDER BY c.created_at DESC
        LIMIT $limit
        RETURN [c.id, c.title, c.created_at, coalesce(c.summary, '')] AS row
        """,
        {"limit": limit},
        tenant=tenant,
    )
    return [{"case_id": r[0], "title": r[1], "created_at": r[2], "summary": r[3]} for r in rows]


def recent_sessions(tenant: str | None = None, limit: int = 3) -> list[dict]:
    tenant = db.resolve_tenant(tenant)
    rows = db.query(
        """
        MATCH (s:Session)
        WHERE s.tenant = $tenant
        WITH s
        ORDER BY s.started_at DESC
        LIMIT $limit
        RETURN [s.id, coalesce(s.summary, ''), s.started_at] AS row
        """,
        {"tenant": tenant, "limit": limit},
        tenant=tenant,
    )
    return [{"session_id": r[0], "summary": r[1], "started_at": r[2]} for r in rows]


def context_digest(question: str, tenant: str | None = None) -> str:
    """Build the memory briefing injected into the agent's system prompt.

    This is the "remember across sessions" half of the agent: before it looks at
    a single fraud record it already knows past findings, open cases and the
    playbook it learned earlier. Everything comes from the active tenant's graph.
    """
    tenant = db.resolve_tenant(tenant)
    lines: list[str] = []

    cases = open_cases(tenant)
    if cases:
        lines.append("Open cases:")
        for case in cases:
            lines.append(f"  - {case['case_id']} | {case['title']} | {case['summary'][:160]}")

    notes = recall("", "", tenant, limit=8)
    if notes:
        lines.append("Memory notes (newest first):")
        for note in notes:
            about = f" -> {', '.join(note['about'])}" if note["about"] else ""
            lines.append(f"  - [{note['kind']}] {note['text'][:200]}{about}")

    learned = playbooks(tenant, limit=3)
    if learned:
        lines.append("Learned playbooks (procedure that produced a verdict before — reuse it):")
        for playbook in learned:
            lines.append(f"  - {playbook['text'][:220]}")

    if question:
        linked = recall(_guess_entity(question), "", tenant, limit=3)
        if linked:
            lines.append(f"Memory linked to entities in this question:")
            for note in linked:
                lines.append(f"  - [{note['kind']}] {note['text'][:200]}")

    sessions = recent_sessions(tenant)
    if sessions:
        lines.append("Recent sessions:")
        for session in sessions:
            lines.append(f"  - {session['session_id']} ({session['started_at']}): {session['summary'][:160]}")

    return "\n".join(lines) if lines else "No prior memory for this tenant."


def _guess_entity(text: str) -> str:
    """Extract something that looks like an entity id (ACC-9003, TX-9001, ...)."""
    for token in text.replace(",", " ").replace("?", " ").split():
        cleaned = token.strip("().[]'\"")
        if "-" in cleaned and any(ch.isdigit() for ch in cleaned):
            return cleaned
    return ""
