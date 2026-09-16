import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from casework.auth import Auth, Principal, create_credentials
from casework.core import Store
from casework.web import make_server


class AuthenticatedWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.sqlite")
        self.store.seed()
        principals = [Principal("operator-one", "demo-shop", "operator"),
                      Principal("reviewer-one", "demo-shop", "reviewer"),
                      Principal("auditor-one", "demo-shop", "auditor"),
                      Principal("other-reviewer", "other-shop", "reviewer")]
        config, self.tokens = create_credentials(principals)
        self.auth = Auth(config)
        self.server = make_server(self.store, port=0, auth=self.auth)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def call(self, method, path, body=None, identity=None, token=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        headers = {"X-CSRF-Token": self.server.csrf_token}
        if identity:
            token = self.tokens[identity]
        if token:
            headers["Authorization"] = "Bearer " + token
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read())
        status = response.status
        connection.close()
        return status, data

    def propose(self):
        status, proposal = self.call("POST", "/api/propose", {
            "case_id": "case-approval", "action": "refund", "amount_pence": 15000,
            "reason": "Verified damaged delivery", "expected_version": 1, "idempotency_key": "create-one",
        }, identity="operator-one")
        self.assertEqual(status, 200)
        return proposal

    def test_bootstrap_without_token_does_not_disclose_identity_or_csrf(self):
        status, data = self.call("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertTrue(data["auth_required"])
        self.assertNotIn("csrf_token", data)
        self.assertNotIn("tenants", data)

    def test_valid_bootstrap_returns_assigned_scope_without_token_echo(self):
        status, data = self.call("GET", "/api/bootstrap", identity="operator-one")
        self.assertEqual(status, 200)
        self.assertEqual(data["principal"], {"subject": "operator-one", "tenant": "demo-shop", "role": "operator"})
        self.assertNotIn(self.tokens["operator-one"], json.dumps(data))
        self.assertEqual(len(data["tenants"]), 1)

    def test_missing_invalid_and_revoked_tokens_cannot_read_or_write(self):
        for method, path, body in [("GET", "/api/cases", None), ("POST", "/api/propose", {})]:
            self.assertEqual(self.call(method, path, body)[0], 401)
            self.assertEqual(self.call(method, path, body, token="x" * 43)[0], 401)
        self.auth.revoke(self.tokens["operator-one"])
        self.assertEqual(self.call("GET", "/api/cases", identity="operator-one")[0], 401)
        self.assertEqual(self.call("POST", "/api/propose", {}, identity="operator-one")[0], 401)

    def test_read_scope_resolved_from_token_and_tenant_tampering_denied(self):
        status, data = self.call("GET", "/api/cases", identity="auditor-one")
        self.assertEqual(status, 200)
        self.assertTrue(all(case["tenant"] == "demo-shop" for case in data["cases"]))
        self.assertEqual(self.call("GET", "/api/cases?tenant=other-shop", identity="auditor-one")[0], 403)

    def test_write_tenant_tampering_denied(self):
        status, data = self.call("POST", "/api/propose", {"tenant": "other-shop", "case_id": "case-lookalike"}, identity="operator-one")
        self.assertEqual(status, 403)
        self.assertEqual(data["error"]["code"], "tenant_forbidden")

    def test_operator_cannot_approve(self):
        proposal = self.propose()
        status, _ = self.call("POST", "/api/approve", {"case_id": "case-approval", "proposal_id": proposal["id"]}, identity="operator-one")
        self.assertEqual(status, 403)

    def test_reviewer_cannot_propose_execute_or_suggest(self):
        for path in ["/api/propose", "/api/execute", "/api/suggest"]:
            self.assertEqual(self.call("POST", path, {"case_id": "case-eligible"}, identity="reviewer-one")[0], 403)

    def test_auditor_cannot_mutate_or_invoke_model(self):
        for path in ["/api/propose", "/api/approve", "/api/execute", "/api/suggest"]:
            self.assertEqual(self.call("POST", path, {"case_id": "case-eligible"}, identity="auditor-one")[0], 403)

    def test_actor_and_role_payload_cannot_change_identity(self):
        proposal = self.propose()
        for field, value in [("actor", "forged-reviewer"), ("role", "operator"), ("subject", "forged")]:
            self.assertEqual(self.call("POST", "/api/approve", {"case_id": "case-approval", "proposal_id": proposal["id"], field: value}, identity="reviewer-one")[0], 403)

    def test_reviewer_identity_is_derived_from_token_then_operator_executes(self):
        proposal = self.propose()
        body = {"case_id": "case-approval", "proposal_id": proposal["id"]}
        self.assertEqual(self.call("POST", "/api/approve", body, identity="reviewer-one")[0], 200)
        events = self.store.trace("demo-shop", "case-approval")
        approved = next(event for event in events if event["event"] == "proposal_approved")
        self.assertEqual(approved["facts"]["actor_label"], "reviewer-one")
        execute = dict(body, idempotency_key="execute-one")
        self.assertEqual(self.call("POST", "/api/execute", execute, identity="operator-one")[0], 200)
        self.assertEqual(self.store.get_case("demo-shop", "case-approval")["order"]["refunded_pence"], 15000)

    def test_other_tenant_reviewer_cannot_approve(self):
        proposal = self.propose()
        status, _ = self.call("POST", "/api/approve", {"tenant": "demo-shop", "case_id": "case-approval", "proposal_id": proposal["id"]}, identity="other-reviewer")
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
