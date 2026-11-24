#!/usr/bin/env bash
# PharmaFieldAssist — everything on the VPS. Run from inside the project folder (backend/, frontend/, docker-compose.yml).
# Idempotent: re-run after every code update or config change.
#
#   bash pfa-vps.sh                                   # app only (mock integrations), public on :8711 behind a demo password
#
#   MAILBOX=jordan@tenant.onmicrosoft.com \           # + live Outlook (Graph)
#   FABRIC_WORKSPACE_ID=<guid> \                      # + live Fabric (creates Lakehouse, uploads data, loads table)
#   SF_LOGIN_URL=https://x.my.salesforce.com SF_CLIENT_ID=... SF_CLIENT_SECRET=... \   # + live Salesforce (seeds accounts)
#   bash pfa-vps.sh
set -euo pipefail

WEB_PORT="${WEB_PORT:-8711}"; API_PORT="${API_PORT:-8710}"; DEMO_USER="${DEMO_USER:-demo}"
FABRIC_LAKEHOUSE="${FABRIC_LAKEHOUSE:-pfa_lakehouse}"; FABRIC_TABLE="weekly_rx_by_territory"
SUDO=""; [[ $(id -u) -ne 0 ]] && SUDO="sudo"
DC="$SUDO docker compose -p pfa"
log()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*"; exit 1; }
[[ -f docker-compose.yml && -d backend && -d frontend ]] || die "Run this from the pharma-field-assist folder"
ROOT="$(pwd)"

# ============================================================== 1. prerequisites
log "Prerequisites"
command -v docker >/dev/null || { echo "installing Docker"; curl -fsSL https://get.docker.com | $SUDO sh; }
for pkg in curl python3 openssl; do command -v $pkg >/dev/null || { $SUDO apt-get update -qq; $SUDO apt-get install -y -qq $pkg; }; done
LIVE=0; [[ -n "${MAILBOX:-}${FABRIC_WORKSPACE_ID:-}${SF_CLIENT_ID:-}" ]] && LIVE=1
if [[ $LIVE -eq 1 ]] && ! command -v az >/dev/null; then
  echo "installing Azure CLI"; curl -sL https://aka.ms/InstallAzureCLIDeb | $SUDO bash >/dev/null
fi
echo "docker $(docker --version | awk '{print $3}' | tr -d ,)  live-setup=$([[ $LIVE -eq 1 ]] && echo yes || echo no)"

# ============================================================== 2. project patches (idempotent)
log "Configuring ports, demo password, image"
$SUDO touch backend/.env
if [[ ! -f frontend/htpasswd ]]; then
  DEMO_PASSWORD="${DEMO_PASSWORD:-$(openssl rand -base64 12 | tr -d '/+=')}"
  printf '%s:%s\n' "$DEMO_USER" "$(openssl passwd -1 "$DEMO_PASSWORD")" > frontend/htpasswd
  printf 'user=%s\npassword=%s\n' "$DEMO_USER" "$DEMO_PASSWORD" > .demo-credentials
  chmod 644 frontend/htpasswd; chmod 600 .demo-credentials
fi
python3 - "$WEB_PORT" "$API_PORT" <<'PY'
import re, sys
web, api = sys.argv[1:3]
p = "docker-compose.yml"; s = open(p).read()
s = re.sub(r'ports: \["(?:[\d.]+:)?\d+:8000"\]', f'ports: ["127.0.0.1:{api}:8000"]', s)
s = re.sub(r'ports: \["(?:[\d.]+:)?\d+:8080"\]', f'ports: ["0.0.0.0:{web}:8080"]', s)
s = re.sub(r"env_file:\n\s+- path: \./backend/\.env\n\s+required: false", "env_file: ./backend/.env", s)
if "htpasswd" not in s:
    s = s.replace("      API_UPSTREAM: http://api:8000\n",
                  "      API_UPSTREAM: http://api:8000\n    volumes: [\"./frontend/htpasswd:/etc/nginx/htpasswd:ro\"]\n")
open(p, "w").write(s)
p = "frontend/nginx.conf"; s = open(p).read()
if "auth_basic" not in s:
    s = s.replace("  root /usr/share/nginx/html;\n",
                  "  root /usr/share/nginx/html;\n  auth_basic \"PharmaFieldAssist demo\";\n  auth_basic_user_file /etc/nginx/htpasswd;\n")
    s = s.replace("location /health/ { proxy_http_version 1.1; proxy_pass ${API_UPSTREAM}; }",
                  "location /health/ { auth_basic off; proxy_http_version 1.1; proxy_pass ${API_UPSTREAM}; }")
open(p, "w").write(s)
p = "backend/Dockerfile"; s = open(p).read()
if "COPY --chown=appuser:appuser scripts ./scripts" not in s:  # ship check_live / seed_salesforce in the image
    s = s.replace("COPY --chown=appuser:appuser data ./data\n",
                  "COPY --chown=appuser:appuser data ./data\nCOPY --chown=appuser:appuser scripts ./scripts\n")
open(p, "w").write(s)
PY
if command -v ufw >/dev/null && $SUDO ufw status | grep -q "Status: active"; then
  $SUDO ufw allow "$WEB_PORT/tcp" >/dev/null && echo "ufw: opened $WEB_PORT/tcp"
fi

# ============================================================== 3. live integrations (only when requested)
if [[ $LIVE -eq 1 ]]; then
  log "Azure sign-in"
  if ! az account show >/dev/null 2>&1; then
    echo "Open the URL below in any browser and enter the code (sign in as a Global Admin of the tenant):"
    az login --use-device-code --allow-no-subscriptions ${AZ_TENANT:+--tenant "$AZ_TENANT"} -o none
  fi
  echo "signed in: $(az account show --query user.name -o tsv)  tenant: $(az account show --query tenantId -o tsv)"

  # ---------------- Fabric: lakehouse + data + table (before Entra setup so the SQL endpoint can be resolved)
  if [[ -n "${FABRIC_WORKSPACE_ID:-}" ]]; then
    log "Fabric: lakehouse '$FABRIC_LAKEHOUSE' in workspace $FABRIC_WORKSPACE_ID"
    FAPI="https://api.fabric.microsoft.com/v1/workspaces/$FABRIC_WORKSPACE_ID"
    fabric_get() { az rest --method get --resource https://api.fabric.microsoft.com --url "$1" "${@:2}"; }
    LH_ID="$(fabric_get "$FAPI/lakehouses" --query "value[?displayName=='$FABRIC_LAKEHOUSE'].id | [0]" -o tsv 2>/dev/null || true)"
    if [[ -z "$LH_ID" || "$LH_ID" == "None" ]]; then
      az rest --method post --resource https://api.fabric.microsoft.com --url "$FAPI/lakehouses" \
        --headers "Content-Type=application/json" --body "{\"displayName\":\"$FABRIC_LAKEHOUSE\"}" -o none \
        || die "Could not create the lakehouse — check the workspace id and that it is on a Fabric (trial) capacity"
      for i in {1..30}; do
        LH_ID="$(fabric_get "$FAPI/lakehouses" --query "value[?displayName=='$FABRIC_LAKEHOUSE'].id | [0]" -o tsv 2>/dev/null || true)"
        [[ -n "$LH_ID" && "$LH_ID" != "None" ]] && break; sleep 5
      done
      echo "created lakehouse $LH_ID"
    else
      echo "lakehouse exists $LH_ID"
    fi

    CSV="backend/data/fabric_weekly_rx_by_territory.csv"
    ST="$(az account get-access-token --resource https://storage.azure.com --query accessToken -o tsv)"
    OL="https://onelake.dfs.fabric.microsoft.com/$FABRIC_WORKSPACE_ID/$LH_ID/Files/pfa/$FABRIC_TABLE.csv"
    SIZE=$(wc -c < "$CSV" | tr -d ' ')
    curl -sf -X PUT   -H "Authorization: Bearer $ST" -H "Content-Length: 0" "$OL?resource=file" >/dev/null \
      && curl -sf -X PATCH -H "Authorization: Bearer $ST" -H "Content-Type: application/octet-stream" --data-binary @"$CSV" "$OL?action=append&position=0" >/dev/null \
      && curl -sf -X PATCH -H "Authorization: Bearer $ST" -H "Content-Length: 0" "$OL?action=flush&position=$SIZE" >/dev/null \
      && echo "uploaded $CSV -> OneLake Files/pfa/" || die "OneLake upload failed (are you a Contributor/Admin on the workspace?)"

    az rest --method post --resource https://api.fabric.microsoft.com --url "$FAPI/lakehouses/$LH_ID/tables/$FABRIC_TABLE/load" \
      --headers "Content-Type=application/json" \
      --body "{\"relativePath\":\"Files/pfa/$FABRIC_TABLE.csv\",\"pathType\":\"File\",\"mode\":\"Overwrite\",\"formatOptions\":{\"format\":\"Csv\",\"header\":true,\"delimiter\":\",\"}}" -o none \
      && echo "table load started: $FABRIC_TABLE" || die "Load-to-table failed"

    echo "waiting for the SQL analytics endpoint"
    for i in {1..36}; do
      FABRIC_SQL_ENDPOINT="$(fabric_get "$FAPI/lakehouses/$LH_ID" --query "properties.sqlEndpointProperties.connectionString" -o tsv 2>/dev/null || true)"
      [[ -n "$FABRIC_SQL_ENDPOINT" && "$FABRIC_SQL_ENDPOINT" != "None" ]] && break; sleep 5
    done
    [[ -n "$FABRIC_SQL_ENDPOINT" && "$FABRIC_SQL_ENDPOINT" != "None" ]] && echo "endpoint $FABRIC_SQL_ENDPOINT" || die "SQL endpoint not ready yet — re-run in a few minutes"
    export FABRIC_SQL_ENDPOINT FABRIC_LAKEHOUSE
  fi

  # ---------------- Entra app + Graph consent + Fabric role + backend/.env
  log "Entra app registration, Graph consent, .env"
  [[ -n "${MAILBOX:-}" ]] || die "MAILBOX is required for live setup (the M365 mailbox the demo rep books into)"
  MAILBOX="$MAILBOX" FABRIC_WORKSPACE_ID="${FABRIC_WORKSPACE_ID:-}" FABRIC_LAKEHOUSE="$FABRIC_LAKEHOUSE" \
  FABRIC_SQL_ENDPOINT="${FABRIC_SQL_ENDPOINT:-}" SF_LOGIN_URL="${SF_LOGIN_URL:-}" SF_CLIENT_ID="${SF_CLIENT_ID:-}" \
  SF_CLIENT_SECRET="${SF_CLIENT_SECRET:-}" bash scripts/setup-live-azure.sh | grep -vE "^(Next:|  cd backend|  docker compose)"
  $SUDO chmod 600 backend/.env
fi

# ============================================================== 4. build, test, start
log "Lint + tests inside the image"
$SUDO docker build -q --target test backend >/dev/null && echo "ruff + pytest: PASS"
log "Build + start"
$DC up --build -d --force-recreate
for i in {1..60}; do curl -sf 127.0.0.1:$API_PORT/health/ready >/dev/null && break; sleep 2; done
curl -sf 127.0.0.1:$API_PORT/health/ready >/dev/null || { $DC logs --tail 60 api; die "API did not become ready (if it says 'configuration is missing', fix backend/.env)"; }
bash scripts/smoke.sh "http://127.0.0.1:$API_PORT"

# ============================================================== 5. live checks
if grep -qE '^PFA_(GRAPH|CRM|FABRIC)_MODE=live' backend/.env; then
  if grep -q '^PFA_CRM_MODE=live' backend/.env; then
    log "Seeding Salesforce demo accounts"
    $DC exec -T api python -m scripts.seed_salesforce || warn "Salesforce seed failed — see message above"
  fi
  log "Live integration check"
  sleep 5
  $DC exec -T api python -m scripts.check_live || warn "Some live checks failed. Graph consent can take 2-5 minutes; re-run: $DC exec api python -m scripts.check_live"
fi

# ============================================================== done
$DC ps
CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$WEB_PORT/")
[[ "$CODE" == 401 ]] && echo "web password protection: OK"
IP=$(curl -s -4 --max-time 5 ifconfig.me || hostname -I | awk '{print $1}')
log "LIVE:  http://$IP:$WEB_PORT"
echo "    login: $(tr '\n' ' ' < .demo-credentials)   (saved in $ROOT/.demo-credentials)"
echo "    logs:  $DC logs -f api        stop: $DC down"
