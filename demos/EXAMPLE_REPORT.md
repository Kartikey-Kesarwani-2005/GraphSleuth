# GraphSleuth investigation report

- **Question:** Investigate ACC-9003 — is this account part of a fraud ring?
- **Session:** SES-aed9a460   **Tenant:** default
- **Brain:** offline-scripted   **Rounds:** 16

## Verdict

**Verdict** — ACC-9003 is a confirmed hop in a 6-account mule ring that pools same-day transfers into beneficiary account ACC-9100. Escalated.

**Evidence chain**
1. Memory: [episodic] 2026-10-10: Alert AL-9001 (velocity: >5 transfers/hour) fired on ACC-9002. Device DEV-9001 on that account was also seen on ACC-9001 and ACC-9003.
2. Inbound: ACC-9002 -> ACC-9003 via TX-9002 (INR 1,820,000, 2026-10-12T11:14:00+00:00)
3. Outbound: ACC-9003 -> ACC-9004 via TX-9003 (INR 1,790,000, 2026-10-12T11:21:00+00:00)
4. Outbound: ACC-9004 -> ACC-9005 via TX-9004 (INR 1,760,000, 2026-10-12T11:28:00+00:00)
5. Outbound: ACC-9004 -> ACC-9100 via TX-9009 (INR 250,000, 2026-10-12T12:03:00+00:00)
6. Shared links: ACC-9002 (score 4.5) devices=['DEV-9001', 'DEV-9002'] beneficiaries=['ACC-9004'] alerts=['AL-9001']
7. Shared links: ACC-9001 (score 2.0) devices=['DEV-9001'] beneficiaries=[] alerts=['AL-9003']
8. Shared links: ACC-9004 (score 2.0) devices=['DEV-9002'] beneficiaries=[] alerts=['AL-9002']
9. Clustering: top cluster score 41.05 over ACC-9002, ACC-9004, ACC-9003, ACC-9100, ACC-9001, ACC-9005

**Graph path**
ACC-EXT-01 -[ORIGINATED]-> TX-9000 -[DESTINED]-> ACC-9001 -[ORIGINATED]-> TX-9001 -[DESTINED]-> ACC-9002 -[ORIGINATED]-> TX-9008 -[DESTINED]-> ACC-9100

**Counterfactual check**
Removing DEV-9001 (minor): the money trail ACC-EXT-01 -> ACC-9100 still runs without DEV-9001 (6 -> 6 hops); the ring still holds without DEV-9001 (5 of 6 members remain, score 41.05 -> 29.33).

**Ring resilience**
Ring of 6 member(s) is mostly_resilient: mean robustness 76/100, mean load-bearing 0.242. No single member is load-bearing — the ring survives losing any one of them.

**Timeline (reconstructed from graph timestamps)**
Activity from 2026-09-15T09:00:00+00:00 to 2026-10-12T12:03:00+00:00: 10 transfer(s) moved INR 12,785,000 (1 in / 9 internal / 0 out), 3 merchant spend(s), 3 alert(s). Burst: 9 events in one hour from 2026-10-12T11:00:00+00:00 (INR 11,650,000). Alerts fired BEFORE the first traced transfer (2026-09-15T09:00:00+00:00 <= 2026-10-12T11:00:00+00:00) — detection led the money.
- activation order: ACC-9001 (11:00) → ACC-9002 (11:07) → ACC-9003 (11:14) → ACC-9004 (11:21) → ACC-9005 (11:28) → ACC-9100 (11:35)

**Pattern check — is this playbook running elsewhere?**
Compared the ring against 4 other candidate group(s): closest match 19/100. Verdict: unique_playbook. No other group in this graph shares the ring's fingerprint — the combination of shared devices, a collector funnel and alerts is unique to this case.

**Members flagged by the clustering algorithm**
- ACC-9002 | risk 0.81 | devices ['DEV-9001', 'DEV-9002'] | beneficiaries ['ACC-9004', 'ACC-9100'] | alerts ['high']
- ACC-9004 | risk 0.85 | devices ['DEV-9002'] | beneficiaries ['ACC-9005', 'ACC-9100'] | alerts ['medium']
- ACC-9003 | risk 0.77 | devices ['DEV-9001', 'DEV-9002'] | beneficiaries ['ACC-9004'] | alerts []
- ACC-9100 (shared_beneficiary) | risk 0.66 | devices [] | beneficiaries ['ACC-9002', 'ACC-9004', 'ACC-9005'] | alerts []
- ACC-9001 | risk 0.72 | devices ['DEV-9001'] | beneficiaries ['ACC-9005'] | alerts ['high']
- ACC-9005 | risk 0.9 | devices ['DEV-9002'] | beneficiaries ['ACC-9100'] | alerts []

**Tools & queries used** — recall, entity_search, graph_neighbors, trace_money_flow, find_shared_links, detect_suspicious_cluster, shortest_connection, what_if_removed, assess_ring_resilience, ring_timeline, find_similar_rings, open_case, add_case_note, flag_account, remember

**Actions taken** — opened CASE-0002 involving ACC-9002, ACC-9004, ACC-9003, ACC-9100, ACC-9001, ACC-9005, DEV-9001, DEV-9002; added evidence note NOTE-6481c0 to CASE-0002; flagged ACC-9003 as high (AL-6a4fa4), status under_review; wrote memory MEM-343061e6 linked to ['ACC-9002', 'ACC-9004', 'ACC-9003', 'ACC-9100', 'ACC-9001', 'ACC-9005']

**Confidence** — high. Signals agree (shared devices + shared beneficiary + prior alerts + same-day pass-through), the verdict survives the counterfactual (robustness 100/100). Still missing: beneficial-owner confirmation for ACC-9100 and the source of funds behind ACC-EXT-01.

## Actions taken (verified from the tool log)

- `open_case(title=Mule ring around ACC-9003, summary=6-account cluster (ACC-9002, ACC-9004..., entity_ids=['ACC-9002', 'ACC-9004', 'ACC-9003', ...)`
- `add_case_note(case_id=CASE-0002, text=Evidence: 6-account cluster (ACC-9002...)`
- `flag_account(account_id=ACC-9003, severity=high, reason=Shares device DEV-9001/DEV-9002 with ...)`
- `remember(kind=episodic, text=Investigated ACC-9003: 6-account clus..., about_ids=['ACC-9002', 'ACC-9004', 'ACC-9003', ...)`

## Evidence trail

```text
 1. recall  [ok]
    args: {'query': 'ACC-9003'}
    query: ACC-9003 | kind: any | notes: 1 items | count: 1 | open_cases: 1 items
 2. entity_search  [ok]
    args: {'query': 'ACC-9003'}
    matches: 1 items | count: 1
 3. graph_neighbors  [ok]
    args: {'entity_id': 'ACC-9003', 'depth': 2}
    start: ACC-9003 | depth: 2 | nodes: 14 items | edges: 27 items | node_count: 14 | edge_count: 27
 4. trace_money_flow  [ok]
    args: {'account_id': 'ACC-9003', 'depth': 4}
    account: ACC-9003 | depth: 4 | outgoing: {levels: 3, reached: 3} | incoming: {levels: 3, reached: 3} | total_out: 5530000.0 | total_in: 5670000.0
 5. find_shared_links  [ok]
    args: {'account_id': 'ACC-9003'}
    account: ACC-9003 | linked_accounts: 4 | links: 4 items | interpretation: shared_devices + shared_beneficiaries + prior alerts are the three mule-ring sig
 6. detect_suspicious_cluster  [ok]
    args: {'min_signals': 2}
    algorithm: union-find over shared devices + beneficiary collectors + alerts | min_signals: 2 | clusters_found: 1 | clusters_filtered_out: 4 | clusters: 1 items
 7. shortest_connection  [ok]
    args: {'id_a': 'ACC-EXT-01', 'id_b': 'ACC-9100', 'max_depth': 8, 'via': ['ORIGINATED', 'DESTINED']}
    from: ACC-EXT-01 | to: ACC-9100 | connected: True | hops: 6 | via: 2 items | path: 7 items | narrative: ACC-EXT-01 -[ORIGINATED]-> TX-9000 -[DESTINED]-> ACC-9001 -[ORIGINATED]-> TX-900
 8. what_if_removed  [ok]
    args: {'entity_id': 'DEV-9001', 'from_id': 'ACC-EXT-01', 'to_id': 'ACC-9100', 'max_depth': 8}
    found: True | entity: DEV-9001 | label: Device | removed_edges: 5 | neighbours: 5 items | bridge: {is_bridge: False, components: 1, pairs_separated: 0, reach_depth: 3} | cluster: {top_before: 2, top_after: 2, members_dropped: 1} | path: {from: ACC-EXT-01, to: ACC-9100, connected_before: True, connected_after: True} | impact: minor | robustness: 100 | verdict: robust | load_bearing_score: 0.0 | min
 9. assess_ring_resilience  [ok]
    args: {'ring_key': 'top'}
    found_ring: True | ring: 6 items | cluster_size_before: 6 | cluster_score_before: 41.05 | endpoints: {from: ACC-EXT-01, to: ACC-9100} | members: 6 items | articulation_points: 0 items | resilience: mostly_resilient | mean_robustness: 76 | mean_load_bearing_score: 0.242 | summary: Ring of 6 member(s) is mostly_resilient: mean robustness 76/100, mean load-beari | note: read-only batch counterfactual
10. ring_timeline  [ok]
    args: {'ring_key': 'top'}
    found: True | members: 6 items | window: {first: 2026-09-15T09:00:00+00:00, last: 2026-10-12T12:03:00+00:00} | totals: {events: 16, transfers: 10, inbound: 1, internal: 9} | burst: {start: 2026-10-12T11:00:00+00:00, end: 2026-10-12T11:56:00+00:00, count: 9, value: 11650000.0} | activation_order: 6 items | detection_lag: alerts fired BEFORE the first traced transfer (2026-09-15T09:00:00+00:00 <= 20
11. find_similar_rings  [ok]
    args: {'ring_key': 'top'}
    found: True | target: {members: 6, size: 6, collectors: 6, alerts: 3} | compared: 4 | closest_similarity: 19 | lookalike_count: 0 | candidates: 3 items | verdict: unique_playbook | verdict_text: No other group in this graph shares the ring's fingerprint — the combination of  | summary: Compared the ring against 4 other candidate group(s): closest match 19/100. Verd | note: read-only — similarity i
12. open_case  [ok]
    args: {'title': 'Mule ring around ACC-9003', 'summary': '6-account cluster (ACC-9002, ACC-9004, ACC-9003, ACC-9100, ACC-9001, ACC-9005), score 41.05', 'entity_ids': ['ACC-9002', 'ACC-9004', 'ACC-9003', 'ACC-9100', 'ACC-9001', 'ACC-9005', 'DEV-9001', 'DEV-9002']}
    case_id: CASE-0002 | title: Mule ring around ACC-9003 | involves: 8 items | status: open
13. add_case_note  [ok]
    args: {'case_id': 'CASE-0002', 'text': 'Evidence: 6-account cluster (ACC-9002, ACC-9004, ACC-9003, ACC-9100, ACC-9001, ACC-9005), score 41.05. Money chain traced from ACC-EXT-01 to ACC-9100 via the shared devices DEV-9001/DEV-9002; prior alerts on ACC-9001, ACC-9002 and ACC-9004.'}
    case_id: CASE-0002 | note_id: NOTE-6481c0 | saved: True
14. flag_account  [ok]
    args: {'account_id': 'ACC-9003', 'severity': 'high', 'reason': 'Shares device DEV-9001/DEV-9002 with flagged accounts, is a pass-through hop in a same-day chain ending at ACC-9100, and sits in the top-ranked cluster.', 'case_id': 'CASE-0002'}
    flagged: True | account: ACC-9003 | alert_id: AL-6a4fa4 | severity: high | case: CASE-0002 | status: under_review
15. remember  [ok]
    args: {'kind': 'episodic', 'text': 'Investigated ACC-9003: 6-account cluster (ACC-9002, ACC-9004, ACC-9003, ACC-9100, ACC-9001, ACC-9005), score 41.05. Confirmed same-day mule chain ACC-EXT-01 -> ACC-9001 -> ... -> ACC-9005 -> ACC-9100 through shared devices DEV-9001/DEV-9002; account flagged and case opened.', 'about_ids': ['ACC-9002', 'ACC-9004', 'ACC-9003', 'ACC-9100', 'ACC-9001', 'ACC-9005']}
    note_id: MEM-343061e6 | kind: episodic | linked_to: 6 items | text: Investigated ACC-9003: 6-account cluster (ACC-9002, ACC-9004, ACC-9003, ACC-9100
```

## Path through the graph

```mermaid
graph LR
    nACC_9003["ACC-9003<br/><i>ACC</i>"]
    nCASE_0001["CASE-0001<br/><i>Case</i>"]
    nTX_M90003["TX-M90003<br/><i>Transaction</i>"]
    nTX_9002["TX-9002<br/><i>Transaction</i>"]
    nTX_9003["TX-9003<br/><i>Transaction</i>"]
    nDEV_9001["DEV-9001<br/><i>Device</i>"]
    nDEV_9002["DEV-9002<br/><i>Device</i>"]
    nP_9003["P-9003<br/><i>Person</i>"]
    nACC_9001["ACC-9001<br/><i>Account</i>"]
    nACC_9002["ACC-9002<br/><i>Account</i>"]
    nM_03["M-03<br/><i>Merchant</i>"]
    nACC_9004["ACC-9004<br/><i>Account</i>"]
    nMEM_2026_10_10_AL9001["MEM-2026-10-10-AL9001<br/><i>MemoryNote</i>"]
    nACC_9005["ACC-9005<br/><i>Account</i>"]
    nACC_9100["ACC-9100<br/><i>Account</i>"]
    nACC_EXT_01["ACC-EXT-01<br/><i>Account</i>"]
    nTX_9000["TX-9000<br/><i>TX</i>"]
    nTX_9001["TX-9001<br/><i>TX</i>"]
    nTX_9008["TX-9008<br/><i>TX</i>"]
    nM_05["M-05<br/><i>Merchant</i>"]
    nM_09["M-09<br/><i>Merchant</i>"]
    nACC_0012["ACC-0012<br/><i>Account</i>"]
    nACC_0013["ACC-0013<br/><i>Account</i>"]
    nACC_0120["ACC-0120<br/><i>Account</i>"]
    nACC_0121["ACC-0121<br/><i>Account</i>"]
    nACC_0044["ACC-0044<br/><i>Account</i>"]
    nACC_0045["ACC-0045<br/><i>Account</i>"]
    nACC_9003 -->|INVOLVES| nCASE_0001
    nACC_9003 -->|ORIGINATED| nTX_M90003
    nACC_9003 -->|DESTINED| nTX_9002
    nACC_9003 -->|ORIGINATED| nTX_9003
    nACC_9003 -->|USED_DEVICE| nDEV_9001
    nACC_9003 -->|USED_DEVICE| nDEV_9002
    nACC_9003 -->|OWNS| nP_9003
    nCASE_0001 -->|INVOLVES| nDEV_9001
    nCASE_0001 -->|INVOLVES| nACC_9001
    nCASE_0001 -->|INVOLVES| nACC_9002
    nCASE_0001 -->|INVOLVES| nACC_9003
    nTX_M90003 -->|PAID| nM_03
    nTX_M90003 -->|ORIGINATED| nACC_9003
    nTX_9002 -->|ORIGINATED| nACC_9002
    nTX_9002 -->|DESTINED| nACC_9003
    nTX_9003 -->|ORIGINATED| nACC_9003
    nTX_9003 -->|DESTINED| nACC_9004
    nDEV_9001 -->|INVOLVES| nCASE_0001
    nDEV_9001 -->|ABOUT| nMEM_2026_10_10_AL9001
    nDEV_9001 -->|USED_DEVICE| nACC_9001
    nDEV_9001 -->|USED_DEVICE| nACC_9002
    nDEV_9001 -->|USED_DEVICE| nACC_9003
    nDEV_9002 -->|USED_DEVICE| nACC_9002
    nDEV_9002 -->|USED_DEVICE| nACC_9003
    nDEV_9002 -->|USED_DEVICE| nACC_9004
    nDEV_9002 -->|USED_DEVICE| nACC_9005
    nP_9003 -->|OWNS| nACC_9003
    nACC_9003 -->|TX-9003| nACC_9004
    nACC_9004 -->|TX-9004| nACC_9005
    nACC_9004 -->|TX-9009| nACC_9100
    nACC_9005 -->|TX-9005| nACC_9100
    nACC_9002 -->|TX-9002| nACC_9003
    nACC_9001 -->|TX-9001| nACC_9002
    nACC_EXT_01 -->|TX-9000| nACC_9001
    nACC_9002 -->|SHARED_DEVICE| nDEV_9001
    nACC_9002 -->|SHARED_DEVICE| nDEV_9002
    nACC_9001 -->|SHARED_DEVICE| nDEV_9001
    nACC_9004 -->|SHARED_DEVICE| nDEV_9002
    nACC_9005 -->|SHARED_DEVICE| nDEV_9002
    nACC_EXT_01 -->|ORIGINATED| nTX_9000
    nTX_9000 -->|DESTINED| nACC_9001
    nACC_9001 -->|ORIGINATED| nTX_9001
    nTX_9001 -->|DESTINED| nACC_9002
    nACC_9002 -->|ORIGINATED| nTX_9008
    nTX_9008 -->|DESTINED| nACC_9100
    nMEM_2026_10_10_AL9001 -->|ABOUT| nDEV_9001
    nACC_9001 -->|USED_DEVICE| nDEV_9001
    nACC_9002 -->|USED_DEVICE| nDEV_9001
    nACC_9003 -->|PAID| nM_03
    nACC_9004 -->|PAID| nM_05
    nACC_9005 -->|PAID| nM_09
    nACC_EXT_01 -->|TX| nACC_9001
    nACC_9001 -->|TX| nACC_9002
    nACC_9002 -->|TX| nACC_9003
    nACC_9003 -->|TX| nACC_9004
    nACC_9004 -->|TX| nACC_9005
    nACC_9005 -->|TX| nACC_9100
    nACC_9002 -->|TX| nACC_9004
    nACC_9001 -->|TX| nACC_9005
    nACC_9002 -->|TX| nACC_9100
```
