"""Real HTTP imports, persisted history, scoped ledger views and reconciliation."""

import csv
import http.client
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from casework.auth import Auth, Principal, create_credentials
from casework.core import Store
from casework.ingestion import ORDER_COLUMNS, REFUND_COLUMNS, import_orders
from casework.web import MAX_BODY, MAX_CSV_BODY, make_server


def csv_text(columns, rows):
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows)
    return output.getvalue()


class OperationsWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "casework.sqlite", clock=lambda: datetime(2026, 9, 19, tzinfo=timezone.utc))
        self.store.seed()
        config, self.tokens = create_credentials([
            Principal("operator", "demo-shop", "operator"),
            Principal("reviewer", "demo-shop", "reviewer"),
            Principal("auditor", "demo-shop", "auditor"),
            Principal("other", "other-shop", "operator"),
        ])
        self.server = make_server(self.store, port=0, auth=Auth(config))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.orders = csv_text(ORDER_COLUMNS, [
            ["import-order", "import-customer", 2500, "2026-09-18T10:00:00+00:00", "import-case", "Please refund the unused item."]
        ])
        self.external = csv_text(REFUND_COLUMNS, [["external-demo", "import-order", 100]])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def call(self, method, path, body=None, role="operator", csrf=True, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        request_headers = {}
        if role is not None:
            request_headers["Authorization"] = "Bearer " + self.tokens[role]
        if csrf:
            request_headers["X-CSRF-Token"] = self.server.csrf_token
        if body is not None:
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        status, data = response.status, json.loads(response.read())
        connection.close()
        return status, data

    def import_batch(self, key="batch-one", text=None, role="operator", **extra):
        return self.call("POST", "/api/import-orders", {"csv_text": self.orders if text is None else text,
                                                         "batch_key": key, **extra}, role=role)

    def snapshot(self):
        with sqlite3.connect(self.store.path) as connection:
            return tuple(connection.iterdump())

    def test_import_replay_conflict_and_exact_persisted_result(self):
        status, result = self.import_batch()
        self.assertEqual(status, 200)
        self.assertEqual(result["accepted"], 1)
        snapshot = self.snapshot()
        self.assertEqual(self.import_batch(), (200, result))
        self.assertEqual(self.snapshot(), snapshot)
        status, conflict = self.import_batch(text=self.orders.replace("2500", "2600"))
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "idempotency_conflict")
        self.assertEqual(self.call("GET", "/api/imports/batch-one"), (200, result))
        self.assertEqual(self.snapshot(), snapshot)

    def test_invalid_row_is_quarantined_while_valid_row_commits(self):
        text = self.orders + "bad-order,bad-customer,-5,2026-09-18T10:00:00+00:00,bad-case,Please refund.\n"
        status, result = self.import_batch(text=text)
        self.assertEqual(status, 200)
        self.assertEqual((result["accepted"], result["quarantined"]), (1, 1))
        self.assertEqual(result["issues"][0]["code"], "invalid_amount")
        self.assertEqual(result["issues"][0]["row"], 3)
        self.assertEqual(self.call("GET", "/api/imports/batch-one")[1], result)

    def test_history_reads_before_import_do_not_initialize_tables(self):
        before = self.snapshot()
        self.assertEqual(self.call("GET", "/api/imports"), (200, {"batches": []}))
        self.assertEqual(self.call("GET", "/api/imports/missing")[0], 404)
        self.assertEqual(self.call("GET", "/api/refunds")[0], 200)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(any("CREATE TABLE import_" in statement for statement in self.snapshot()))

    def test_history_is_latest_25_stable_and_read_only(self):
        for index in range(27):
            import_orders(self.store, "demo-shop", self.orders, f"batch-{index:02d}")
        before = self.snapshot()
        status, data = self.call("GET", "/api/imports", role="auditor")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["batches"]), 25)
        self.assertEqual([batch["batch_key"] for batch in data["batches"]], [f"batch-{i:02d}" for i in range(26, 1, -1)])
        self.assertEqual(set(data["batches"][0]), {"batch_key", "batch_hash", "created_at", "accepted", "updated", "unchanged", "quarantined"})
        self.assertEqual(self.call("GET", "/api/imports", role="reviewer")[1], data)
        self.assertEqual(self.snapshot(), before)

    def test_only_operator_can_import_but_all_roles_can_reconcile(self):
        for role in ("reviewer", "auditor"):
            self.assertEqual(self.import_batch(role=role)[0], 403)
        for role in ("operator", "reviewer", "auditor"):
            before = self.snapshot()
            status, result = self.call("POST", "/api/reconcile-refunds", {"csv_text": self.external}, role=role)
            self.assertEqual(status, 200)
            self.assertEqual(len(result["unknown_external"]), 1)
            self.assertEqual(self.snapshot(), before)

    def test_new_routes_require_authentication(self):
        for path in ("/api/imports", "/api/imports/batch-one", "/api/refunds"):
            self.assertEqual(self.call("GET", path, role=None)[0], 401)
        for path in ("/api/import-orders", "/api/reconcile-refunds"):
            self.assertEqual(self.call("POST", path, {}, role=None)[0], 401)

    def test_new_post_routes_require_csrf_and_same_origin(self):
        for path in ("/api/import-orders", "/api/reconcile-refunds"):
            self.assertEqual(self.call("POST", path, {}, csrf=False)[0], 403)
            self.assertEqual(self.call("POST", path, {}, headers={"Origin": "https://other.example"})[0], 403)
            self.assertEqual(self.call("POST", path, {}, headers={"Host": "other.example"})[0], 403)

    def test_cross_tenant_requests_and_batch_discovery_are_blocked(self):
        self.assertEqual(self.import_batch()[0], 200)
        for path in ("/api/imports", "/api/imports/batch-one", "/api/refunds"):
            self.assertEqual(self.call("GET", path + "?tenant=other-shop")[0], 403)
        self.assertEqual(self.import_batch(tenant="other-shop")[0], 403)
        self.assertEqual(self.call("POST", "/api/reconcile-refunds", {"tenant": "other-shop", "csv_text": self.external})[0], 403)
        self.assertEqual(self.call("GET", "/api/imports", role="other"), (200, {"batches": []}))
        self.assertEqual(self.call("GET", "/api/imports/batch-one", role="other")[0], 404)
        self.assertEqual(self.call("GET", "/api/refunds", role="other"), (200, {"refunds": []}))

    def test_unexpected_keys_actor_and_role_are_rejected(self):
        for field in ("extra", "case_id", "actor", "role"):
            self.assertIn(self.call("POST", "/api/import-orders", {"csv_text": self.orders, "batch_key": "batch-one", field: "untrusted"})[0], (400, 403))
            self.assertIn(self.call("POST", "/api/reconcile-refunds", {"csv_text": self.external, field: "untrusted"})[0], (400, 403))
        self.assertEqual(self.call("GET", "/api/imports")[1], {"batches": []})

    def test_malformed_missing_fields_and_non_string_csv_are_rejected(self):
        for payload in ({}, {"batch_key": "k"}, {"csv_text": self.orders}, {"csv_text": None, "batch_key": "k"},
                        {"csv_text": "wrong,header\na,b\n", "batch_key": "k"}, {"csv_text": self.orders, "batch_key": "../x"}):
            self.assertEqual(self.call("POST", "/api/import-orders", payload)[0], 400)
        self.assertEqual(self.call("POST", "/api/reconcile-refunds", {"csv_text": []})[0], 400)

    def test_csv_route_accepts_payload_above_ordinary_16k_limit(self):
        rows = [[f"order-{i}", f"customer-{i}", 1000, "2026-09-18T10:00:00+00:00", f"case-{i}", "x" * 3000] for i in range(6)]
        text = csv_text(ORDER_COLUMNS, rows)
        self.assertGreater(len(text), MAX_BODY)
        status, result = self.import_batch(text=text)
        self.assertEqual(status, 200)
        self.assertEqual(result["accepted"], 6)

    def test_csv_and_json_size_limits_and_unchanged_other_route_limit(self):
        # Oversized declared lengths are rejected before the body is read.
        for path in ("/api/import-orders", "/api/reconcile-refunds"):
            self.assertEqual(self.call("POST", path, {}, headers={"Content-Length": str(MAX_CSV_BODY + 1)})[0], 413)
        self.assertEqual(self.call("POST", "/api/propose", {}, headers={"Content-Length": str(MAX_BODY + 1)})[0], 413)
        for path in ("/api/import-orders", "/api/reconcile-refunds"):
            payload = {"csv_text": "x" * (2 * 1024 * 1024 + 1)}
            if path == "/api/import-orders":
                payload["batch_key"] = "oversized"
            self.assertEqual(self.call("POST", path, payload)[0], 400)
        self.assertEqual(self.call("POST", "/api/reconcile-refunds", {"csv_text": "é" * (1024 * 1024 + 1)})[0], 400)

    def test_demo_mode_remains_available_with_explicit_workspace(self):
        self.server.auth = None
        self.assertEqual(self.import_batch(role=None, tenant="demo-shop")[0], 200)
        self.assertEqual(self.call("POST", "/api/reconcile-refunds", {"tenant": "demo-shop", "csv_text": self.external}, role=None)[0], 200)
        self.assertEqual(self.call("GET", "/api/imports?tenant=demo-shop", role=None)[0], 200)
        self.assertEqual(self.import_batch(role=None)[0], 400)

    def test_end_to_end_import_refund_ledger_export_and_reconciliation(self):
        self.assertEqual(self.import_batch()[0], 200)
        status, proposal = self.call("POST", "/api/propose", {"case_id": "import-case", "action": "refund",
            "amount_pence": 500, "reason": "Verified partial refund", "expected_version": 1, "idempotency_key": "p-one"})
        self.assertEqual(status, 200)
        status, execution = self.call("POST", "/api/execute", {"case_id": "import-case", "proposal_id": proposal["id"], "idempotency_key": "e-one"})
        self.assertEqual(status, 200)
        status, ledger = self.call("GET", "/api/refunds", role="auditor")
        self.assertEqual(status, 200)
        self.assertEqual([entry["id"] for entry in ledger["refunds"]], sorted(entry["id"] for entry in ledger["refunds"]))
        self.assertTrue(any(entry["id"] == execution["ledger_id"] and entry["amount_pence"] == 500 for entry in ledger["refunds"]))
        text = csv_text(REFUND_COLUMNS, [[entry["id"], entry["order_id"], entry["amount_pence"]] for entry in ledger["refunds"]])
        before = self.snapshot()
        status, result = self.call("POST", "/api/reconcile-refunds", {"csv_text": text}, role="auditor")
        self.assertEqual(status, 200)
        self.assertEqual(len(result["matched"]), len(ledger["refunds"]))
        for category in ("missing_external", "duplicate_external", "mismatched", "unknown_external", "invalid_rows"):
            self.assertEqual(result[category], [])
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
