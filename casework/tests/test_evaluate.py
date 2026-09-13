import json
from pathlib import Path
import tempfile
import unittest

from casework.evaluate import DEFAULT_SCENARIOS, evaluate


class EvaluationTests(unittest.TestCase):
    def test_authored_policy_scenarios_pass_with_persisted_state_evidence(self):
        report = evaluate()
        self.assertEqual(report["failed"], 0, report["results"])
        self.assertEqual(report["scenario_count"], 14)
        for result in report["results"]:
            self.assertEqual(result["expected_refund_delta_pence"], result["actual_refund_delta_pence"])
            self.assertEqual(result["expected_execution_count"], result["actual_execution_count"])
            self.assertIn("final_case_status", result)
            self.assertIn("audit_event_count", result)

    def test_wrong_expected_refund_is_reported_as_failure(self):
        spec = json.loads(DEFAULT_SCENARIOS.read_text(encoding="utf-8"))
        spec["scenarios"] = [spec["scenarios"][0]]
        spec["scenarios"][0]["expected"]["refund_delta_pence"] = 4999
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deliberately-wrong-expectation.json"
            path.write_text(json.dumps(spec), encoding="utf-8")
            report = evaluate(path)
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["passed"], 0)
        self.assertIn("Expected refund delta 4999, observed 5000", report["results"][0]["error"])


if __name__ == "__main__":
    unittest.main()
