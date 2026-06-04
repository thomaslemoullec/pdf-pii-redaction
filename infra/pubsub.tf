# Lifecycle events: external systems subscribe here to learn when a batch job
# STARTS and FINISHES (each message carries the job id, counts, and a logs URL).
resource "google_pubsub_topic" "events" {
  name = "pdf-anonymiser-events-${var.environment}"
}

# Event-driven completion: GCS fires an OBJECT_FINALIZE notification when any object
# under the control prefix is written, so a subscriber can react to per-document and
# whole-job completion without polling. Scoped to the _pii_runs/ prefix.
data "google_storage_project_service_account" "gcs" {}

# The GCS service agent must be allowed to publish to the topic for notifications.
resource "google_pubsub_topic_iam_member" "gcs_publisher" {
  topic  = google_pubsub_topic.events.id
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${data.google_storage_project_service_account.gcs.email_address}"
}

resource "google_storage_notification" "run_events" {
  bucket             = google_storage_bucket.data.name
  payload_format     = "JSON_API_V1"
  topic              = google_pubsub_topic.events.id
  event_types        = ["OBJECT_FINALIZE"]
  object_name_prefix = "_pii_runs/"
  depends_on         = [google_pubsub_topic_iam_member.gcs_publisher]
}
