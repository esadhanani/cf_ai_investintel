"""A small deterministic retrieval regression set, not an external benchmark."""
import json
import tempfile
from pathlib import Path

from .store import Store, seed

CASES = [
    ("investment", "grid connection", "harbour-grid.md"),
    ("investment", "transformer", "harbour-grid.md"),
    ("investment", "covenant headroom", "marsh-covenants.csv"),
    ("investment", "downgrades", "marsh-covenants.csv"),
    ("investment", "renewal pipeline", "copper-renewals.json"),
    ("investment", "conflicting revenue definitions", "research-policy.txt"),
    ("public-service", "housing repairs", "housing-repairs.md"),
    ("public-service", "triage", "housing-repairs.md"),
    ("public-service", "step-free access", "accessible-transport.csv"),
    ("public-service", "permit backlog", "permit-backlog.json"),
    ("investment", "Fenwick", None),
    ("public-service", "Marsh", None),
    ("investment", "xylophoniczebra", None),
    ("public-service", "xylophoniczebra", None),
]


def evaluate():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "eval.db")
        seed(store, Path(__file__).resolve().parent.parent / "fixtures")
        rows = []
        for tenant, query, expected in CASES:
            found = store.search(tenant, query, 3)["results"]
            sources = [item["source"] for item in found]
            passed = expected in sources if expected else not sources
            provenance_ok = True
            for result in found:
                source = store.document(tenant, result["document_id"], result["version"])
                lines = source["content"].splitlines()
                provenance_ok &= result["content"] == "\n".join(lines[result["line_start"]-1:result["line_end"]])
                provenance_ok &= source["content_hash"] == result["content_hash"]
            rows.append({"tenant": tenant, "query": query, "expected_source": expected,
                "sources": sources, "passed": bool(passed and provenance_ok)})
        return {"cases": len(rows), "passed": sum(row["passed"] for row in rows),
            "scope": "Synthetic authored regression cases; expected source within top 3, four scoped no-evidence cases, and exact citation verification.", "results": rows}


if __name__ == "__main__":
    report = evaluate()
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["passed"] == report["cases"] else 1)
