"""High-concurrency read/write benchmark against FalkorDB (Track 02).

Measures what the brief calls "high-concurrency reads and writes": N writer
threads and M reader threads hammering the *same tenant graph* at once, with
per-operation latency percentiles. Writes are real graph writes (`BenchTick`
nodes, cleaned up afterwards); reads mix graph counts, entity lookups and
memory recall — the exact query shapes the agents use.

    python -m graphsleuth bench --writers 4 --readers 16 --seconds 6
"""

from __future__ import annotations

import statistics
import threading
import time
import uuid
from typing import Any

from . import db, memory


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return round(ordered[index], 2)


def _summarise(samples: dict[str, list[float]], elapsed: float, errors: list[str]) -> dict[str, Any]:
    reads = samples["read"]
    writes = samples["write"]
    return {
        "seconds": round(elapsed, 2),
        "reads": len(reads),
        "writes": len(writes),
        "total_ops": len(reads) + len(writes),
        "read_ops_per_s": round(len(reads) / elapsed, 1) if elapsed else 0,
        "write_ops_per_s": round(len(writes) / elapsed, 1) if elapsed else 0,
        "total_ops_per_s": round((len(reads) + len(writes)) / elapsed, 1) if elapsed else 0,
        "read_p50_ms": _percentile(reads, 0.50),
        "read_p95_ms": _percentile(reads, 0.95),
        "write_p50_ms": _percentile(writes, 0.50),
        "write_p95_ms": _percentile(writes, 0.95),
        "read_mean_ms": round(statistics.fmean(reads), 2) if reads else 0.0,
        "write_mean_ms": round(statistics.fmean(writes), 2) if writes else 0.0,
        "errors": len(errors),
        "error_samples": errors[:3],
    }


def run(
    tenant: str = db.DEFAULT_TENANT,
    writers: int = 4,
    readers: int = 16,
    seconds: float = 6.0,
) -> dict[str, Any]:
    """Run the benchmark and return the measured numbers."""
    db.set_tenant(tenant)
    stop_at = time.monotonic() + seconds
    samples: dict[str, list[float]] = {"read": [], "write": []}
    errors: list[str] = []
    lock = threading.Lock()

    def record(bucket: str, elapsed_ms: float) -> None:
        with lock:
            samples[bucket].append(elapsed_ms)

    def note(error: str) -> None:
        with lock:
            errors.append(str(error)[:160])

    def writer(worker: int) -> None:
        db.set_tenant(tenant)
        while time.monotonic() < stop_at:
            started = time.perf_counter()
            try:
                db.query(
                    "CREATE (n:BenchTick {id: $id, worker: $worker, at: $ts}) RETURN n.id AS row",
                    {"id": f"BENCH-{uuid.uuid4().hex[:8]}", "worker": worker, "ts": time.time()},
                )
            except Exception as exc:  # noqa: BLE001
                note(exc)
                continue
            record("write", (time.perf_counter() - started) * 1000)

    def reader(worker: int) -> None:
        db.set_tenant(tenant)
        queries = [
            # graph count — the shape `status` uses
            "MATCH (n:Account) WITH count(n) AS c OPTIONAL MATCH ()-[r]->() RETURN [c, count(r)] AS row",
            # neighbourhood lookup — the shape graph_neighbors uses
            "MATCH (c:Account {id: 'ACC-9003'})-[r]-(n) RETURN [type(r), n.id] AS row LIMIT 10",
            # entity resolution — the shape entity_search uses
            "MATCH (n) WHERE toLower(coalesce(n.id, '')) CONTAINS 'acc-9' RETURN n.id AS row LIMIT 5",
        ]
        iteration = 0
        while time.monotonic() < stop_at:
            started = time.perf_counter()
            try:
                db.query(queries[iteration % len(queries)])
                if iteration % 3 == 0:
                    memory.recall("ACC-9003", "", tenant, limit=5)
            except Exception as exc:  # noqa: BLE001
                note(exc)
                iteration += 1
                continue
            record("read", (time.perf_counter() - started) * 1000)
            iteration += 1

    threads = [
        threading.Thread(target=writer, args=(index,), name=f"writer-{index}", daemon=True)
        for index in range(max(1, writers))
    ] + [
        threading.Thread(target=reader, args=(index,), name=f"reader-{index}", daemon=True)
        for index in range(max(1, readers))
    ]

    started_at = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter() - started_at

    # clean the tick nodes so reported graph stats stay honest
    try:
        db.query("MATCH (n:BenchTick) DELETE n")
    except Exception as exc:  # noqa: BLE001
        note(exc)

    report = _summarise(samples, elapsed, errors)
    report.update(
        {
            "tenant": db.resolve_tenant(tenant),
            "graph": db.graph_name_for(db.resolve_tenant(tenant)),
            "writer_threads": max(1, writers),
            "reader_threads": max(1, readers),
            "concurrent_threads": len(threads),
        }
    )
    return report


def render(report: dict[str, Any]) -> str:
    """Human-readable summary for the CLI."""
    lines = [
        f"Concurrency benchmark — graph '{report['graph']}' (tenant {report['tenant']})",
        f"  threads        : {report['concurrent_threads']} "
        f"({report['writer_threads']} writers + {report['reader_threads']} readers), "
        f"{report['seconds']}s",
        f"  reads          : {report['reads']}  ({report['read_ops_per_s']}/s)",
        f"  writes         : {report['writes']}  ({report['write_ops_per_s']}/s)",
        f"  total          : {report['total_ops']} ops ({report['total_ops_per_s']}/s)",
        f"  read latency   : p50 {report['read_p50_ms']}ms  p95 {report['read_p95_ms']}ms  "
        f"mean {report['read_mean_ms']}ms",
        f"  write latency  : p50 {report['write_p50_ms']}ms  p95 {report['write_p95_ms']}ms  "
        f"mean {report['write_mean_ms']}ms",
        f"  errors         : {report['errors']}",
    ]
    if report.get("error_samples"):
        lines.append(f"  error sample   : {report['error_samples'][0]}")
    return "\n".join(lines)
