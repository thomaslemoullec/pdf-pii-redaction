# === Observability: log-based metrics + a per-job Logs & Metrics dashboard =====
#
# The app emits one structured log per document and per job-lifecycle step (obs.py,
# event="pii.document" / "pii.job"). These log-based metrics extract the numbers from
# those logs — so the app never calls a metrics API — and the dashboard charts them
# alongside a live logs panel. The dashboard URL is injected into the Cloud Run env
# (PII_DASHBOARD_URL) so it appears in the UI and in the Pub/Sub started/finished events.

resource "google_project_service" "monitoring" {
  project            = var.project_id
  service            = "monitoring.googleapis.com"
  disable_on_destroy = false
}

# Documents processed, labelled by verdict (pass / review / fail / error) → throughput,
# failure rate and leak (fail) rate over time.
resource "google_logging_metric" "documents" {
  project     = var.project_id
  name        = "pdf_anonymiser/documents"
  description = "PDF anonymiser: documents processed, by verdict."
  filter      = "jsonPayload.event=\"pii.document\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    labels {
      key        = "verdict"
      value_type = "STRING"
    }
  }
  label_extractors = { "verdict" = "EXTRACT(jsonPayload.verdict)" }
}

# Per-document processing time → latency p50/p95.
resource "google_logging_metric" "document_seconds" {
  project         = var.project_id
  name            = "pdf_anonymiser/document_seconds"
  description     = "PDF anonymiser: per-document processing seconds."
  filter          = "jsonPayload.event=\"pii.document\" jsonPayload.processing_seconds>0"
  value_extractor = "EXTRACT(jsonPayload.processing_seconds)"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "s"
  }
  bucket_options {
    exponential_buckets {
      num_finite_buckets = 16
      growth_factor      = 2
      scale              = 0.5
    }
  }
}

# Anonymise→judge attempts per document → how hard the retry loop is working.
resource "google_logging_metric" "attempts" {
  project         = var.project_id
  name            = "pdf_anonymiser/attempts"
  description     = "PDF anonymiser: anonymise/judge attempts per document."
  filter          = "jsonPayload.event=\"pii.document\" jsonPayload.attempts>0"
  value_extractor = "EXTRACT(jsonPayload.attempts)"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
  }
  bucket_options {
    linear_buckets {
      num_finite_buckets = 8
      width              = 1
      offset             = 0
    }
  }
}

locals {
  _m   = "logging.googleapis.com/user"
  dash = "PDF Anonymiser — ${var.environment}"
}

resource "google_monitoring_dashboard" "jobs" {
  project    = var.project_id
  depends_on = [google_project_service.monitoring]
  dashboard_json = jsonencode({
    displayName = local.dash
    mosaicLayout = { columns = 12, tiles = [
      {
        width = 6, height = 4, xPos = 0, yPos = 0
        widget = {
          title = "Documents by verdict (rate)"
          xyChart = { dataSets = [{
            timeSeriesQuery = { timeSeriesFilter = {
              filter      = "metric.type=\"${local._m}/pdf_anonymiser/documents\""
              aggregation = { alignmentPeriod = "300s", perSeriesAligner = "ALIGN_DELTA", crossSeriesReducer = "REDUCE_SUM", groupByFields = ["metric.label.\"verdict\""] }
            } }
            plotType = "STACKED_BAR"
          }] }
        }
      },
      {
        width = 6, height = 4, xPos = 6, yPos = 0
        widget = {
          title = "Document latency (p50 / p95)"
          xyChart = { dataSets = [
            { timeSeriesQuery = { timeSeriesFilter = {
              filter      = "metric.type=\"${local._m}/pdf_anonymiser/document_seconds\""
              aggregation = { alignmentPeriod = "300s", perSeriesAligner = "ALIGN_PERCENTILE_50" }
            } }, plotType = "LINE", legendTemplate = "p50" },
            { timeSeriesQuery = { timeSeriesFilter = {
              filter      = "metric.type=\"${local._m}/pdf_anonymiser/document_seconds\""
              aggregation = { alignmentPeriod = "300s", perSeriesAligner = "ALIGN_PERCENTILE_95" }
            } }, plotType = "LINE", legendTemplate = "p95" },
          ] }
        }
      },
      {
        width = 6, height = 4, xPos = 0, yPos = 4
        widget = {
          title = "Attempts per document (p50 / p95)"
          xyChart = { dataSets = [
            { timeSeriesQuery = { timeSeriesFilter = {
              filter      = "metric.type=\"${local._m}/pdf_anonymiser/attempts\""
              aggregation = { alignmentPeriod = "300s", perSeriesAligner = "ALIGN_PERCENTILE_50" }
            } }, plotType = "LINE", legendTemplate = "p50" },
            { timeSeriesQuery = { timeSeriesFilter = {
              filter      = "metric.type=\"${local._m}/pdf_anonymiser/attempts\""
              aggregation = { alignmentPeriod = "300s", perSeriesAligner = "ALIGN_PERCENTILE_95" }
            } }, plotType = "LINE", legendTemplate = "p95" },
          ] }
        }
      },
      {
        width = 6, height = 4, xPos = 6, yPos = 4
        widget = {
          title = "Job logs (structured)"
          logsPanel = {
            filter        = "jsonPayload.event=(\"pii.document\" OR \"pii.job\")"
            resourceNames = ["projects/${var.project_id}"]
          }
        }
      },
    ] }
  })
}

locals {
  dashboard_id  = regex("[^/]+$", google_monitoring_dashboard.jobs.id)
  dashboard_url = "https://console.cloud.google.com/monitoring/dashboards/builder/${local.dashboard_id}?project=${var.project_id}"
}
