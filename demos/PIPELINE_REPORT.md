# GraphSleuth pipeline — ACC-9003 (tenant: default)

Two agents, one graph: **triage -> handoff -> investigator**

- handoff created: `HANDOFF-f91c02`
- handoff accepted: `HANDOFF-f91c02` (accepted)
- triage brain: offline-scripted (offline fallback)
- investigator brain: offline-scripted (offline fallback)

---

## Agent 1 — Triage

**Triage verdict** — escalate: ACC-9003 carries multiple graph signals.

**Signals**
- ACC-9002: devices=['DEV-9001', 'DEV-9002'] beneficiaries=['ACC-9004'] alerts=['AL-9001'] (score 4.5)
- ACC-9001: devices=['DEV-9001'] beneficiaries=[] alerts=['AL-9003'] (score 2.0)
- ACC-9004: devices=['DEV-9002'] beneficiaries=[] alerts=['AL-9002'] (score 2.0)
- ACC-9005: devices=['DEV-9002'] beneficiaries=[] alerts=[] (score 1.0)

**Handoff** — HANDOFF-f91c02 -> investigator | entities: ACC-9002, ACC-9004, ACC-9003, ACC-9100, ACC-9001, ACC-9005

**Memory** — MEM-e06e6e0c

### Triage tool trace

1. `list_alerts` → `alerts=[{'alert_id': 'AL-82e5cd', 'severity': 'high', 'status': 'open', 'account': 'ACC-9003', 'reason': 'Shares device DEV-9001/DEV-9002 with flagged accounts, is a pass-through hop in a same-day cha`
2. `recall` → `query=ACC-9003, kind=any, notes=[{'note_id': 'MEM-11401f7f', 'kind': 'episodic', 'text': 'Investigated ACC-9003: 6-account cluster (ACC-9002, ACC-9004, ACC-9003, ACC-9100, ACC-9001, ACC-9005), score 4`
3. `entity_search` → `matches=[{'id': 'ACC-9003', 'labels': ['Account'], 'props': {'type': 'current', 'status': 'under_review', 'risk_score': 0.77, 'opened_at': '2026-03-16T09:00:00+00:00'}}], count=1`
4. `find_shared_links` → `account=ACC-9003, linked_accounts=4, links=[{'account': 'ACC-9002', 'score': 4.5, 'shared_devices': ['DEV-9001', 'DEV-9002'], 'shared_beneficiaries': ['ACC-9004'], 'alerts': ['AL-9001']}, {'account': `
5. `detect_suspicious_cluster` → `algorithm=union-find over shared devices + beneficiary collectors + alerts, min_signals=2, clusters_found=1, clusters_filtered_out=4`
6. `create_handoff` → `handoff_id=HANDOFF-f91c02, from_role=triage, to_role=investigator, status=pending`
7. `remember` → `note_id=MEM-e06e6e0c, kind=episodic, linked_to=['ACC-9002', 'ACC-9004', 'ACC-9003', 'ACC-9100', 'ACC-9001', 'ACC-9005'], text=Triage on ACC-9003: 6-account cluster (ACC-9002, ACC-9004, ACC-9003, ACC-9`

**Actions taken (verified from the tool log):** `create_handoff(to_role=investigator, from_role=triage, context=Triage escalated ACC-9003: 6-account ...)`, `remember(kind=episodic, text=Triage on ACC-9003: 6-account cluster..., about_ids=['ACC-9002', 'ACC-9004', 'ACC-9003', ...)`

---

## Agent 2 — Investigator

**Verdict** — ACC-9003 is a confirmed hop in a 6-account mule ring that pools same-day transfers into beneficiary account ACC-9100. Escalated.

**Evidence chain**
1. Memory: [episodic] Triage on ACC-9003: 6-account cluster (ACC-9002, ACC-9004, ACC-9003, ACC-9100, ACC-9001, ACC-9005), score 42.05 — escalated to the investigator via ha
2. Inbound: ACC-9002 -> ACC-9003 via TX-9002 (INR 1,820,000, 2026-10-12T11:14:00+00:00)
3. Outbound: ACC-9003 -> ACC-9004 via TX-9003 (INR 1,790,000, 2026-10-12T11:21:00+00:00)
4. Outbound: ACC-9004 -> ACC-9005 via TX-9004 (INR 1,760,000, 2026-10-12T11:28:00+00:00)
5. Outbound: ACC-9004 -> ACC-9100 via TX-9009 (INR 250,000, 2026-10-12T12:03:00+00:00)
6. Shared links: ACC-9002 (score 4.5) devices=['DEV-9001', 'DEV-9002'] beneficiaries=['ACC-9004'] alerts=['AL-9001']
7. Shared links: ACC-9001 (score 2.0) devices=['DEV-9001'] beneficiaries=[] alerts=['AL-9003']
8. Shared links: ACC-9004 (score 2.0) devices=['DEV-9002'] beneficiaries=[] alerts=['AL-9002']
9. Clustering: top cluster score 42.05 over ACC-9002, ACC-9004, ACC-9003, ACC-9100, ACC-9001, ACC-9005

**Graph path**
ACC-EXT-01 -[ORIGINATED]-> TX-9000 -[DESTINED]-> ACC-9001 -[ORIGINATED]-> TX-9001 -[DESTINED]-> ACC-9002 -[ORIGINATED]-> TX-9008 -[DESTINED]-> ACC-9100

**Members flagged by the clustering algorithm**
- ACC-9002 | risk 0.81 | devices ['DEV-9001', 'DEV-9002'] | beneficiaries ['ACC-9004', 'ACC-9100'] | alerts ['high']
- ACC-9004 | risk 0.85 | devices ['DEV-9002'] | beneficiaries ['ACC-9005', 'ACC-9100'] | alerts ['medium']
- ACC-9003 | risk 0.77 | devices ['DEV-9001', 'DEV-9002'] | beneficiaries ['ACC-9004'] | alerts ['high']
- ACC-9100 (shared_beneficiary) | risk 0.66 | devices [] | beneficiaries ['ACC-9002', 'ACC-9004', 'ACC-9005'] | alerts []
- ACC-9001 | risk 0.72 | devices ['DEV-9001'] | beneficiaries ['ACC-9005'] | alerts ['high']
- ACC-9005 | risk 0.9 | devices ['DEV-9002'] | beneficiaries ['ACC-9100'] | alerts []

**Tools & queries used** — list_handoffs, accept_handoff, recall, entity_search, graph_neighbors, trace_money_flow, find_shared_links, detect_suspicious_cluster, shortest_connection, open_case, add_case_note, flag_account, remember

**Actions taken** — opened CASE-0003 involving ACC-9002, ACC-9004, ACC-9003, ACC-9100, ACC-9001, ACC-9005, DEV-9001, DEV-9002; added evidence note NOTE-a04b4f to CASE-0003; flagged ACC-9003 as high (AL-2b0e34), status under_review; wrote memory MEM-6222485a linked to ['ACC-9002', 'ACC-9004', 'ACC-9003', 'ACC-9100', 'ACC-9001', 'ACC-9005']

**Confidence** — high. Signals agree (shared devices + shared beneficiary + prior alerts + same-day pass-through). Still missing: beneficial-owner confirmation for ACC-9100 and the source of funds behind ACC-EXT-01.

### Investigator tool trace

1. `list_handoffs` → `handoffs=[{'handoff_id': 'HANDOFF-f91c02', 'from_role': 'triage', 'to_role': 'investigator', 'status': 'pending', 'context': 'Triage escalated ACC-9003: 6-account cluster (ACC-9002, ACC-9004, ACC-9003`
2. `accept_handoff` → `handoff_id=HANDOFF-f91c02, from_role=triage, to_role=investigator, context=Triage escalated ACC-9003: 6-account cluster (ACC-9002, ACC-9004, ACC-9003, ACC-9100, ACC-9001, ACC-9005), score 42.05. Share`
3. `recall` → `query=ACC-9003, kind=any, notes=[{'note_id': 'MEM-e06e6e0c', 'kind': 'episodic', 'text': 'Triage on ACC-9003: 6-account cluster (ACC-9002, ACC-9004, ACC-9003, ACC-9100, ACC-9001, ACC-9005), score 42.0`
4. `entity_search` → `matches=[{'id': 'ACC-9003', 'labels': ['Account'], 'props': {'type': 'current', 'status': 'under_review', 'risk_score': 0.77, 'opened_at': '2026-03-16T09:00:00+00:00'}}], count=1`
5. `graph_neighbors` → `start=ACC-9003, depth=2, nodes=[{'id': 'MEM-e06e6e0c', 'labels': ['MemoryNote'], 'props': {'kind': 'episodic', 'text': 'Triage on ACC-9003: 6-account cluster (ACC-9002, ACC-9004, ACC-9003, ACC-9100, A`
6. `trace_money_flow` → `account=ACC-9003, depth=4, outgoing={'levels': [{'level': 1, 'hops': [{'from': 'ACC-9003', 'to': 'ACC-9004', 'tx': 'TX-9003', 'amount': 1790000.0, 'timestamp': '2026-10-12T11:21:00+00:00'}]}, {'level'`
7. `find_shared_links` → `account=ACC-9003, linked_accounts=4, links=[{'account': 'ACC-9002', 'score': 4.5, 'shared_devices': ['DEV-9001', 'DEV-9002'], 'shared_beneficiaries': ['ACC-9004'], 'alerts': ['AL-9001']}, {'account': `
8. `detect_suspicious_cluster` → `algorithm=union-find over shared devices + beneficiary collectors + alerts, min_signals=2, clusters_found=1, clusters_filtered_out=4`
9. `shortest_connection` → `from=ACC-EXT-01, to=ACC-9100, connected=True, hops=6`
10. `open_case` → `case_id=CASE-0003, title=Mule ring around ACC-9003, involves=['ACC-9002', 'ACC-9004', 'ACC-9003', 'ACC-9100', 'ACC-9001', 'ACC-9005', 'DEV-9001', 'DEV-9002'], status=open`
11. `add_case_note` → `case_id=CASE-0003, note_id=NOTE-a04b4f, saved=True`
12. `flag_account` → `flagged=True, account=ACC-9003, alert_id=AL-2b0e34, severity=high`
13. `remember` → `note_id=MEM-6222485a, kind=episodic, linked_to=['ACC-9002', 'ACC-9004', 'ACC-9003', 'ACC-9100', 'ACC-9001', 'ACC-9005'], text=Investigated ACC-9003: 6-account cluster (ACC-9002, ACC-9004, ACC-9003, AC`

**Actions taken (verified from the tool log):** `accept_handoff(handoff_id=HANDOFF-f91c02)`, `open_case(title=Mule ring around ACC-9003, summary=6-account cluster (ACC-9002, ACC-9004..., entity_ids=['ACC-9002', 'ACC-9004', 'ACC-9003', ...)`, `add_case_note(case_id=CASE-0003, text=Evidence: 6-account cluster (ACC-9002...)`, `flag_account(account_id=ACC-9003, severity=high, reason=Shares device DEV-9001/DEV-9002 with ...)`, `remember(kind=episodic, text=Investigated ACC-9003: 6-account clus..., about_ids=['ACC-9002', 'ACC-9004', 'ACC-9003', ...)`