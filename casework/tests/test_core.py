import concurrent.futures
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from casework.core import DomainError, Store


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / "casework.db"
        self.now = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)
        self.store = Store(self.db, clock=lambda: self.now)
        self.store.seed()

    def proposal(self, case="case-eligible", amount=10000, key="propose", action="refund", version=None, tenant="demo-shop"):
        current = self.store.get_case(tenant, case)
        return self.store.propose(tenant, case, {"action": action, "amount_pence": amount,
            "reason": "Synthetic customer request reviewed.", "expected_version": current["version"] if version is None else version,
            "idempotency_key": key})

    def assert_error(self, code, fn):
        with self.assertRaises(DomainError) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)

    def execute(self, proposal, case="case-eligible", key="execute", tenant="demo-shop"):
        return self.store.execute(tenant, case, proposal["id"], key)

    def test_full_refund_updates_ledger_case_order_and_trace(self):
        result = self.execute(self.proposal())
        case = self.store.get_case("demo-shop", "case-eligible")
        self.assertEqual(result["status"], "refunded")
        self.assertEqual(case["order"]["remaining_pence"], 0)
        self.assertEqual((case["version"], case["order"]["version"]), (2, 2))
        events = self.store.trace("demo-shop", "case-eligible")
        self.assertEqual(events[-1]["facts"]["ledger_id"], result["ledger_id"])

    def test_partial_then_remaining_refund(self):
        self.execute(self.proposal(amount=6000))
        self.assertEqual(self.store.get_case("demo-shop", "case-eligible")["status"], "partially_refunded")
        second = self.proposal(amount=4000, key="second")
        self.execute(second, key="second-execute")
        self.assertEqual(self.store.get_case("demo-shop", "case-eligible")["order"]["refunded_pence"], 10000)

    def test_approval_threshold_is_strictly_above_100_pounds(self):
        self.assertFalse(self.proposal()["requires_approval"])
        larger = self.proposal("case-approval", amount=10001, key="large")
        self.assertTrue(larger["requires_approval"])
        self.assert_error("approval_required", lambda: self.execute(larger, "case-approval"))

    def test_approved_refund_executes_once(self):
        proposal = self.proposal("case-approval", amount=25000)
        approved = self.store.approve("demo-shop", "case-approval", proposal["id"])
        self.assertEqual(approved["status"], "approved")
        again = self.store.approve("demo-shop", "case-approval", proposal["id"])
        self.assertEqual(again, approved)
        result = self.execute(proposal, "case-approval")
        self.assertEqual(result["amount_pence"], 25000)
        self.assertEqual(sum(e["event"] == "proposal_approved" for e in self.store.trace("demo-shop", "case-approval")), 1)

    def test_expired_purchase_rejected_without_proposal(self):
        self.assert_error("window_expired", lambda: self.proposal("case-expired", amount=1000))
        self.assertEqual(self.store.get_case("demo-shop", "case-expired")["proposals"], [])

    def test_refund_window_rechecked_at_execution(self):
        proposal = self.proposal()
        self.now += timedelta(days=21)
        self.assert_error("window_expired", lambda: self.execute(proposal))
        self.assertEqual(self.store.get_case("demo-shop", "case-eligible")["order"]["refunded_pence"], 0)

    def test_exact_30_day_boundary_allowed_but_future_date_rejected(self):
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE orders SET purchased_at=? WHERE tenant=? AND id=?", ((self.now-timedelta(days=30)).isoformat(), "demo-shop", "order-eligible"))
        self.execute(self.proposal())
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE orders SET purchased_at=? WHERE tenant=? AND id=?", ((self.now+timedelta(seconds=1)).isoformat(), "demo-shop", "order-injection"))
        self.assert_error("window_expired", lambda: self.proposal("case-injection", amount=1000, key="future"))

    def test_existing_refund_and_excess_amount_blocked(self):
        self.assert_error("amount_exceeds_remaining", lambda: self.proposal("case-refunded", amount=1))
        self.assert_error("amount_exceeds_remaining", lambda: self.proposal(amount=10001))

    def test_invalid_amounts_and_actions_rejected(self):
        for value in [0, -1, True, 1.5, "100", None, []]:
            with self.subTest(value=value):
                self.assert_error("invalid_request", lambda: self.proposal(amount=value))
        for action in ["shell", "approve", [], None]:
            with self.subTest(action=action):
                self.assert_error("invalid_request", lambda: self.proposal(action=action))

    def test_escalation_has_no_financial_mutation(self):
        proposal = self.proposal("case-expired", amount=0, action="escalate")
        result = self.execute(proposal, "case-expired")
        case = self.store.get_case("demo-shop", "case-expired")
        self.assertEqual(result["status"], "escalated")
        self.assertIsNone(result["ledger_id"])
        self.assertEqual(case["order"]["version"], 1)
        self.assertEqual(case["order"]["refunded_pence"], 0)
        self.assert_error("invalid_request", lambda: self.proposal(action="escalate", amount=1, key="invalid-escalation"))

    def test_propose_retries_return_original_snapshot(self):
        first = self.proposal()
        self.execute(first)
        retry = self.proposal(version=1)
        self.assertEqual(retry, first)
        self.assertEqual(len(self.store.get_case("demo-shop", "case-eligible")["proposals"]), 1)

    def test_proposal_idempotency_payload_conflict(self):
        self.proposal(amount=1000)
        self.assert_error("idempotency_conflict", lambda: self.proposal(amount=2000))

    def test_execution_retries_different_keys_cannot_double_refund(self):
        proposal = self.proposal()
        result = self.execute(proposal)
        self.assertEqual(self.execute(proposal), result)
        self.assertEqual(self.execute(proposal, key="new-key"), result)
        events = self.store.trace("demo-shop", "case-eligible")
        self.assertEqual(sum(e["event"] == "action_executed" for e in events), 1)

    def test_execution_idempotency_key_cannot_change_case(self):
        first = self.proposal(amount=1000)
        second = self.proposal("case-injection", amount=1000, key="second")
        self.execute(first)
        self.assert_error("idempotency_conflict", lambda: self.execute(second, "case-injection"))

    def test_stale_case_is_checked_at_proposal_and_execution(self):
        self.assert_error("stale_case", lambda: self.proposal(version=2))
        a = self.proposal(amount=5000, key="a")
        b = self.proposal(amount=5000, key="b")
        self.execute(a)
        self.assert_error("stale_case", lambda: self.execute(b, key="b"))

    def test_stale_order_is_checked_at_approval_and_execution(self):
        proposal = self.proposal("case-approval", amount=25000)
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE orders SET version=version+1 WHERE tenant=? AND id=?", ("demo-shop", "order-approval"))
        self.assert_error("stale_order", lambda: self.store.approve("demo-shop", "case-approval", proposal["id"]))
        self.assert_error("stale_order", lambda: self.execute(proposal, "case-approval"))

    def test_policy_change_invalidates_approved_proposal(self):
        proposal = self.proposal("case-approval", amount=25000)
        self.store.approve("demo-shop", "case-approval", proposal["id"])
        self.store.update_policy("2026-09-v2")
        self.assert_error("stale_policy", lambda: self.execute(proposal, "case-approval"))
        fresh = self.proposal("case-approval", amount=25000, key="fresh")
        self.assertEqual(fresh["status"], "pending_approval")

    def test_case_customer_must_own_order_even_after_proposal(self):
        proposal = self.proposal()
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE orders SET customer_id='different-customer' WHERE tenant=? AND id=?", ("demo-shop", "order-eligible"))
        self.assert_error("ownership_mismatch", lambda: self.execute(proposal))

    def test_workspace_lookalikes_remain_separate(self):
        proposal = self.proposal("case-lookalike", amount=8000)
        self.execute(proposal, "case-lookalike")
        other = self.store.get_case("other-shop", "case-lookalike")
        self.assertEqual(other["order"]["remaining_pence"], 20000)
        self.assertEqual(other["proposals"], [])
        self.assert_error("not_found", lambda: self.store.execute("other-shop", "case-lookalike", proposal["id"], "cross-tenant"))
        self.assert_error("not_found", lambda: self.store.get_case("other-shop", "case-eligible"))

    def test_customer_instruction_cannot_approve_or_expand_refund(self):
        case = self.store.get_case("demo-shop", "case-injection")
        self.assertIn("SYSTEM OVERRIDE", case["customer_message"])
        self.assert_error("amount_exceeds_remaining", lambda: self.proposal("case-injection", amount=900000))
        self.assertEqual(case["order"]["refunded_pence"], 0)
        self.assertEqual(case["proposals"], [])
        self.assertEqual(len(self.store.list_cases("other-shop")), 1)

    def test_concurrent_same_proposal_returns_one_ledger_entry(self):
        proposal = self.proposal()
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda i: self.execute(proposal, key=f"retry-{i}"), range(12)))
        self.assertEqual(len({row["ledger_id"] for row in results}), 1)
        self.assertEqual(self.store.get_case("demo-shop", "case-eligible")["order"]["refunded_pence"], 10000)

    def test_concurrent_distinct_proposals_cannot_overrefund(self):
        proposals = [self.proposal(amount=7000, key=f"p-{i}") for i in range(2)]
        def run(pair):
            i, proposal = pair
            try:
                return self.execute(proposal, key=f"e-{i}")["status"]
            except DomainError as exc:
                return exc.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, enumerate(proposals)))
        self.assertCountEqual(results, ["partially_refunded", "stale_case"])
        self.assertEqual(self.store.get_case("demo-shop", "case-eligible")["order"]["refunded_pence"], 7000)

    def test_two_cases_for_one_order_share_remaining_balance(self):
        with sqlite3.connect(self.db) as con:
            con.execute("""INSERT INTO cases SELECT tenant,'case-duplicate',customer_id,order_id,
                'A second contact about the same order.','open',1 FROM cases WHERE tenant=? AND id=?""",
                ("demo-shop", "case-eligible"))
        first = self.proposal(amount=7000, key="first-contact")
        second = self.proposal("case-duplicate", amount=7000, key="second-contact")
        def run(args):
            proposal, case, key = args
            try:
                return self.execute(proposal, case, key)["status"]
            except DomainError as exc:
                return exc.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, [(first, "case-eligible", "first-exec"), (second, "case-duplicate", "second-exec")]))
        self.assertCountEqual(results, ["partially_refunded", "stale_order"])
        self.assertEqual(self.store.get_case("demo-shop", "case-eligible")["order"]["refunded_pence"], 7000)
        self.assertEqual(self.store.get_case("demo-shop", "case-duplicate")["order"]["remaining_pence"], 3000)

    def test_ledger_status_and_audit_roll_back_together(self):
        proposal = self.proposal()
        with patch.object(self.store, "_audit", side_effect=RuntimeError("Simulated audit write failure")):
            with self.assertRaises(RuntimeError):
                self.execute(proposal)
        case = self.store.get_case("demo-shop", "case-eligible")
        self.assertEqual(case["order"]["refunded_pence"], 0)
        self.assertEqual(case["status"], "open")
        self.assertEqual(case["version"], 1)
        self.assertEqual(case["proposals"][0]["status"], "ready")
        self.assertEqual(self.execute(proposal)["amount_pence"], 10000)

    def test_seed_replay_does_not_reset_live_demo_state(self):
        self.execute(self.proposal())
        self.assertEqual(self.store.seed()["cases"], 7)
        self.assertEqual(self.store.get_case("demo-shop", "case-eligible")["status"], "refunded")

    def test_audit_contains_policy_facts_not_raw_customer_text(self):
        self.execute(self.proposal("case-injection", amount=7500), "case-injection")
        events = self.store.trace("demo-shop", "case-injection")
        self.assertTrue(all("source" in event["facts"] for event in events))
        self.assertNotIn("SYSTEM OVERRIDE", str(events))

    def test_invalid_identifiers_and_request_fields(self):
        self.assert_error("invalid_request", lambda: self.store.list_cases("../shop"))
        self.assert_error("invalid_request", lambda: self.store.propose("demo-shop", "case-eligible", {}))
        self.assert_error("invalid_request", lambda: self.proposal(key="' OR 1=1"))


if __name__ == "__main__":
    unittest.main()
