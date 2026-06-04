# The single data bucket. It holds three prefixes:
#   <source>/...                 the input PDFs you point a job at
#   _pii_runs/<job>/...          the GCS-only control plane (job.json, results, index)
#   <output>/{unvalidated,validated}/<doc>/...   the PII-free output
#
# Locked down for documents that contain real PII: uniform IAM, public access
# prevention enforced, optional CMEK + retention.
resource "google_storage_bucket" "data" {
  name                        = "${var.project_id}-pdf-anonymiser-${var.environment}"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  # Tidy up stale noncurrent versions so the bucket doesn't grow unbounded.
  lifecycle_rule {
    condition {
      num_newer_versions = 3
    }
    action {
      type = "Delete"
    }
  }

  dynamic "encryption" {
    for_each = var.kms_key == "" ? [] : [var.kms_key]
    content {
      default_kms_key_name = encryption.value
    }
  }

  dynamic "retention_policy" {
    for_each = var.bucket_retention_days == 0 ? [] : [var.bucket_retention_days]
    content {
      retention_period = retention_policy.value * 24 * 3600
    }
  }
}
