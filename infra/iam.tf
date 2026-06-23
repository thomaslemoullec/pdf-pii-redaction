# ONE dedicated, least-privilege service account runs both the UI service and the
# batch job. It can read/write ONLY the data bucket (not project-wide), call Vertex
# AI and (optionally) Cloud DLP, publish lifecycle events, and trigger the batch job.
resource "google_service_account" "app" {
  account_id   = "pdf-anonymiser-${var.environment}"
  display_name = "PDF Anonymiser (${var.environment})"
}

# Storage on the data bucket ONLY, split read from write so the SA can read the source PDFs
# but never mutate or delete them — writes/deletes are confined to the output and control-
# plane prefixes. See docs/security-iam-evidence.md for the full analysis.
#
# READ + LIST is bucket-wide (objectViewer, UNCONDITIONED) on purpose: storage.objects.list
# is authorised against the BUCKET resource, not the object path, so a prefix condition
# (resource.name.startsWith(".../objects/<prefix>")) is false for every list call. The batch
# job lists the source prefix (list_source_pdfs) and the result store lists _pii_runs/
# (list_jobs, results), so both need bucket-wide list. This bucket is single-purpose and
# holds nothing the app shouldn't read.
resource "google_storage_bucket_iam_member" "app_storage_read" {
  bucket = google_storage_bucket.data.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.app.email}"
}

# WRITE (create/delete/overwrite) is restricted by IAM Condition to the configurable output
# prefix (var.data_output_prefix) and _pii_runs/ — the SA cannot write or delete anywhere
# else (source folders stay read-only to it). objectAdmin -> objectUser because the bucket is
# uniform_bucket_level_access, so the per-object ACL/IAM management objectAdmin adds is inert;
# objectUser keeps get/create/delete. resource.name.startsWith conditions apply to object-level
# ops (create/delete) — exactly the write path; listing is served by the read grant above.
#
# This is the HARD guardrail: a job whose output URI is outside the write prefix is denied at
# write time regardless of the app. The UI validates job URIs against the SAME prefix to fail
# fast (webapp launch_job + validate_job_uris, fed by PII_OUTPUT_PREFIX) so policy and app
# never drift. The write prefixes are <data_output_prefix>/ and _pii_runs/.
resource "google_storage_bucket_iam_member" "app_storage_write" {
  bucket = google_storage_bucket.data.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.app.email}"

  condition {
    title       = "writes-to-output-and-control-only"
    description = "Object create/delete limited to ${var.data_output_prefix}/ and _pii_runs/; everything else stays read-only."
    expression = join(" || ", [
      "resource.name.startsWith(\"projects/_/buckets/${google_storage_bucket.data.name}/objects/${var.data_output_prefix}/\")",
      "resource.name.startsWith(\"projects/_/buckets/${google_storage_bucket.data.name}/objects/_pii_runs/\")",
    ])
  }
}

# Vertex AI (Gemini vision + image generation).
resource "google_project_iam_member" "app_vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.app.email}"
}

# Cloud DLP — needed by the input-scan ensemble (pii_use_dlp) and/or the certified
# value-carryover leak check (pii_dlp_leak_check). Granted if either is enabled.
resource "google_project_iam_member" "app_dlp" {
  count   = (var.pii_use_dlp || var.pii_dlp_leak_check) ? 1 : 0
  project = var.project_id
  role    = "roles/dlp.user"
  member  = "serviceAccount:${google_service_account.app.email}"
}

# Publish "started" / "finished" lifecycle events.
resource "google_pubsub_topic_iam_member" "app_publisher" {
  topic  = google_pubsub_topic.events.id
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.app.email}"
}

# The UI service triggers the batch job (Cloud Run Admin :run). run.developer on the
# job lets it start executions; it acts AS the same SA, so it can set itself as the
# run identity.
resource "google_cloud_run_v2_job_iam_member" "ui_runs_job" {
  name     = google_cloud_run_v2_job.batch.name
  location = var.region
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.app.email}"
}

resource "google_service_account_iam_member" "ui_acts_as_app" {
  service_account_id = google_service_account.app.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.app.email}"
}
