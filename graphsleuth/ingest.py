"""CSV ingestion — bring your own accounts, transfers, devices and alerts.

    python -m graphsleuth import --accounts accounts.csv --transactions tx.csv \
        [--devices devices.csv] [--people people.csv] [--merchants merchants.csv] \
        [--alerts alerts.csv] [--tenant acme] [--reset] [--dry-run]

The data lands in the same schema `seed` builds — `Account`, `Person`, `Device`,
`Merchant`, `Transaction`, `Alert` nodes and `OWNS` / `USED_DEVICE` / `ORIGINATED` /
`DESTINED` / `PAID` / `FLAGGED_BY` edges — so every tool, the console and the agents
work on imported data unchanged.

Design rules:

* **Validate before writing.** Required columns, duplicate ids, numbers and ISO-8601
  timestamps are checked first; rows that reference an account which is neither in
  the file nor already in the graph are an error (they would otherwise be dropped
  silently by a `MATCH`), unless `--allow-missing` says to skip them.
* **Re-import is a sync, not an append.** Account/Person/Merchant properties are
  `SET`; Transaction, Device and Alert nodes are replaced by id, so editing the CSV
  and importing again cannot leave a stale money path or device behind.
* **`--dry-run` needs no database** — it parses, validates and prints what would
  happen.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import db

DEFAULT_COLUMNS = {
    "accounts": "id,type,status,risk_score,opened_at",
    "transactions": "id,from,to,amount,timestamp,channel[,currency,merchant,category]",
    "devices": "device_id,account[,fingerprint]",
    "people": "id,name,country,kyc,created_at[,account]",
    "merchants": "id,name,category",
    "alerts": "id,account,rule,severity,created_at",
}

# Header names that mean the canonical column, per table.
_ALIASES: dict[str, dict[str, str]] = {
    "accounts": {"account_id": "id", "account_type": "type", "risk": "risk_score", "created_at": "opened_at"},
    "transactions": {
        "tx_id": "id",
        "transaction_id": "id",
        "from_account": "from",
        "sender": "from",
        "source": "from",
        "to_account": "to",
        "receiver": "to",
        "destination": "to",
        "beneficiary": "to",
        "ts": "timestamp",
        "date": "timestamp",
        "datetime": "timestamp",
        "amount_inr": "amount",
    },
    "devices": {"device_id": "id", "device": "id", "account_id": "account", "fp": "fingerprint"},
    "people": {"person_id": "id", "account_id": "account", "created": "created_at", "name_full": "name"},
    "merchants": {"merchant_id": "id", "merchant_name": "name"},
    "alerts": {
        "alert_id": "id",
        "account_id": "account",
        "created": "created_at",
        "ts": "created_at",
        "timestamp": "created_at",
        "rule_id": "rule",
    },
}


class IngestError(ValueError):
    """A file or row that cannot be imported safely."""


@dataclass
class Bundle:
    """Validated rows, ready to write (no database involved)."""

    accounts: list[dict] = field(default_factory=list)
    people: list[dict] = field(default_factory=list)
    devices: list[dict] = field(default_factory=list)
    merchants: list[dict] = field(default_factory=list)
    transactions: list[dict] = field(default_factory=list)
    alerts: list[dict] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {name: len(getattr(self, name)) for name in DEFAULT_COLUMNS}

    def total(self) -> int:
        return sum(self.counts().values())


# --------------------------------------------------------------------------- #
# parsing (pure — no graph access, fully unit-testable)
# --------------------------------------------------------------------------- #
def _table(path: str, required: tuple[str, ...], table: str = "") -> list[dict]:
    file = Path(path)
    if not file.exists():
        raise IngestError(f"{table or file.name}: file not found: {path}")
    with file.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        raw_header = [str(name).strip().lower().replace(" ", "_") for name in (reader.fieldnames or [])]
        if not raw_header:
            raise IngestError(f"{table}: {path} has no header row")
        aliases = _ALIASES.get(table, {})
        header = [aliases.get(name, name) for name in raw_header]
        missing = [column for column in required if column not in header]
        if missing:
            raise IngestError(
                f"{table}: {path} is missing required column(s) {', '.join(missing)} "
                f"(expected: {DEFAULT_COLUMNS.get(table, ', '.join(required))})"
            )
        rows: list[dict] = []
        for raw in reader:
            row: dict[str, str] = {}
            for key, value in raw.items():
                if key is None:
                    continue
                name = str(key).strip().lower().replace(" ", "_")
                row[aliases.get(name, name)] = (value or "").strip()
            rows.append(row)
        return rows


def _pick(row: dict, *names: str) -> str:
    for name in names:
        if row.get(name):
            return str(row[name])
    return ""


def _required(value: str, table: str, line: int, column: str) -> str:
    if not value:
        raise IngestError(f"{table} line {line}: '{column}' is required")
    return value


def _number(value: str, table: str, line: int, column: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise IngestError(f"{table} line {line}: '{column}'='{value}' is not a number") from exc


def _timestamp(value: str, table: str, line: int, column: str) -> str:
    if not value:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IngestError(
            f"{table} line {line}: '{column}'='{value}' is not ISO-8601 "
            "(for example 2026-10-12T11:21:00+00:00)"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat(timespec="seconds")


def _boolean(value: str, table: str, line: int, column: str, default: bool) -> bool:
    if not value:
        return default
    text = value.strip().lower()
    if text in {"1", "true", "yes", "y", "verified"}:
        return True
    if text in {"0", "false", "no", "n", "unverified"}:
        return False
    raise IngestError(f"{table} line {line}: '{column}'='{value}' is not true/false")


def _unique(value: str, seen: set[str], table: str, line: int) -> str:
    if value in seen:
        raise IngestError(f"{table} line {line}: duplicate id '{value}'")
    seen.add(value)
    return value


def load_accounts(path: str) -> list[dict]:
    rows = _table(path, ("id",), "accounts")
    accounts: list[dict] = []
    seen: set[str] = set()
    for line, row in enumerate(rows, start=2):
        identifier = _unique(_required(_pick(row, "id"), "accounts", line, "id"), seen, "accounts", line)
        accounts.append(
            {
                "id": identifier,
                "type": _pick(row, "type") or "current",
                "status": _pick(row, "status") or "active",
                "risk_score": _number(_pick(row, "risk_score") or "0.1", "accounts", line, "risk_score"),
                "opened_at": _timestamp(_pick(row, "opened_at", "created_at"), "accounts", line, "opened_at"),
            }
        )
    return accounts


def load_people(path: str) -> list[dict]:
    rows = _table(path, ("id",), "people")
    people: list[dict] = []
    seen: set[str] = set()
    for line, row in enumerate(rows, start=2):
        identifier = _unique(_required(_pick(row, "id"), "people", line, "id"), seen, "people", line)
        people.append(
            {
                "id": identifier,
                "name": _pick(row, "name") or identifier,
                "country": _pick(row, "country"),
                "kyc": _boolean(_pick(row, "kyc", "verified"), "people", line, "kyc", True),
                "created_at": _timestamp(_pick(row, "created_at", "created"), "people", line, "created_at"),
                "account": _pick(row, "account"),
            }
        )
    return people


def load_devices(path: str) -> list[dict]:
    rows = _table(path, ("id", "account"), "devices")
    devices: list[dict] = []
    for line, row in enumerate(rows, start=2):
        # No duplicate-id check here: one device id appearing on several accounts is
        # the shared-device signal, not an error (that is the row shape that matters).
        devices.append(
            {
                "id": _required(_pick(row, "id"), "devices", line, "device_id"),
                "account": _required(_pick(row, "account"), "devices", line, "account"),
                "fingerprint": _pick(row, "fingerprint") or _pick(row, "id"),
            }
        )
    return devices


def load_merchants(path: str) -> list[dict]:
    rows = _table(path, ("id",), "merchants")
    merchants: list[dict] = []
    seen: set[str] = set()
    for line, row in enumerate(rows, start=2):
        identifier = _unique(_required(_pick(row, "id"), "merchants", line, "id"), seen, "merchants", line)
        merchants.append(
            {
                "id": identifier,
                "name": _pick(row, "name") or identifier,
                "category": _pick(row, "category") or "other",
            }
        )
    return merchants


def load_transactions(path: str) -> list[dict]:
    rows = _table(path, ("id",), "transactions")
    transactions: list[dict] = []
    seen: set[str] = set()
    for line, row in enumerate(rows, start=2):
        identifier = _unique(_required(_pick(row, "id"), "transactions", line, "id"), seen, "transactions", line)
        source = _pick(row, "from")
        destination = _pick(row, "to")
        merchant = _pick(row, "merchant")
        if merchant:
            if not source:
                raise IngestError(
                    f"transactions line {line}: a merchant payment needs a 'from' account "
                    "(the payer); 'to' is then optional"
                )
        elif not (source and destination):
            raise IngestError(
                f"transactions line {line}: needs 'from' and 'to' account ids, or 'from' + 'merchant'"
            )
        transactions.append(
            {
                "id": identifier,
                "from": source,
                "to": destination,
                "merchant": merchant,
                "amount": _number(_required(_pick(row, "amount"), "transactions", line, "amount"), "transactions", line, "amount"),
                "ts": _timestamp(_pick(row, "timestamp", "datetime"), "transactions", line, "timestamp"),
                "channel": _pick(row, "channel") or "imported",
                "currency": _pick(row, "currency") or "INR",
                "category": _pick(row, "category"),
            }
        )
    return transactions


def load_alerts(path: str) -> list[dict]:
    rows = _table(path, ("id", "account"), "alerts")
    alerts: list[dict] = []
    seen: set[str] = set()
    for line, row in enumerate(rows, start=2):
        identifier = _unique(_required(_pick(row, "id"), "alerts", line, "id"), seen, "alerts", line)
        alerts.append(
            {
                "id": identifier,
                "account": _required(_pick(row, "account"), "alerts", line, "account"),
                "rule": _pick(row, "rule") or "imported",
                "severity": _pick(row, "severity") or "medium",
                "created_at": _timestamp(_pick(row, "created_at"), "alerts", line, "created_at"),
            }
        )
    return alerts


def load_files(
    accounts_path: str,
    transactions_path: str,
    *,
    devices_path: str = "",
    people_path: str = "",
    merchants_path: str = "",
    alerts_path: str = "",
) -> Bundle:
    """Parse and validate every file. Raises `IngestError`; touches no database."""
    return Bundle(
        accounts=load_accounts(accounts_path),
        transactions=load_transactions(transactions_path),
        devices=load_devices(devices_path) if devices_path else [],
        people=load_people(people_path) if people_path else [],
        merchants=load_merchants(merchants_path) if merchants_path else [],
        alerts=load_alerts(alerts_path) if alerts_path else [],
    )


# --------------------------------------------------------------------------- #
# reference checking (pure)
# --------------------------------------------------------------------------- #
@dataclass
class Filtered:
    bundle: Bundle
    dropped: list[str]
    messages: list[str]


def filter_unknown_references(bundle: Bundle, known_accounts: set[str], allow_missing: bool) -> Filtered:
    """Drop (or refuse) rows whose account id is neither imported nor already in the graph.

    A `MATCH (a:Account {id: ...})` that finds nothing silently writes no row at all,
    which would make a partially imported file look complete. Instead the bad rows are
    collected and either reported as an error or skipped explicitly.
    """
    kept = Bundle(accounts=bundle.accounts, merchants=bundle.merchants)
    dropped: list[str] = []

    def check(account: str, table: str, identifier: str) -> bool:
        if account and account not in known_accounts:
            dropped.append(f"{table} '{identifier}' -> unknown account '{account}'")
            return False
        return True

    kept.people = [p for p in bundle.people if check(p["account"], "people", p["id"])]
    kept.devices = [d for d in bundle.devices if check(d["account"], "devices", d["id"])]
    kept.alerts = [a for a in bundle.alerts if check(a["account"], "alerts", a["id"])]
    for transaction in bundle.transactions:
        ok = check(transaction["from"], "transactions", transaction["id"])
        if ok and transaction["to"]:
            ok = check(transaction["to"], "transactions", transaction["id"])
        if not ok:
            continue
        if transaction["merchant"]:
            kept.merchants.append(
                {
                    "id": transaction["merchant"],
                    "name": transaction["merchant"],
                    "category": transaction.get("category") or "other",
                }
            )
        kept.transactions.append(transaction)

    messages: list[str] = []
    if dropped:
        detail = "; ".join(dropped[:5]) + (f" (+{len(dropped) - 5} more)" if len(dropped) > 5 else "")
        if allow_missing:
            messages.append(f"skipped {len(dropped)} row(s) referencing accounts not in the import or graph: {detail}")
        else:
            raise IngestError(
                f"{len(dropped)} row(s) reference accounts that are in neither the accounts file nor the graph: "
                f"{detail}. Add them to the accounts file, or pass --allow-missing to skip."
            )
    # merchants may appear twice (a merchants.csv row and a payment referencing the same id)
    seen: set[str] = set()
    deduped: list[dict] = []
    for merchant in kept.merchants:
        if merchant["id"] not in seen:
            seen.add(merchant["id"])
            deduped.append(merchant)
    kept.merchants = deduped
    return Filtered(bundle=kept, dropped=dropped, messages=messages)


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #
def _replace_by_id(label: str, rows: list[dict]) -> None:
    """Delete existing nodes of this label with these ids, so a re-import cannot
    leave a stale edge behind (an edited transaction endpoint, a moved device)."""
    if not rows:
        return
    db.query(
        f"UNWIND $rows AS r MATCH (n:{label} {{id: r.id}}) DETACH DELETE n RETURN count(n) AS row",
        {"rows": [{"id": row["id"]} for row in rows]},
    )


def write_bundle(bundle: Bundle, *, reset: bool = False) -> dict:
    """Write a validated bundle into the active tenant's graph."""
    before = db.ping()
    if reset:
        db.wipe_graph()
    db.create_indexes(("Account", "Person", "Device", "Merchant", "Transaction", "Alert"))

    db.insert_rows(
        "UNWIND $rows AS r MERGE (a:Account {id: r.id}) "
        "SET a.type = r.type, a.status = r.status, a.risk_score = r.risk_score, a.opened_at = r.opened_at "
        "RETURN count(a) AS row",
        bundle.accounts,
    )
    db.insert_rows(
        "UNWIND $rows AS r MERGE (p:Person {id: r.id}) "
        "SET p.name = r.name, p.country = r.country, p.kyc = r.kyc, p.created_at = r.created_at "
        "RETURN count(p) AS row",
        bundle.people,
    )
    db.insert_rows(
        "UNWIND $rows AS r MATCH (p:Person {id: r.id}), (a:Account {id: r.account}) "
        "MERGE (p)-[:OWNS]->(a) RETURN count(*) AS row",
        [p for p in bundle.people if p["account"]],
    )
    db.insert_rows(
        "UNWIND $rows AS r MERGE (m:Merchant {id: r.id}) "
        "SET m.name = r.name, m.category = r.category RETURN count(m) AS row",
        bundle.merchants,
    )

    # Devices and alerts are replaced wholesale by id: their edges are the data.
    _replace_by_id("Device", bundle.devices)
    db.insert_rows(
        "UNWIND $rows AS r MATCH (a:Account {id: r.account}) MERGE (d:Device {id: r.id}) "
        "SET d.fingerprint = r.fingerprint MERGE (a)-[:USED_DEVICE]->(d) RETURN count(d) AS row",
        bundle.devices,
    )
    _replace_by_id("Alert", bundle.alerts)
    db.insert_rows(
        "UNWIND $rows AS r MATCH (a:Account {id: r.account}) MERGE (al:Alert {id: r.id}) "
        "SET al.rule = r.rule, al.severity = r.severity, al.created_at = r.created_at, al.status = 'open' "
        "MERGE (a)-[:FLAGGED_BY]->(al) RETURN count(al) AS row",
        bundle.alerts,
    )

    _replace_by_id("Transaction", bundle.transactions)
    db.insert_rows(
        "UNWIND $rows AS r MATCH (a:Account {id: r.from}), (b:Account {id: r.to}) "
        "MERGE (t:Transaction {id: r.id}) "
        "SET t.amount = r.amount, t.currency = r.currency, t.timestamp = r.ts, t.channel = r.channel, "
        "t.status = 'completed' MERGE (a)-[:ORIGINATED]->(t) MERGE (t)-[:DESTINED]->(b) "
        "RETURN count(t) AS row",
        [t for t in bundle.transactions if not t["merchant"]],
    )
    db.insert_rows(
        "UNWIND $rows AS r MATCH (a:Account {id: r.from}), (m:Merchant {id: r.merchant}) "
        "MERGE (t:Transaction {id: r.id}) "
        "SET t.amount = r.amount, t.currency = r.currency, t.timestamp = r.ts, t.channel = r.channel, "
        "t.status = 'completed', t.category = r.category "
        "MERGE (a)-[:ORIGINATED]->(t) MERGE (t)-[:PAID]->(m) RETURN count(t) AS row",
        [t for t in bundle.transactions if t["merchant"]],
    )

    after = db.ping()
    return {"before": before, "after": after}


def import_csv(
    accounts_path: str,
    transactions_path: str,
    *,
    devices_path: str = "",
    people_path: str = "",
    merchants_path: str = "",
    alerts_path: str = "",
    tenant: str | None = None,
    reset: bool = False,
    allow_missing: bool = False,
    dry_run: bool = False,
) -> dict:
    """Parse, validate and (unless `dry_run`) import the files. Returns a report dict.

    Everything runs inside `db.tenant_scope(tenant)`, so the import lands in the graph
    it names — never in whatever tenant the caller happened to be in.
    """
    tenant = db.resolve_tenant(tenant)
    with db.tenant_scope(tenant):
        return _import_in_scope(
            accounts_path,
            transactions_path,
            devices_path=devices_path,
            people_path=people_path,
            merchants_path=merchants_path,
            alerts_path=alerts_path,
            dry_run=dry_run,
            reset=reset,
            allow_missing=allow_missing,
        )


def _import_in_scope(
    accounts_path: str,
    transactions_path: str,
    *,
    devices_path: str,
    people_path: str,
    merchants_path: str,
    alerts_path: str,
    dry_run: bool,
    reset: bool,
    allow_missing: bool,
) -> dict:
    """The import, already scoped to the tenant whose graph it writes."""
    tenant = db.resolve_tenant()
    bundle = load_files(
        accounts_path,
        transactions_path,
        devices_path=devices_path,
        people_path=people_path,
        merchants_path=merchants_path,
        alerts_path=alerts_path,
    )

    notes: list[str] = []
    try:
        known = {str(row) for row in db.query("MATCH (a:Account) RETURN a.id AS row")}
    except Exception as exc:  # noqa: BLE001
        if not dry_run:
            raise
        known = set()
        notes.append(f"graph not reachable ({type(exc).__name__}) — cross-references checked against the files only")
    known |= {account["id"] for account in bundle.accounts}

    filtered = filter_unknown_references(bundle, known, allow_missing)
    notes.extend(filtered.messages)

    report: dict = {
        "tenant": tenant,
        "graph": db.graph_name_for(tenant),
        "dry_run": dry_run,
        "counts": filtered.bundle.counts(),
        "skipped": filtered.dropped,
        "notes": notes,
    }
    if dry_run:
        return report
    stats = write_bundle(filtered.bundle, reset=reset)
    report.update(stats)
    return report


def render_report(report: dict) -> str:
    """Human-readable summary of an import (or a dry run)."""
    lines = [
        f"Import — tenant '{report['tenant']}' (graph '{report['graph']}')"
        + ("  [dry run: nothing written]" if report["dry_run"] else ""),
    ]
    for table, count in report["counts"].items():
        if count:
            lines.append(f"  {table:<13} {count:>5} row(s)")
    if report["dry_run"]:
        lines.append("  nothing was written; re-run without --dry-run to import")
    else:
        before, after = report["before"], report["after"]
        lines.append(
            f"  graph: {before['nodes']} → {after['nodes']} nodes "
            f"(+{after['nodes'] - before['nodes']}), "
            f"{before['relationships']} → {after['relationships']} relationships "
            f"(+{after['relationships'] - before['relationships']})"
        )
    for note in report["notes"]:
        lines.append(f"  note: {note}")
    return "\n".join(lines)
