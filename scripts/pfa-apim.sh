#!/usr/bin/env bash
# PharmaFieldAssist: add the APIM AI gateway as the ONLY path to the LLM.
# Run from the project folder on the machine that is signed in to Azure (az login), after pfa-azure.sh.
# Idempotent: safe to re-run. ASCII only, works with macOS bash 3.2.
#
#   container app --(UAMI token)--> APIM (validate-azure-ad-token, azure-openai-token-limit,
#   azure-openai-emit-token-metric, authentication-managed-identity) --> Azure OpenAI (keys disabled)
#
# Creates in pfa-demo-rg: Azure OpenAI account + current small chat model deployment, API Management (BasicV2),
# APIM logger/diagnostics to App Insights, role assignment APIM-MI -> OpenAI User; rebuilds the API
# image and points the API container app at the gateway with PFA_LLM_GATEWAY_ONLY=true.
set -euo pipefail

RG="${RG:-pfa-demo-rg}"
# Model is picked at run time from what the account can deploy NOW (versions get deprecated).
# Non-reasoning chat models only: they honour max_tokens + temperature=0 + JSON mode, which the
# token cap and JSON-only guardrails depend on. Override with MODEL=... MODEL_VERSION=...
MODEL="${MODEL:-}"; MODEL_VERSION="${MODEL_VERSION:-}"; CAPACITY="${CAPACITY:-10}"
MODEL_CANDIDATES="${MODEL_CANDIDATES:-gpt-4.1-mini gpt-4o-mini gpt-4.1-nano gpt-4.1 gpt-4o}"
AOAI_REGIONS="${AOAI_REGIONS:-eastus2 eastus westus3 swedencentral northcentralus}"
APIM_SKU="${APIM_SKU:-BasicV2}"; TPM_LIMIT="${TPM_LIMIT:-20000}"
# Spend caps enforced AT THE GATEWAY (requests over quota get 403 and never reach the model):
MAX_OUTPUT_TOKENS="${MAX_OUTPUT_TOKENS:-400}"        # per-call ceiling, rewritten into every request body
DAILY_TOKEN_QUOTA="${DAILY_TOKEN_QUOTA:-500000}"     # prompt+completion tokens per UTC day
MONTHLY_TOKEN_QUOTA="${MONTHLY_TOKEN_QUOTA:-5000000}" # per calendar month (small model: a few dollars)
BUDGET_USD="${BUDGET_USD:-100}"                      # Azure cost budget on the resource group (email alerts)
BUDGET_EMAIL="${BUDGET_EMAIL:-}"                     # defaults to the signed-in az account
API_VERSION_AOAI="2024-10-21"; ARM="https://management.azure.com"; APIM_API_VER="2024-05-01"

log()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*"; exit 1; }
ok()   { printf '  \033[1;32mOK\033[0m %s\n' "$*"; }
[[ -f docker-compose.yml && -d backend && -d frontend ]] || die "Run this from the pharma-field-assist folder"
export PATH="$HOME/.local/bin:$HOME/.local/pfa-bin:$PATH"
command -v az >/dev/null || die "Azure CLI not found (run pfa-azure.sh setup first)"
command -v python3 >/dev/null || die "python3 not found"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

# ---------------------------------------------------------------- context
log "Context"
az account show -o none 2>/dev/null || az login -o none
SUB_ID="$(az account show --query id -o tsv)"; TENANT="$(az account show --query tenantId -o tsv)"
az group show -n "$RG" -o none 2>/dev/null || die "Resource group $RG not found - run scripts/pfa-azure.sh first"
LOCATION="$(az group show -n "$RG" --query location -o tsv)"
ACR="$(az acr list -g "$RG" --query "[0].name" -o tsv)"; [[ -n "$ACR" ]] || die "No container registry in $RG"
SUFFIX="${ACR#pfaacr}"
UAMI_CID="$(az identity show -g "$RG" -n id-pfa --query clientId -o tsv)"
APPI_CS="$(az monitor app-insights component show -g "$RG" -a appi-pfa --query connectionString -o tsv)"
AOAI="aoai-pfa-$SUFFIX"; APIM="apim-pfa-$SUFFIX"; API_APP="ca-pfa-api"; WEB_APP="ca-pfa-web"
echo "subscription $SUB_ID  rg $RG ($LOCATION)  aoai $AOAI  apim $APIM"
for ns in Microsoft.CognitiveServices Microsoft.ApiManagement; do az provider register -n "$ns" -o none; done
for ns in Microsoft.CognitiveServices Microsoft.ApiManagement; do
  for i in $(seq 1 60); do [[ "$(az provider show -n $ns --query registrationState -o tsv)" == "Registered" ]] && break; sleep 5; done
done

# ---------------------------------------------------------------- Azure OpenAI (keys disabled)
log "Azure OpenAI $AOAI (GlobalStandard, ${CAPACITY}K TPM)"
if az cognitiveservices account show -g "$RG" -n "$AOAI" -o none 2>/dev/null; then
  AOAI_LOC="$(az cognitiveservices account show -g "$RG" -n "$AOAI" --query location -o tsv)"
  echo "  exists in $AOAI_LOC"
else
  AOAI_LOC=""
  for loc in $AOAI_REGIONS; do
    for cand in ${MODEL:-$MODEL_CANDIDATES}; do
      # One value per query: az -o tsv prints list items on separate lines, which breaks shell math
      q="[?name.value=='OpenAI.GlobalStandard.$cand'] | [0]"
      limit="$(az cognitiveservices usage list -l "$loc" --query "$q.limit" -o tsv 2>/dev/null | head -1 | cut -d. -f1 || true)"
      used="$(az cognitiveservices usage list -l "$loc" --query "$q.currentValue" -o tsv 2>/dev/null | head -1 | cut -d. -f1 || true)"
      case "$limit" in ''|*[!0-9]*) limit=0;; esac; case "$used" in ''|*[!0-9]*) used=0;; esac
      if [ $((limit - used)) -ge "$CAPACITY" ]; then AOAI_LOC="$loc"; echo "  quota OK in $loc for $cand ($used/$limit used)"; break 2; fi
    done
    echo "  no free GlobalStandard quota in $loc for: ${MODEL:-$MODEL_CANDIDATES}"
  done
  [[ -n "$AOAI_LOC" ]] || die "No Azure OpenAI quota in: $AOAI_REGIONS. Request quota at https://aka.ms/oai/quotaincrease or set AOAI_REGIONS. Nothing was created."
  az cognitiveservices account create -g "$RG" -n "$AOAI" -l "$AOAI_LOC" --kind OpenAI --sku S0 \
    --custom-domain "$AOAI" --yes -o none
  ok "account created"
fi
AOAI_ID="$(az cognitiveservices account show -g "$RG" -n "$AOAI" --query id -o tsv)"
az resource update --ids "$AOAI_ID" --set properties.disableLocalAuth=true -o none
ok "API keys disabled (Entra-only)"
# Reuse a deployment from an earlier run, else pick a deployable (not deprecating) model version
EXISTING="$(az cognitiveservices account deployment list -g "$RG" -n "$AOAI" --query "[].name" -o tsv 2>/dev/null | head -1 || true)"
if [[ -n "$EXISTING" && -z "$MODEL" ]]; then
  MODEL="$EXISTING"; echo "  reusing deployment $MODEL"
else
  az cognitiveservices account list-models -g "$RG" -n "$AOAI" -o json > "$WORK/models.json" \
    || die "could not list models for $AOAI"
  PICK="$(python3 - "$WORK/models.json" "$MODEL_CANDIDATES" "$MODEL" "$MODEL_VERSION" <<'PY'
import datetime, json, sys
models, cands, want, want_ver = json.load(open(sys.argv[1])), sys.argv[2].split(), sys.argv[3], sys.argv[4]
order = [want] if want else cands
soon = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)).isoformat()
def alive(date):  # deprecation date absent, or more than 30 days away
    return not date or str(date) > soon
ok = []
for m in models:
    name, ver = m.get("name"), str(m.get("version", ""))
    if m.get("format") != "OpenAI" or name not in order or (want_ver and ver != want_ver):
        continue
    if "deprecat" in str(m.get("lifecycleStatus", "")).lower():
        continue
    if str((m.get("capabilities") or {}).get("chatCompletion", "")).lower() != "true":
        continue
    if not alive((m.get("deprecation") or {}).get("inference")):
        continue
    if not any(s.get("name") == "GlobalStandard" and alive(s.get("deprecationDate")) for s in (m.get("skus") or [])):
        continue
    ok.append((order.index(name), [-ord(c) for c in ver], name, ver))
if not ok:
    chat = sorted({f"{m.get('name')}:{m.get('version')}:{m.get('lifecycleStatus')}" for m in models
                   if str((m.get("capabilities") or {}).get("chatCompletion", "")).lower() == "true"})
    print("NONE " + " ".join(chat[:40]))
else:
    ok.sort(); print(f"{ok[0][2]} {ok[0][3]}")
PY
)"
  case "$PICK" in NONE*) die "No deployable model among: ${MODEL:-$MODEL_CANDIDATES}. Chat models offered here: ${PICK#NONE }. Re-run with MODEL=<name> MODEL_VERSION=<version>";; esac
  MODEL="${PICK%% *}"; MODEL_VERSION="${PICK##* }"
  echo "  model: $MODEL version $MODEL_VERSION (current, GlobalStandard, not deprecating)"
  if ! az cognitiveservices account deployment show -g "$RG" -n "$AOAI" --deployment-name "$MODEL" -o none 2>/dev/null; then
    az cognitiveservices account deployment create -g "$RG" -n "$AOAI" --deployment-name "$MODEL" \
      --model-name "$MODEL" --model-version "$MODEL_VERSION" --model-format OpenAI \
      --sku-name GlobalStandard --sku-capacity "$CAPACITY" -o none \
      || die "deployment of $MODEL $MODEL_VERSION failed (quota?) - re-run with MODEL=<other> or CAPACITY=<lower>"
  fi
fi
ok "deployment $MODEL ready"

# ---------------------------------------------------------------- API Management
log "API Management $APIM ($APIM_SKU) - first creation takes 5-30 minutes"
APIM_URL="$ARM/subscriptions/$SUB_ID/resourceGroups/$RG/providers/Microsoft.ApiManagement/service/$APIM"
if ! az rest --method get --url "$APIM_URL?api-version=$APIM_API_VER" -o none 2>/dev/null; then
  python3 - "$WORK/apim.json" "$LOCATION" "$APIM_SKU" <<'PY'
import json, sys
path, loc, sku = sys.argv[1:4]
json.dump({"location": loc, "sku": {"name": sku, "capacity": 1}, "identity": {"type": "SystemAssigned"},
           "properties": {"publisherEmail": "pfa-admin@example.com", "publisherName": "PharmaFieldAssist"}},
          open(path, "w"))
PY
  az rest --method put --url "$APIM_URL?api-version=$APIM_API_VER" --body @"$WORK/apim.json" -o none \
    || die "APIM create failed (try APIM_SKU=Developer, or another region via RG location)"
fi
for i in $(seq 1 120); do
  state="$(az rest --method get --url "$APIM_URL?api-version=$APIM_API_VER" --query properties.provisioningState -o tsv 2>/dev/null || true)"
  [[ "$state" == "Succeeded" ]] && break
  [[ "$state" == "Failed" ]] && die "APIM provisioning failed - check the portal"
  printf '  provisioning: %s (%s min)\r' "${state:-pending}" "$((i / 2))"; sleep 30
done
[[ "$state" == "Succeeded" ]] || die "APIM still provisioning after 60 minutes - re-run this script later (it resumes)"
GATEWAY="$(az rest --method get --url "$APIM_URL?api-version=$APIM_API_VER" --query properties.gatewayUrl -o tsv)"
APIM_PID="$(az rest --method get --url "$APIM_URL?api-version=$APIM_API_VER" --query identity.principalId -o tsv)"
echo; ok "gateway $GATEWAY"

log "Role: APIM managed identity -> Cognitive Services OpenAI User on $AOAI"
if ! az role assignment list --assignee "$APIM_PID" --scope "$AOAI_ID" --role "Cognitive Services OpenAI User" --query "[0].id" -o tsv | grep -q .; then
  az role assignment create --assignee-object-id "$APIM_PID" --assignee-principal-type ServicePrincipal \
    --role "Cognitive Services OpenAI User" --scope "$AOAI_ID" -o none
fi
ok "only APIM's identity can call Azure OpenAI"

log "Gateway API, policies, telemetry"
python3 - "$WORK" "$AOAI" "$TENANT" "$UAMI_CID" "$TPM_LIMIT" "$APPI_CS" "$APIM_URL" \
  "$MAX_OUTPUT_TOKENS" "$DAILY_TOKEN_QUOTA" "$MONTHLY_TOKEN_QUOTA" <<'PY'
import json, re, sys
import xml.etree.ElementTree as ET
work, aoai, tenant, uami, tpm, appi_cs, apim_url, max_out, daily, monthly = sys.argv[1:11]
apim_id = apim_url.replace("https://management.azure.com", "")
w = lambda name, obj: json.dump(obj, open(f"{work}/{name}.json", "w"))
w("logger", {"properties": {"loggerType": "applicationInsights", "isBuffered": True,
                             "credentials": {"connectionString": appi_cs}}})
w("api", {"properties": {"displayName": "Azure OpenAI (PharmaFieldAssist)", "path": "aoai/openai",
                         "protocols": ["https"], "serviceUrl": f"https://{aoai}.openai.azure.com/openai",
                         "subscriptionRequired": False, "apiType": "http"}})
w("op", {"properties": {"displayName": "Chat completions", "method": "POST",
                        "urlTemplate": "/deployments/{deployment-id}/chat/completions",
                        "templateParameters": [{"name": "deployment-id", "type": "string", "required": True}]}})
policy = f"""<policies>
  <inbound>
    <base />
    <validate-azure-ad-token tenant-id="{tenant}" header-name="Authorization" failed-validation-httpcode="401"
        failed-validation-error-message="Only the PharmaFieldAssist API identity may call this gateway">
      <client-application-ids><application-id>{uami}</application-id></client-application-ids>
      <audiences><audience>https://cognitiveservices.azure.com</audience></audiences>
    </validate-azure-ad-token>
    <!-- Every response: JSON object only, at most {max_out} generated tokens. Overrides whatever the caller sent. -->
    <set-body>@{{
      var body = context.Request.Body.As<JObject>(preserveContent: true);
      int cap = {max_out};
      int asked = body["max_tokens"] != null ? body["max_tokens"].Value<int>() : cap;
      body["max_tokens"] = Math.Min(asked, cap);
      body.Remove("max_completion_tokens");
      body.Remove("stream");
      body["response_format"] = new JObject(new JProperty("type", "json_object"));
      return body.ToString();
    }}</set-body>
    <azure-openai-token-limit counter-key="pfa-api" tokens-per-minute="{tpm}"
        token-quota="{daily}" token-quota-period="Daily" estimate-prompt-tokens="true"
        remaining-tokens-header-name="x-pfa-remaining-tokens" />
    <azure-openai-token-limit counter-key="pfa-api-month" token-quota="{monthly}" token-quota-period="Monthly"
        estimate-prompt-tokens="true" remaining-quota-tokens-header-name="x-pfa-remaining-month-tokens" />
    <azure-openai-emit-token-metric namespace="pfa-llm">
      <dimension name="Client" value="pfa-api" />
      <dimension name="Deployment" value='@(context.Request.MatchedParameters["deployment-id"])' />
    </azure-openai-emit-token-metric>
    <authentication-managed-identity resource="https://cognitiveservices.azure.com" />
  </inbound>
  <backend><base /></backend>
  <outbound>
    <base />
    <set-header name="x-pfa-gateway" exists-action="override"><value>apim</value></set-header>
  </outbound>
  <on-error><base /></on-error>
</policies>"""
# Well-formedness check (policy expressions contain C# generics like As<JObject>, so blank them first)
ET.fromstring(re.sub(r"@\{.*?\}</set-body>", "x</set-body>", policy, flags=re.S))
w("policy", {"properties": {"format": "rawxml", "value": policy}})
w("diag", {"properties": {"loggerId": f"{apim_id}/loggers/appi", "alwaysLog": "allErrors", "metrics": True,
                          "verbosity": "information", "sampling": {"samplingType": "fixed", "percentage": 100}}})
PY
put() { az rest --method put --url "$APIM_URL/$1?api-version=$APIM_API_VER" --body @"$WORK/$2.json" -o none || die "APIM: failed to configure $1"; }
put "loggers/appi" logger;                          ok "App Insights logger"
put "apis/aoai" api;                                ok "API aoai/openai -> $AOAI"
put "apis/aoai/operations/chat-completions" op;     ok "operation POST /deployments/{id}/chat/completions"
put "apis/aoai/policies/policy" policy;             ok "policies: validate-azure-ad-token, JSON-only + max ${MAX_OUTPUT_TOKENS} output tokens, ${TPM_LIMIT} TPM, quota ${DAILY_TOKEN_QUOTA}/day ${MONTHLY_TOKEN_QUOTA}/month, managed-identity auth"
put "apis/aoai/diagnostics/applicationinsights" diag; ok "token metrics -> Application Insights (namespace pfa-llm)"

# ---------------------------------------------------------------- app: new image + gateway-only config
log "API container app -> gateway only"
TAG="$(date -u +%Y%m%d%H%M%S)"
az acr build -r "$ACR" -t "pfa-api:$TAG" --target runtime backend --no-logs -o none || die "API image build failed: az acr task logs -r $ACR"
ok "image pfa-api:$TAG"
OLD_REV="$(az containerapp show -g "$RG" -n "$API_APP" --query properties.latestRevisionName -o tsv)"
az containerapp update -g "$RG" -n "$API_APP" --image "$ACR.azurecr.io/pfa-api:$TAG" \
  --set-env-vars "PFA_AZURE_OPENAI_ENDPOINT=$GATEWAY/aoai" "PFA_AZURE_OPENAI_DEPLOYMENT=$MODEL" \
                 "PFA_AZURE_OPENAI_API_VERSION=$API_VERSION_AOAI" "PFA_LLM_GATEWAY_ONLY=true" "PFA_LLM_REQUIRED=true" "PFA_LLM_MAX_OUTPUT_TOKENS=$MAX_OUTPUT_TOKENS" "PFA_APP_VERSION=$TAG" \
  --remove-env-vars PFA_AZURE_OPENAI_API_KEY -o none 2>/dev/null \
  || az containerapp update -g "$RG" -n "$API_APP" --image "$ACR.azurecr.io/pfa-api:$TAG" \
       --set-env-vars "PFA_AZURE_OPENAI_ENDPOINT=$GATEWAY/aoai" "PFA_AZURE_OPENAI_DEPLOYMENT=$MODEL" \
                      "PFA_AZURE_OPENAI_API_VERSION=$API_VERSION_AOAI" "PFA_LLM_GATEWAY_ONLY=true" "PFA_LLM_REQUIRED=true" "PFA_LLM_MAX_OUTPUT_TOKENS=$MAX_OUTPUT_TOKENS" "PFA_APP_VERSION=$TAG" -o none
NEW_REV="$(az containerapp show -g "$RG" -n "$API_APP" --query properties.latestRevisionName -o tsv)"
echo "  waiting for revision $NEW_REV to be healthy"
for i in $(seq 1 60); do
  h="$(az containerapp revision show -g "$RG" -n "$API_APP" --revision "$NEW_REV" --query properties.healthState -o tsv 2>/dev/null || true)"
  [[ "$h" == "Healthy" ]] && break; sleep 5
done
if [[ "$h" != "Healthy" ]]; then
  az containerapp logs show -g "$RG" -n "$API_APP" --revision "$NEW_REV" --tail 30 --type console 2>/dev/null || true
  die "New revision not healthy - traffic stays on $OLD_REV (no impact). Logs above."
fi
az containerapp ingress traffic set -g "$RG" -n "$API_APP" --revision-weight "$NEW_REV=100" -o none
ok "traffic 100% -> $NEW_REV (rollback: az containerapp ingress traffic set -g $RG -n $API_APP --revision-weight $OLD_REV=100)"

# ---------------------------------------------------------------- cost budget (alerts)
log "Cost budget \$${BUDGET_USD}/month on $RG"
[[ -n "$BUDGET_EMAIL" ]] || BUDGET_EMAIL="$(az account show --query user.name -o tsv)"
python3 - "$WORK/budget.json" "$BUDGET_USD" "$BUDGET_EMAIL" <<'PY'
import datetime, json, sys
path, amount, email = sys.argv[1:4]
start = datetime.date.today().replace(day=1).isoformat() + "T00:00:00Z"
note = lambda pct, kind: {"enabled": True, "operator": "GreaterThanOrEqualTo", "threshold": pct,
                          "thresholdType": kind, "contactEmails": [email]}
json.dump({"properties": {"category": "Cost", "amount": float(amount), "timeGrain": "Monthly",
                          "timePeriod": {"startDate": start, "endDate": "2030-12-01T00:00:00Z"},
                          "notifications": {"actual50": note(50, "Actual"), "actual80": note(80, "Actual"),
                                            "actual100": note(100, "Actual"), "forecast100": note(100, "Forecasted")}}},
          open(path, "w"))
PY
if az rest --method put --url "$ARM/subscriptions/$SUB_ID/resourceGroups/$RG/providers/Microsoft.Consumption/budgets/pfa-budget?api-version=2023-05-01" \
     --body @"$WORK/budget.json" -o none 2>/dev/null; then
  ok "budget \$${BUDGET_USD}/month: email $BUDGET_EMAIL at 50/80/100% actual and 100% forecast"
else
  warn "could not create the budget (some subscription types do not support Budgets) - set one in Cost Management"
fi

# ---------------------------------------------------------------- verify
log "Verification"
code="$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{"messages":[]}' \
  "$GATEWAY/aoai/openai/deployments/$MODEL/chat/completions?api-version=$API_VERSION_AOAI")"
[[ "$code" == "401" ]] && ok "gateway rejects callers without the app identity (401)" || warn "expected 401 from gateway without token, got $code"
[[ "$(az resource show --ids "$AOAI_ID" --query properties.disableLocalAuth -o tsv)" == "true" ]] && ok "Azure OpenAI keys disabled" || warn "disableLocalAuth not true"

WEB_FQDN="$(az containerapp show -g "$RG" -n "$WEB_APP" --query properties.configuration.ingress.fqdn -o tsv 2>/dev/null || true)"
if [[ -n "$WEB_FQDN" && -f .demo-credentials ]]; then
  U="$(grep '^user=' .demo-credentials | cut -d= -f2)"; P="$(grep '^password=' .demo-credentials | cut -d= -f2)"
  sleep 10
  TOKEN="$(curl -s -u "$U:$P" -X POST "https://$WEB_FQDN/api/auth/demo-token" -H 'Content-Type: application/json' -d '{"persona":"rep"}' \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')"
  curl -s -u "$U:$P" -X POST "https://$WEB_FQDN/api/chat" -H "X-PFA-Token: $TOKEN" -H 'Content-Type: application/json' \
    -d '{"message":"What is the starting dose of Cardivex?"}' > "$WORK/turn.json"
  python3 - "$WORK/turn.json" <<'PY' || warn "end-to-end check did not confirm the gateway path (see above)"
import json, sys
t = json.load(open(sys.argv[1]))
if "route" not in t:
    print("  unexpected response:", json.dumps(t)[:300]); sys.exit(1)
llm = [c for c in t["tool_calls"] if c["tool"] == "llm.complete"]
print(f"  route: {t['route']['method']}  agent: {t['agent']}  citations: {len(t['citations'])}")
for c in llm:
    print(f"  llm.complete: {c['status']}  {c['identity']}  {c['detail']}")
print("  answer:", t["answer"][:160].replace("\n", " "))
ok = t["route"]["method"] == "llm" and llm and llm[0]["status"] == "ok" and "APIM gateway" in llm[0]["detail"]
print("  \033[1;32mOK\033[0m model answer served through the APIM gateway" if ok else "  [warn] not via gateway yet")
sys.exit(0 if ok else 1)
PY
else
  warn "web app or .demo-credentials not found - skipped end-to-end check"
fi

log "DONE - LLM traffic: container app -> APIM ($GATEWAY) -> Azure OpenAI ($AOAI, keys disabled)"
echo "    App:      https://${WEB_FQDN:-<web>}   (login in .demo-credentials)"
echo "    APIM:     https://portal.azure.com/#@/resource$( echo "$APIM_URL" | sed "s#$ARM##" )/apim-apis"
echo "    Metrics:  App Insights appi-pfa > Metrics > namespace pfa-llm (Total Tokens by Client)"
echo "    Caps:     ${MAX_OUTPUT_TOKENS} output tokens/call, ${TPM_LIMIT} TPM, ${DAILY_TOKEN_QUOTA} tokens/day, ${MONTHLY_TOKEN_QUOTA} tokens/month (403 past quota)"
echo "    Fixed:    APIM $APIM_SKU is about \$150/month whether used or not - remove everything: bash scripts/pfa-azure-down.sh"
