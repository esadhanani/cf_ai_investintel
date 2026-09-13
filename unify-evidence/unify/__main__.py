import argparse
import json
from pathlib import Path

from .store import Store, seed


def main():
    parser = argparse.ArgumentParser(description="Unify Evidence local demo")
    parser.add_argument("--db", default="unify.db")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed", help="Load the included synthetic fixtures")
    ingest = sub.add_parser("ingest")
    ingest.add_argument("tenant")
    ingest.add_argument("file", type=Path)
    search = sub.add_parser("search")
    search.add_argument("tenant")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    try:
        store = Store(args.db)
        if args.command == "seed":
            result = seed(store, Path(__file__).resolve().parent.parent / "fixtures")
        elif args.command == "ingest":
            if args.file.stat().st_size > 1_000_000:
                raise ValueError("Document exceeds the 1 MB limit.")
            result = store.ingest(args.tenant, args.file.name, args.file.read_text(encoding="utf-8"))
        elif args.command == "search":
            result = store.search(args.tenant, args.query, args.limit)
        else:
            from .web import serve
            serve(store, args.port)
            return
    except (ValueError, OSError, UnicodeError) as exc:
        parser.exit(2, f"Error: {exc}\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
