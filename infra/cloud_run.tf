locals {
  job_name = "pdf-anonymiser-batch-${var.environment}"

  # Shared environment for both the UI service and the batch job — one config surface.
  app_env = {
    GCP_PROJECT        = var.project_id
    GCP_REGION         = var.region
    GEMINI_LOCATION    = var.gemini_location
    VISION_MODEL       = var.vision_model
    PLANNER_MODEL      = var.planner_model
    IMAGE_MODEL        = var.image_model
    PII_USE_DLP        = var.pii_use_dlp ? "1" : "0"
    PII_DLP_LEAK_CHECK = var.pii_dlp_leak_check ? "1" : "0"
    PII_MAX_PARALLEL   = tostring(var.pii_max_parallel)
    PII_CONTROL_URI    = "gs://${google_storage_bucket.data.name}/_pii_runs"
    PII_EVENTS_TOPIC   = google_pubsub_topic.events.name
    PII_BATCH_JOB_NAME = local.job_name
    # Deep link to the Logs & Metrics dashboard — surfaced in the UI and in the
    # Pub/Sub started/finished events so subscribers and reviewers can jump straight in.
    PII_DASHBOARD_URL = local.dashboard_url
  }
}

# --- The batch job: a Cloud Run Job fanned out by the UI (taskCount per run) ---
resource "google_cloud_run_v2_job" "batch" {
  name                = local.job_name
  location            = var.region
  deletion_protection = false

  template {
    parallelism = var.batch_parallelism
    task_count  = 1 # overridden per run (taskCount = document count, capped at 100)

    template {
      service_account = google_service_account.app.email
      max_retries     = var.batch_max_retries
      timeout         = var.batch_task_timeout

      containers {
        image = var.image
        # The shard entrypoint; the UI appends --job-id/--source/--output/... per run.
        command = ["pdf-anonymise", "batch"]

        resources {
          limits = {
            cpu    = "2"
            memory = "2Gi"
          }
        }

        dynamic "env" {
          for_each = local.app_env
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }
}

# --- The review UI: a continuously-running Cloud Run service ------------------
# Uses the google-beta provider because iap_enabled (direct IAP on Cloud Run) is only
# exposed there in provider v6.x.
resource "google_cloud_run_v2_service" "ui" {
  provider            = google-beta
  name                = "pdf-anonymiser-ui-${var.environment}"
  location            = var.region
  deletion_protection = false
  ingress             = "INGRESS_TRAFFIC_ALL"

  # When enable_iap=true, Identity-Aware Proxy gates this service directly (no load
  # balancer). Requests must carry a valid IAP-issued identity on the iap_members
  # allowlist. See infra/iap.tf and the README "Securing the UI with IAP" section.
  iap_enabled = var.enable_iap

  template {
    service_account = google_service_account.app.email

    scaling {
      # Keep ≥1 instance warm by default so reviewers never hit a cold start (and the
      # per-instance read cache stays hot). Set ui_min_instances=0 to trade warmth for cost.
      min_instance_count = var.ui_min_instances
      max_instance_count = 4
    }

    containers {
      image   = var.image
      command = ["pdf-anonymise", "serve"]

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "2Gi" # headroom for rendering PDF page previews under concurrent loads
        }
        startup_cpu_boost = true # faster cold start on the rare scale-from-zero
      }

      dynamic "env" {
        for_each = local.app_env
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }
}

# Access control for the UI. Default DENIES unauthenticated access (the UI renders
# source documents containing real PII). Set ui_allow_unauthenticated=true ONLY for a
# throwaway demo. For real use, set enable_iap=true (see infra/iap.tf) — do NOT set both.
resource "google_cloud_run_v2_service_iam_member" "ui_public" {
  count    = var.ui_allow_unauthenticated ? 1 : 0
  name     = google_cloud_run_v2_service.ui.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}
