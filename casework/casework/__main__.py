"""Small local CLI. The web application has no public deployment mode."""
import argparse
import json
import os
from pathlib import Path
import sys
import uuid

from casework.core import DomainError, Store


def main():
    parser = argparse.ArgumentParser(description='Casework local support workflow')
    parser.add_argument('--db', default='.casework/demo.sqlite')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('seed')
    serve = sub.add_parser('serve')
    serve.add_argument('--port', type=int, default=8767)
    serve.add_argument('--auth-config', help='Local hash-only credential configuration')
    provision = sub.add_parser('provision', help='Create three local role credentials; never prints tokens')
    provision.add_argument('--tenant', default='demo-shop')
    provision.add_argument('--directory', default='.casework/access')
    ingest = sub.add_parser('import-orders', help='Import orders and quarantine invalid CSV rows')
    ingest.add_argument('csv_file')
    ingest.add_argument('--tenant', default='demo-shop')
    ingest.add_argument('--batch-key', required=True)
    reconcile = sub.add_parser('reconcile-refunds', help='Compare a CSV export against the local refund ledger')
    reconcile.add_argument('csv_file')
    reconcile.add_argument('--tenant', default='demo-shop')
    cases = sub.add_parser('cases')
    cases.add_argument('--tenant', default='demo-shop')
    plan = sub.add_parser('plan')
    plan.add_argument('case_id')
    plan.add_argument('--tenant', default='demo-shop')
    plan.add_argument('--model', default='mistral:latest')
    plan.add_argument('--timeout', type=float, default=45)
    plan.add_argument('--propose', action='store_true', help='Record a validated proposal; never approve or execute it')
    args = parser.parse_args()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    store = Store(args.db)
    try:
        if args.command == 'seed':
            result = store.seed()
        elif args.command == 'cases':
            result = store.list_cases(args.tenant)
        elif args.command == 'provision':
            from casework.auth import Principal, create_credentials
            config, tokens = create_credentials([
                Principal(role, args.tenant, role) for role in ('operator', 'reviewer', 'auditor')
            ])
            directory = Path(args.directory)
            directory.mkdir(parents=True, exist_ok=False, mode=0o700)
            for name, data in [('config.json', config), ('tokens.json', tokens)]:
                descriptor = os.open(str(directory / name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'w') as handle:
                    json.dump(data, handle, indent=2)
            result = {'config': str(directory / 'config.json'), 'private_tokens': str(directory / 'tokens.json'),
                      'notice': 'Local credentials created. Keep both files private; distribute each token only to its role holder.'}
        elif args.command == 'import-orders':
            from casework.ingestion import import_orders
            result = import_orders(store, args.tenant, Path(args.csv_file).read_text(encoding='utf-8-sig'), args.batch_key)
        elif args.command == 'reconcile-refunds':
            from casework.ingestion import reconcile_refunds
            result = reconcile_refunds(store, args.tenant, Path(args.csv_file).read_text(encoding='utf-8-sig'))
        elif args.command == 'plan':
            from casework.planner import plan_case
            result = plan_case(store, args.tenant, args.case_id, args.model, args.timeout)
            if args.propose:
                request = {k: result[k] for k in ('action', 'amount_pence', 'reason', 'expected_version')}
                request['idempotency_key'] = str(uuid.uuid4())
                result = {'model_output': result, 'proposal': store.propose(args.tenant, args.case_id, request)}
        else:
            from casework.web import make_server
            if not 0 <= args.port <= 65535:
                parser.error('Port must be between 0 and 65535.')
            from casework.auth import Auth
            auth = Auth.from_file(args.auth_config) if args.auth_config else None
            server = make_server(store, port=args.port, auth=auth)
            print('Casework: http://127.0.0.1:' + str(server.server_address[1]), flush=True)
            print('Authenticated local workspace.' if auth else 'Unauthenticated local demo.', flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return 0
        print(json.dumps(result, indent=2))
        return 0
    except DomainError as exc:
        print(json.dumps({'error': exc.code, 'message': exc.message}), file=sys.stderr)
        return 1
    except Exception as exc:
        from casework.planner import PlannerError
        if isinstance(exc, PlannerError):
            print(json.dumps({'error': 'planner_error', 'message': str(exc)}), file=sys.stderr)
            return 1
        raise


if __name__ == '__main__':
    raise SystemExit(main())
