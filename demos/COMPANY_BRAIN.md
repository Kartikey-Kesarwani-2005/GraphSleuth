# GraphSleuth — Company Brain (Track 03)

One FalkorDB graph, six Track 03 questions. System in focus: `SYS-ledger-db`; decision in focus: `ADR-002`.

## 1. Who knows about this system?

```cypher
MATCH (p:Person)-[:WORKED_ON]->(pr:Project)-[:USES]->(s:System {id: $system})
WITH p, count(pr) AS projects
ORDER BY projects DESC, p.id
RETURN [p.id, p.name, projects] AS row
```

  - Aarav Sharma (`EMP-001`) — 2 project(s)  ·declared expert
  - Isha Verma (`EMP-002`) — 1 project(s)
  - Rohan Patel (`EMP-003`) — 1 project(s)  ·declared expert
  - Saanvi Iyer (`EMP-010`) — 1 project(s)

Declared experts (`:KNOWS`): Aarav Sharma (`EMP-001`), Rohan Patel (`EMP-003`)

## 2. Who owns it?

  - Rohan Patel (`EMP-003`, Person)

Reachable owners' teammates: Rohan Patel, Vihaan Singh, Dev Mehta

## 3. What breaks if it goes down?

Affected systems (reverse `:DEPENDS_ON` closure):
  - Payments API (`SYS-payments-api`, critical)
  - Mobile BFF (`SYS-mobile-bff`, high)
  - Auth Service (`SYS-auth-service`, high)

Projects at risk: Payments Core (`PROJ-payments-core`), Ledger Migration (`PROJ-ledger`), Checkout Revamp (`PROJ-checkout`), SSO Rollout (`PROJ-auth`)

Teams at risk: Payments Platform (`TEAM-payments`), Platform & Reliability (`TEAM-platform`), Identity & Access (`TEAM-identity`)

## 4. Why was this decision made? (traced to source)

**Migrate the ledger to schema v9** (`ADR-002`, status: accepted)

Decided by: Aarav Sharma

Affects: Ledger DB (`SYS-ledger-db`)

Sourced from:
  - [Doc] `DOC-002` — Ledger schema reference
  - [Ticket] `TKT-102` — Ledger DB migration to schema v9

Supersedes: _(nothing)_

Superseded by: _(still current)_

## 5. What do we know, and where from? (docs · tickets · chats)

Docs:
  - `DOC-002` Ledger schema reference — by Isha Verma, last 2026-08-28

Tickets:
  - `TKT-102` Ledger DB migration to schema v9 [in_progress] — Aarav Sharma

Chats:
  - `CHAT-202` "ledger-db failover drill is green again" — Rohan Patel

## 6. What knowledge is going stale?

Docs not updated in 180 days:
  - Payments API design (`DOC-001`, last 2025-04-20)
  - Mobile BFF runbook (`DOC-005`, last 2025-09-07)
  - Analytics pipeline map (`DOC-007`, last 2026-03-16)

Superseded decisions:
  - `ADR-004` Keep the in-house login service → replaced by `ADR-003` Replace in-house login with SSO

Bus-factor risk (≤1 declared expert but in use):
  - Auth Service (`SYS-auth-service`): 1 expert(s), used by 1 project(s)
  - Observability Stack (`SYS-observability-stack`): 1 expert(s), used by 1 project(s)
  - Mobile BFF (`SYS-mobile-bff`): 1 expert(s), used by 1 project(s)

---

_Every answer above is a graph traversal, and every claim names its source nodes._