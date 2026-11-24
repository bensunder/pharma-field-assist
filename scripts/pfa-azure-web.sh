#!/usr/bin/env bash
# Rebuild + roll only the web app (ca-pfa-web) on Azure Container Apps, then verify end to end.
# Run from the project folder on the machine that has `az` logged in. ASCII-only, bash 3.2 safe (macOS).
set -euo pipefail
RG="${AZ_RESOURCE_GROUP:-pfa-demo-rg}"; WEB="ca-pfa-web"
log() { printf '\n==> %s\n' "$*"; }
die() { printf '[fail] %s\n' "$*" >&2; exit 1; }
[[ -f frontend/nginx.conf && -f scripts/smoke.sh ]] || die "Run this from the pharma-field-assist folder"
[[ -f frontend/htpasswd && -f .demo-credentials ]] || die "frontend/htpasswd or .demo-credentials missing - copy them from the previous deploy folder"
command -v az >/dev/null || die "az not on PATH (export PATH=\"\$HOME/.local/bin:\$HOME/.local/pfa-bin:\$PATH\")"

log "Patching web image (basic auth, ACA proxy headers, token header mapping)"
python3 - <<'PY'
p = "frontend/nginx.conf"; s = open(p).read()
if "auth_basic" not in s:
    s = s.replace("  root /usr/share/nginx/html;\n",
                  "  root /usr/share/nginx/html;\n  auth_basic \"PharmaFieldAssist demo\";\n  auth_basic_user_file /etc/nginx/htpasswd;\n")
    s = s.replace("location /health/ { proxy_http_version 1.1; proxy_pass ${API_UPSTREAM}; }",
                  "location /health/ { auth_basic off; proxy_ssl_server_name on; proxy_http_version 1.1; proxy_pass ${API_UPSTREAM}; }")
s = s.replace("proxy_set_header Host $host;", "proxy_set_header Host $proxy_host;\n    proxy_ssl_server_name on;")
assert "pfa_upstream_auth" in s, "nginx.conf is old - unzip the newest pharma-field-assist.zip"
open(p, "w").write(s)
p = "frontend/Dockerfile"; s = open(p).read()
if "COPY htpasswd /etc/nginx/htpasswd" not in s:
    s = s.replace("COPY nginx.conf /etc/nginx/templates/default.conf.template\n",
                  "COPY nginx.conf /etc/nginx/templates/default.conf.template\nCOPY htpasswd /etc/nginx/htpasswd\n")
open(p, "w").write(s)
a = open("frontend/src/lib/api.ts").read()
assert '"X-PFA-Token": token' in a, "frontend is old - unzip the newest pharma-field-assist.zip"
print("  nginx.conf, Dockerfile and api.ts ready")
PY

ACR="$(az acr list -g "$RG" --query "[0].name" -o tsv)"; [[ -n "$ACR" ]] || die "No ACR in $RG"
TAG="$(date -u +%Y%m%d%H%M%S)"
log "Building pfa-web:$TAG in ACR $ACR"
az acr build -r "$ACR" -t "pfa-web:$TAG" frontend --no-logs -o none || die "ACR build failed"
log "Rolling $WEB to the new image"
az containerapp update -g "$RG" -n "$WEB" --image "$ACR.azurecr.io/pfa-web:$TAG" -o none 2>/dev/null
W="$(az containerapp show -g "$RG" -n "$WEB" --query properties.configuration.ingress.fqdn -o tsv 2>/dev/null)"
U="$(grep '^user=' .demo-credentials | cut -d= -f2)"; P="$(grep '^password=' .demo-credentials | cut -d= -f2)"

log "Waiting for the new revision"
c=""; for i in $(seq 1 60); do
  c="$(curl -s -o /dev/null -w '%{http_code}' "https://$W/health/ready")"
  r="$(az containerapp revision list -g "$RG" -n "$WEB" --query "[?properties.template.containers[0].image=='$ACR.azurecr.io/pfa-web:$TAG' && properties.healthState=='Healthy'] | length(@)" -o tsv 2>/dev/null || echo 0)"
  [[ "$c" == "200" && "$r" != "0" ]] && break; sleep 5
done
echo "  health/ready: $c   new revision healthy: $r"
[[ "$c" == "200" && "$r" != "0" ]] || die "New web revision not healthy - run: az containerapp logs show -g $RG -n $WEB --tail 30"
sleep 10  # let ingress drain the old revision

log "Smoke test (basic auth at the edge + app token through nginx)"
bash scripts/smoke.sh "https://$U:$P@$W"

printf '\nOPEN:     https://%s\nuser:     %s\npassword: %s\n' "$W" "$U" "$P"
