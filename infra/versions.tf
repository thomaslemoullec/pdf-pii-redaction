terraform {
  required_version = ">= 1.6"

  # Remote state in GCS (locking + shared, team-operable). The bucket is created by
  # `make tf-backend`; `make` passes bucket/prefix via -backend-config at init, so this
  # block stays empty (partial backend config). For an existing local state, migrate once:
  #   terraform -chdir=infra init -migrate-state \
  #     -backend-config="bucket=<PROJECT>-tfstate" -backend-config="prefix=pdf-anonymiser/<env>"
  backend "gcs" {}

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    # Used only for google_project_service_identity (the IAP service agent). Same version line.
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}
