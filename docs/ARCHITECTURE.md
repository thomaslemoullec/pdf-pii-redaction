# Architecture

PDF Anonymiser is a small, GCS-only system with three moving parts: a **pipeline**
(the anonymisation work), a **store** (GCS-only state), and a **review UI**. Everything
I/O-bound sits behind a protocol so the core is unit-tested without GCS or Gemini.

```
                 ┌──────────────────────────── Cloud Run service: review UI ─┐
                 │  FastAPI + HTMX   launch · live progress · guided review   │
                 └───────────┬───────────────────────────────▲───────────────┘
            create job +      │ :run (taskCount = doc count)   │ reads results
            "started" event   ▼                                │
   ┌──────────────── Cloud Run job: batch (N parallel shards) ─┴─────────────┐
   │  per shard:  list source ─► round-robin shard ─► for each PDF:          │
   │     render ─► scan ─► (parallel pages) anonymise+judge ─► write result   │
   │  last doc latches completion (create-once marker) ─► "finished" event    │
   └───────────────┬───────────────────────────────────────────────────────┘
                   │ immutable objects                ▲ OBJECT_FINALIZE
                   ▼                                  │ notifications
   ┌──────── Cloud Storage (one bucket) ──────────────┴───┐     ┌── Pub/Sub ──┐
   │  incoming/…                 source PDFs              │     │  events      │
   │  _pii_runs/<job>/…          control plane (state)    │────►│  started     │
   │  anonymised/{unvalidated,validated}/<doc>/…  output  │     │  finished    │
   └──────────────────────────────────────────────────────┘     └─────────────┘
```

## The pipeline (`pii_review.py`, `synthesize.py`, `pii.py`, `redaction_judge.py`)

One document flows through these stages, emitted as a stream of events so the UI can
render progress live. (A per-job **Flash** planner, `pii_type_agent.py`, first maps the
operator's free-text description to a scoped PII-type list.)

1. **Scan** (`pii.scan_document`). Every page is read by the detection ensemble
   (`EnsemblePiiDetector` = Gemini-vision **∪** Cloud DLP). Detectors report PII
   **types and locations, never values** — a leak in the report would defeat the
   purpose. Each finding keeps its provenance (`gemini` / `dlp`) so the UI can show who
   spotted what. DLP adds certified, checksum-validated detectors (e.g. IBAN) where
   Gemini is contextual; a single detector failing degrades to the others rather than
   sinking the scan.

2. **Anonymise + check**, per page, **concurrently**, run by the **redaction agent**
   (`redaction_agent.py`). The agent calls each signal — and the retry/stop decision — as
   an explicit, swappable tool: `SynthesizeTool` (the image model regenerates the page),
   then three independent leak signals — deterministic **metrics**, a **certified DLP
   value-carryover** check (`DlpLeakTool`), and the **LLM-as-judge** subagent (`JudgeTool`)
   — combined by a **deterministic** `RedactionPolicy` that decides retry or stop, with a
   `FeedbackTool` turning a failure into a targeted correction for the next attempt.
   Bounded by `max_attempts`; the best-scoring attempt wins (a certified leak sinks an
   attempt's score to zero). The decision is deterministic on purpose — it gates PII
   leaks, so it must be auditable, not a model call. Pages are independent and the slow leg
   is image generation, so they run on a thread pool capped by `PII_MAX_PARALLEL` (bounded
   by Vertex quota — over-driving it causes 504s). `synthesize.anonymize_until_clean` is a
   thin back-compat wrapper over the agent.

3. **Assemble** into a per-document result and write the PII-free pages + a recombined
   PDF under `…/unvalidated/<doc>/`.

**Score & verdict** (`redaction_metrics.py`): `score = removal_recall × fidelity`;
verdict is `pass` (no leak, fidelity ≥ 0.9), `review` (no leak, lower fidelity), `fail`
(a real value survived), or `error`. Document = worst page verdict, mean score.

### Certified leak check: value-carryover, *on* by default
The output check compares **values**, not types: DLP reads the real values on the source
page and on the synthetic page (`scan_values`, `include_quote=true`, in-memory only), and a
source value that still appears in the output is a certified leak (`certified_value_leaks`,
`PII_DLP_LEAK_CHECK=1`). Because it compares values, a synthetic fake (a *different* value)
never trips it — which is why it's safe **on by default**, unlike a naive type-presence
re-scan (which flagged the fakes and was unusable). It complements the metrics' own
value-match and the LLM judge: a real value must slip past two independent extractors to be
missed. DLP also runs on the *input* scan (`PII_USE_DLP=1`).

## The store (`pii_result_store.py`) — GCS-only, no database

State lives entirely in the bucket under `_pii_runs/<job_id>/`:

```
job.json                 the job header (label, source/output, totals, review policy)
results/<doc>.json       ONE immutable object per document (written once, by its task)
index.json               a completion-time compaction of all results (portable / BQ-able)
_complete.marker         the exactly-once completion latch
```

Three properties make this safe under parallel writers without a database:

- **One immutable object per document.** Parallel tasks never write the same object, so
  there's no contention and no lost update. A retried task re-writes its own object
  with identical content — idempotent.
- **Exactly-once completion.** When a task observes that the last document has landed it
  attempts to create `_complete.marker` with `ifGenerationMatch=0` ("create only if
  absent"). GCS serialises concurrent attempts so **exactly one** wins, fires the
  `finished` event, and writes `index.json`. Whoever loses is a no-op.
- **Live reads, snapshot artifact.** The UI lists documents by reading the live
  `results/` objects (so a human validation shows immediately), *not* `index.json` —
  the index is a frozen completion snapshot kept as the portable, BigQuery-loadable
  artifact. (Reading the index for the live view was a real bug: it hid later
  validations.) Reads are TTL-cached (~3s) to keep the polling UI cheap.

The store sits behind a `PiiResultStore` **protocol** with two implementations:
`InMemoryPiiResultStore` (tests) and `GcsPiiResultStore` (prod), selected by
`result_store_from_env`. The same protocol could back a Firestore implementation later
without touching the pipeline or the UI.

## Batch fan-out (`pii_batch.py`, `batch_runner.py`)

A Cloud Run **Job** runs the folder in parallel. The UI sets `taskCount` = document
count (capped at 100) when it triggers the job; each task takes a **round-robin** shard
(`shard(items, index, count)`) — round-robin, not contiguous blocks, so a run of large
documents is spread across tasks rather than piling on one. A per-document failure is
isolated (logged, recorded as an `error` document) so one bad PDF never sinks a shard.
`batch_max_retries` (default 3) lets a crashed task retry; because the work-list is
re-derived deterministically and per-doc objects are immutable, a retry safely
re-processes its shard.

## Observability & notifications (`obs.py`, `pii_events.py`, `infra/monitoring.tf`)

The app emits one **structured JSON log** per document and per job-lifecycle step
(`event=pii.document` / `pii.job`, with verdict, attempts, leaks, seconds). On Cloud Run
these land in Cloud Logging as `jsonPayload`; Terraform defines **log-based metrics** over
them and a **Logs & Metrics dashboard** — so the app never calls a metrics API. The
dashboard URL is injected as `PII_DASHBOARD_URL` and surfaced on each job page and in the
events. **Pub/Sub** `started` / `finished` events let external systems react; `finished`
is published by whoever wins the completion latch (a worker or the web backstop), so it
fires exactly once and carries the verdict breakdown + logs/dashboard links.
**Terraform state** is remote (GCS backend) for locking + team operation.

## The review UI (`webapp/app.py`)

A small FastAPI app, server-rendered with HTMX for the live progress poll (10s, plus a
manual refresh). The four collaborators — object store, result store, batch launcher,
type planner — are constructor-injected, so the entire HTTP surface is tested with
in-memory fakes (no GCS, no Gemini). The **guided review** walks the queue worst-score
first: validating or rejecting a document advances to the next one and keeps a
"X of N awaiting review" counter; the policy chosen at launch decides who's in the
queue at all.

## Seams (why it tests without the cloud)

| Protocol / seam | Prod | Test double |
|-----------------|------|-------------|
| `PiiResultStore` | `GcsPiiResultStore` | `InMemoryPiiResultStore` |
| `ObjectStore` | `GcsObjectStore` | `InMemoryObjectStore` |
| `PiiDetector` | Gemini / DLP / ensemble | fakes returning fixed findings |
| `PiiReviewService` | `GeminiPiiReviewService` | a fake review |
| batch launcher / type planner | Cloud Run REST / LLM planner | injected callables |

## Security & residency

- One **dedicated, least-privilege** service account: bucket-scoped `objectAdmin` (not
  project-wide), `aiplatform.user`, optional `dlp.user`, publish-only on the events
  topic, and `run.developer` on its own job.
- The bucket enforces uniform IAM + public-access-prevention, with optional CMEK and
  retention.
- **Residency**: page content (real PII) is the model input; the client defaults to an
  EU Vertex location. Preview models may be global-only — a documented, deliberate
  trade-off (see the README).
- Detectors are **PII-minimal** (types + locations, never values), so stored metadata
  can't itself leak.

## What this is not

Routing labels (`SensitivityPolicy` → "low / sensitive / highly sensitive") are an
**advisory hint** shown to the reviewer, *not* an enforced egress gate. The product's
job is to remove the PII; the label just flags how sensitive the original was.
