# Running on Temporal Cloud

Nothing in the code changes. `soc.settings.connect()` uses Temporal's standard environment
config (`ClientConfigProfile`), so the same worker and console reach the local dev server or a
Cloud namespace depending only on environment variables. This repo has never been run against
Cloud (no credentials in the take-home). The steps below are the checklist for doing it.

## 1. Connect

```bash
export TEMPORAL_ADDRESS=<namespace>.<account>.tmprl.cloud:7233   # or the regional API endpoint
export TEMPORAL_NAMESPACE=<namespace>.<account>
export TEMPORAL_API_KEY=...        # never commit it; .env is git-ignored
make cloud                         # console :8080 + worker + mocks, now talking to Cloud
```

Instead of the variables, you can use a named profile (`TEMPORAL_PROFILE=cloud`, in
`~/.config/temporalio/temporal.toml`) or mTLS certificates (`TEMPORAL_TLS_CLIENT_CERT_PATH`,
`TEMPORAL_TLS_CLIENT_KEY_PATH`).

## 2. Register the search attributes once

On a dev server the app registers `CaseStatus`, `Assignee` and `Severity` itself. On Cloud,
an API key for a namespace usually cannot do that. If it can't, startup logs a warning, and
you register them once in the Cloud UI (Namespace → Search Attributes) or with `tcld`:

```bash
tcld namespace search-attributes add -n $TEMPORAL_NAMESPACE \
  --sa CaseStatus=Keyword --sa Assignee=Keyword --sa Severity=Keyword
```

Until then, run with `SEARCH_ATTRIBUTES=0`. Cases are then not indexed, and the queue falls back
to one Query per case.

## 3. Encrypt payloads (recommended on Cloud)

```bash
export SOC_CODEC_KEY=$(openssl rand -base64 32)   # the same key in every process, kept in a secret store
```

Alerts, prompts and model answers then reach Cloud only as AES-GCM ciphertext. To read them in
the Cloud UI, set the namespace's codec endpoint (or your browser's) to wherever the console's
`/codec` route is reachable over HTTPS. Add the UI origin to `CODEC_CORS_ORIGINS`. The key
stays in your process, and Cloud never holds it.

## 4. Everything else is unchanged

* **Schedules**: the shift report schedule is created at startup and runs on Cloud's clock.
* **Rate limit**: `LLM_RATE_PER_S` is enforced by the server for the whole `-llm` task queue,
  however many workers you run.
* **Metrics**: `METRICS_BIND=0.0.0.0:9464` gives worker-side Prometheus metrics. Cloud's own
  metrics endpoint adds the server side (schedule-to-start latency, and so on).
* **Workers** still run in your environment. Cloud runs only the Temporal service, so the mocks,
  SOAR and Splunk stay private.

## How it would extend (not built)

* **Nexus**: expose "open a case" as a Nexus operation, so other teams' namespaces (an EDR
  team, an IT ticketing team) can start investigations without sharing a namespace or task queue.
* **Multi-region**: a Cloud namespace with a replica in a second region. If the active region
  fails, it fails over, and in-flight cases continue from their replicated histories.
  No code change is needed.
* **Continue-as-new** for cases: not needed today, because a case's history is a few hundred
  events. The intake loop, which runs forever, already uses it.
