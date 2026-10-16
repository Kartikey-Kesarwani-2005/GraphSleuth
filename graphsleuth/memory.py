"""Agent memory — stored in the same FalkorDB graph as the fraud network.

Three kinds of memory live in the graph:

* **episodic**  — what happened in past sessions (`Session` + `Message` nodes)
* **semantic**  — durable facts the agent learned (`MemoryNote` kind=semantic)
* **procedural** — how the agent should work, playbooks (kind=procedural)

Notes are written with `remember()` and can be linked to any entity in the
fraud graph (`:ABOUT`), which is what makes recall graph-aware rather than a
flat vector lookup: the agent can ask "what do I already know about ACC-9003?"
and follow edges to the note.
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


def append_message(session_id: str, role: str, content: str, tenant: str = DEFAULT_TENANT) -> None:
    """Persist one conversation turn — episodic memory that survives restarts."""
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
    )


def set_session_summary(session_id: str, summary: str) -> None:
    db.query(
        "MATCH (s:Session {id: $sid}) SET s.summary = $summary, s.closed_at = $ts RETURN s.id AS row",
        {"sid": session_id, "summary": summary[:2000], "ts": _now()},
    )


def remember(
    kind: str,
    text: str,
    about_ids: list[str] | None = None,
    tenant: str = DEFAULT_TENANT,
    note_id: str | None = None,
) -> dict:
    """Write a memory note, optionally linked to entities in the graph."""
    kind = kind if kind in {"episodic", "semantic", "procedural"} else "semantic"
    note_id = note_id or f"MEM-{uuid.uuid4().hex[:8]}"

    db.query(
        """
        MERGE (n:MemoryNote {id: $id})
        ON CREATE SET n.kind = $kind, n.text = $text, n.created_at = $ts, n.tenant = $tenant
        RETURN n.id AS row
        """,
        {"id": note_id, "kind": kind, "text": text[:4000], "ts": _now(), "tenant": tenant},
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
        )
        if rows:
            linked.append(entity_id)

    return {"note_id": note_id, "kind": kind, "linked_to": linked, "text": text[:200]}


def recall(query_text: str = "", kind: str = "", tenant: str = DEFAULT_TENANT, limit: int = 8) -> list[dict]:
    """Search memory notes; `query_text` can be an entity id or free text."""
    rows = db.query(
        """
        MATCH (n:MemoryNote)
        WHERE n.tenant = $tenant
          AND ($kind = '' OR n.kind = $kind)
          AND ($q = '' OR toLower(n.text) CONTAINS toLower($q) OR n.id CONTAINS $q)
        OPTIONAL MATCH (n)-[:ABOUT]->(e)
        RETURN [n.id, n.kind, n.text, n.created_at, collect(e.id)] AS row
        ORDER BY n.created_at DESC
        LIMIT $limit
        """,
        {"q": query_text, "kind": kind, "tenant": tenant, "limit": limit},
    )
    return [
        {"note_id": r[0], "kind": r[1], "text": r[2], "created_at": r[3], "about": r[4]}
        for r in rows
    ]


def open_cases(tenant: str = DEFAULT_TENANT, limit: int = 5) -> list[dict]:
    rows = db.query(
        """
        MATCH (c:Case)
        WHERE coalesce(c.status, 'open') = 'open'
        RETURN [c.id, c.title, c.created_at, coalesce(c.summary, '')] AS row
        ORDER BY c.created_at DESC
        LIMIT $limit
        """,
        {"limit": limit},
    )
    return [{"case_id": r[0], "title": r[1], "created_at": r[2], "summary": r[3]} for r in rows]


def recent_sessions(tenant: str = DEFAULT_TENANT, limit: int = 3) -> list[dict]:
    rows = db.query(
        """
        MATCH (s:Session)
        WHERE s.tenant = $tenant
        RETURN [s.id, coalesce(s.summary, ''), s.started_at] AS row
        ORDER BY s.started_at DESC
        LIMIT $limit
        """,
        {"tenant": tenant, "limit": limit},
    )
    return [{"session_id": r[0], "summary": r[1], "started_at": r[2]} for r in rows]


def context_digest(question: str, tenant: str = DEFAULT_TENANT) -> str:
    """Build the memory briefing injected into the agent's system prompt.

    This is the "remember across sessions" half of the agent: before it looks at
    a single fraud record it already knows past findings, open cases and the
    playbook it learned earlier.
    """
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
