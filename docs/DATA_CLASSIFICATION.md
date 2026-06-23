# Data classification

The anonymiser already classifies every document's sensitivity during the PII scan.
This document describes that classification, **where it is recorded today**, and — in a
clearly-labelled, non-binding section — **example** actions a customer could wire to each
tier later.

> **Scope.** The classification is **informative only**. It is computed, persisted, and
> surfaced — it does **not** currently change any pipeline action (no model/endpoint
> routing, no review gating, no access control, no retention or DLP change). The
> "example action paths" further down are illustrative and **not implemented**.

## Tier taxonomy

The tiers are the existing PII types in
[`src/pdf_anonymiser/pii.py`](../src/pdf_anonymiser/pii.py) — no new taxonomy is
introduced. Two values, both computed by `SensitivityPolicy.routing(...)` over a
document's findings:

- **`Sensitivity`** (`NONE < LOW < MEDIUM < HIGH`) — the document's *maximum* per-finding
  sensitivity. Per-type defaults live in `_TYPE_SENSITIVITY` (e.g. phone/email = `LOW`,
  name/address/signature = `MEDIUM`, date-of-birth/ID/IBAN = `HIGH`).
- **`Routing`** — an advisory handling label derived from the sensitivity and whether the
  sensitive findings are localised (box-bounded):

| Sensitivity tier | Routing label        | When                                                        |
| ---------------- | -------------------- | ----------------------------------------------------------- |
| `NONE`           | `ok_for_global`      | No PII found.                                                |
| `LOW`            | `ok_for_global`      | Max sensitivity at/below the policy low-water mark (`LOW`).  |
| `MEDIUM`/`HIGH`  | `redact_first`       | Over-threshold findings, **all** localised to known boxes.  |
| `MEDIUM`/`HIGH`  | `in_perimeter_only`  | Over-threshold findings, at least one **not** localised.    |

`Routing` is a *hint about the original's sensitivity*, not an enforced egress gate — the
anonymiser removes the PII regardless of the label.

## How the tier is recorded today

The computed `max_sensitivity` (tier) and `routing` (label) are persisted in three places,
all consistent with what the review UI shows:

1. **Per-document result record** — `PiiDoc.max_sensitivity` and `PiiDoc.routing`, written
   to `gs://<control>/<job_id>/results/<doc>.json`
   ([`pii_result_store.py`](../src/pdf_anonymiser/pii_result_store.py)).
2. **Run index** — the compacted `gs://<control>/<job_id>/index.json` is built from those
   same records at completion, so the tier/routing are queryable per run.
3. **GCS object custom metadata** — every **output** write (the synthetic page PNGs and
   the combined PDF under `…/unvalidated/<doc>/`) and every **control-plane result** write
   carry custom metadata so the classification travels with the object itself, queryable
   without parsing the JSON body (`gsutil stat`, the Storage API, or a metadata-based
   lifecycle/inventory query):

   ```
   sensitivity   = <NONE|LOW|MEDIUM|HIGH>
   routing       = <ok_for_global|redact_first|in_perimeter_only>
   classified_by = sensitivity_policy
   ```

   This metadata is re-attached when a result object is rewritten (human validation or a
   rerun) and travels with the object on copy/promotion, so it is never silently dropped.

The review UI surfaces the same `routing` and `max_sensitivity` values (document detail
page and the run progress table), so the recorded classification and the displayed
classification are one and the same — it is **persisted, not merely shown**.

---

## Example action paths per tier (ILLUSTRATIVE — customer-configurable, NOT implemented today)

The matrix below is a **menu of options**, not current behaviour. Nothing here is wired
into the pipeline; each row is an example a customer might choose to implement per
engagement, against their own risk appetite and regulatory posture. The system today takes
**the same action for every tier** and merely records the tier.

### Processing residency / model endpoint

- **Low (`ok_for_global`):** *could* permit the global Gemini endpoint and global Cloud DLP
  for cost/latency.
- **High (`in_perimeter_only`):** *could* force the EU-pinned Gemini model and avoid any
  `location="global"` service for page content.

> **Known residency consideration (do not infer it is resolved).** Today Gemini is
> EU-pinned by default (`gemini_location=europe-west4`,
> [`config.py`](../src/pdf_anonymiser/config.py)), **but** the Cloud DLP detector defaults
> to `location="global"` (`DlpPiiDetector(..., location="global")` in
> [`pii.py`](../src/pdf_anonymiser/pii.py), ~line 322). For documents that must stay in an
> EU perimeter this is a residency gap to resolve **per deployment** (e.g. pin DLP to an EU
> location, or disable the DLP legs). It is flagged here intentionally and **not changed**
> by this informative layer.

### Verification rigor

- **Low:** *could* accept the standard single-pass scan/judge.
- **High:** *could* always run the certified value-carryover leak check
  (`pii_dlp_leak_check`), allow more anonymise→judge attempts, raise the pass threshold,
  and hard-fail on any residual certified leak.

### Human review gating

- **Low:** *could* auto-validate documents that pass on metrics, surfacing only exceptions.
- **High:** *could* always require a human sign-off before a document is marked
  `validated`, regardless of score.

### Output storage tier / access

- **Low:** *could* use the standard `unvalidated/` → `validated/` output prefixes.
- **High:** *could* land output under a dedicated prefix with reviewer access restricted via
  IAM Conditions, and optionally use CMEK on those objects.

### Retention / audit

- **Low:** *could* follow the default retention/audit posture.
- **High:** *could* apply tighter retention, stricter (e.g. data-access) audit logging, and
  longer-lived audit trails.

---

### Wiring any of these later

Because the tier and routing are already persisted on each object and result record, a
future enforcement layer can read them directly (object metadata or the run index) and act
**without re-classifying**. Implementing any row above is a deliberate, customer-scoped
change — gated on the customer's choice — and would be specified and reviewed separately
from this informative classification layer.
