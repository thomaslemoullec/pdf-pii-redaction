# VPC Service Controls — rollout runbook (pdf-anonymiser)

VPC-SC puts a perimeter around this project so Storage / Vertex AI / Pub/Sub / DLP can't be
reached from outside it — blocking data exfiltration **even with valid stolen credentials**,
which IAM alone can't do. It is **org-level** and **off by default** in this repo; an **org
admin** applies it in stages. Nothing is created unless `enable_vpc_sc = true`.

Terraform: `infra/vpc_sc.tf` (perimeter) + vars `enable_vpc_sc`, `access_policy_id`,
`vpc_sc_enforced` in `infra/variables.tf`.

## Why it isn't applied automatically
- It's an Access Context Manager resource on the **org node** and spans projects — broader
  than this project's scope.
- Needs `roles/accesscontextmanager.policyAdmin` at the org (an org admin holds the authority
  but may need to grant themselves this specific role).
- An **enforced** perimeter applied cold can instantly cut off the app, CI, and your console
  from Storage/Vertex. Hence: always dry-run first.

## Prerequisites (org admin, once)
1. Grant yourself the ACM admin role at the org:
   ```bash
   gcloud organizations add-iam-policy-binding 195620200421 \
     --member="user:admin@thomaslmc.altostrat.com" \
     --role="roles/accesscontextmanager.policyAdmin"
   ```
2. Enable the API, billed to THIS project (not the ADC default project):
   ```bash
   gcloud services enable accesscontextmanager.googleapis.com \
     --project=pii-documents --billing-project=pii-documents
   ```
3. Find or create the org access policy and note its **number**:
   ```bash
   gcloud access-context-manager policies list --organization=195620200421 \
     --billing-project=pii-documents
   # none yet? create one (scoped to the org):
   gcloud access-context-manager policies create \
     --organization=195620200421 --title="org-policy" --billing-project=pii-documents
   ```

## Step 1 — apply in DRY-RUN (logs only, blocks nothing)
In `infra/environments/dev.tfvars`:
```hcl
enable_vpc_sc    = true
access_policy_id = "POLICY_NUMBER"   # from prereq 3
vpc_sc_enforced  = false             # dry-run
```
```bash
make plan ENV=dev      # review: one google_access_context_manager_service_perimeter, with a `spec`
make deploy ENV=dev
```

## Step 2 — exercise the system, then read the dry-run violations
Run a normal batch + review in the UI (so every real access path is exercised), then:
```bash
gcloud logging read \
  'protoPayload.metadata.@type="type.googleapis.com/google.cloud.audit.VpcServiceControlAuditMetadata" AND severity>=ERROR' \
  --project=pii-documents --freshness=2h --limit=50
```
Each line is something the perimeter *would* have blocked if enforced. Expected: intra-project
calls (Cloud Run → Vertex/Storage in the same project) do **not** appear. Investigate anything
that does — that's a real access path needing an ingress/egress rule.

## Step 3 — add ingress/egress rules for legitimate cross-perimeter access
Common cases: your console/CI reaching Storage from outside, or the SA reaching an external
bucket. Add `ingress_policies` / `egress_policies` to the `spec` block (example egress is
stubbed in `vpc_sc.tf`), re-apply in dry-run, and confirm the violations clear.

## Step 4 — enforce
Only once dry-run is clean:
```hcl
vpc_sc_enforced = true
```
```bash
make plan ENV=dev      # the config moves from `spec` (dry-run) into `status` (enforced)
make deploy ENV=dev
```
Verify the app still runs (launch a job, open the UI). If something breaks, set
`vpc_sc_enforced = false` and re-apply to revert to log-only immediately.

## Rollback
- Disable enforcement: `vpc_sc_enforced = false` → `make deploy` (back to dry-run, nothing blocked).
- Remove entirely: `enable_vpc_sc = false` → `make deploy` (perimeter destroyed).

## Notes
- Add a second project to the same perimeter by extending `local.vpc_sc_resources` in
  `vpc_sc.tf` (e.g. a shared logging project) — that's a deliberate org-level decision.
- Pair with `enable_data_access_audit_logs = true` (see `infra/audit.tf`) so you have the
  object-level access trail alongside the perimeter.
