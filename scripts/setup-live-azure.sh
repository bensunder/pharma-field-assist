#!/usr/bin/env bash
# All-in-one setup for LIVE integrations. Idempotent — safe to re-run.
#
#   1. Entra app registration "PharmaFieldAssist-Integration" + service principal + client secret
#   2. Microsoft Graph application permission Calendars.ReadWrite, admin-consented
#   3. Verifies the demo mailbox exists
#   4. (optional) Fabric: grants the service principal Viewer on the workspace and looks up the
#      Lakehouse SQL analytics endpoint
#   5. Writes backend/.env with every PFA_* setting (previous file backed up)
#
# Prereqs: Azure CLI logged in to the tenant that owns the mailbox, as a Global Admin or
# Privileged Role Admin (needed for admin consent):   az login --tenant <tenant> --allow-no-subscriptions
#
# Usage:
#   MAILBOX=jordan@contoso.onmicrosoft.com \
#   FABRIC_WORKSPACE_ID=<guid> FABRIC_LAKEHOUSE=pfa_lakehouse \        # optional
#   SF_LOGIN_URL=https://<mydomain>.my.salesforce.com SF_CLIENT_ID=... SF_CLIENT_SECRET=... \   # optional
#   scripts/setup-live-azure.sh
set -euo pipefail

: "${MAILBOX:?Set MAILBOX to the M365 mailbox the demo rep books into (UPN)}"
APP_NAME="${APP_NAME:-PharmaFieldAssist-Integration}"
GRAPH_APP_ID="00000003-0000-0000-c000-000000000000"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT/backend/.env"

log() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }

command -v az >/dev/null || { echo "Azure CLI (az) is required"; exit 1; }
TENANT_ID="$(az account show --query tenantId -o tsv)"
log "Tenant: $TENANT_ID ($(az account show --query user.name -o tsv))"

# ---------------------------------------------------------------- 1. app registration
log "App registration: $APP_NAME"
APP_ID="$(az ad app list --display-name "$APP_NAME" --query "[0].appId" -o tsv)"
if [[ -z "$APP_ID" ]]; then
  APP_ID="$(az ad app create --display-name "$APP_NAME" --sign-in-audience AzureADMyOrg --query appId -o tsv)"
  echo "created appId=$APP_ID"
else
  echo "exists  appId=$APP_ID"
fi
SP_OID="$(az ad sp show --id "$APP_ID" --query id -o tsv 2>/dev/null || true)"
if [[ -z "$SP_OID" ]]; then
  SP_OID="$(az ad sp create --id "$APP_ID" --query id -o tsv)"
  sleep 15 # let the new service principal replicate before granting roles
fi
echo "service principal objectId=$SP_OID"

# ---------------------------------------------------------------- 2. Graph permission + consent
log "Graph application permission Calendars.ReadWrite (admin consent)"
GRAPH_SP_OID="$(az ad sp show --id "$GRAPH_APP_ID" --query id -o tsv)"
ROLE_ID="$(az ad sp show --id "$GRAPH_APP_ID" --query "appRoles[?value=='Calendars.ReadWrite'].id | [0]" -o tsv)"
az ad app permission add --id "$APP_ID" --api "$GRAPH_APP_ID" --api-permissions "${ROLE_ID}=Role" -o none 2>/dev/null || true
EXISTING="$(az rest --method get \
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_OID/appRoleAssignments" \
  --query "value[?appRoleId=='$ROLE_ID'] | length(@)" -o tsv)"
if [[ "$EXISTING" == "0" ]]; then
  # Granting the app role assignment directly == admin consent for an application permission.
  az rest --method post \
    --url "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_OID/appRoleAssignments" \
    --headers "Content-Type=application/json" \
    --body "{\"principalId\":\"$SP_OID\",\"resourceId\":\"$GRAPH_SP_OID\",\"appRoleId\":\"$ROLE_ID\"}" -o none
  echo "consent granted"
else
  echo "consent already present"
fi

# ---------------------------------------------------------------- client secret
log "Client secret (1 year)"
SECRET="$(az ad app credential reset --id "$APP_ID" --display-name "pfa-demo-$(date +%Y%m%d)" --years 1 --append --query password -o tsv)"
echo "secret created (written to backend/.env only)"

# ---------------------------------------------------------------- 3. mailbox check
log "Checking mailbox $MAILBOX"
if az rest --method get --url "https://graph.microsoft.com/v1.0/users/$MAILBOX?\$select=userPrincipalName,mail,assignedLicenses" \
     --query "{upn:userPrincipalName, mail:mail, licenses:length(assignedLicenses)}" -o tsv; then
  :
else
  warn "Could not read $MAILBOX — check the UPN. The user needs an Exchange Online license."
fi

# ---------------------------------------------------------------- 4. Fabric (optional)
FABRIC_SQL_ENDPOINT="${FABRIC_SQL_ENDPOINT:-}"
FABRIC_LAKEHOUSE="${FABRIC_LAKEHOUSE:-pfa_lakehouse}"
if [[ -n "${FABRIC_WORKSPACE_ID:-}" ]]; then
  log "Fabric workspace $FABRIC_WORKSPACE_ID: Viewer for the service principal"
  az rest --method post --resource "https://api.fabric.microsoft.com" \
    --url "https://api.fabric.microsoft.com/v1/workspaces/$FABRIC_WORKSPACE_ID/roleAssignments" \
    --headers "Content-Type=application/json" \
    --body "{\"principal\":{\"id\":\"$SP_OID\",\"type\":\"ServicePrincipal\"},\"role\":\"Viewer\"}" -o none 2>/dev/null \
    && echo "role assigned" || echo "role already assigned (or insufficient rights — add it in the workspace Manage access pane)"
  log "Lakehouse $FABRIC_LAKEHOUSE: SQL analytics endpoint"
  FABRIC_SQL_ENDPOINT="$(az rest --method get --resource "https://api.fabric.microsoft.com" \
    --url "https://api.fabric.microsoft.com/v1/workspaces/$FABRIC_WORKSPACE_ID/lakehouses" \
    --query "value[?displayName=='$FABRIC_LAKEHOUSE'].properties.sqlEndpointProperties.connectionString | [0]" -o tsv || true)"
  [[ -n "$FABRIC_SQL_ENDPOINT" && "$FABRIC_SQL_ENDPOINT" != "None" ]] && echo "endpoint=$FABRIC_SQL_ENDPOINT" \
    || warn "Lakehouse '$FABRIC_LAKEHOUSE' not found (or SQL endpoint still provisioning) — set FABRIC_SQL_ENDPOINT manually."
fi

modes() { [[ -n "$1" ]] && echo live || echo mock; }
GRAPH_MODE=live
CRM_MODE="$(modes "${SF_CLIENT_ID:-}")"
FABRIC_MODE="$(modes "$FABRIC_SQL_ENDPOINT")"
[[ "$FABRIC_SQL_ENDPOINT" == "None" ]] && FABRIC_MODE=mock

# ---------------------------------------------------------------- 5. backend/.env
log "Writing $ENV_FILE"
[[ -f "$ENV_FILE" ]] && cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)" && echo "backed up previous .env"
umask 077
cat > "$ENV_FILE" <<EOF
# Generated by scripts/setup-live-azure.sh on $(date -u +%FT%TZ). Contains secrets — never commit.
PFA_AUTH_MODE=demo
PFA_ENTRA_TENANT_ID=$TENANT_ID

PFA_GRAPH_MODE=$GRAPH_MODE
PFA_CRM_MODE=$CRM_MODE
PFA_FABRIC_MODE=$FABRIC_MODE

PFA_INTEGRATION_CLIENT_ID=$APP_ID
PFA_INTEGRATION_CLIENT_SECRET=$SECRET
PFA_GRAPH_DEMO_MAILBOX=$MAILBOX

PFA_SF_LOGIN_URL=${SF_LOGIN_URL:-}
PFA_SF_CLIENT_ID=${SF_CLIENT_ID:-}
PFA_SF_CLIENT_SECRET=${SF_CLIENT_SECRET:-}

PFA_FABRIC_SQL_ENDPOINT=$FABRIC_SQL_ENDPOINT
PFA_FABRIC_DATABASE=$FABRIC_LAKEHOUSE
PFA_FABRIC_TABLE=weekly_rx_by_territory

# Optional real model:
# PFA_AZURE_OPENAI_ENDPOINT=https://<aoai>.openai.azure.com
# PFA_AZURE_OPENAI_API_KEY=
# PFA_AZURE_OPENAI_DEPLOYMENT=gpt-4o
EOF
echo "graph=$GRAPH_MODE crm=$CRM_MODE fabric=$FABRIC_MODE"

cat <<EOF

Next:
  cd backend && python -m scripts.seed_salesforce   # if CRM is live (once)
  cd backend && python -m scripts.check_live        # PASS/FAIL per integration
  docker compose up --build                         # or scripts/dev.sh

Security note: an application permission on Calendars.ReadWrite can reach every mailbox in the
tenant. Outside a dev tenant, scope it to the demo mailbox with Exchange RBAC for Applications
(New-ManagementScope + New-ManagementRoleAssignment -App $APP_ID). Graph permission changes can
take a few minutes to apply; if check_live returns 403 right away, wait 2-5 minutes and re-run.
EOF
