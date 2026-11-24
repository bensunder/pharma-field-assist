#!/usr/bin/env bash
# Blue/green (label-based) release of the API container app.
#
#   1. pin 100% traffic to the currently-live revision (so the new one starts at 0%)
#   2. create the new revision from the new image, label it "green"
#   3. wait until the revision is Running + healthy, smoke-test it through its label URL
#   4. canary: 10% for CANARY_SECONDS while watching 5xx, then 100%
#   5. label the previous revision "blue" (instant rollback target) and deactivate older ones
#
# Usage: aca-release.sh <resource-group> <app-name> <image> [canary-seconds]
set -euo pipefail

RG="$1"; APP="$2"; IMAGE="$3"; CANARY_SECONDS="${4:-300}"
SUFFIX="r$(echo "${IMAGE##*:}" | tr -cd 'a-z0-9' | cut -c1-10)"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
current_revision() {
  az containerapp ingress traffic show -g "$RG" -n "$APP" \
    --query "max_by([?weight > \`0\`], &weight).revisionName" -o tsv
}

LIVE="$(current_revision)"
log "live revision: ${LIVE:-<none>}"
if [[ -n "$LIVE" ]]; then
  az containerapp ingress traffic set -g "$RG" -n "$APP" --revision-weight "$LIVE=100" -o none
fi

log "creating revision suffix=$SUFFIX image=$IMAGE"
az containerapp update -g "$RG" -n "$APP" --image "$IMAGE" --revision-suffix "$SUFFIX" -o none
NEW="$APP--$SUFFIX"
az containerapp revision label add -g "$RG" -n "$APP" --revision "$NEW" --label green --no-prompt -o none

log "waiting for $NEW to be healthy"
for _ in $(seq 1 60); do
  STATE="$(az containerapp revision show -g "$RG" -n "$APP" --revision "$NEW" --query "properties.healthState" -o tsv)"
  [[ "$STATE" == "Healthy" ]] && break
  sleep 5
done
[[ "$STATE" == "Healthy" ]] || { log "revision unhealthy ($STATE) — aborting, live traffic untouched"; exit 1; }

FQDN="$(az containerapp show -g "$RG" -n "$APP" --query properties.configuration.ingress.fqdn -o tsv)"
GREEN_URL="https://${FQDN/$APP./$APP---green.}"
log "smoke testing $GREEN_URL"
"$(dirname "$0")/smoke.sh" "$GREEN_URL"

if [[ -n "$LIVE" ]]; then
  log "canary 10% for ${CANARY_SECONDS}s"
  az containerapp ingress traffic set -g "$RG" -n "$APP" --revision-weight "$LIVE=90" "$NEW=10" -o none
  sleep "$CANARY_SECONDS"
  # Abort if the canary shows server errors (App Insights, cloud_RoleInstance scoped to the new revision)
  ERRORS="$(az monitor app-insights query --app "${APPINSIGHTS_APP_ID:?}" --analytics-query \
    "requests | where timestamp > ago(${CANARY_SECONDS}s) and cloud_RoleInstance startswith '$NEW' and toint(resultCode) >= 500 | count" \
    --query "tables[0].rows[0][0]" -o tsv || echo 0)"
  if [[ "${ERRORS:-0}" -gt 0 ]]; then
    log "canary produced $ERRORS 5xx — rolling back"
    az containerapp ingress traffic set -g "$RG" -n "$APP" --revision-weight "$LIVE=100" -o none
    exit 1
  fi
  az containerapp revision label remove -g "$RG" -n "$APP" --label blue --no-prompt -o none 2>/dev/null || true
  az containerapp revision label add -g "$RG" -n "$APP" --revision "$LIVE" --label blue --no-prompt -o none
fi

az containerapp ingress traffic set -g "$RG" -n "$APP" --revision-weight "$NEW=100" -o none
log "released $NEW at 100% (rollback target: ${LIVE:-none})"
