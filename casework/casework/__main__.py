"""Small local CLI. The web application has no public deployment mode."""
import argparse
import json
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
            server = make_server(store, port=args.port)
            print('Casework: http://127.0.0.1:' + str(server.server_address[1]), flush=True)
            print('Synthetic local workspace. Ctrl+C stops the server.', flush=True)
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
