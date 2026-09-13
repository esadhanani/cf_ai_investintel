"""Optional four-case integration smoke against an installed local model.

Uses fictional records in a temporary database. Approval is scripted for testing,
not a claim that a person reviewed it. No real payment system is connected.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from casework.core import DomainError, Store
from casework.planner import PlannerError, plan_case


def run(model, timeout):
    results = []
    with tempfile.TemporaryDirectory() as temp:
        store = Store(Path(temp)/'smoke.sqlite', clock=lambda: datetime(2026, 9, 13, 12, tzinfo=timezone.utc))
        store.seed()
        for name in ('eligible', 'approval', 'expired', 'injection'):
            case_id = 'case-' + name
            result = {'case_id': case_id, 'before': store.get_case('demo-shop', case_id)['order']['refunded_pence']}
            try:
                suggested = plan_case(store, 'demo-shop', case_id, model=model, timeout=timeout)
                result['suggestion'] = suggested
                request = {k: suggested[k] for k in ('action', 'amount_pence', 'reason', 'expected_version')}
                request['idempotency_key'] = 'smoke-propose-' + name
                proposed = store.propose('demo-shop', case_id, request)
                result['proposal_status'] = proposed['status']
                if proposed['requires_approval']:
                    try:
                        store.execute('demo-shop', case_id, proposed['id'], 'smoke-execute-' + name)
                        raise AssertionError('Execution bypassed required approval')
                    except DomainError as exc:
                        if exc.code != 'approval_required':
                            raise
                        result['before_approval_error'] = exc.code
                    store.approve('demo-shop', case_id, proposed['id'], actor='scripted-test-reviewer')
                    result['approval_scope'] = 'scripted local test action, not human review'
                executed = store.execute('demo-shop', case_id, proposed['id'], 'smoke-execute-' + name)
                replayed = store.execute('demo-shop', case_id, proposed['id'], 'smoke-execute-' + name)
                result['executed'] = executed
                result['exact_replay'] = replayed == executed
            except DomainError as exc:
                result['policy_rejection'] = {'code': exc.code, 'message': exc.message}
            except PlannerError as exc:
                result['planner_error'] = str(exc)
            result['after'] = store.get_case('demo-shop', case_id)['order']['refunded_pence']
            result['case_status'] = store.get_case('demo-shop', case_id)['status']
            results.append(result)
            print(case_id + ': ' + result['case_status'], file=sys.stderr, flush=True)
    return {'observed_at': datetime.now(timezone.utc).isoformat(), 'model': model,
            'scope': 'Four authored synthetic integration smoke cases. No model-quality or real-world accuracy estimate.',
            'live_local_model': True, 'external_payments': False, 'results': results}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', default='mistral:latest')
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--output', default='artifacts/local-model-smoke.json')
    args = parser.parse_args()
    result = run(args.model, args.timeout)
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2)+'\n')
    print(path)
    raise SystemExit(1 if any('planner_error' in row for row in result['results']) else 0)
