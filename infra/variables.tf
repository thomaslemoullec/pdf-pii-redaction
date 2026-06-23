variable "project_id" {
  type        = string
  description = "GCP project to deploy into."
}

variable "region" {
  type        = string
  description = "Region for the bucket, Cloud Run service and job."
  default     = "europe-west3"
}

variable "environment" {
  type        = string
  description = "Environment suffix (dev / prod) used in resource names."
  default     = "dev"
}

variable "image" {
  type        = string
  description = "Container image (Artifact Registry) running both the UI service and the batch job."
}

# --- Storage layout ----------------------------------------------------------

variable "data_output_prefix" {
  type        = string
  description = <<-EOT
    Top-level object prefix in the data bucket that jobs write PII-free output under
    (e.g. "output" or "anonymised"). Drives BOTH the IAM write condition (the SA can only
    create/delete objects under this prefix and _pii_runs/) and the UI job-launch validation,
    so policy and app never drift. Reads/lists stay bucket-wide — see infra/iam.tf.
  EOT
  default     = "output"

  validation {
    condition     = can(regex("^[^/]+$", var.data_output_prefix))
    error_message = "data_output_prefix must be a single path segment, no leading/trailing slash (e.g. \"output\")."
  }
}

# --- Model / residency -------------------------------------------------------

variable "gemini_location" {
  type        = string
  description = <<-EOT
    Vertex AI location for the Gemini calls. Defaults to an EU region so raw page
    content (which contains real PII at scan/judge time) stays in the EU perimeter.
    Some preview models are global-only — see the README "Data residency" section.
  EOT
  default     = "europe-west4"
}

variable "vision_model" {
  type        = string
  description = "Grounded vision model for PII detection, the redaction judge, and the source analyzer (Pro tier, GA). gemini-2.5-pro is the only GA Pro model — all Gemini 3.x Pro are preview-only."
  default     = "gemini-2.5-pro"
}

variable "planner_model" {
  type        = string
  description = "Lightweight model for the free-text → PII-type planner (Flash tier, GA; not on the detection path). gemini-2.5-flash in EU; gemini-3.5-flash on global."
  default     = "gemini-2.5-flash"
}

variable "image_model" {
  type        = string
  description = "Image model for synthetic anonymisation. GA Nano Banana family (gemini-2.5-flash-image in EU; gemini-3-pro-image on global)."
  default     = "gemini-2.5-flash-image"
}

variable "pii_use_dlp" {
  type        = bool
  description = "Union Gemini-vision findings with Cloud DLP's certified detectors (input scan)."
  default     = true
}

variable "pii_dlp_leak_check" {
  type        = bool
  description = <<-EOT
    Certified value-carryover leak check: DLP reads the real values on the source and
    synthetic pages, and any source value that survives forces a retry/fail. ON by
    default; compares values, so it never false-fires on the synthetic fakes. Needs DLP
    (roles/dlp.user). Set false to rely on the Gemini judge + metrics alone.
  EOT
  default     = true
}

variable "pii_max_parallel" {
  type        = number
  description = "Pages anonymised+judged concurrently per document (bounded by Vertex quota)."
  default     = 4
}

# --- Batch fan-out -----------------------------------------------------------

variable "batch_parallelism" {
  type        = number
  description = "Max Cloud Run tasks running at once for one batch job."
  default     = 8
}

variable "batch_max_retries" {
  type        = number
  description = <<-EOT
    Per-task retries for the batch job. A retried task re-derives and re-processes its
    shard idempotently (immutable per-doc objects + a create-once completion marker make
    re-runs safe), so a crashed task can't strand its shard or hang the job.
  EOT
  default     = 3
}

variable "batch_task_timeout" {
  type        = string
  description = "Per-task timeout for the batch job."
  default     = "3600s"
}

# --- Security ----------------------------------------------------------------

variable "kms_key" {
  type        = string
  description = "Optional CMEK key for the bucket (resource id). Empty = Google-managed keys."
  default     = ""
}

variable "bucket_soft_delete_days" {
  type        = number
  description = <<-EOT
    Soft-delete retention (days) for the data bucket: deleted/overwritten objects stay
    recoverable for this window WITHOUT blocking normal writes/deletes — the right
    durability tool for this mixed-use bucket (mutable control plane + promote-deletes).
    Pairs with object versioning. GCS allows 0 (disabled) or 7-90 days.
  EOT
  default     = 30

  validation {
    condition     = var.bucket_soft_delete_days == 0 || (var.bucket_soft_delete_days >= 7 && var.bucket_soft_delete_days <= 90)
    error_message = "bucket_soft_delete_days must be 0 (disabled) or between 7 and 90."
  }
}

variable "bucket_retention_days" {
  type        = number
  description = <<-EOT
    Optional WORM retention (days) for the data bucket. 0 = off (default).
    WARNING: a bucket-wide retention policy blocks object overwrites AND deletes until the
    period elapses, which BREAKS this pipeline (job.json overwrites, promote
    unvalidated->validated deletes, noncurrent-version cleanup). For recoverability use
    bucket_soft_delete_days; for true WORM use a SEPARATE deliverables bucket. See
    docs/security-iam-evidence.md.
  EOT
  default     = 0
}

# --- Audit logging -----------------------------------------------------------

variable "enable_data_access_audit_logs" {
  type        = bool
  description = <<-EOT
    Enable Cloud Audit *Data Access* logs (DATA_READ/DATA_WRITE) for the PII data path —
    Cloud Storage (the bucket) and Vertex AI (page content at scan/judge time). Gives a
    "who read/wrote which object" trail, important for PII/compliance. ON by default.
    NOTE: Data Access logs add log volume/cost (every object read is logged); set false to
    disable. Admin Activity logs are always on regardless of this flag.
  EOT
  default     = true
}

# --- VPC Service Controls (OPTIONAL, org-level) ------------------------------
# Off by default. VPC-SC is an ORG-LEVEL change an org admin applies via a staged rollout —
# see docs/vpc-sc-runbook.md. Nothing here is created unless enable_vpc_sc = true.

variable "enable_vpc_sc" {
  type        = bool
  description = "Create the VPC-SC service perimeter for this project (Storage + Vertex AI, etc.). OFF by default; org-level — see docs/vpc-sc-runbook.md."
  default     = false
}

variable "access_policy_id" {
  type        = string
  description = "Org Access Context Manager access-policy NUMBER. Required when enable_vpc_sc = true."
  default     = ""
}

variable "vpc_sc_enforced" {
  type        = bool
  description = <<-EOT
    false (default) = DRY-RUN: the perimeter only LOGS violations (use_explicit_dry_run_spec);
    nothing is blocked — apply this first. true = ENFORCED: actually blocks cross-perimeter
    access. Flip to true only after reviewing dry-run violation logs and adding any needed
    ingress/egress rules (see docs/vpc-sc-runbook.md).
  EOT
  default     = false
}

variable "ui_min_instances" {
  type        = number
  description = <<-EOT
    Minimum warm instances for the review UI service. 1 (default) avoids cold-start
    latency for reviewers and keeps the per-instance read cache hot, at the cost of one
    small always-on instance. Set 0 to scale to zero when idle (cheaper, slower first hit).
  EOT
  default     = 1
}

variable "ui_allow_unauthenticated" {
  type        = bool
  description = <<-EOT
    If true, the UI Cloud Run service allows unauthenticated access (demo only). Keep
    false in any real deployment — the UI shows source documents with real PII. Front it
    with IAP / an internal load balancer, or invoke with an identity token.
  EOT
  default     = false
}
