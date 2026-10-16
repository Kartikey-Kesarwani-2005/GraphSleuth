"""Seed the FalkorDB graph with a synthetic-but-realistic fraud network.

The dataset is deliberately shaped so that *connections matter*:

* ~200 ordinary accounts with ordinary devices and transactions (noise)
* one mule ring: ACC-9001..ACC-9005, which share devices, pass money along a
  chain the same day, and funnel it into the beneficiary account ACC-9100
* prior alerts + a partially investigated case, so the agent's memory has
  something to recall on its very first run

Run with:  python -m graphsleuth seed
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from . import db, memory

RNG = random.Random(42)

FIRST_NAMES = [
    "Aarav", "Isha", "Rohan", "Meera", "Kabir", "Anaya", "Vihaan", "Diya",
    "Arjun", "Saanvi", "Dev", "Nisha", "Rahul", "Priya", "Imran", "Sara",
    "Vikram", "Tara", "Aditya", "Neha", "Karan", "Pooja", "Manish", "Riya",
    "Siddharth", "Kavya", "Naveen", "Anita", "Farhan", "Lakshmi",
]
LAST_NAMES = [
    "Sharma", "Verma", "Patel", "Reddy", "Nair", "Iyer", "Khan", "Singh",
    "Gupta", "Mehta", "Joshi", "Chopra", "Bose", "Rao", "Kapoor", "Das",
]
COUNTRIES = ["IN", "IN", "IN", "AE", "SG", "GB", "US"]
MERCHANT_NAMES = [
    ("M-01", "QuickMart Online", "grocery"),
    ("M-02", "SkyJet Travels", "travel"),
    ("M-03", "PixelPlay Games", "gaming"),
    ("M-04", "CashEasy Loans", "financial"),
    ("M-05", "GoldVault Jewels", "luxury"),
    ("M-06", "FuelPoint", "fuel"),
    ("M-07", "MediCare Pharmacy", "health"),
    ("M-08", "TechBazaar", "electronics"),
    ("M-09", "CryptoBridge Exchange", "crypto"),
    ("M-10", "Foodly", "food"),
]

NORMAL_ACCOUNTS = 200
NORMAL_TRANSFERS = 900
MERCHANT_PAYMENTS = 300

RING_ACCOUNTS = ["ACC-9001", "ACC-9002", "ACC-9003", "ACC-9004", "ACC-9005"]
BENEFICIARY = "ACC-9100"
EXTERNAL = "ACC-EXT-01"
RING_DAY = datetime(2026, 10, 12, 11, 0, tzinfo=timezone.utc)


def _ts(offset_days: float = 0.0, base: datetime | None = None) -> str:
    base = base or datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
    return (base + timedelta(days=offset_days)).isoformat(timespec="seconds")


def _batch(cypher: str, rows: list[dict], size: int = 400) -> None:
    for start in range(0, len(rows), size):
        db.query(cypher, {"rows": rows[start : start + size]})


def _wipe() -> None:
    db.query("MATCH (n) DETACH DELETE n RETURN count(n) AS row")


def _create_indexes() -> None:
    """Range indexes on the ids we look up constantly (DDL: no RETURN clause)."""
    statements = [
        "CREATE INDEX FOR (a:Account) ON (a.id)",
        "CREATE INDEX FOR (p:Person) ON (p.id)",
        "CREATE INDEX FOR (t:Transaction) ON (t.id)",
        "CREATE INDEX FOR (d:Device) ON (d.id)",
        "CREATE INDEX FOR (m:Merchant) ON (m.id)",
        "CREATE INDEX FOR (al:Alert) ON (al.id)",
        "CREATE INDEX FOR (c:Case) ON (c.id)",
        "CREATE INDEX FOR (n:MemoryNote) ON (n.id)",
    ]
    for statement in statements:
        try:
            db.query(statement)
        except Exception:
            pass  # an existing index (or older syntax) is not a problem


def _make_entities() -> tuple[list[dict], list[dict], list[dict]]:
    people: list[dict] = []
    accounts: list[dict] = []
    devices: list[dict] = []

    for i in range(1, NORMAL_ACCOUNTS + 1):
        pid, acc = f"P-{i:04d}", f"ACC-{i:04d}"
        name = f"{RNG.choice(FIRST_NAMES)} {RNG.choice(LAST_NAMES)}"
        people.append(
            {
                "id": pid,
                "name": name,
                "country": RNG.choice(COUNTRIES),
                "kyc": RNG.random() > 0.15,
                "created_at": _ts(-RNG.uniform(200, 1500)),
            }
        )
        accounts.append(
            {
                "id": acc,
                "type": RNG.choice(["savings", "current", "wallet"]),
                "status": "active",
                "risk_score": round(RNG.uniform(0.02, 0.3), 2),
                "opened_at": _ts(-RNG.uniform(200, 1500)),
            }
        )
        # Mostly one device per account; a few benign shares keep it realistic.
        for d in range(1, RNG.choice([1, 1, 1, 2])):
            devices.append(
                {
                    "account": acc,
                    "device": f"DEV-{i:04d}-{d}",
                    "fingerprint": f"fp-{RNG.randrange(10**9):09d}",
                }
            )

    # A handful of innocent device shares (so shared-device alone is not proof).
    for i in (12, 13, 44, 45, 77, 78, 120, 121):
        devices.append(
            {
                "account": f"ACC-{i:04d}",
                "device": f"DEV-SHARE-{i // 10}",
                "fingerprint": f"fp-{RNG.randrange(10**9):09d}",
            }
        )

    return people, accounts, devices


def _make_normal_transfers(accounts: list[str]) -> list[dict]:
    transfers = []
    for i in range(1, NORMAL_TRANSFERS + 1):
        src, dst = RNG.sample(accounts, 2)
        transfers.append(
            {
                "id": f"TX-{i:05d}",
                "from": src,
                "to": dst,
                "amount": round(RNG.uniform(500, 80000), 2),
                "ts": _ts(-RNG.uniform(0, 90)),
                "channel": RNG.choice(["upi", "neft", "imps", "wallet"]),
            }
        )
    return transfers


def _make_merchant_payments(accounts: list[str]) -> list[dict]:
    payments = []
    for i in range(1, MERCHANT_PAYMENTS + 1):
        merchant_id, _, category = RNG.choice(MERCHANT_NAMES)
        amount = round(RNG.uniform(100, 8000), 2)
        if category in {"luxury", "crypto", "financial"}:
            amount = round(RNG.uniform(20000, 250000), 2)
        payments.append(
            {
                "id": f"TX-M{i:05d}",
                "account": RNG.choice(accounts),
                "merchant": merchant_id,
                "amount": amount,
                "ts": _ts(-RNG.uniform(0, 90)),
                "channel": "card",
                "category": category,
            }
        )
    return payments


def _ring_people_and_accounts() -> tuple[list[dict], list[dict]]:
    people = [
        {
            "id": "P-9001", "name": "Ravi Chandran", "country": "IN", "kyc": True,
            "created_at": _ts(-420),
        },
        {
            "id": "P-9002", "name": "Sunita Yadav", "country": "IN", "kyc": False,
            "created_at": _ts(-300),
        },
        {
            "id": "P-9003", "name": "Deepak Rana", "country": "NP", "kyc": False,
            "created_at": _ts(-210),
        },
        {
            "id": "P-9004", "name": "Mohan Pillai", "country": "IN", "kyc": True,
            "created_at": _ts(-180),
        },
        {
            "id": "P-9005", "name": "Farida Sheikh", "country": "AE", "kyc": False,
            "created_at": _ts(-120),
        },
        {
            "id": "P-9100", "name": "Nagaraj Traders", "country": "IN", "kyc": True,
            "created_at": _ts(-600),
        },
        {
            "id": "P-EXT-01", "name": "Overseas Remittance Desk", "country": "AE",
            "kyc": True, "created_at": _ts(-900),
        },
    ]
    accounts = [
        {"id": "ACC-9001", "type": "savings", "status": "active", "risk_score": 0.72, "opened_at": _ts(-420)},
        {"id": "ACC-9002", "type": "savings", "status": "active", "risk_score": 0.81, "opened_at": _ts(-300)},
        {"id": "ACC-9003", "type": "current", "status": "active", "risk_score": 0.77, "opened_at": _ts(-210)},
        {"id": "ACC-9004", "type": "wallet", "status": "active", "risk_score": 0.85, "opened_at": _ts(-180)},
        {"id": "ACC-9005", "type": "savings", "status": "active", "risk_score": 0.9, "opened_at": _ts(-120)},
        {"id": "ACC-9100", "type": "current", "status": "active", "risk_score": 0.66, "opened_at": _ts(-600)},
        {"id": "ACC-EXT-01", "type": "external", "status": "dormant", "risk_score": 0.4, "opened_at": _ts(-900)},
    ]
    return people, accounts


def _ring_devices() -> list[dict]:
    fingerprint = "fp-9001aa77"
    shared = "fp-9002bb88"
    return [
        {"account": "ACC-9001", "device": "DEV-9001", "fingerprint": fingerprint},
        {"account": "ACC-9002", "device": "DEV-9001", "fingerprint": fingerprint},
        {"account": "ACC-9003", "device": "DEV-9001", "fingerprint": fingerprint},
        {"account": "ACC-9003", "device": "DEV-9002", "fingerprint": shared},
        {"account": "ACC-9004", "device": "DEV-9002", "fingerprint": shared},
        {"account": "ACC-9005", "device": "DEV-9002", "fingerprint": shared},
        {"account": "ACC-9002", "device": "DEV-9002", "fingerprint": shared},
        {"account": "ACC-9001", "device": "DEV-9001-B", "fingerprint": "fp-9001cc99"},
        {"account": "ACC-9005", "device": "DEV-9005-B", "fingerprint": "fp-9005dd11"},
        {"account": "ACC-9100", "device": "DEV-9100", "fingerprint": "fp-9100ee22"},
    ]


def _ring_transactions() -> list[dict]:
    chain = [
        ("TX-9000", EXTERNAL, "ACC-9001", 2000000.0),
        ("TX-9001", "ACC-9001", "ACC-9002", 1850000.0),
        ("TX-9002", "ACC-9002", "ACC-9003", 1820000.0),
        ("TX-9003", "ACC-9003", "ACC-9004", 1790000.0),
        ("TX-9004", "ACC-9004", "ACC-9005", 1760000.0),
        ("TX-9005", "ACC-9005", BENEFICIARY, 1730000.0),
        ("TX-9006", "ACC-9002", "ACC-9004", 260000.0),
        ("TX-9007", "ACC-9001", "ACC-9005", 140000.0),
    ]
    transactions = []
    for index, (tx_id, src, dst, amount) in enumerate(chain):
        transactions.append(
            {
                "id": tx_id,
                "from": src,
                "to": dst,
                "amount": amount,
                "ts": (RING_DAY + timedelta(minutes=7 * index)).isoformat(timespec="seconds"),
                "channel": "rtgs" if amount > 1000000 else "imps",
            }
        )
    return transactions


def _ring_merchant_payments() -> list[dict]:
    return [
        {"id": "TX-M90001", "account": "ACC-9005", "merchant": "M-09", "amount": 480000.0,
         "ts": _ts(-1.2), "channel": "card", "category": "crypto"},
        {"id": "TX-M90002", "account": "ACC-9004", "merchant": "M-05", "amount": 310000.0,
         "ts": _ts(-2.4), "channel": "card", "category": "luxury"},
        {"id": "TX-M90003", "account": "ACC-9003", "merchant": "M-03", "amount": 95000.0,
         "ts": _ts(-3.1), "channel": "card", "category": "gaming"},
    ]


def _ring_alerts() -> list[dict]:
    return [
        {"id": "AL-9001", "account": "ACC-9002", "rule": "velocity: >5 transfers/hour",
         "severity": "high", "ts": _ts(-6)},
        {"id": "AL-9002", "account": "ACC-9004", "rule": "chargeback_ratio: 0.4",
         "severity": "medium", "ts": _ts(-14)},
        {"id": "AL-9003", "account": "ACC-9001", "rule": "mule_account_watchlist",
         "severity": "high", "ts": _ts(-27)},
    ]


def seed(reset: bool = True, verbose: bool = True) -> dict:
    """Create the whole dataset. Safe to run repeatedly (it wipes first by default)."""
    if reset:
        _wipe()
    _create_indexes()

    people, accounts, devices = _make_entities()
    normal_ids = [a["id"] for a in accounts]

    ring_people, ring_accounts = _ring_people_and_accounts()
    people += ring_people
    accounts += ring_accounts
    devices += _ring_devices()

    # --- nodes -----------------------------------------------------------------
    # MERGE + ON CREATE SET keeps re-seeding idempotent (no duplicates on --keep).
    _batch(
        "UNWIND $rows AS r MERGE (p:Person {id: r.id}) "
        "ON CREATE SET p.name = r.name, p.country = r.country, p.kyc = r.kyc, p.created_at = r.created_at "
        "RETURN count(p) AS row",
        people,
    )
    _batch(
        "UNWIND $rows AS r MERGE (a:Account {id: r.id}) "
        "ON CREATE SET a.type = r.type, a.status = r.status, a.risk_score = r.risk_score, "
        "a.opened_at = r.opened_at RETURN count(a) AS row",
        accounts,
    )
    _batch(
        "UNWIND $rows AS r MERGE (m:Merchant {id: r.id}) "
        "ON CREATE SET m.name = r.name, m.category = r.category RETURN count(m) AS row",
        [{"id": mid, "name": name, "category": cat} for mid, name, cat in MERCHANT_NAMES],
    )

    # --- ownership + devices ----------------------------------------------------
    ownership = []
    account_ids = {a["id"] for a in accounts}
    for person in people:
        candidate = f"ACC-{person['id'][2:]}"
        if candidate in account_ids:
            ownership.append({"person": person["id"], "account": candidate})
    _batch(
        "UNWIND $rows AS r MATCH (p:Person {id: r.person}), (a:Account {id: r.account}) "
        "MERGE (p)-[:OWNS]->(a) RETURN count(*) AS row",
        ownership,
    )

    _batch(
        "UNWIND $rows AS r MATCH (a:Account {id: r.account}) "
        "MERGE (d:Device {id: r.device}) ON CREATE SET d.fingerprint = r.fingerprint "
        "MERGE (a)-[:USED_DEVICE]->(d) RETURN count(*) AS row",
        devices,
    )

    # --- transactions -----------------------------------------------------------
    transfers = _make_normal_transfers(normal_ids) + _ring_transactions()
    _batch(
        "UNWIND $rows AS r MATCH (a:Account {id: r.from}), (b:Account {id: r.to}) "
        "MERGE (t:Transaction {id: r.id}) "
        "ON CREATE SET t.amount = r.amount, t.currency = 'INR', t.timestamp = r.ts, "
        "t.channel = r.channel, t.status = 'completed' "
        "MERGE (a)-[:ORIGINATED]->(t) MERGE (t)-[:DESTINED]->(b) "
        "RETURN count(t) AS row",
        transfers,
    )

    payments = _make_merchant_payments(normal_ids) + _ring_merchant_payments()
    _batch(
        "UNWIND $rows AS r MATCH (a:Account {id: r.account}), (m:Merchant {id: r.merchant}) "
        "MERGE (t:Transaction {id: r.id}) "
        "ON CREATE SET t.amount = r.amount, t.currency = 'INR', t.timestamp = r.ts, "
        "t.channel = r.channel, t.status = 'completed', t.category = r.category "
        "MERGE (a)-[:ORIGINATED]->(t) MERGE (t)-[:PAID]->(m) "
        "RETURN count(t) AS row",
        payments,
    )

    # --- alerts -----------------------------------------------------------------
    _batch(
        "UNWIND $rows AS r MATCH (a:Account {id: r.account}) "
        "MERGE (al:Alert {id: r.id}) "
        "ON CREATE SET al.rule = r.rule, al.severity = r.severity, al.created_at = r.ts, "
        "al.status = 'open' "
        "MERGE (a)-[:FLAGGED_BY]->(al) RETURN count(al) AS row",
        _ring_alerts(),
    )

    # --- agent memory baseline --------------------------------------------------
    _seed_memory()

    stats = db.query(
        "MATCH (n) WITH count(n) AS nodes OPTIONAL MATCH ()-[r]->() "
        "WITH nodes, count(r) AS rels RETURN [nodes, rels] AS row"
    )[0]
    summary = {"nodes": stats[0], "relationships": stats[1], "accounts": len(accounts)}
    if verbose:
        print(f"Seeded graph: {summary['nodes']} nodes, {summary['relationships']} relationships")
    return summary


def _seed_memory() -> None:
    """Baseline memory: a playbook, a past finding, and an already-open case."""
    memory.remember(
        "procedural",
        "Playbook: (1) look up the entity, (2) pull its 2-hop neighbourhood, (3) trace money flow "
        "in and out, (4) check shared devices and shared beneficiaries, (5) rank suspicious "
        "clusters, (6) only then conclude. Two or more signals = escalate.",
        note_id="MEM-PLAYBOOK",
    )
    memory.remember(
        "episodic",
        "2026-10-10: Alert AL-9001 (velocity: >5 transfers/hour) fired on ACC-9002. "
        "Device DEV-9001 on that account was also seen on ACC-9001 and ACC-9003.",
        about_ids=["ACC-9002", "DEV-9001", "AL-9001"],
        note_id="MEM-2026-10-10-AL9001",
    )
    memory.remember(
        "semantic",
        "ACC-9100 (Nagaraj Traders) mostly receives small routine transfers, but on 2026-10-12 "
        "it received INR 1,730,000 from ACC-9005 — far outside its normal pattern.",
        about_ids=["ACC-9100", "ACC-9005", "TX-9005"],
        note_id="MEM-ACC9100-OUTLIER",
    )

    db.query(
        "MERGE (c:Case {id: 'CASE-0001'}) "
        "ON CREATE SET c.title = 'Outbound ring around device DEV-9001', c.status = 'open', "
        "c.created_at = $ts, c.summary = 'Opened after AL-9001. Suspected mule chain through "
        "ACC-9001, ACC-9002 and ACC-9003; beneficiary not yet confirmed.' "
        "RETURN c.id AS row",
        {"ts": _ts(-6)},
    )
    for account_id in ("ACC-9001", "ACC-9002", "ACC-9003", "DEV-9001"):
        db.query(
            "MATCH (c:Case {id: 'CASE-0001'}), (e {id: $eid}) MERGE (c)-[:INVOLVES]->(e) "
            "RETURN e.id AS row",
            {"eid": account_id},
        )
