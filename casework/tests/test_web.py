import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from casework.core import Store
from casework.web import make_server


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.sqlite")
        self.store.seed()
        self.server = make_server(self.store, port=0)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        self.temp.cleanup()

    def call(self, method, path, body=None, headers=None, token=True, raw=False):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        final_headers = {}
        if body is not None:
            if not raw:
                body = json.dumps(body)
            final_headers["Content-Type"] = "application/json"
        if token:
            final_headers["X-CSRF-Token"] = self.server.csrf_token
        final_headers.update(headers or {})
        connection.request(method, path, body, final_headers)
        response = connection.getresponse()
        data = response.read()
        status, response_headers = response.status, dict(response.getheaders())
        connection.close()
        if "application/json" in response_headers.get("Content-Type", ""):
            data = json.loads(data)
        return status, data, response_headers

    def proposal(self, case_id="case-eligible", amount=1000, key="proposal-test"):
        return self.call("POST", "/api/propose", {
            "tenant": "demo-shop", "case_id": case_id, "action": "refund", "amount_pence": amount,
            "reason": "Verified order refund request", "expected_version": 1, "idempotency_key": key,
        })

    def test_successful_refund_and_replay_has_one_ledger_entry(self):
        status, proposal, _ = self.proposal()
        self.assertEqual(status, 200)
        body = {"tenant": "demo-shop", "case_id": "case-eligible", "proposal_id": proposal["id"],
                "idempotency_key": "execute-test"}
        first = self.call("POST", "/api/execute", body)
        second = self.call("POST", "/api/execute", body)
        self.assertEqual(first[0], 200)
        self.assertEqual(first[1], second[1])
        case = self.store.get_case("demo-shop", "case-eligible")
        self.assertEqual(case["order"]["refunded_pence"], 1000)
        self.assertEqual(sum(e["event"] == "action_executed" for e in self.store.trace("demo-shop", "case-eligible")), 1)

    def test_large_refund_requires_actual_approval_action(self):
        status, proposal, _ = self.proposal("case-approval", 15000)
        self.assertEqual(status, 200)
        body = {"tenant": "demo-shop", "case_id": "case-approval", "proposal_id": proposal["id"],
                "idempotency_key": "large-execution"}
        self.assertNotEqual(self.call("POST", "/api/execute", body)[0], 200)
        self.assertEqual(self.call("POST", "/api/approve", body)[0], 200)
        self.assertEqual(self.call("POST", "/api/execute", body)[0], 200)

    def test_missing_csrf_cannot_write(self):
        status, data, _ = self.call("POST", "/api/propose", {}, token=False)
        self.assertEqual(status, 403)
        self.assertEqual(data["error"]["code"], "missing_csrf")

    def test_non_ascii_csrf_rejected_cleanly(self):
        status, _, _ = self.call("POST", "/api/propose", {}, headers={"X-CSRF-Token": "é"})
        self.assertEqual(status, 403)

    def test_suggestion_does_not_mutate_case_or_audit(self):
        before = self.store.get_case("demo-shop", "case-eligible")
        before_trace = self.store.trace("demo-shop", "case-eligible")
        with patch("casework.web.plan_case", return_value={"action": "refund", "amount_pence": 1000,
                                                          "reason": "Draft only", "expected_version": 1}) as planner:
            status, data, _ = self.call("POST", "/api/suggest", {"tenant": "demo-shop", "case_id": "case-eligible"})
        self.assertEqual(status, 200)
        self.assertEqual(data["action"], "refund")
        planner.assert_called_once_with(self.store, "demo-shop", "case-eligible", model="mistral:latest", timeout=45)
        self.assertEqual(self.store.get_case("demo-shop", "case-eligible"), before)
        self.assertEqual(self.store.trace("demo-shop", "case-eligible"), before_trace)

    def test_malformed_json_rejected(self):
        self.assertEqual(self.call("POST", "/api/propose", "{", raw=True)[0], 400)
        self.assertEqual(self.call("POST", "/api/propose", "[]", raw=True)[0], 400)

    def test_external_origin_rejected_even_with_token(self):
        status, data, _ = self.call("POST", "/api/propose", {}, headers={"Origin": "https://example.com"})
        self.assertEqual(status, 403)
        self.assertEqual(data["error"]["code"], "invalid_origin")

    def test_dns_rebinding_host_rejected(self):
        status, data, _ = self.call("GET", "/api/bootstrap", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(data["error"]["code"], "invalid_host")

    def test_bootstrap_and_assets_are_same_origin_and_no_store(self):
        status, data, headers = self.call("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(data["csrf_token"], self.server.csrf_token)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        for path in ["/", "/app.js", "/style.css"]:
            self.assertEqual(self.call("GET", path)[0], 200)

    def test_case_detail_includes_policy_and_audit(self):
        status, data, _ = self.call("GET", "/api/cases/case-eligible?tenant=demo-shop")
        self.assertEqual(status, 200)
        self.assertEqual(data["case"]["id"], "case-eligible")
        self.assertEqual(data["policy"]["refund_window_days"], 30)
        self.assertGreater(len(data["audit"]), 0)

    def test_tenant_detail_does_not_cross_workspace(self):
        status, _, _ = self.call("GET", "/api/cases/case-eligible?tenant=other-shop")
        self.assertEqual(status, 404)

    def test_oversized_and_wrong_content_type_rejected(self):
        self.assertEqual(self.call("POST", "/api/propose", "x" * 16385, raw=True)[0], 413)
        self.assertEqual(self.call("POST", "/api/propose", {}, headers={"Content-Type": "text/plain"})[0], 415)


if __name__ == "__main__":
    unittest.main()
