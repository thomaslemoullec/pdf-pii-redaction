# Securing the UI with IAP

The review UI displays **source documents that contain real PII**, so it must never be
open. The deployment puts **Identity-Aware Proxy directly on the Cloud Run service** — no
load balancer, static IP, or certificate. Users open the normal `run.app` URL, sign in
with their Google account, and pass only if they're on your allowlist.

## Turn it on

In `infra/environments/<env>.tfvars`:

```hcl
enable_iap               = true
ui_allow_unauthenticated = false        # required: IAP and public access are mutually exclusive
iap_members = [
  "group:pdf-reviewers@yourdomain.com", # recommended — manage people in the group
  "user:alice@yourdomain.com",
]
```

`make deploy`, then `terraform -chdir=infra output ui_access` ⇒ `iap`.

## Connect

Open the UI URL in a browser — IAP shows a Google sign-in; allowlisted users get in. For
scripts: `curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$URL"`.

## Update who's allowed

Best practice is a single `group:` — add/remove people in the group, no Terraform change.
To change the list directly, edit `iap_members` and `make deploy`. Removing a member
revokes access immediately.

## Whose accounts work

Any Google / Cloud Identity account — **including Azure AD / Entra** users federated into
Cloud Identity via SAML SSO (ordinary `user:`/`group:` members). Pure Workforce Identity
Federation users use a `principalSet://…/workforcePools/POOL_ID/group/GROUP_ID` member.

> Demo without IAP: set `ui_allow_unauthenticated=true` **and** `enable_iap=false` for a
> throwaway, world-readable UI. Never with real documents.
