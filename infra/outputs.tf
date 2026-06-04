output "bucket" {
  description = "The data bucket (source PDFs, _pii_runs/ control plane, anonymised output)."
  value       = google_storage_bucket.data.name
}

output "ui_url" {
  description = "URL of the review UI Cloud Run service."
  value       = google_cloud_run_v2_service.ui.uri
}

output "ui_access" {
  description = "How the UI is protected: iap, public (demo), or authenticated (Cloud Run IAM)."
  value       = var.enable_iap ? "iap" : (var.ui_allow_unauthenticated ? "public" : "authenticated")
}

output "batch_job" {
  description = "Name of the batch Cloud Run job."
  value       = google_cloud_run_v2_job.batch.name
}

output "events_topic" {
  description = "Pub/Sub topic carrying job started/finished lifecycle events."
  value       = google_pubsub_topic.events.name
}

output "dashboard_url" {
  description = "Cloud Monitoring Logs & Metrics dashboard for jobs."
  value       = local.dashboard_url
}

output "service_account" {
  description = "The dedicated service account both workloads run as."
  value       = google_service_account.app.email
}
