import copy
import io
import json
import unittest
from unittest.mock import Mock, patch

from casework.planner import PlannerError, plan_case, validate_proposal


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.store = Mock()
        self.store.get_case.return_value = {'id': 'case', 'customer_message': 'Please refund my order.',
                                           'status': 'open', 'version': 2,
                                           'order': {'total_pence': 6000, 'remaining_pence': 6000}}
        self.store.get_policy.return_value = {'version': 1, 'return_days': 30}
        self.valid = {'action': 'refund', 'amount_pence': 6000, 'reason': 'Refund requested.'}

    def call_with(self, body):
        opener = Mock()
        opener.open.return_value = io.BytesIO(body)
        with patch('casework.planner.urllib.request.build_opener', return_value=opener):
            result = plan_case(self.store, 'demo-shop', 'case')
        return result, opener

    def test_real_protocol_and_snapshot_version_without_mutation(self):
        result, opener = self.call_with(json.dumps({'done': True, 'response': json.dumps(self.valid)}).encode())
        self.assertEqual(result['expected_version'], 2)
        self.assertEqual(result['amount_pence'], 6000)
        req = opener.open.call_args.args[0]
        self.assertEqual(req.full_url, 'http://127.0.0.1:11434/api/generate')
        sent = json.loads(req.data)
        self.assertFalse(sent['stream'])
        self.assertEqual(sent['options']['temperature'], 0)
        self.assertEqual(json.loads(sent['prompt'])['case']['id'], 'case')
        self.store.propose.assert_not_called()
        self.store.approve.assert_not_called()
        self.store.execute.assert_not_called()

    def test_extra_authority_fields_rejected(self):
        for key in ('actor', 'approved', 'tenant', 'case_id', 'expected_version'):
            with self.subTest(key=key), self.assertRaises(PlannerError):
                validate_proposal(dict(self.valid, **{key: True}))

    def test_amount_requires_integer_not_bool_float_or_string(self):
        for amount in (True, 1.5, '6000', -1, 0, 1_000_000_001):
            with self.subTest(amount=amount), self.assertRaises(PlannerError):
                validate_proposal(dict(self.valid, amount_pence=amount))

    def test_escalation_cannot_move_money(self):
        with self.assertRaises(PlannerError):
            validate_proposal(dict(self.valid, action='escalate'))
        self.assertEqual(validate_proposal(dict(self.valid, action='escalate', amount_pence=0))['amount_pence'], 0)

    def test_duplicate_keys_rejected(self):
        body = {'done': True, 'response': '{"action":"escalate","action":"refund","amount_pence":6000,"reason":"x"}'}
        with self.assertRaises(PlannerError):
            self.call_with(json.dumps(body).encode())

    def test_incomplete_truncated_and_malformed_responses(self):
        for body in ({'done': False, 'response': json.dumps(self.valid)},
                     {'done': True, 'done_reason': 'length', 'response': json.dumps(self.valid)},
                     {'done': True, 'response': {}}, {'done': True, 'response': 'invalid'},
                     {'done': True, 'error': 'failed', 'response': json.dumps(self.valid)}):
            with self.subTest(body=body), self.assertRaises(PlannerError):
                self.call_with(json.dumps(body).encode())

    def test_oversize_response_rejected(self):
        with self.assertRaises(PlannerError):
            self.call_with(b' ' * 65537)

    def test_reason_and_action_validation(self):
        for field, value in [('reason', ''), ('reason', 'x' * 301), ('reason', 7), ('action', 'approve')]:
            with self.subTest(field=field, value=value), self.assertRaises(PlannerError):
                validate_proposal(dict(self.valid, **{field: value}))

    def test_invalid_configuration_never_calls_model(self):
        with patch('casework.planner.urllib.request.build_opener') as opener:
            for timeout in (0, -1, float('nan'), True, 121):
                with self.assertRaises(PlannerError):
                    plan_case(self.store, 'demo-shop', 'case', timeout=timeout)
            opener.assert_not_called()


if __name__ == '__main__':
    unittest.main()
