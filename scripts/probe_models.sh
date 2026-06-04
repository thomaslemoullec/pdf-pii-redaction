#!/usr/bin/env bash
# Probe which Gemini models a project can actually call, per location — and recommend
# (or write) a working set of VISION_MODEL / PLANNER_MODEL / IMAGE_MODEL + gemini_location.
#
# Preview model IDs get renamed/retired and regional availability varies, so a config
# that works in one project 404s in another. This issues a tiny generateContent call
# (1 output token) for a curated list of model IDs against each location, prints what's
# reachable, and picks the best available per role (the lists below are in priority
# order, newest/most-capable first).
#
# Usage:
#   scripts/probe_models.sh [PROJECT] [LOCATION ...]      # probe + print a recommended block
# Env (used by `make models-write`):
#   WRITE_TFVARS=path     after probing, write the recommended models into this tfvars
#   WRITE_LOCATION=loc    which probed location to base the write on (default: first one)
set -euo pipefail

PROJECT="${1:-$(gcloud config get-value project 2>/dev/null)}"
shift || true
LOCATIONS=("$@"); [ ${#LOCATIONS[@]} -eq 0 ] && LOCATIONS=(global europe-west4)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[ -n "$PROJECT" ] || { echo "❌ no project — pass one or set 'gcloud config set project'." >&2; exit 1; }
TOKEN="$(gcloud auth print-access-token 2>/dev/null)" || { echo "❌ gcloud auth failed." >&2; exit 1; }

# Curated candidates by role, PRIORITY ORDER (best first). GA (non-preview) IDs only —
# we deliberately avoid -preview models (renamed/retired without notice) and old 2.x.
# Unknown IDs simply report unavailable, so listing a few plausible GA variants is safe.
PRO=(gemini-3-pro gemini-3.5-pro gemini-3.1-pro gemini-3.0-pro gemini-2.5-pro)
FLASH=(gemini-3.5-flash gemini-3-flash gemini-3.1-flash gemini-2.5-flash)
IMAGE=(gemini-3-pro-image gemini-3.1-flash-image gemini-3.5-flash-image gemini-3-flash-image gemini-2.5-flash-image)

BODY='{"contents":[{"role":"user","parts":[{"text":"hi"}]}],"generationConfig":{"maxOutputTokens":1}}'
declare -A AVAIL  # AVAIL["model@loc"] = 1 when reachable

STATUS=""  # set by probe (avoid command substitution, which would lose AVAIL in a subshell)
probe() { # model location -> sets global STATUS + AVAIL
  local m="$1" loc="$2" host code
  host="${loc}-aiplatform.googleapis.com"; [ "$loc" = "global" ] && host="aiplatform.googleapis.com"
  code=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -X POST "https://$host/v1/projects/$PROJECT/locations/$loc/publishers/google/models/$m:generateContent" \
    -d "$BODY")
  case "$code" in
    200|400) AVAIL["$m@$loc"]=1; STATUS="available" ;;
    404)     STATUS="—" ;;
    401|403) STATUS="denied($code)" ;;
    *)       STATUS="($code)" ;;
  esac
}

best() { # location role-array... -> first available model for that role at that loc
  local loc="$1"; shift
  local m
  for m in "$@"; do [ -n "${AVAIL[$m@$loc]:-}" ] && { echo "$m"; return; }; done
  echo ""
}

echo "project: $PROJECT"
for loc in "${LOCATIONS[@]}"; do
  echo ""
  echo "=== $loc ==="
  echo "  PRO (vision/judge/analyzer)"
  for m in "${PRO[@]}";   do probe "$m" "$loc"; printf "    %-30s %s\n" "$m" "$STATUS"; done
  echo "  FLASH (planner)"
  for m in "${FLASH[@]}"; do probe "$m" "$loc"; printf "    %-30s %s\n" "$m" "$STATUS"; done
  echo "  IMAGE (synthesis)"
  for m in "${IMAGE[@]}"; do probe "$m" "$loc"; printf "    %-30s %s\n" "$m" "$STATUS"; done
done

# Recommended tfvars block(s) — best available per role at each location.
for loc in "${LOCATIONS[@]}"; do
  pro="$(best "$loc" "${PRO[@]}")"; flash="$(best "$loc" "${FLASH[@]}")"; img="$(best "$loc" "${IMAGE[@]}")"
  echo ""
  if [ -n "$pro" ] && [ -n "$flash" ] && [ -n "$img" ]; then
    echo "✅ Recommended tfvars for $loc:"
    echo "     gemini_location = \"$loc\""
    echo "     vision_model    = \"$pro\""
    echo "     planner_model   = \"$flash\""
    echo "     image_model     = \"$img\""
  else
    echo "⚠️  $loc: no complete set available (pro=${pro:-none} flash=${flash:-none} image=${img:-none})"
  fi
done

# Optional: write the recommendation for WRITE_LOCATION into WRITE_TFVARS.
if [ -n "${WRITE_TFVARS:-}" ]; then
  loc="${WRITE_LOCATION:-${LOCATIONS[0]}}"
  pro="$(best "$loc" "${PRO[@]}")"; flash="$(best "$loc" "${FLASH[@]}")"; img="$(best "$loc" "${IMAGE[@]}")"
  echo ""
  if [ -z "$pro" ] || [ -z "$flash" ] || [ -z "$img" ]; then
    echo "❌ refusing to write: no complete model set available at $loc." >&2; exit 1
  fi
  echo "Writing the $loc recommendation into $WRITE_TFVARS:"
  python3 "$HERE/set_models.py" "$WRITE_TFVARS" "$loc" "$pro" "$flash" "$img"
fi
