# === Cloud Audit Logs — Data Access for the PII data path ====================
#
# Admin Activity logs are always on. *Data Access* logs (DATA_READ / DATA_WRITE) are OFF by
# default in GCP and are the ones that give you a "who read/wrote which PII object" trail —
# the audit evidence you want for a bucket holding real PII. Scoped to the two services that
# actually touch PII: Cloud Storage (the data bucket) and Vertex AI (raw page content at
# scan/judge time). Toggle with var.enable_data_access_audit_logs.
#
# COST: Data Access logs are billed by volume; every object read (each UI page view, each
# source read) is logged. That's usually the point for PII, but set the flag false to opt out.
# These resources are authoritative for the named service — don't also manage its audit
# config elsewhere.

resource "google_project_iam_audit_config" "storage" {
  count   = var.enable_data_access_audit_logs ? 1 : 0
  project = var.project_id
  service = "storage.googleapis.com"

  audit_log_config {
    log_type = "ADMIN_READ"
  }
  audit_log_config {
    log_type = "DATA_READ"
  }
  audit_log_config {
    log_type = "DATA_WRITE"
  }
}

resource "google_project_iam_audit_config" "aiplatform" {
  count   = var.enable_data_access_audit_logs ? 1 : 0
  project = var.project_id
  service = "aiplatform.googleapis.com"

  audit_log_config {
    log_type = "DATA_READ"
  }
  audit_log_config {
    log_type = "DATA_WRITE"
  }
}
