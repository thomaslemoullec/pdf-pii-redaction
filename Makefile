# PDF Anonymiser — dev + deploy tasks.
.PHONY: help install test lint fmt samples serve \
        setup check-vars enable-apis tf-backend repo build-perms image models models-write seed plan deploy destroy clean

ENV     ?= dev
REPO    ?= pdf-anonymiser
PY      ?= .venv/bin/python
# Which Gemini location `make models-write` targets (global | europe-west4).
LOC     ?= global

# Single source of truth: project + region are read from the tfvars you fill in, so
# `make setup` needs no extra flags. Falls back to your active gcloud project if the
# tfvars hasn't been created yet (e.g. for a plain `make image`).
TFVARS  := infra/environments/$(ENV).tfvars
PROJECT := $(or $(shell sed -nE 's/^[[:space:]]*project_id[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' $(TFVARS) 2>/dev/null),$(shell gcloud config get-value project 2>/dev/null))
REGION  := $(or $(shell sed -nE 's/^[[:space:]]*region[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' $(TFVARS) 2>/dev/null),europe-west3)
IMAGE   := $(REGION)-docker.pkg.dev/$(PROJECT)/$(REPO)/app:latest

# Remote Terraform state (GCS backend). The bucket is created by `make tf-backend`;
# bucket + prefix are passed to `terraform init` as partial backend config.
STATE_BUCKET ?= $(PROJECT)-tfstate
STATE_PREFIX ?= pdf-anonymiser/$(ENV)
TF_BACKEND   := -backend-config="bucket=$(STATE_BUCKET)" -backend-config="prefix=$(STATE_PREFIX)"

# APIs a fresh project needs for the whole stack.
APIS := run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
        storage.googleapis.com aiplatform.googleapis.com pubsub.googleapis.com \
        dlp.googleapis.com iam.googleapis.com cloudresourcemanager.googleapis.com \
        logging.googleapis.com monitoring.googleapis.com

help:  ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-13s\033[0m %s\n", $$1, $$2}'

# === one-command setup for a new GCP project =================================
# Fill in infra/environments/$(ENV).tfvars (copy the .example), authenticate once
# (`gcloud auth login` + `gcloud auth application-default login`), then `make setup`.
setup: check-vars enable-apis tf-backend repo build-perms image deploy  ## One command: APIs → state → repo → image → deploy
	@B=$$(cd infra && terraform output -raw bucket 2>/dev/null); \
	echo ""; \
	echo "✅ Done."; \
	echo "   Review UI : $$(cd infra && terraform output -raw ui_url 2>/dev/null)"; \
	echo "   Bucket    : $$B"; \
	echo ""; \
	echo "   Try it now with the bundled samples:"; \
	echo "     make seed        # single-page → incoming-single-page/, multi-page → incoming-multi-page/"; \
	echo ""; \
	echo "   Then launch a job in the UI with the Source folder set to:"; \
	echo "     gs://$$B/incoming-single-page/   or   gs://$$B/incoming-multi-page/"; \
	echo "     Output location : gs://$$B/anonymised"

check-vars:  ## Verify the tfvars exists and project/region are set
	@test -f $(TFVARS) || { echo "❌ $(TFVARS) not found — copy $(TFVARS).example and fill it in."; exit 1; }
	@test -n "$(PROJECT)" || { echo "❌ project_id not set in $(TFVARS)."; exit 1; }
	@command -v gcloud >/dev/null || { echo "❌ gcloud not installed."; exit 1; }
	@command -v terraform >/dev/null || { echo "❌ terraform not installed."; exit 1; }
	@echo "→ project=$(PROJECT)  region=$(REGION)  env=$(ENV)"
	@echo "→ image=$(IMAGE)"

enable-apis:  ## Enable the required GCP APIs on the project
	gcloud services enable $(APIS) --project=$(PROJECT)

tf-backend:  ## Create the GCS bucket holding Terraform remote state (idempotent)
	@if gcloud storage buckets describe gs://$(STATE_BUCKET) --project=$(PROJECT) >/dev/null 2>&1; then \
		echo "→ Terraform state bucket gs://$(STATE_BUCKET) already exists"; \
	else \
		echo "→ creating Terraform state bucket gs://$(STATE_BUCKET)"; \
		gcloud storage buckets create gs://$(STATE_BUCKET) --project=$(PROJECT) \
			--location=$(REGION) --uniform-bucket-level-access --public-access-prevention; \
		gcloud storage buckets update gs://$(STATE_BUCKET) --versioning; \
	fi

repo:  ## Create the Artifact Registry docker repo (idempotent)
	@gcloud artifacts repositories describe $(REPO) --location=$(REGION) --project=$(PROJECT) >/dev/null 2>&1 \
		|| gcloud artifacts repositories create $(REPO) --repository-format=docker \
			--location=$(REGION) --project=$(PROJECT)

build-perms:  ## Grant Cloud Build's runtime SA the builder role (idempotent)
	@PN=$$(gcloud projects describe $(PROJECT) --format='value(projectNumber)'); \
	SA="$$PN-compute@developer.gserviceaccount.com"; \
	echo "→ granting roles/cloudbuild.builds.builder to $$SA"; \
	gcloud projects add-iam-policy-binding $(PROJECT) \
		--member="serviceAccount:$$SA" \
		--role="roles/cloudbuild.builds.builder" --condition=None >/dev/null
	@echo "  (IAM may take ~30-60s to propagate before the build can read its source)"

image:  ## Build + push a uniquely-tagged image (+ :latest) so a deploy rolls new code
	@T=$$(date +%Y%m%d-%H%M%S); \
	IMG=$(REGION)-docker.pkg.dev/$(PROJECT)/$(REPO)/app; \
	echo "→ building $$IMG:$$T"; \
	gcloud builds submit --project=$(PROJECT) --tag=$$IMG:$$T .; \
	gcloud artifacts docker tags add $$IMG:$$T $$IMG:latest >/dev/null 2>&1 || true; \
	echo "$$T" > infra/.image-tag; \
	echo "→ recorded tag $$T (make deploy will roll it into a new revision)"

models:  ## Probe which Gemini models this project can call + print a recommended tfvars block
	@bash scripts/probe_models.sh $(PROJECT) global europe-west4

models-write:  ## Probe + write the best available models into your tfvars (LOC=global|europe-west4)
	@WRITE_TFVARS=$(TFVARS) WRITE_LOCATION=$(LOC) bash scripts/probe_models.sh $(PROJECT) $(LOC)
	@echo "→ review $(TFVARS), then: make deploy"

seed:  ## Upload the bundled samples (single- + multi-page) to the bucket for a quick try
	@B=$$(cd infra && terraform output -raw bucket 2>/dev/null); \
	test -n "$$B" || { echo "❌ no bucket output — run 'make setup' first."; exit 1; }; \
	gsutil -m cp samples/*.pdf gs://$$B/incoming-single-page/; \
	gsutil -m cp samples/multipage/*.pdf gs://$$B/incoming-multi-page/; \
	echo ""; \
	echo "✅ Uploaded $$(ls samples/*.pdf | wc -l) single-page → gs://$$B/incoming-single-page/"; \
	echo "   Uploaded $$(ls samples/multipage/*.pdf | wc -l) multi-page  → gs://$$B/incoming-multi-page/"; \
	echo "   Launch a job in the UI with one of these as the Source folder:"; \
	echo "     gs://$$B/incoming-single-page/   (single-page synthetic docs)"; \
	echo "     gs://$$B/incoming-multi-page/    (multi-page packages)"; \
	echo "   Output location: gs://$$B/anonymised"

IMG_BASE := $(REGION)-docker.pkg.dev/$(PROJECT)/$(REPO)/app
# The image to deploy = the last one `make image` built (recorded in infra/.image-tag),
# falling back to :latest. Using the recorded immutable tag is what makes terraform see a
# change and roll a NEW revision — a re-pushed :latest never would.
DEPLOY_IMAGE = $(IMG_BASE):$(shell cat infra/.image-tag 2>/dev/null || echo latest)

plan:  ## terraform plan (against the last-built image)
	cd infra && terraform init -input=false $(TF_BACKEND) && \
		terraform plan -var-file=environments/$(ENV).tfvars -var=image=$(DEPLOY_IMAGE)

deploy:  ## terraform apply with the last-built image → rolls a NEW revision (run `make image` first)
	@echo "→ deploying $(DEPLOY_IMAGE)"
	cd infra && terraform init -input=false $(TF_BACKEND) && \
		terraform apply -var-file=environments/$(ENV).tfvars -var=image=$(DEPLOY_IMAGE)

destroy:  ## Tear everything down (terraform destroy)
	cd infra && terraform init -input=false $(TF_BACKEND) && \
		terraform destroy -var-file=environments/$(ENV).tfvars -var=image=$(IMAGE)

# === local dev ================================================================
install:  ## Create a venv and install the package with dev + gcp extras
	python3 -m venv .venv
	$(PY) -m pip install -U pip
	$(PY) -m pip install -e ".[dev,gcp]"

test:  ## Run the unit suite with coverage (gate: 85%)
	$(PY) -m pytest

lint:  ## Ruff lint
	$(PY) -m ruff check src tests scripts

fmt:  ## Ruff autoformat + import-sort
	$(PY) -m ruff check --fix src tests scripts

samples:  ## (Re)generate the dozen synthetic sample PDFs into ./samples
	$(PY) scripts/generate_samples.py

serve:  ## Run the review UI locally on :8080
	$(PY) -m pdf_anonymiser.webapp

clean:  ## Remove caches + build artifacts
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov dist build src/*.egg-info
