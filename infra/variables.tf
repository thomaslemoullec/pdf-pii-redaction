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

variable "bucket_retention_days" {
  type        = number
  description = "Optional object retention in days for the data bucket. 0 = no retention."
  default     = 0
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
