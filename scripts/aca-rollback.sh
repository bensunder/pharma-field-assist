#!/usr/bin/env bash
# Instant rollback: shift 100% traffic to the revision labelled "blue" (previous good) or an explicit revision.
# No rebuild, no redeploy — old revisions stay provisioned (maxInactiveRevisions=5).
# Usage: aca-rollback.sh <resource-group> <app-name> [revision-name]
set -euo pipefail
RG="$1"; APP="$2"; TARGET="${3:-}"
if [[ -z "$TARGET" ]]; then
  TARGET="$(az containerapp ingress traffic show -g "$RG" -n "$APP" --query "[?label=='blue'].revisionName | [0]" -o tsv)"
fi
[[ -n "$TARGET" ]] || { echo "no rollback target (no 'blue' label and no revision given)"; exit 1; }
az containerapp revision activate -g "$RG" -n "$APP" --revision "$TARGET" -o none 2>/dev/null || true
az containerapp ingress traffic set -g "$RG" -n "$APP" --revision-weight "$TARGET=100" -o none
echo "rolled back $APP to $TARGET"
az containerapp ingress traffic show -g "$RG" -n "$APP" -o table
