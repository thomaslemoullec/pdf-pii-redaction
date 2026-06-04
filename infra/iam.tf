# ONE dedicated, least-privilege service account runs both the UI service and the
# batch job. It can read/write ONLY the data bucket (not project-wide), call Vertex
# AI and (optionally) Cloud DLP, publish lifecycle events, and trigger the batch job.
resource "google_service_account" "app" {
  account_id   = "pdf-anonymiser-${var.environment}"
  display_name = "PDF Anonymiser (${var.environment})"
}

# Storage: object admin on the data bucket ONLY (read source, write output + control).
resource "google_storage_bucket_iam_member" "app_storage" {
  bucket = google_storage_bucket.data.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.app.email}"
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
