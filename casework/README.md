# Casework

A local support desk that turns a proposed refund into a checked, traceable database action. The optional language model suggests what to do. A separate execution layer decides whether the action is allowed, records any required review, and updates the order, case and ledger together.

The application includes a browser workspace, a Python API, synthetic orders, regression scenarios and a live local-model smoke report. Built in September 2026 with AI coding assistance.

## Run

Python 3.11 or newer. No Python packages or paid API services required.

```sh
git clone https://github.com/esadhanani/cf_ai_investintel.git
cd cf_ai_investintel/casework
python3 -m casework seed
python3 -m casework serve
```

Open `http://127.0.0.1:8767`. Choose a case, review its order and customer message, and propose a refund or escalation. Refunds above GBP 100 require a separate review action before execution. The activity trail shows the resulting state changes.

The default database is `.casework/demo.sqlite`. Seed is additive: running it again does not reset completed cases. Use `python3 -m casework --db .casework/another.sqlite seed` and the same `--db` value with `serve` for a fresh workspace.

All stores, customers, orders and money movements are fictional. Execution changes the local database; no payment processor, customer inbox or production system is connected.

## Try three things

1. Resolve the eligible GBP 100 case. Execute the same proposal again: its original result is returned and no second refund is created.
2. Propose the GBP 250 refund. It cannot execute before a review action. Approval is attached to that exact proposal and its case, order and policy versions.
3. Try to refund the 45-day-old order. The execution rules reject it regardless of what the model or customer message says. Escalation remains available.

## Import and reconcile

```sh
python3 -m casework import-orders fixtures/import/orders-mixed.csv --batch-key first-import
python3 -m casework reconcile-refunds fixtures/import/refunds.csv
python3 scripts/ingestion_benchmark.py
```

The importer accepts `order_id,customer_id,total_pence,purchased_at,case_id,customer_message`. Valid rows, source provenance and the batch result commit together; invalid rows are quarantined with a reason and physical CSV line. An exact batch retry returns the original report without duplicating records. Reusing its key for different data is rejected. Order or case updates invalidate stale proposals through version changes. Conflicting duplicate rows are quarantined; the first valid row wins.

Reconciliation compares an external CSV export (`external_id,order_id,amount_pence`) with the local refund ledger. `external_id` must be the internal refund ID. Missing, duplicate, mismatched, unknown and invalid records are reported separately. It never writes to either ledger and is not a payment-provider connector.

The [recorded synthetic import](evaluation/ingestion-report.json) created 5,000 orders, cases, audit entries and provenance records; an exact retry added none. Its local timing describes one machine and fixture, not production throughput.

## Separate local roles

```sh
python3 -m casework provision
python3 -m casework serve --auth-config .casework/access/config.json
```

Provisioning creates a new mode-0700 directory with mode-0600 configuration and token-delivery files. It refuses to overwrite an existing directory. `config.json` stores only token hashes; `tokens.json` holds the private credentials for local distribution. Keep both files out of version control. Tokens are not printed by the command.

In this mode, the server derives the workspace and role from the token. Operators propose and execute, reviewers approve, and auditors can only read. A client cannot supply its own reviewer identity or expand its workspace. Removing a credential hash from the configuration revokes it on the next request. The browser keeps its token in memory and clears it at sign-out.

These credentials demonstrate role separation, not verified human identity: whoever holds a token can use that role. The default server without `--auth-config` is explicitly an unauthenticated demo. Offline database and import commands are trusted local administration and bypass HTTP authorization.

## Local model

With Ollama running and a model already installed:

```sh
python3 -m casework plan case-eligible --model mistral:latest
python3 -m casework plan case-eligible --model mistral:latest --propose
```

The first command only generates a suggestion. The second records it as a proposal if it passes validation. Neither approves nor executes it. Model requests go only to `127.0.0.1:11434`; there is no external API fallback or model download. The server interface uses `mistral:latest` for its optional local-model suggestion.

Model output must contain exactly an action, integer amount in pence and short reason. Case identity, expected version, approval state and execution controls are assigned by application code. Customer messages are data, not authority.

## An observed failure

The checked-in [local-model smoke report](evaluation/local-model-smoke.json) contains four real calls to the installed `mistral:latest` model using fictional cases. The model proposed a refund for a 45-day-old purchase and incorrectly described it as within the policy window. Its explanation also confused pence with pounds. The deterministic policy check rejected the proposal with `window_expired`; the order's refunded balance stayed at zero.

The GBP 250 case was blocked until the smoke script recorded an explicitly labelled test approval. The malicious-message case produced a refund limited to the actual GBP 75 order value; this is one observation, not proof of prompt-injection resistance. The model is not trusted to calculate eligibility or explain policy correctly.

Reproduce the integration smoke separately from the offline tests:

```sh
python3 scripts/model_smoke.py --model mistral:latest
```

This uses a temporary database. The four examples are not a model-quality benchmark, and outputs and timings may change across runs, model versions and machines.

## Execution design

```text
Customer message + scoped order + policy
                  |
          Human or local model
                  |
          Structured proposal
                  |
        Validate and bind versions
                  |
     Separate review if amount > GBP 100
                  |
      SQLite BEGIN IMMEDIATE transaction
        - recheck owner, date and balance
        - verify case/order/policy versions
        - resolve idempotent retries
        - write refund, case state and audit
                  |
          Commit all, or roll back all
```

Refund balances derive from the ledger. An order version prevents two separate cases from spending the same remaining balance. Exact retries return the original result; reusing a key for a different request is a conflict. A proposal can be executed only once even if a caller changes its retry key. If audit writing fails, the associated refund and status updates roll back.

Policy rules are intentionally small and fixed: a 30-day return window, positive integer amounts within the remaining order value, matching customer ownership, and review above GBP 100. Policy revision identifiers invalidate outstanding proposals. This is not a configurable policy language.

## Tests and evaluation

```sh
python3 -m unittest discover -s tests -v
python3 -m casework.evaluate
```

85 tests cover real database transactions, concurrent refunds, ownership checks, stale approvals, retry behaviour, rollback, HTTP requests and malformed model responses. CI runs offline and does not require Ollama.

The separate [14 authored scenarios](evaluation/scenarios.json) verify persisted balances, execution counts, case state and audit references in a clean database per case. The [report](evaluation/report.json) records actual versus expected effects. A harness test deliberately changes an expected balance to confirm the evaluation reports a failure. These scenarios test execution policy, not language-model understanding.

## Boundaries

This remains a local prototype. Optional opaque-token access enforces roles and workspace scope at the HTTP boundary; the default demo has no authentication. A production integration needs managed identity, TLS, a durable payment adapter, signed webhooks, provider-specific reconciliation and externally protected audit storage. The current audit records successful state changes, not every rejected request, and is not tamper-proof against database access.

The browser server binds to loopback, validates Host and Origin, requires a per-process request token for writes, limits body size and renders customer text as text nodes. Keep it local and use synthetic data. SQLite transactions cover this database only: they cannot guarantee exactly-once effects in an external payment provider.

The model connector uses Ollama's [generate API](https://docs.ollama.com/api/generate) with [structured output](https://docs.ollama.com/capabilities/structured-outputs). Schema validity does not establish factual correctness.
