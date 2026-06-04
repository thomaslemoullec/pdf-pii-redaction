# PDF Anonymiser

Turn a folder of PDFs that contain personal data into a **PII-free** set you can share,
with a **human in the loop**. For each page it detects the PII, **regenerates** the page
with every real value swapped for a realistic *synthetic* one (same type, same format,
same place), checks nothing leaked, and routes documents to a review queue. **Cloud
Storage only — no database.**

```
 configure → plan PII scope → for each page: scan → synthesise → check → (retry) → human review
                                             Gemini + DLP   Nano Banana   metrics·DLP·LLM   validated/ + unvalidated/
```

Why regenerate instead of redact? Masking destroys layout and misses the easy-to-miss
values (salutations, headers, the printed name under a signature). Regeneration keeps the
document genuine-looking while removing every real value.

---

## Quickstart (local, no cloud)

```bash
make install   # venv + deps
make test      # the unit suite (no network)
make serve     # review UI on http://localhost:8080
```

The UI runs locally; the anonymisation itself calls Gemini, so a real run needs a GCP
project — see **Deploy**.

## Deploy to your GCP project (one command)

```bash
gcloud auth login && gcloud auth application-default login   # once
cp infra/environments/dev.tfvars.example infra/environments/dev.tfvars
$EDITOR infra/environments/dev.tfvars                        # project_id, region, IAP allowlist
make setup                                                   # APIs → image → terraform apply
```

`make setup` prints the UI URL and bucket when done. Re-run any time; `make destroy` tears
it down. Needs `gcloud` + `terraform`; the image builds in the cloud (no local Docker).
Terraform state lives in a GCS bucket (`make tf-backend` creates it) — team-operable, with
locking. *(Upgrading an existing local-state deploy? Migrate once — see `infra/versions.tf`.)*

- **Pick models:** `make models` shows what your project can call and prints a recommended
  block; `make models-write [LOC=global|europe-west4]` writes it into your tfvars.
- **Secure the UI:** it shows real PII, so it ships with **IAP** on (`enable_iap=true`).
  List who may in `iap_members` (a `group:` is best). See [docs/IAP.md](docs/IAP.md).

## Use it

```bash
make seed                                          # upload the bundled sample PDFs to the bucket
open "$(terraform -chdir=infra output -raw ui_url)" # sign in via IAP
```

`make seed` uploads two sets: single-page synthetic docs → `gs://<bucket>/incoming-single-page/`
and a few multi-page packages → `gs://<bucket>/incoming-multi-page/`.

In the UI: **Launch** a job (source = one of those prefixes, output
`gs://<bucket>/anonymised`, optional description, review policy), watch progress, then
**Start review** — original vs synthetic side-by-side, the detected PII, the score, the
verdict. **Validate** or **Reject**; validated docs move to `…/validated/`.

## Monitoring & notifications

- **Dashboard.** Every deploy ships a Cloud Monitoring **Logs & Metrics dashboard**
  (documents by verdict, latency p50/p95, retry attempts, live logs). The link shows on
  each job page in the UI and is included in the events below. Open it any time with
  `terraform -chdir=infra output -raw dashboard_url`.
- **Pub/Sub events.** A `started` and a `finished` message are published to the events
  topic for every job, so a customer can subscribe and react. `finished` carries the
  verdict breakdown (failed/leaked counts) and the logs + dashboard links.
- The app emits **structured JSON logs** (`event=pii.document`/`pii.job`) that feed the
  dashboard's log-based metrics — no metrics API calls.

---

## How it works (in brief)

1. **Plan** — a free-text description → a scoped PII-type list (Gemini **Flash**).
2. **Scan** — each page is read by **Gemini vision ∪ Cloud DLP** (types + location, never
   values).
3. **Synthesise** — **Nano Banana Pro** regenerates the page, PII → realistic fakes,
   layout intact.
4. **Check** — three independent signals: deterministic **metrics**, a **certified DLP
   value-carryover** check (no real value survived), and an **LLM-as-judge**. Any leak →
   retry with targeted feedback (bounded), else done.
5. **Review** — worst-scoring first; a human validates or rejects.

Steps 3–4 are a small **redaction agent** (`redaction_agent.py`) that calls each signal —
and the retry/stop decision — as an explicit, swappable tool; the decision tool is
deterministic so the leak gate stays auditable.

→ Full walkthrough with the "why" behind each step: **[docs/PIPELINE.md](docs/PIPELINE.md)**.
→ Design (GCS-only store, exactly-once latch, concurrency): **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

## Verdict & score

```
score = removal_recall × fidelity        # did we remove all PII × did we leave the rest alone
```

| Verdict | Meaning |
|---|---|
| **pass** | nothing leaked and fidelity ≥ 0.9 |
| **review** | no leak, but layout/fidelity slipped |
| **fail** | a real value survived |

A document's verdict is the worst of its pages. The launch **review policy** sends *all*
docs to the queue, or only *flagged* ones (so large clean runs auto-approve).

## Data residency

Page content (with real PII at scan/judge time) is sent to Gemini. The default is an **EU**
Vertex location. The newest preview models are **global-only** — choosing them means
content leaves the EU. `make models` shows both options; pick consciously.

## Configuration

Common knobs (full list in `src/pdf_anonymiser/config.py` / `infra/variables.tf`):

| Setting | Default | What |
|---|---|---|
| `gemini_location` | `europe-west4` | Vertex location (residency) |
| `vision_model` / `planner_model` / `image_model` | 3.x Pro / 3.5 Flash / Pro image | the models |
| `pii_use_dlp` | on | union Cloud DLP into the scan |
| `pii_dlp_leak_check` | on | certified value-carryover leak check |
| `pii_max_parallel` | 4 | pages anonymised concurrently |
| `enable_iap` / `iap_members` | on / — | who can open the UI |

## Make targets

`make help` lists all. Most-used: `install` · `test` · `serve` · `setup` · `models` ·
`models-write` · `seed` · `deploy` · `destroy`.
