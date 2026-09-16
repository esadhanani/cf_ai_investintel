"""Bounded CSV import and read-only refund reconciliation for local synthetic data."""

import csv
import hashlib
import io
import json
import re
from collections import defaultdict
from datetime import datetime, timezone

from .core import DomainError, fail, identifier

MAX_BYTES = 2 * 1024 * 1024
MAX_PENCE = 2**63 - 1
ORDER_COLUMNS = ("order_id", "customer_id", "total_pence", "purchased_at", "case_id", "customer_message")
REFUND_COLUMNS = ("external_id", "order_id", "amount_pence")


def _csv(text, columns):
    if not isinstance(text, str):
        fail("invalid_csv", "CSV must be UTF-8 text.", 400)
    try:
        raw = text.encode("utf-8")
    except UnicodeError:
        fail("invalid_csv", "CSV must be valid UTF-8 text.", 400)
    if not raw or len(raw) > MAX_BYTES or "\x00" in text:
        fail("invalid_csv", "CSV must be nonempty, null-free and at most 2 MiB.", 400)
    try:
        reader = csv.reader(io.StringIO(text.removeprefix("\ufeff")), strict=True)
        header = next(reader, None)
        if header != list(columns):
            fail("invalid_header", "Expected columns in this order: " + ",".join(columns), 400)
        rows = []
        while True:
            start = reader.line_num + 1
            row = next(reader, None)
            if row is None:
                break
            rows.append((start, row))
    except csv.Error:
        fail("invalid_csv", "CSV is malformed or contains an oversized field.", 400)
    if not rows:
        fail("invalid_csv", "CSV must contain at least one data row.", 400)
    return hashlib.sha256(raw).hexdigest(), rows


def _pence(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", value):
        fail("invalid_amount", "Amount must be positive integer pence without signs or decimals.")
    amount = int(value)
    if amount > MAX_PENCE:
        fail("invalid_amount", "Amount exceeds the SQLite integer limit.")
    return amount


def _date(value, now):
    try:
        # Require a full ISO date/time and explicit offset rather than locale assumptions.
        if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", value):
            raise ValueError()
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        parsed = parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        fail("invalid_date", "Purchase timestamp must be a valid ISO timestamp with timezone.")
    if parsed > now:
        fail("future_purchase", "Purchase timestamp cannot be in the future.")
    return parsed.isoformat()


def _migrate(con):
    # executescript would implicitly commit, so each DDL statement stays in the batch transaction.
    statements = [
        """CREATE TABLE IF NOT EXISTS import_batches (
          tenant TEXT NOT NULL,batch_key TEXT NOT NULL,batch_hash TEXT NOT NULL,
          response TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(tenant,batch_key))""",
        """CREATE TABLE IF NOT EXISTS import_issues (
          tenant TEXT NOT NULL,batch_key TEXT NOT NULL,row INTEGER NOT NULL,code TEXT NOT NULL,
          message TEXT NOT NULL,row_hash TEXT NOT NULL,PRIMARY KEY(tenant,batch_key,row),
          FOREIGN KEY(tenant,batch_key) REFERENCES import_batches(tenant,batch_key))""",
        """CREATE TABLE IF NOT EXISTS import_records (
          tenant TEXT NOT NULL,batch_key TEXT NOT NULL,row INTEGER NOT NULL,order_id TEXT NOT NULL,
          case_id TEXT NOT NULL,status TEXT NOT NULL,row_hash TEXT NOT NULL,
          PRIMARY KEY(tenant,batch_key,row),
          FOREIGN KEY(tenant,batch_key) REFERENCES import_batches(tenant,batch_key))""",
    ]
    for statement in statements:
        con.execute(statement)


def import_orders(store, tenant, csv_text, batch_key):
    """Commit accepted rows, quarantines and provenance atomically. Retry by exact bytes."""
    identifier(tenant, "tenant")
    identifier(batch_key, "batch key")
    digest, rows = _csv(csv_text, ORDER_COLUMNS)
    now = store.now()
    result = {"batch_key": batch_key, "batch_hash": digest, "status": "completed",
        "accepted": 0, "updated": 0, "unchanged": 0, "quarantined": 0, "issues": [], "rows": []}
    with store.connection(write=True) as con:
        _migrate(con)
        old = con.execute("SELECT * FROM import_batches WHERE tenant=? AND batch_key=?", (tenant, batch_key)).fetchone()
        if old:
            if old["batch_hash"] != digest:
                fail("idempotency_conflict", "Batch key was already used for different CSV bytes.", 409)
            return json.loads(old["response"])
        con.execute("INSERT INTO import_batches VALUES(?,?,?,?,?)", (tenant, batch_key, digest, "{}", now.isoformat()))
        seen_orders, seen_cases = {}, {}
        for line, values in rows:
            row_hash = hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
            try:
                if len(values) != len(ORDER_COLUMNS):
                    fail("column_count", "Row does not match the required column count.")
                item = dict(zip(ORDER_COLUMNS, values))
                for key in ("order_id", "customer_id", "case_id"):
                    identifier(item[key], key)
                amount = _pence(item["total_pence"])
                purchased = _date(item["purchased_at"], now)
                message = item["customer_message"]
                if not message.strip() or len(message) > 4000:
                    fail("invalid_message", "Customer message must contain 1-4000 characters.")
                order_signature = (item["customer_id"], amount, purchased)
                case_signature = (item["order_id"], item["customer_id"], message)
                if item["order_id"] in seen_orders and seen_orders[item["order_id"]] != order_signature:
                    fail("duplicate_order_conflict", "Repeated order ID has conflicting values in this batch.")
                if item["case_id"] in seen_cases and seen_cases[item["case_id"]] != case_signature:
                    fail("duplicate_case_conflict", "Repeated case ID has conflicting values in this batch.")
                order = con.execute("SELECT * FROM orders WHERE tenant=? AND id=?", (tenant, item["order_id"])).fetchone()
                case = con.execute("SELECT * FROM cases WHERE tenant=? AND id=?", (tenant, item["case_id"])).fetchone()
                refunded = con.execute("SELECT COALESCE(SUM(amount_pence),0) FROM refunds WHERE tenant=? AND order_id=?", (tenant, item["order_id"])).fetchone()[0]
                if order and order["customer_id"] != item["customer_id"]:
                    fail("customer_change", "Existing order customer cannot be changed by import.")
                if amount < refunded:
                    fail("below_refunded_total", "Order total cannot fall below the recorded refund total.")
                if case and (case["customer_id"] != item["customer_id"] or case["order_id"] != item["order_id"]):
                    fail("case_identity_conflict", "Existing case cannot be reassigned to another customer or order.")
            except DomainError as exc:
                issue = {"row": line, "code": exc.code, "message": exc.message}
                result["issues"].append(issue)
                result["quarantined"] += 1
                con.execute("INSERT INTO import_issues VALUES(?,?,?,?,?,?)", (tenant, batch_key, line, exc.code, exc.message, row_hash))
                continue

            # No business validation is left after this point. Unexpected errors roll back the entire batch.
            seen_orders[item["order_id"]] = order_signature
            seen_cases[item["case_id"]] = case_signature
            changed_order = bool(order and (order["total_pence"] != amount or order["purchased_at"] != purchased))
            changed_case = bool(case and case["customer_message"] != message)
            if not order:
                con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?)", (tenant, item["order_id"], item["customer_id"], amount, purchased, 1))
            elif changed_order:
                con.execute("UPDATE orders SET total_pence=?,purchased_at=?,version=version+1 WHERE tenant=? AND id=?", (amount, purchased, tenant, item["order_id"]))
                con.execute("""UPDATE cases SET status=? WHERE tenant=? AND order_id=?
                    AND status IN ('refunded','partially_refunded')""",
                    ("refunded" if amount == refunded else "partially_refunded", tenant, item["order_id"]))
            if not case:
                status = "refunded" if refunded == amount else "partially_refunded" if refunded else "open"
                con.execute("INSERT INTO cases VALUES(?,?,?,?,?,?,?)", (tenant, item["case_id"], item["customer_id"], item["order_id"], message, status, 1))
            elif changed_case:
                con.execute("UPDATE cases SET customer_message=?,version=version+1 WHERE tenant=? AND id=?", (message, tenant, item["case_id"]))
            outcome = "accepted" if not order or not case else "updated" if changed_order or changed_case else "unchanged"
            result[outcome] += 1
            row_result = {"row": line, "order_id": item["order_id"], "case_id": item["case_id"], "status": outcome}
            result["rows"].append(row_result)
            con.execute("INSERT INTO import_records VALUES(?,?,?,?,?,?,?)", (tenant, batch_key, line, item["order_id"], item["case_id"], outcome, row_hash))
            store._audit(con, tenant, item["case_id"], "csv_import_record", {"source": "csv_import", "batch_key": batch_key,
                "batch_hash": digest, "row": line, "row_hash": row_hash, "outcome": outcome, "order_id": item["order_id"]})
        con.execute("UPDATE import_batches SET response=? WHERE tenant=? AND batch_key=?", (json.dumps(result, sort_keys=True), tenant, batch_key))
    return result


def reconcile_refunds(store, tenant, csv_text):
    """Compare external_id to internal refund.id. Never create ledger rows or import state."""
    identifier(tenant, "tenant")
    digest, rows = _csv(csv_text, REFUND_COLUMNS)
    result = {"source_hash": digest, "matched": [], "missing_external": [], "duplicate_external": [],
        "mismatched": [], "unknown_external": [], "invalid_rows": []}
    external = defaultdict(list)
    for line, values in rows:
        try:
            if len(values) != len(REFUND_COLUMNS):
                fail("column_count", "Row does not match the required column count.")
            item = dict(zip(REFUND_COLUMNS, values))
            identifier(item["external_id"], "external refund ID")
            identifier(item["order_id"], "order ID")
            item["amount_pence"] = _pence(item["amount_pence"])
            external[item["external_id"]].append(dict(item, row=line))
        except DomainError as exc:
            result["invalid_rows"].append({"row": line, "code": exc.code, "message": exc.message})
    with store.connection() as con:
        ledger = {row["id"]: dict(row) for row in con.execute("SELECT id,order_id,amount_pence FROM refunds WHERE tenant=? ORDER BY id", (tenant,))}
    for external_id, entries in sorted(external.items()):
        if len(entries) > 1:
            result["duplicate_external"].append({"external_id": external_id, "rows": [entry["row"] for entry in entries]})
            continue
        item = entries[0]
        expected = ledger.get(external_id)
        if expected is None:
            result["unknown_external"].append(item)
        elif (item["order_id"], item["amount_pence"]) != (expected["order_id"], expected["amount_pence"]):
            result["mismatched"].append({"external": item, "internal": expected})
        else:
            result["matched"].append(item)
    result["missing_external"] = [entry for key, entry in ledger.items() if key not in external]
    return result
