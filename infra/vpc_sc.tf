# === VPC Service Controls perimeter (OPTIONAL, org-level) ====================
#
# Closes the biggest residual gap: data exfiltration of Storage / Vertex AI *even with valid
# stolen credentials* — something IAM alone can't stop. It is OFF by default and creates
# NOTHING unless enable_vpc_sc = true, because VPC-SC is an ORG-LEVEL resource that an org
# admin rolls out in stages. Full procedure: docs/vpc-sc-runbook.md.
#
# Dry-run by default (vpc_sc_enforced = false): the perimeter only LOGS would-be violations
# (use_explicit_dry_run_spec); nothing is blocked. Flip vpc_sc_enforced = true to enforce,
# only after the dry-run logs are clean and any ingress/egress rules are added.
#
# data.google_project.this is defined in iap.tf.

locals {
  # Services confined to the perimeter. Add/remove per your data path.
  vpc_sc_restricted_services = [
    "storage.googleapis.com",
    "aiplatform.googleapis.com",
    "pubsub.googleapis.com",
    "dlp.googleapis.com",
  ]
  vpc_sc_resources = ["projects/${data.google_project.this.number}"]
}

resource "google_access_context_manager_service_perimeter" "data" {
  count  = var.enable_vpc_sc ? 1 : 0
  parent = "accessPolicies/${var.access_policy_id}"
  name   = "accessPolicies/${var.access_policy_id}/servicePerimeters/pdf_anonymiser_${var.environment}"
  title  = "pdf-anonymiser ${var.environment}"

  # Enforced config. Only restricts services when vpc_sc_enforced = true; in dry-run it lists
  # the project but no restricted services, so it blocks nothing while the spec below logs.
  status {
    resources           = local.vpc_sc_resources
    restricted_services = var.vpc_sc_enforced ? local.vpc_sc_restricted_services : []
  }

  # Dry-run (proposed) config — evaluated in log-only mode. Present unless enforcing.
  dynamic "spec" {
    for_each = var.vpc_sc_enforced ? [] : [1]
    content {
      resources           = local.vpc_sc_resources
      restricted_services = local.vpc_sc_restricted_services

      # Example egress rule (commented): allow the SA to reach an approved external project's
      # GCS. Uncomment + adapt only if a real cross-perimeter need shows up in the dry-run logs.
      # egress_policies {
      #   egress_from { identities = ["serviceAccount:${google_service_account.app.email}"] }
      #   egress_to {
      #     resources = ["projects/EXTERNAL_PROJECT_NUMBER"]
      #     operations { service_name = "storage.googleapis.com"
      #       method_selectors { method = "*" } }
      #   }
      # }
    }
  }

  use_explicit_dry_run_spec = !var.vpc_sc_enforced

  lifecycle {
    precondition {
      condition     = var.access_policy_id != ""
      error_message = "access_policy_id (the org ACM access-policy number) is required when enable_vpc_sc = true."
    }
  }
}
