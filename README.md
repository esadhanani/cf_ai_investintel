# Applied AI projects

Small applications for working with evidence, customer requests and financial information.

## Casework

[Casework](casework/) is a support workflow application with a browser interface, optional local-model proposals and a transactional refund ledger. Its browser workspace imports orders, exposes invalid rows, checks policy and reviewer permissions before executing refunds, and reconciles external refund exports. Transactions prevent duplicate refunds and retain an audit trail.

The [project README](casework/README.md) includes a reproducible demo and an observed failure: the local model proposed an expired refund, and the execution layer rejected it without changing the order balance. All data and payment effects are synthetic and local.

## Unify Evidence

[Unify Evidence](unify-evidence/) is a local Python application for searching a versioned document collection. It uses SQLite full-text search, keeps sources attached to results and separates documents by tenant. It includes synthetic investment and public-service examples, an evaluation script and regression tests.

The new September 2026 implementation develops ideas from an earlier Unify prototype. It runs offline and does not need a model API key. The included data is fictional.

Start with the [project README](unify-evidence/README.md) for the demo and test commands.

## InvestIntel

The original November 2025 experiment is a small Cloudflare Workers AI summarisation endpoint. It sends user-supplied text to a model; it does not retrieve live news, verify financial facts or make investment decisions.

The September 2026 update removes shared server-side history, validates requests, limits input size and handles provider errors. The repository no longer tracks installed dependencies.

### Run the checks

Node.js 24 or newer is required. There are no npm package dependencies.

```sh
npm test
```

The tests use a stub model and make no external requests. A passing test does not verify model quality or a live Cloudflare deployment.

### API

- `GET /` describes the service.
- `POST /query` accepts JSON with a non-empty `topic` string, up to 8,000 characters. Supply the text you want summarised.
- `GET /history` returns `410 Gone`. History is no longer stored in a shared worker instance.

Successful queries return `summary` and `time`. Invalid requests return `400`, `413` or `415`; unsupported routes or methods return `404` or `405`; provider failures return `502` without internal exception details.

`wrangler.toml` retains the original Workers AI binding. Running the live endpoint requires your own Cloudflare account and may incur model charges. This update does not deploy it. The model receives the supplied text. There is no built-in authentication or rate limiting, so the endpoint is intended as a small local or access-controlled demonstration.

## Development

The September 2026 work was developed with AI coding assistance and checked with executable tests. Examples and measurements should be read within their stated scope, not as evidence of production deployments or customers.
