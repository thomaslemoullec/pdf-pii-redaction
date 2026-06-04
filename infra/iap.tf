# === Identity-Aware Proxy in front of the review UI ==========================
#
# The UI renders source documents that contain real PII, so it must never be open.
# When enable_iap=true this puts Identity-Aware Proxy directly on the Cloud Run
# service (no load balancer, no static IP, no managed cert): users hit the run.app
# URL, sign in with their Google / Cloud Identity account (this includes accounts
# federated from Azure AD / Entra via SAML SSO), and are allowed through only if they
# are on the iap_members allowlist below.
#
# Off by default so a bare `make setup` still works; flip enable_iap=true in your
# tfvars once you've listed the reviewers. IAP and public access are mutually
# exclusive — keep ui_allow_unauthenticated=false whenever IAP is on (enforced by the
# precondition on the invoker binding).

variable "enable_iap" {
  type        = bool
  description = "Gate the review UI behind Identity-Aware Proxy (direct on Cloud Run, no load balancer)."
  default     = false
}

variable "iap_members" {
  type        = list(string)
  description = <<-EOT
    Principals allowed through IAP to the UI (granted roles/iap.httpsResourceAccessor).
    IAM member syntax — the common cases:
      "user:alice@yourdomain.com"
      "group:pdf-reviewers@yourdomain.com"      # recommended: manage people in the group
    Azure-AD users that have Cloud Identity accounts (SAML SSO) use the same user:/group:
    forms. Pure Workforce Identity Federation users (no Cloud Identity account) use:
      "principalSet://iam.googleapis.com/locations/global/workforcePools/POOL_ID/group/GROUP_ID"
  EOT
  default     = []
}

data "google_project" "this" {
  project_id = var.project_id
}

# IAP API on the project (only when IAP is enabled).
resource "google_project_service" "iap" {
  count              = var.enable_iap ? 1 : 0
  project            = var.project_id
  service            = "iap.googleapis.com"
  disable_on_destroy = false
}

# Force-create IAP's service agent so the invoker binding below can reference it.
resource "google_project_service_identity" "iap" {
  provider   = google-beta
  count      = var.enable_iap ? 1 : 0
  project    = var.project_id
  service    = "iap.googleapis.com"
  depends_on = [google_project_service.iap]
}

# IAP fronts the request and then invokes Cloud Run as its own service agent, so that
# agent needs run.invoker on the UI service.
resource "google_cloud_run_v2_service_iam_member" "iap_invoker" {
  count    = var.enable_iap ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.ui.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_project_service_identity.iap[0].email}"

  lifecycle {
    precondition {
      condition     = !var.ui_allow_unauthenticated
      error_message = "ui_allow_unauthenticated must be false when enable_iap is true — IAP and public (allUsers) access are mutually exclusive."
    }
  }
}

# The allowlist: who may pass through IAP to reach the UI. Edit var.iap_members and
# re-apply to add/remove people (or, better, manage a Google group and list it once).
resource "google_iap_web_cloud_run_service_iam_member" "ui_users" {
  for_each               = var.enable_iap ? toset(var.iap_members) : toset([])
  project                = var.project_id
  location               = var.region
  cloud_run_service_name = google_cloud_run_v2_service.ui.name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = each.value
  depends_on             = [google_project_service.iap]
}
