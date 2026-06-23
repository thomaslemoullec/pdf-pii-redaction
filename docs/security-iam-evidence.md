# IAM least-privilege: evidence & analysis

Scope: the `pdf-anonymiser-<env>` service account (`google_service_account.app`) in the
**pii-documents** project. This is the single SA that runs both the Cloud Run UI service
and the batch job. Auth is pure workload identity / ADC — no static secrets, no Secret
Manager (intentional).

Environment captured: `dev`
- Project: `pii-documents` (number `332106233455`)
- Organization: `195620200421`
- Service account: `pdf-anonymiser-dev@pii-documents.iam.gserviceaccount.com`
- Data bucket: `pii-documents-pdf-anonymiser-dev`
- Region: `europe-west3`

---

## 1. Evidence capture — CAPTURED (2026-06-22)

`recommender.googleapis.com` and `cloudasset.googleapis.com` were enabled on `pii-documents`
(only — not on any other project) and the read-only commands run:

- **IAM Recommender** → `docs/iam-recommender-pii-documents.json` = `[]`. Expected: the
  recommender needs ~60–90 days of usage telemetry to emit role-downgrade recommendations,
  and this project is below that threshold.
- **Asset IAM analysis** → `docs/iam-asset-analysis-pii-documents.json`. The **org-scoped**
  call (`--organization`) was denied — `admin@thomaslmc.altostrat.com` lacks
  `cloudasset.assets.analyzeIamPolicy` at org `195620200421`, and granting org IAM is out of
  scope. Fell back to a **project-scoped** analysis (`--project=pii-documents
  --billing-project=pii-documents`), which succeeded and lists the SA's effective bindings —
  confirming the pre-change `roles/storage.objectAdmin` over-grant that section 2 fixes.

> Note: `gcloud asset analyze-iam-policy` defaults its API quota/billing to the gcloud ADC
> quota project (here `commerzbank-intelligent-doc`). `--billing-project=pii-documents`
> pins it to this project so no other project is touched.

Authenticated as `admin@thomaslmc.altostrat.com`; active project `pii-documents` (correct).

### Run it yourself

```bash
PROJECT=pii-documents
ORG_ID=195620200421
SA=pdf-anonymiser-dev@pii-documents.iam.gserviceaccount.com

# Enable the two APIs (state change — done once):
gcloud services enable recommender.googleapis.com cloudasset.googleapis.com --project=$PROJECT

# IAM Recommender (surfaces over-granted roles for the SA):
gcloud recommender recommendations list --project=$PROJECT --location=global \
  --recommender=google.iam.policy.Recommender --format=json \
  | tee docs/iam-recommender-$PROJECT.json

# Org-wide effective access for the SA:
gcloud asset analyze-iam-policy --organization=$ORG_ID \
  --identity="serviceAccount:$SA" --format=json \
  | tee docs/iam-asset-analysis-$PROJECT.json
```

> The IAM Recommender needs ~60–90 days of usage data to produce role-downgrade
> recommendations; on a fresh/idle project it may legitimately return `[]`.

---

## 2. Storage role downgrade — APPLIED to `infra/iam.tf`

`roles/storage.objectAdmin` → `roles/storage.objectUser` on the data-bucket binding
(`google_storage_bucket_iam_member.app_storage`).

Because the bucket uses `uniform_bucket_level_access`, the only thing `objectAdmin` adds
over `objectUser` is per-object ACL / IAM management (`storage.objects.getIamPolicy` /
`setIamPolicy`), which UBLA makes inert. `objectUser` keeps `get` / `create` / `delete` /
`list` — everything the pipeline actually uses (read source PDFs, write output pages/PDFs,
write the `_pii_runs/` control plane, promote unvalidated→validated). No behavioural change,
strictly fewer permissions.

---

## 3. Prefix read/write split — SHIPPED (with the corrections below)

The requested split was:
- read-only (`roles/storage.objectViewer`) on objects under `source/`
- write (`roles/storage.objectUser`) on objects under `output/` and `_pii_runs/`

Grounding the code surfaced **two** independent reasons a *naive* version of this split
breaks the pipeline. The shipped version corrects for both: read+list is bucket-wide, only
the **write** path is prefix-conditioned, and the app now enforces the fixed prefixes so
operators can't point a job somewhere IAM would deny.

### Reason 1 — `storage.objects.list` is bucket-scoped (the documented gotcha)

IAM Conditions using `resource.name.startsWith("projects/_/buckets/<b>/objects/<prefix>")`
match **object-level** operations (get/create/delete). But `storage.objects.list` is
authorised against the **bucket** resource (`projects/_/buckets/<b>`), which never starts
with `.../objects/<prefix>` — so a prefix condition evaluates **false** for every list call.

The app lists objects in two places (confirmed in code):
- `pii_batch.list_source_pdfs` → `store.list(source_uri)` — lists the **source** prefix
  (`src/pdf_anonymiser/pii_batch.py:368`, `:124`).
- `pii_result_store` — lists `_pii_runs/` to enumerate jobs (`list_jobs`,
  `src/.../pii_result_store.py:375`) and `_pii_runs/<job>/results/`
  (`_result_gens`, `:345`).

So a `objectViewer`-on-`source/` condition would break source enumeration, and a
`objectUser`-on-`_pii_runs/` condition would break job enumeration. Listing genuinely needs
**bucket-wide** `storage.objects.list`; it cannot be prefix-scoped via IAM Conditions.

### Reason 2 — the source/output prefixes are NOT fixed in code

The source and output prefixes are an operator-chosen convention, not fixed paths:
- `webapp/app.py:391,403` — `source` and `output` are free-form `Form(...)` fields.
- `pii_batch.py:276` — the batch writes to `<output>/unvalidated/<doc>`, where `<output>` is
  the operator-supplied output root, so the layout follows whatever prefix a job is given.

Only `_pii_runs/` is a constant prefix (`PII_CONTROL_URI = gs://<bucket>/_pii_runs`,
`infra/cloud_run.tf`). A write condition pinned to a single hard-coded prefix would therefore
**deny** any job whose output root differs — which is exactly why the write root is a
configurable variable (`data_output_prefix`) shared by the IAM condition and the app, rather
than hard-coded.

### What shipped

**Infra (`infra/iam.tf`)** — the single binding became two:
- `google_storage_bucket_iam_member.app_storage_read` — `roles/storage.objectViewer`,
  **unconditioned** (bucket-wide read + list; list can't be prefix-scoped, see Reason 1).
- `google_storage_bucket_iam_member.app_storage_write` — `roles/storage.objectUser`,
  **conditioned** to `objects/output/` and `objects/_pii_runs/`. The SA can no longer
  create or delete objects under `source/` — the input PII is effectively read-only to it.

This is the **hard guardrail**: any object write outside `output/`/`_pii_runs/` is denied by
IAM regardless of application code.

**App (`src/pdf_anonymiser/webapp/app.py`)** — `validate_job_uris` enforces the layout at
job launch: `output` must be under the configured write root `gs://<data-bucket>/<PII_OUTPUT_PREFIX>/`,
and `source` must simply live in the data bucket (reads are bucket-wide, so any input prefix —
e.g. `incoming-*/` — is allowed). The data bucket is derived from `PII_CONTROL_URI` and the
write root from `PII_OUTPUT_PREFIX` (the same value that drives the IAM condition). A misdirected
job is rejected with a clear 400 + banner instead of a late 403 at write time. Enforcement is
active only when `PII_CONTROL_URI` is set (i.e. in the deployed app). The launch form's
placeholders/hints reflect the configured write prefix (`webapp/templates/launch.html`).
Covered by tests in `tests/test_webapp.py`.

### Net effect (write root is configurable via `data_output_prefix`)

| Prefix                    | read | list | write / delete |
|---------------------------|:----:|:----:|:--------------:|
| input (e.g. `incoming-*/`)|  ✅  |  ✅  |   ❌ (denied)  |
| `<data_output_prefix>/`   |  ✅  |  ✅  |       ✅       |
| `_pii_runs/`              |  ✅  |  ✅  |       ✅       |

Plus the objectAdmin→objectUser downgrade (per-object ACL/IAM management dropped, inert
under UBLA). Read/list stay bucket-wide because `storage.objects.list` cannot be prefix-
scoped and the app legitimately lists the source prefix and `_pii_runs/`.

The write root is the Terraform variable `data_output_prefix` (default `output`); it drives
both the IAM condition and the app's `PII_OUTPUT_PREFIX` env (→ `validate_job_uris`), so the
two never drift. This deployment sets it to **`anonymised`** to match the live convention.

## 4. Deployed & verified with real data (2026-06-22)

`terraform apply` (env `dev`, image pinned to the running `:20260604-200125` — no image roll):
`app_storage` (objectAdmin) destroyed; `app_storage_read` (objectViewer) + `app_storage_write`
(objectUser, conditioned to `anonymised/`+`_pii_runs/`) created; `PII_OUTPUT_PREFIX=anonymised`
added to the job + UI. Live bucket policy confirmed: objectViewer (no condition) + objectUser
(condition `writes-to-output-and-control-only`); objectAdmin gone.

Tested by executing the Cloud Run **Job** (runs AS `pdf-anonymiser-dev`, so it exercises the
real IAM — not the operator's credentials):

- **Positive** — `incoming-single-page/` → `anonymised`, `--limit 1`: completed, wrote
  `anonymised/unvalidated/<doc>/` (pages + PDF) and `_pii_runs/<job>/results/`. Read + list +
  conditioned write all work.
- **Negative** — `incoming-single-page/` → `zzz-iam-denytest` (outside the write root): the
  SA was denied — `403 … storage.objects.create denied` — and nothing was written under the
  forbidden prefix. The guardrail holds.

Test artifacts (the two `_pii_runs/iamtest-*` jobs and the transient unvalidated output) were
removed afterward; the doc's pre-existing `validated/` deliverable was untouched.

## 5. Data durability — soft delete, NOT bucket retention (2026-06-23)

A bucket-wide **retention policy** was requested but is the wrong tool for this bucket: it
makes objects immutable (no overwrite/delete) until the period elapses, which breaks the
pipeline — `set_job_status` overwrites `_pii_runs/<job>/job.json`, `promote_document` deletes
the `unvalidated/` original on approval, and the `numNewerVersions=3` lifecycle rule deletes
old versions. All would start failing with retention 403s.

Shipped instead (applied to dev): **soft delete raised 7d → 30d** plus the existing object
**versioning** — deleted/overwritten objects stay recoverable for 30 days WITHOUT blocking
normal operation. New var `bucket_soft_delete_days` (default 30); `bucket_retention_days`
kept but documented as off-by-default with a loud warning (use only on a future dedicated,
write-once deliverables bucket). Live value confirmed: `retentionDurationSeconds = 2592000`.

## 6. Audit logging — Data Access logs APPLIED (2026-06-23)

Cloud Audit **Data Access** logs (off by default in GCP) were enabled for the PII data path so
there's a "who read/wrote which object" trail. Applied to dev and confirmed live on the project
IAM policy:
- `storage.googleapis.com`: ADMIN_READ, DATA_READ, DATA_WRITE
- `aiplatform.googleapis.com`: DATA_READ, DATA_WRITE

Terraform: `infra/audit.tf`, gated by `var.enable_data_access_audit_logs` (default true). These
logs are billed by volume — set the flag false to opt out. Admin Activity logs are always on.

## 7. VPC Service Controls — AUTHORED, optional, NOT applied (org-level)

VPC-SC closes the biggest remaining gap (exfiltration of GCS/Vertex even with valid stolen
credentials). The Terraform is written but **off by default** — it creates nothing unless
`enable_vpc_sc = true`, and is **dry-run** (log-only) until `vpc_sc_enforced = true`.

Terraform: `infra/vpc_sc.tf` + vars `enable_vpc_sc` / `access_policy_id` / `vpc_sc_enforced`.
Full staged rollout: **`docs/vpc-sc-runbook.md`**.

Not applied here because it is **org-level**: a perimeter lives on the org node and spans
projects (broader than "this project only"); it needs `roles/accesscontextmanager.policyAdmin`
at the org (the account is org admin but doesn't currently hold that specific role) and the
`accesscontextmanager` API enabled on this project; and an *enforced* perimeter applied cold can
cut off the app, CI, and console — so it must go dry-run → review logs → ingress/egress rules →
enforce. The resource schema was verified with a `terraform plan` (enable_vpc_sc=true + a dummy
policy id) showing a valid dry-run perimeter (`use_explicit_dry_run_spec = true`); that plan was
discarded, not applied.
