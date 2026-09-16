"""Reproduce one bounded 5,000-row synthetic ingestion run. Not a load benchmark."""

import csv
import io
import json
import platform
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from casework.core import Store
from casework.ingestion import ORDER_COLUMNS, import_orders


def main():
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(ORDER_COLUMNS)
    for index in range(5000):
        writer.writerow([f"order-{index:05d}", f"customer-{index:05d}", 1000+index,
            "2026-09-10T12:00:00Z", f"case-{index:05d}", f"Synthetic return request {index:05d}."])
    text = stream.getvalue()
    fixed_time = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory)/"benchmark.db", clock=lambda: fixed_time)
        started = time.perf_counter()
        result = import_orders(store, "benchmark-shop", text, "synthetic-5000-v1")
        duration = time.perf_counter()-started
        retry_started = time.perf_counter()
        retry = import_orders(store, "benchmark-shop", text, "synthetic-5000-v1")
        retry_duration = time.perf_counter()-retry_started
        with store.connection() as con:
            counts = {table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("orders", "cases", "audit", "import_records", "import_issues", "import_batches")}
        passed = result["accepted"] == 5000 and result["quarantined"] == 0 and retry == result
        passed &= counts == {"orders":5000,"cases":5000,"audit":5000,"import_records":5000,"import_issues":0,"import_batches":1}
        report = {"run_at_utc": datetime.now(timezone.utc).isoformat(), "reference_time": fixed_time.isoformat(),
            "scope": "One local run of 5000 deterministic synthetic rows in a temporary SQLite database; not production throughput or a customer workload.",
            "environment": {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                "os": platform.system(), "os_release": platform.release(), "architecture": platform.machine()},
            "input_rows": 5000, "input_bytes": len(text.encode()), "batch_hash": result["batch_hash"],
            "import_duration_seconds": round(duration, 6), "exact_retry_duration_seconds": round(retry_duration, 6),
            "result": {key: result[key] for key in ("accepted", "updated", "unchanged", "quarantined")},
            "persisted_counts": counts, "exact_retry_identical": retry == result, "passed": bool(passed)}
    destination = ROOT/"evaluation"/"ingestion-report.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(report, indent=2)+"\n")
    print(json.dumps(report, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
