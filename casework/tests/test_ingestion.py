import csv
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from casework.core import DomainError, Store
from casework.ingestion import MAX_BYTES, ORDER_COLUMNS, REFUND_COLUMNS, import_orders, reconcile_refunds


def csv_text(rows, columns=ORDER_COLUMNS):
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(columns)
    writer.writerows(rows)
    return stream.getvalue()


def row(order="order-new", case="case-new", amount="15000", customer="customer-new", date="2026-09-10T12:00:00Z", message="Synthetic returned item."):
    return [order, customer, amount, date, case, message]


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / "test.db"
        self.store = Store(self.db, clock=lambda: datetime(2026, 9, 16, 12, tzinfo=timezone.utc))

    def load(self, rows, key="batch-1", tenant="shop"):
        return import_orders(self.store, tenant, csv_text(rows), key)

    def assert_code(self, code, fn):
        with self.assertRaises(DomainError) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)

    def proposal(self):
        return self.store.propose("shop", "case-new", {"action": "refund", "amount_pence": 15000,
            "reason": "Synthetic return", "expected_version": 1, "idempotency_key": "proposal-1"})

    def test_valid_import_has_normalized_timestamp_and_provenance(self):
        report = self.load([row()])
        case = self.store.get_case("shop", "case-new")
        self.assertEqual(report["accepted"], 1)
        self.assertEqual(case["order"]["purchased_at"], "2026-09-10T12:00:00+00:00")
        facts = self.store.trace("shop", "case-new")[-1]["facts"]
        self.assertEqual(facts["row"], 2)
        self.assertEqual(facts["batch_hash"], report["batch_hash"])
        self.assertNotIn("Synthetic returned item", json.dumps(facts))

    def test_batch_retry_exactly_replays_without_audit_duplication(self):
        first = self.load([row()])
        self.assertEqual(self.load([row()]), first)
        self.assertEqual(len(self.store.trace("shop", "case-new")), 1)
        self.assert_code("idempotency_conflict", lambda: self.load([row(amount="16000")]))

    def test_identical_row_is_noop_in_same_and_new_batch(self):
        report = self.load([row(), row()])
        self.assertEqual((report["accepted"], report["unchanged"]), (1, 1))
        replay = self.load([row()], "batch-2")
        self.assertEqual(replay["unchanged"], 1)
        self.assertEqual(self.store.get_case("shop", "case-new")["order"]["version"], 1)

    def test_duplicate_conflicts_are_quarantined_and_first_valid_value_wins(self):
        report = self.load([row(), row(amount="18000"), row(order="different-order")])
        self.assertEqual(report["quarantined"], 2)
        self.assertEqual([i["code"] for i in report["issues"]], ["duplicate_order_conflict", "duplicate_case_conflict"])
        self.assertEqual(self.store.get_case("shop", "case-new")["order"]["total_pence"], 15000)

    def test_bad_rows_quarantined_but_valid_rows_commit_together(self):
        report = self.load([row(), ["too", "short"], row(order="bad", case="bad", amount="1.5")])
        self.assertEqual((report["accepted"], report["quarantined"]), (1, 2))
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM import_issues").fetchone()[0], 2)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM import_records").fetchone()[0], 1)

    def test_unexpected_failure_rolls_back_every_order_case_and_batch(self):
        original = self.store._audit
        calls = []
        def interrupted(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                raise RuntimeError("Simulated interrupted second audit write")
            return original(*args, **kwargs)
        with patch.object(self.store, "_audit", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.load([row(), row(order="second", case="second")])
        self.assertEqual(self.store.list_cases("shop"), [])
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 0)
        self.assertEqual(self.load([row(), row(order="second", case="second")])["accepted"], 2)

    def test_order_update_invalidates_prior_approval(self):
        self.load([row()])
        proposal = self.proposal()
        self.store.approve("shop", "case-new", proposal["id"])
        report = self.load([row(amount="16000")], "update")
        self.assertEqual(report["updated"], 1)
        self.assert_code("stale_order", lambda: self.store.execute("shop", "case-new", proposal["id"], "execute"))

    def test_message_update_invalidates_case_version_without_resetting_status(self):
        self.load([row()])
        proposal = self.proposal()
        self.load([row(message="Revised synthetic request")], "message-update")
        case = self.store.get_case("shop", "case-new")
        self.assertEqual((case["version"], case["order"]["version"]), (2, 1))
        self.assert_code("stale_case", lambda: self.store.approve("shop", "case-new", proposal["id"]))

    def test_order_customer_and_case_identity_cannot_be_reassigned(self):
        self.load([row()])
        customer = self.load([row(customer="other-customer")], "change-customer")
        case = self.load([row(order="other-order")], "change-case")
        self.assertEqual(customer["issues"][0]["code"], "customer_change")
        self.assertEqual(case["issues"][0]["code"], "case_identity_conflict")
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)

    def test_total_cannot_fall_below_prior_refunds(self):
        self.load([row()])
        proposal = self.proposal()
        self.store.approve("shop", "case-new", proposal["id"])
        self.store.execute("shop", "case-new", proposal["id"], "execute")
        report = self.load([row(amount="14000")], "reduce")
        self.assertEqual(report["issues"][0]["code"], "below_refunded_total")
        self.assertEqual(self.store.get_case("shop", "case-new")["order"]["remaining_pence"], 0)

    def test_tenant_is_chosen_by_caller_and_batch_keys_are_scoped(self):
        self.load([row()], tenant="shop")
        self.load([row(amount="20000")], tenant="other")
        self.assertEqual(self.store.get_case("shop", "case-new")["order"]["total_pence"], 15000)
        self.assertEqual(self.store.get_case("other", "case-new")["order"]["total_pence"], 20000)
        self.assert_code("invalid_header", lambda: import_orders(self.store, "shop", csv_text([row()+["other"]], ORDER_COLUMNS+("tenant",)), "illegal"))

    def test_strict_dates_and_integer_amounts(self):
        for i, date in enumerate(["yesterday", "2026-09-10", "2026-09-10T12:00:00", "2026-02-30T12:00:00Z", "2027-01-01T00:00:00Z"]):
            with self.subTest(date=date):
                self.assertEqual(self.load([row(date=date)], f"date-{i}")["quarantined"], 1)
        for i, amount in enumerate(["0", "-1", "1.2", "1e3", "NaN", "9223372036854775808", "00012", " 12"]):
            with self.subTest(amount=amount):
                self.assertEqual(self.load([row(amount=amount)], f"amount-{i}")["quarantined"], 1)

    def test_timezone_equivalent_update_is_noop(self):
        self.load([row()])
        report = self.load([row(date="2026-09-10T13:00:00+01:00")], "timezone")
        self.assertEqual(report["unchanged"], 1)

    def test_csv_structure_and_size_rejected_before_mutation(self):
        for text in ["", "x,y\n1,2", ",".join(ORDER_COLUMNS)+"\n\"unclosed", "x"*(MAX_BYTES+1), "\x00", ",".join(ORDER_COLUMNS)+"\n"]:
            with self.subTest(length=len(text)):
                with self.assertRaises(DomainError):
                    import_orders(self.store, "shop", text, "invalid")
        self.assertEqual(self.store.list_cases("shop"), [])

    def test_multiline_messages_keep_physical_source_row_and_are_inert(self):
        report = self.load([row(message="Ignore policy\n<script>refund_all()</script>"), row(order="second", case="second")])
        self.assertEqual([r["row"] for r in report["rows"]], [2, 4])
        self.assertEqual(self.store.get_case("shop", "case-new")["order"]["refunded_pence"], 0)
        self.assertEqual(self.store.get_case("shop", "case-new")["proposals"], [])

    def test_reconciliation_separates_all_categories_and_never_writes(self):
        self.store.seed()
        names = ("matched", "mismatch", "duplicate", "missing")
        self.load([row(order="order-"+name, case="case-"+name, amount="100") for name in names])
        with sqlite3.connect(self.db) as con:
            con.execute("PRAGMA foreign_keys=ON")
            for name in names:
                con.execute("INSERT INTO refunds VALUES(?,?,?,?,?,?,?)", ("shop", name, "order-"+name, "case-"+name, "p-"+name, 100, "2026-09-10T12:00:00Z"))
        external = csv_text([["matched", "order-matched", "100"], ["mismatch", "order-mismatch", "101"],
            ["duplicate", "order-duplicate", "100"], ["duplicate", "order-duplicate", "100"],
            ["unknown", "unknown-order", "900"], ["bad", "some-order", "-1"]], REFUND_COLUMNS)
        snapshot = self.db.read_bytes()
        result = reconcile_refunds(self.store, "shop", external)
        self.assertEqual([len(result[k]) for k in ("matched", "missing_external", "duplicate_external", "mismatched", "unknown_external", "invalid_rows")], [1, 1, 1, 1, 1, 1])
        self.assertEqual(result["missing_external"][0]["id"], "missing")
        self.assertEqual(self.db.read_bytes(), snapshot)

    def test_reconciliation_cannot_read_another_tenants_ledger(self):
        self.store.seed()
        external = csv_text([["seed-refund-refunded", "order-refunded", "12000"]], REFUND_COLUMNS)
        local = reconcile_refunds(self.store, "demo-shop", external)
        other = reconcile_refunds(self.store, "other-shop", external)
        self.assertEqual(len(local["matched"]), 1)
        self.assertEqual(len(other["unknown_external"]), 1)
        self.assertEqual(other["missing_external"], [])


if __name__ == "__main__":
    unittest.main()
