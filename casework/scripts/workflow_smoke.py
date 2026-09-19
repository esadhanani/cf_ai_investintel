"""Exercise the complete synthetic operations workflow over real local HTTP.

No model/API calls, external payments, persistent credentials or customer data.
Approvals are automated test actions performed with distinct role credentials.
"""
import argparse
import csv
from datetime import datetime, timedelta, timezone
import http.client
import io
import json
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from casework.auth import Auth, Principal, create_credentials
from casework.core import Store
from casework.web import make_server


def csv_text(columns, rows):
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(columns)
    writer.writerows(rows)
    return stream.getvalue()


def run():
    now = datetime.now(timezone.utc)
    events = []
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / 'workflow.sqlite', clock=lambda: now)
        config, tokens = create_credentials([
            Principal(role, 'demo-shop', role) for role in ('operator', 'reviewer', 'auditor')
        ])
        server = make_server(store, port=0, auth=Auth(config))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def call(role, method, path, payload=None, expected=200):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=10)
            headers = {'Authorization': 'Bearer ' + tokens[role], 'X-CSRF-Token': server.csrf_token}
            body = None
            if payload is not None:
                headers['Content-Type'] = 'application/json'
                body = json.dumps(payload)
            try:
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                result = json.loads(response.read())
                if response.status != expected:
                    raise AssertionError(f'{method} {path}: expected {expected}, got {response.status}: {result}')
                events.append({'role': role, 'method': method, 'path': path, 'status': response.status})
                return result
            finally:
                connection.close()
        try:
            purchased = (now - timedelta(days=2)).isoformat()
            source = csv_text(['order_id','customer_id','total_pence','purchased_at','case_id','customer_message'], [
                ['workflow-large','customer-large','25000',purchased,'workflow-review','The fictional item arrived damaged.'],
                ['workflow-small','customer-small','7500',purchased,'workflow-ready','The fictional item was not suitable.'],
                ['workflow-invalid','customer-invalid','-100',purchased,'workflow-rejected','This row has an invalid amount.'],
            ])
            payload = {'csv_text': source, 'batch_key': 'workflow-import'}
            imported = call('operator', 'POST', '/api/import-orders', payload)
            assert imported['accepted'] == 2 and imported['quarantined'] == 1
            assert call('operator', 'POST', '/api/import-orders', payload) == imported
            history = call('auditor', 'GET', '/api/imports')['batches']
            assert len(history) == 1 and history[0]['batch_key'] == 'workflow-import'
            assert call('auditor', 'GET', '/api/imports/workflow-import') == imported
            denied_import = call('reviewer', 'POST', '/api/import-orders', payload, expected=403)
            case = call('operator', 'GET', '/api/cases/workflow-review')['case']
            proposal = call('operator', 'POST', '/api/propose', {
                'case_id':case['id'], 'action':'refund', 'amount_pence':25000,
                'reason':'Synthetic damaged-item workflow check', 'expected_version':case['version'],
                'idempotency_key':'workflow-proposal',
            })
            approval = {'case_id':case['id'], 'proposal_id':proposal['id']}
            denied_approval = call('operator', 'POST', '/api/approve', approval, expected=403)
            execution = dict(approval, idempotency_key='workflow-execution')
            # The policy status is asserted from the response to avoid relying on HTTP status semantics alone.
            connection = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=10)
            try:
                connection.request('POST','/api/execute',json.dumps(execution),{
                    'Authorization':'Bearer '+tokens['operator'], 'X-CSRF-Token':server.csrf_token,
                    'Content-Type':'application/json'})
                response=connection.getresponse();blocked=json.loads(response.read())
                assert response.status >= 400 and blocked['error']['code']=='approval_required'
                events.append({'role':'operator','method':'POST','path':'/api/execute','status':response.status})
            finally:
                connection.close()
            call('reviewer', 'POST', '/api/approve', approval)
            executed = call('operator', 'POST', '/api/execute', execution)
            assert call('operator', 'POST', '/api/execute', execution) == executed
            ledger = call('auditor', 'GET', '/api/refunds')['refunds']
            assert len(ledger) == 1 and ledger[0]['amount_pence'] == 25000
            columns=['external_id','order_id','amount_pence']
            wrong = csv_text(columns,[[ledger[0]['id'],ledger[0]['order_id'],24900]])
            mismatched = call('auditor','POST','/api/reconcile-refunds',{'csv_text':wrong})
            assert len(mismatched['mismatched']) == 1 and not mismatched['matched']
            correct = csv_text(columns,[[ledger[0]['id'],ledger[0]['order_id'],25000]])
            reconciled = call('auditor','POST','/api/reconcile-refunds',{'csv_text':correct})
            assert len(reconciled['matched']) == 1
            assert not any(reconciled[k] for k in ('missing_external','duplicate_external','mismatched','unknown_external','invalid_rows'))
            assert call('auditor', 'GET', '/api/refunds')['refunds'] == ledger
            final=call('auditor','GET','/api/cases/workflow-review')
            assert final['case']['order']['refunded_pence']==25000
            approval_event=next(e for e in final['audit'] if e['event']=='proposal_approved')
            assert approval_event['facts']['actor_label']=='reviewer'
            return {
                'run_at_utc':now.isoformat(), 'passed':True,
                'scope':'Synthetic local HTTP integration. Scripted role credentials, not human review or a payment processor. No model called.',
                'input_rows':3, 'accepted_rows':2, 'quarantined_rows':1,
                'batch_replay_identical':True, 'history_persisted':True,
                'reviewer_import_denied':denied_import['error']['code'],
                'operator_approval_denied':denied_approval['error']['code'],
                'execution_before_approval_denied':blocked['error']['code'],
                'execution_replay_identical':True, 'refund_count':len(ledger),
                'refund_pence':25000, 'mismatch_detected':True, 'corrected_export_matches':True,
                'reconciliation_did_not_mutate_ledger':True,
                'recorded_reviewer':approval_event['facts']['actor_label'],
                'requests':events,
            }
        finally:
            server.shutdown();server.server_close();thread.join(timeout=2)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='artifacts/workflow-report.json')
    args=parser.parse_args()
    result=run();path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'passed':result['passed'],'http_requests':len(result['requests']),'report':str(path)}))
