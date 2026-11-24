#!/usr/bin/env bash
# PharmaFieldAssist -> Azure Container Apps. Run on the VPS from inside the project folder. Idempotent.
#
# Creates in your subscription (resource group pfa-demo-rg, region centralus by default):
#   Log Analytics + Application Insights, Azure Container Registry (images built IN Azure via ACR Tasks),
#   user-assigned managed identity (AcrPull + Key Vault Secrets User), Key Vault (RBAC, secrets),
#   Container Apps environment, API container app (internal ingress) + web container app (public HTTPS).
# Any PFA_* settings in backend/.env (live Graph/Salesforce/Fabric, Azure OpenAI) are carried over:
#   values whose name contains SECRET or KEY go to Key Vault, the rest become plain env vars.
#
#   bash scripts/pfa-azure.sh                       # deploy / redeploy
#   LOCATION=eastus2 AZ_SUBSCRIPTION=<id> bash scripts/pfa-azure.sh
#   az group delete -n pfa-demo-rg --yes --no-wait  # remove everything afterwards
set -euo pipefail

LOCATION="${LOCATION:-centralus}"; RG="${RG:-pfa-demo-rg}"
log()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*"; exit 1; }
[[ -f docker-compose.yml && -d backend && -d frontend ]] || die "Run this from the pharma-field-assist folder"
SUDO=""; [[ $(id -u) -ne 0 ]] && SUDO="sudo"

# ---------------------------------------------------------------- tooling + sign-in
log "Azure CLI"
command -v az >/dev/null || { echo "installing Azure CLI"; curl -sL https://aka.ms/InstallAzureCLIDeb | $SUDO bash >/dev/null; }
az config set extension.use_dynamic_install=yes_without_prompt extension.dynamic_install_allow_preview=true -o none 2>/dev/null || true
az extension add --name containerapp --upgrade -y -o none 2>/dev/null || true
if ! az account show >/dev/null 2>&1; then
  echo "Open the URL below in any browser and enter the code:"
  az login --use-device-code -o none
fi
[[ -n "${AZ_SUBSCRIPTION:-}" ]] && az account set --subscription "$AZ_SUBSCRIPTION"
SUB_ID="$(az account show --query id -o tsv)"
echo "subscription: $(az account show --query name -o tsv) ($SUB_ID)   region: $LOCATION"
if command -v sha256sum >/dev/null; then SHA="sha256sum"; else SHA="shasum -a 256"; fi  # macOS has only shasum
SUFFIX="$(printf '%s' "$SUB_ID$RG" | $SHA | cut -c1-6)"
ACR="pfaacr$SUFFIX"; KV="kv-pfa-$SUFFIX"; LAW="log-pfa"; APPI="appi-pfa"; UAMI="id-pfa"
CAE="cae-pfa"; API="ca-pfa-api"; WEB="ca-pfa-web"
TAG="$(date -u +%Y%m%d%H%M%S)"

# ---------------------------------------------------------------- project patches (idempotent)
log "Preparing images (demo password baked into the web image, ACA-safe proxy headers)"
if [[ ! -f frontend/htpasswd ]]; then
  DEMO_PASSWORD="${DEMO_PASSWORD:-$(openssl rand -base64 12 | tr -d '/+=')}"
  printf 'demo:%s\n' "$(openssl passwd -1 "$DEMO_PASSWORD")" > frontend/htpasswd
  printf 'user=demo\npassword=%s\n' "$DEMO_PASSWORD" > .demo-credentials
  chmod 644 frontend/htpasswd; chmod 600 .demo-credentials
fi
python3 - <<'PY'
p = "frontend/nginx.conf"; s = open(p).read()
if "auth_basic" not in s:
    s = s.replace("  root /usr/share/nginx/html;\n",
                  "  root /usr/share/nginx/html;\n  auth_basic \"PharmaFieldAssist demo\";\n  auth_basic_user_file /etc/nginx/htpasswd;\n")
    s = s.replace("location /health/ { proxy_http_version 1.1; proxy_pass ${API_UPSTREAM}; }",
                  "location /health/ { auth_basic off; proxy_http_version 1.1; proxy_pass ${API_UPSTREAM}; }")
# ACA routes by Host header: forward the upstream's host, and send SNI for the https upstream
s = s.replace("proxy_set_header Host $host;", "proxy_set_header Host $proxy_host;\n    proxy_ssl_server_name on;")
s = s.replace("location /health/ { auth_basic off; proxy_http_version 1.1; proxy_pass ${API_UPSTREAM}; }",
              "location /health/ { auth_basic off; proxy_ssl_server_name on; proxy_http_version 1.1; proxy_pass ${API_UPSTREAM}; }")
open(p, "w").write(s)
p = "frontend/Dockerfile"; s = open(p).read()
if "COPY htpasswd /etc/nginx/htpasswd" not in s:
    s = s.replace("COPY nginx.conf /etc/nginx/templates/default.conf.template\n",
                  "COPY nginx.conf /etc/nginx/templates/default.conf.template\nCOPY htpasswd /etc/nginx/htpasswd\n")
open(p, "w").write(s)
p = "backend/Dockerfile"; s = open(p).read()
if "COPY --chown=appuser:appuser scripts ./scripts" not in s:
    s = s.replace("COPY --chown=appuser:appuser data ./data\n",
                  "COPY --chown=appuser:appuser data ./data\nCOPY --chown=appuser:appuser scripts ./scripts\n")
open(p, "w").write(s)
PY

# ---------------------------------------------------------------- platform resources
log "Resource providers"
for ns in Microsoft.App Microsoft.OperationalInsights Microsoft.ContainerRegistry Microsoft.KeyVault Microsoft.ManagedIdentity Microsoft.Insights; do
  az provider register -n "$ns" -o none
done
for ns in Microsoft.App Microsoft.OperationalInsights Microsoft.ContainerRegistry Microsoft.KeyVault; do
  for i in {1..60}; do [[ "$(az provider show -n $ns --query registrationState -o tsv)" == "Registered" ]] && break; sleep 5; done
done

log "Resource group $RG"
az group create -n "$RG" -l "$LOCATION" --tags app=pfa purpose=demo -o none

log "Log Analytics + Application Insights"
az monitor log-analytics workspace show -g "$RG" -n "$LAW" -o none 2>/dev/null \
  || az monitor log-analytics workspace create -g "$RG" -n "$LAW" -l "$LOCATION" --retention-time 30 -o none
LAW_ID="$(az monitor log-analytics workspace show -g "$RG" -n "$LAW" --query id -o tsv)"
LAW_CUSTOMER="$(az monitor log-analytics workspace show -g "$RG" -n "$LAW" --query customerId -o tsv)"
LAW_KEY="$(az monitor log-analytics workspace get-shared-keys -g "$RG" -n "$LAW" --query primarySharedKey -o tsv)"
az monitor app-insights component show -g "$RG" -a "$APPI" -o none 2>/dev/null \
  || az monitor app-insights component create -g "$RG" -a "$APPI" -l "$LOCATION" --workspace "$LAW_ID" --kind web -o none
APPI_CS="$(az monitor app-insights component show -g "$RG" -a "$APPI" --query connectionString -o tsv)"

log "Container registry $ACR"
az acr show -n "$ACR" -o none 2>/dev/null || az acr create -g "$RG" -n "$ACR" --sku Basic --admin-enabled false -o none
ACR_ID="$(az acr show -n "$ACR" --query id -o tsv)"

log "Managed identity $UAMI"
az identity show -g "$RG" -n "$UAMI" -o none 2>/dev/null || az identity create -g "$RG" -n "$UAMI" -l "$LOCATION" -o none
UAMI_ID="$(az identity show -g "$RG" -n "$UAMI" --query id -o tsv)"
UAMI_PID="$(az identity show -g "$RG" -n "$UAMI" --query principalId -o tsv)"
UAMI_CID="$(az identity show -g "$RG" -n "$UAMI" --query clientId -o tsv)"

log "Key Vault $KV (RBAC)"
az keyvault show -n "$KV" -o none 2>/dev/null \
  || az keyvault create -g "$RG" -n "$KV" -l "$LOCATION" --enable-rbac-authorization true -o none
KV_ID="$(az keyvault show -n "$KV" --query id -o tsv)"
ME="$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)"

assign() { # role scope principalId principalType
  az role assignment list --assignee "$3" --role "$1" --scope "$2" --query "[0].id" -o tsv 2>/dev/null | grep -q . \
    || az role assignment create --role "$1" --scope "$2" --assignee-object-id "$3" --assignee-principal-type "$4" -o none
}
assign "AcrPull" "$ACR_ID" "$UAMI_PID" ServicePrincipal
assign "Key Vault Secrets User" "$KV_ID" "$UAMI_PID" ServicePrincipal
[[ -n "$ME" ]] && assign "Key Vault Secrets Officer" "$KV_ID" "$ME" User
echo "waiting for role assignments to propagate"; sleep 45

# ---------------------------------------------------------------- secrets + settings from backend/.env
log "Secrets -> Key Vault"
setsecret() { # name value
  for i in 1 2 3 4 5 6; do
    az keyvault secret set --vault-name "$KV" -n "$1" --value "$2" -o none 2>/dev/null && return 0; sleep 15
  done
  die "Could not write secret $1 to Key Vault (role propagation can take a few minutes - re-run)"
}
az keyvault secret show --vault-name "$KV" -n demo-signing-key -o none 2>/dev/null \
  || setsecret demo-signing-key "$(openssl rand -hex 32)"
setsecret appinsights-cs "$APPI_CS"

KV_URI="https://$KV.vault.azure.net/secrets"
SECRETS=("demo-signing-key=keyvaultref:$KV_URI/demo-signing-key,identityref:$UAMI_ID"
         "appinsights-cs=keyvaultref:$KV_URI/appinsights-cs,identityref:$UAMI_ID")
ENVS=("PFA_ENVIRONMENT=dev" "PFA_AUTH_MODE=demo" "PFA_LOG_LEVEL=INFO" "PFA_APP_VERSION=$TAG"
      "PFA_DEMO_SIGNING_KEY=secretref:demo-signing-key"
      "PFA_APPLICATIONINSIGHTS_CONNECTION_STRING=secretref:appinsights-cs"
      "AZURE_CLIENT_ID=$UAMI_CID")
if [[ -f backend/.env ]]; then
  while IFS='=' read -r k v; do
    [[ "$k" =~ ^PFA_[A-Z0-9_]+$ && -n "$v" ]] || continue
    case "$k" in PFA_ENVIRONMENT|PFA_AUTH_MODE|PFA_LOG_LEVEL|PFA_APP_VERSION|PFA_DEMO_SIGNING_KEY|PFA_APPLICATIONINSIGHTS_CONNECTION_STRING) continue;; esac
    if [[ "$k" == *SECRET* || "$k" == *KEY* ]]; then
      name="$(echo "${k#PFA_}" | tr 'A-Z_' 'a-z-')"
      setsecret "$name" "$v"
      SECRETS+=("$name=keyvaultref:$KV_URI/$name,identityref:$UAMI_ID")
      ENVS+=("$k=secretref:$name"); echo "  secret  $k"
    else
      ENVS+=("$k=$v"); echo "  setting $k"
    fi
  done < <(grep -E '^PFA_[A-Z0-9_]+=' backend/.env || true)
fi

# ---------------------------------------------------------------- images (built in Azure)
log "Building images in ACR (tag $TAG) - runs in Azure, not on this VPS"
build_image() { # name context [target]
  local name="$1" ctx="$2" target="${3:-}"
  if az acr build -r "$ACR" -t "$name:$TAG" ${target:+--target "$target"} "$ctx" --no-logs -o none 2>/tmp/pfa-acr.err; then
    echo "  $name:$TAG built in ACR"; return 0
  fi
  if grep -q "TasksOperationsNotAllowed" /tmp/pfa-acr.err; then
    # Free-trial / some subscriptions disallow ACR Tasks: build locally and push instead.
    echo "  ACR Tasks not allowed on this subscription - building $name locally and pushing"
    az acr login -n "$ACR" -o none
    $SUDO docker build -q ${target:+--target "$target"} -t "$ACR.azurecr.io/$name:$TAG" "$ctx" >/dev/null
    $SUDO docker push -q "$ACR.azurecr.io/$name:$TAG" >/dev/null && echo "  $name:$TAG pushed"; return 0
  fi
  cat /tmp/pfa-acr.err; die "$name image build failed (details: az acr task logs -r $ACR)"
}
build_image pfa-api backend runtime
build_image pfa-web frontend

# ---------------------------------------------------------------- container apps
log "Container Apps environment $CAE"
az containerapp env show -g "$RG" -n "$CAE" -o none 2>/dev/null \
  || az containerapp env create -g "$RG" -n "$CAE" -l "$LOCATION" \
       --logs-workspace-id "$LAW_CUSTOMER" --logs-workspace-key "$LAW_KEY" -o none

log "API app $API (internal ingress)"
# Single replica: the demo's audit store (SQLite) and HITL actions are per-process.
if az containerapp show -g "$RG" -n "$API" -o none 2>/dev/null; then
  az containerapp secret set -g "$RG" -n "$API" --secrets "${SECRETS[@]}" -o none
  az containerapp update -g "$RG" -n "$API" --image "$ACR.azurecr.io/pfa-api:$TAG" \
    --replace-env-vars "${ENVS[@]}" --min-replicas 1 --max-replicas 1 -o none
else
  az containerapp create -g "$RG" -n "$API" --environment "$CAE" \
    --image "$ACR.azurecr.io/pfa-api:$TAG" --registry-server "$ACR.azurecr.io" --registry-identity "$UAMI_ID" \
    --user-assigned "$UAMI_ID" --ingress internal --target-port 8000 --transport http \
    --cpu 0.5 --memory 1.0Gi --min-replicas 1 --max-replicas 1 --revisions-mode multiple \
    --secrets "${SECRETS[@]}" --env-vars "${ENVS[@]}" -o none
fi
API_FQDN="$(az containerapp show -g "$RG" -n "$API" --query properties.configuration.ingress.fqdn -o tsv)"
API_REV="$(az containerapp show -g "$RG" -n "$API" --query properties.latestRevisionName -o tsv)"
az containerapp ingress traffic set -g "$RG" -n "$API" --revision-weight "$API_REV=100" -o none
echo "  $API_FQDN (revision $API_REV at 100%)"

log "Web app $WEB (public HTTPS)"
if az containerapp show -g "$RG" -n "$WEB" -o none 2>/dev/null; then
  az containerapp update -g "$RG" -n "$WEB" --image "$ACR.azurecr.io/pfa-web:$TAG" \
    --replace-env-vars "API_UPSTREAM=https://$API_FQDN" -o none
else
  az containerapp create -g "$RG" -n "$WEB" --environment "$CAE" \
    --image "$ACR.azurecr.io/pfa-web:$TAG" --registry-server "$ACR.azurecr.io" --registry-identity "$UAMI_ID" \
    --user-assigned "$UAMI_ID" --ingress external --target-port 8080 --transport http \
    --cpu 0.25 --memory 0.5Gi --min-replicas 1 --max-replicas 2 \
    --env-vars "API_UPSTREAM=https://$API_FQDN" -o none
fi
WEB_FQDN="$(az containerapp show -g "$RG" -n "$WEB" --query properties.configuration.ingress.fqdn -o tsv)"

# ---------------------------------------------------------------- verify
log "Verifying https://$WEB_FQDN"
CREDS_USER="$(grep '^user=' .demo-credentials | cut -d= -f2)"; CREDS_PASS="$(grep '^password=' .demo-credentials | cut -d= -f2)"
for i in {1..60}; do curl -sf "https://$WEB_FQDN/health/ready" >/dev/null && break; sleep 5; done
curl -sf "https://$WEB_FQDN/health/ready" >/dev/null || {
  az containerapp logs show -g "$RG" -n "$API" --tail 40 --type console 2>/dev/null || true
  die "API not ready through the web app - logs above"; }
CODE="$(curl -s -o /dev/null -w '%{http_code}' "https://$WEB_FQDN/")"
[[ "$CODE" == 401 ]] && echo "password protection: OK" || warn "expected 401 without login, got $CODE"
bash scripts/smoke.sh "https://$CREDS_USER:$CREDS_PASS@$WEB_FQDN"

if grep -qE '^PFA_(GRAPH|CRM|FABRIC)_MODE=live' backend/.env 2>/dev/null; then
  log "Live integration check (inside the Azure container)"
  az containerapp exec -g "$RG" -n "$API" --command "python -m scripts.check_live" 2>/dev/null \
    || warn "Run manually: az containerapp exec -g $RG -n $API --command 'python -m scripts.check_live'"
fi

log "LIVE ON AZURE:  https://$WEB_FQDN"
echo "    login: user=$CREDS_USER password=$CREDS_PASS"
echo "    portal: https://portal.azure.com/#@/resource/subscriptions/$SUB_ID/resourceGroups/$RG/overview"
echo "    logs:   az containerapp logs show -g $RG -n $API --follow"
echo "    remove: az group delete -n $RG --yes --no-wait"

# ---------------------------------------------------------------- APIM AI gateway (default ON)
# The model is only ever reachable through APIM (token caps, JSON-only, daily/monthly quotas, budget).
# SKIP_APIM=1 deploys without any LLM at all (zero model spend, extractive answers) - never a direct path.
if [[ "${SKIP_APIM:-0}" != "1" ]]; then
  log "Adding the APIM AI gateway in front of the model (scripts/pfa-apim.sh)"
  bash scripts/pfa-apim.sh
else
  warn "SKIP_APIM=1: no LLM configured - answers are extractive from approved content, model spend is zero"
fi
