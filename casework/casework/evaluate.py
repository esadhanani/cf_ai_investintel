"""Run authored policy scenarios against real database mutations.

No model is called. Passing these cases is regression evidence, not a claim of
production readiness, model accuracy or a real-world success rate.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile


DEFAULT_SCENARIOS = Path(__file__).resolve().parents[1] / "evaluation" / "scenarios.json"


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def run_scenario(scenario, reference_time):
    from .core import DomainError, Store

    clock = datetime.fromisoformat(reference_time)
    tenant = scenario["tenant"]
    case_id = scenario["case_id"]
    action = scenario.get("action", "refund")
    observations = []
    with tempfile.TemporaryDirectory(prefix="casework-eval-") as directory:
        db = Path(directory) / "casework.sqlite3"
        store = Store(db, clock=lambda: clock)
        store.seed()
        initial = store.get_case(tenant, case_id)
        initial_refund = initial["order"]["refunded_pence"]
        proposal = None
        last_execution = None
        proposal_key = scenario["id"] + "-proposal"
        execution_key = scenario["id"] + "-execute"

        for step in scenario["steps"]:
            operation = step["operation"]
            expected_error = step.get("error")
            try:
                if operation == "propose":
                    current = store.get_case(tenant, case_id)
                    result = store.propose(tenant, case_id, {
                        "action": action,
                        "amount_pence": step.get("amount_pence", scenario["amount_pence"]),
                        "reason": "Authored evaluation scenario " + scenario["id"],
                        "expected_version": current["version"] + step.get("version_offset", 0),
                        "idempotency_key": proposal_key,
                    })
                    proposal = result
                elif operation == "approve":
                    result = store.approve(tenant, case_id, proposal["id"], actor="evaluation-reviewer")
                elif operation == "execute":
                    result = store.execute(tenant, case_id, proposal["id"], execution_key)
                    if step.get("same_as_previous"):
                        _assert(result == last_execution, "Idempotent replay changed the result")
                    last_execution = result
                elif operation == "advance_order_version":
                    # A fixture simulates an intervening order-system write. It
                    # changes no refund balance and is not a second refund API.
                    with sqlite3.connect(db) as connection:
                        changed = connection.execute(
                            "UPDATE orders SET version=version+1 WHERE tenant=? AND id=?",
                            (tenant, initial["order"]["id"]),
                        ).rowcount
                    _assert(changed == 1, "Order-version fixture did not update exactly one order")
                    result = {"fixture": "intervening_order_update"}
                elif operation == "advance_case_version":
                    with sqlite3.connect(db) as connection:
                        changed = connection.execute(
                            "UPDATE cases SET version=version+1 WHERE tenant=? AND id=?",
                            (tenant, case_id),
                        ).rowcount
                    _assert(changed == 1, "Case-version fixture did not update exactly one case")
                    result = {"fixture": "intervening_case_update"}
                elif operation == "advance_policy":
                    store.update_policy("evaluation-policy-v2")
                    result = {"fixture": "policy_revision"}
                elif operation == "wrong_tenant_read":
                    result = store.get_case("other-shop", case_id)
                elif operation == "read_only":
                    before = store.get_case(tenant, case_id)
                    result = store.get_case(tenant, case_id)
                    _assert(result == before, "Reading customer text changed case state")
                    _assert(result["order"]["refunded_pence"] == initial_refund,
                            "Customer text caused an implicit refund")
                else:
                    raise ValueError("Unknown evaluation operation: " + operation)
            except DomainError as error:
                observations.append({"operation": operation, "error": error.code})
                _assert(error.code == expected_error,
                        f"{operation}: expected error {expected_error!r}, observed {error.code!r}")
            else:
                _assert(expected_error is None,
                        f"{operation}: expected {expected_error!r}, but operation succeeded")
                observations.append({"operation": operation, "outcome": "success"})

        final = store.get_case(tenant, case_id)
        audit = store.trace(tenant, case_id)
        expected = scenario["expected"]
        refund_delta = final["order"]["refunded_pence"] - initial_refund
        _assert(refund_delta == expected["refund_delta_pence"],
                f"Expected refund delta {expected['refund_delta_pence']}, observed {refund_delta}")
        # Inspect persisted effects independently of the API's return value.
        with sqlite3.connect(db) as connection:
            execution_count = connection.execute(
                "SELECT COUNT(*) FROM executions e JOIN proposals p ON p.tenant=e.tenant AND p.id=e.proposal_id WHERE p.tenant=? AND p.case_id=?",
                (tenant, case_id),
            ).fetchone()[0]
        _assert(execution_count == expected["execution_count"],
                f"Expected {expected['execution_count']} ledger entries, observed {execution_count}")
        execution_events = [event for event in audit if event["event"] == "action_executed"]
        _assert(len(execution_events) == execution_count,
                "Execution audit count does not match persisted executions")
        if expected["execution_count"]:
            _assert(last_execution["action"] == expected["action"], "Wrong executed action")
            _assert(last_execution["amount_pence"] == scenario["amount_pence"], "Wrong executed amount")
            _assert(bool(audit), "Executed action has no audit trace")
            _assert(final["status"] == expected["final_status"], "Unexpected terminal case state")
            _assert(execution_events[0]["facts"]["amount_pence"] == scenario["amount_pence"],
                    "Audit amount differs from the requested amount")
            _assert(execution_events[0]["facts"]["action"] == action,
                    "Audit action differs from the requested action")
            _assert(execution_events[0]["facts"]["ledger_id"] == last_execution["ledger_id"],
                    "Audit ledger reference differs from execution result")
        else:
            _assert(final["status"] == initial["status"], "Blocked operation changed case state")
        return {
            "id": scenario["id"], "passed": True,
            "expected_refund_delta_pence": expected["refund_delta_pence"],
            "actual_refund_delta_pence": refund_delta,
            "expected_execution_count": expected["execution_count"],
            "actual_execution_count": execution_count,
            "final_case_status": final["status"],
            "audit_event_count": len(audit), "steps": observations,
        }


def evaluate(scenarios_path=DEFAULT_SCENARIOS):
    spec = json.loads(Path(scenarios_path).read_text(encoding="utf-8"))
    scenarios = spec["scenarios"]
    _assert(bool(scenarios), "No evaluation scenarios")
    ids = [scenario["id"] for scenario in scenarios]
    _assert(len(ids) == len(set(ids)), "Scenario IDs must be unique")
    results = []
    for scenario in scenarios:
        try:
            result = run_scenario(scenario, spec["reference_time"])
        except Exception as error:
            result = {"id": scenario["id"], "passed": False,
                      "error_type": type(error).__name__, "error": str(error)}
        results.append(result)
    passed = sum(result["passed"] for result in results)
    return {
        "evaluation": "deterministic_policy_regression",
        "reference_time": spec["reference_time"],
        "scope": "Authored synthetic fixtures using the real workflow core and a clean database per scenario. No LLM or production-quality measurement.",
        "scenario_count": len(results), "passed": passed,
        "failed": len(results) - passed, "results": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate(args.scenarios)
    rendered = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    raise SystemExit(0 if report["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
