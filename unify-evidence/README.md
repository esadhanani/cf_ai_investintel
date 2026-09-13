# Unify Evidence

A local research workspace that keeps search results attached to their sources. It ingests text, Markdown, CSV and JSON, then returns excerpts with document versions, content hashes and line references.

Built in September 2026 as a new, smaller implementation of an earlier document-search prototype. The included investment and public-service cases are fictional. This is a portfolio demonstration, not a deployed client system.

## Run

Requires Python 3.11+ with SQLite FTS5. No packages, API keys or external services are needed.

```sh
git clone https://github.com/esadhanani/cf_ai_investintel.git
cd cf_ai_investintel/unify-evidence
python -m unify seed
python -m unify serve
```

Open `http://127.0.0.1:8765`. Search for `grid connection`, `covenant headroom`, `housing repairs` or `step-free access`. Select a citation to inspect the exact document version and normalized line numbers.

The parent repository contains a separate, earlier application. These commands run only Unify Evidence.

## Command line

```sh
python -m unify search investment "grid connection"
python -m unify search public-service "housing repairs"
python -m unify ingest investment ./example.md
python -m unify --db another.db seed
python -m unify --db another.db serve --port 8766
```

Reingesting the same filename in the same workspace preserves its document ID. Changed content creates a new revision and replaces the current search chunks. Unchanged normalized content is skipped. Earlier versions remain available through the document API.

Filenames are source identifiers, so two unrelated files with the same basename in one workspace represent revisions of the same document. Rename them before ingestion if they should remain separate.

## HTTP API

The local server exposes read-only endpoints:

| Endpoint | Parameters | Result |
| --- | --- | --- |
| `/api/search` | `tenant`, `q` | Ranked excerpts and citations; `no_evidence` if no term matches |
| `/api/documents` | `tenant` | Current document metadata |
| `/api/document` | `tenant`, `id`, optional `version` | Normalized text and hash for a specific revision |

Example: `/api/search?tenant=investment&q=transformer`.

Documents are ingested through the CLI. The web server accepts no upload, write or execution requests. All source content is displayed through text nodes rather than inserted as HTML.

## Design

```text
TXT / Markdown / CSV / JSON
        |
        v
Validate and normalize UTF-8 text
        |
        v
SQLite transaction
  documents: stable ID, workspace, current version
  revisions: retained text and content hash
  chunks: current text with normalized line ranges
  search_index: FTS5 lexical index
        |
        v
Workspace-filtered BM25 search
        |
        v
Excerpts + source@version:lines -> original normalized revision
```

Each document ID derives from its workspace and filename. Revision writes, old-index removal and new-index insertion happen in one transaction. Concurrent writers are serialized using SQLite's write lock.

Text is grouped into four-line chunks. CSV rows become labeled text; JSON is formatted with sorted keys. Citations point to this normalized representation, not the line numbers or byte hash of the uploaded file. Search is case-insensitive lexical matching with quoted OR terms and BM25 ranking, not semantic search or generated answers.

## Verification

```sh
python -m unittest discover -s tests -v
python -m unify.evaluate
```

The suite contains 32 tests covering ingestion, retained revisions, transactional concurrent writes, workspace filtering, malformed inputs, path validation, exact citations and the local HTTP interface. HTTP tests use an ephemeral loopback port.

The evaluation uses eight authored synthetic documents across two workspaces and 14 deterministic cases. Ten cases check that an expected source appears within the first three excerpts. Four require no evidence, including two requests for terms present only in the other workspace. Returned excerpts are checked against stored version text and hashes.

This is a small regression set, not an independent benchmark or evidence of real-world accuracy. Query wording was authored alongside the fixtures. The source-safety case demonstrates that document text remains inert in this retrieval-only application; it does not test an LLM's resistance to prompt injection.

## Boundaries

- Intended only for local synthetic demonstrations. Workspace identifiers are filters, not authenticated identities. Any user of the local API can select either workspace.
- One SQLite file contains all workspaces. There is no encryption, user management, audit-log service or production deployment hardening. BM25 statistics are shared across the index, although returned documents are filtered by workspace.
- The server binds to `127.0.0.1` and rejects unexpected Host headers. Do not expose it through a public tunnel.
- No model calls, autonomous actions, shell execution from documents or external network requests. Retrieved text may be wrong, malicious or contradictory; inspect its source.
- No PDF, OCR, spreadsheet workbook ingestion, semantic retrieval or automatic conflict resolution. No claims of commercial results, customer adoption or production scale.
- A document is limited to 1 MB. The demonstration retains revision history and does not implement deletion or retention policies. Use synthetic material only.

## Next useful extension

Evaluate retrieval against questions written independently of the source fixtures, then compare lexical search against a semantic retriever. Add authentication and separate tenant storage before considering any shared deployment.
