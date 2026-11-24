#!/usr/bin/env bash
# Throttling drill: temporarily lower the APIM token-per-minute limit so a couple of chat questions
# trigger a real 429 at the gateway (visible in App Insights > Failures), then restore it.
#   bash scripts/pfa-throttle-drill.sh on    # limit -> 1000 TPM
#   bash scripts/pfa-throttle-drill.sh off   # limit -> 20000 TPM (normal)
# ASCII-only, bash 3.2 safe (macOS). Only the tokens-per-minute value is changed; every other
# policy (identity check, JSON + token cap, daily/monthly quotas) stays exactly as deployed.
set -euo pipefail
MODE="${1:-}"; RG="${RG:-pfa-demo-rg}"; API_VER="2024-05-01"
case "$MODE" in on) NEW="${DRILL_TPM:-1000}";; off) NEW="${NORMAL_TPM:-20000}";; *) echo "usage: $0 on|off"; exit 1;; esac
export PATH="$HOME/.local/bin:$HOME/.local/pfa-bin:$PATH"
command -v az >/dev/null || { echo "[fail] az not found"; exit 1; }
SUB="$(az account show --query id -o tsv)"
APIM="$(az resource list -g "$RG" --resource-type Microsoft.ApiManagement/service --query "[0].name" -o tsv)"
[ -n "$APIM" ] || { echo "[fail] no APIM in $RG"; exit 1; }
URL="https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.ApiManagement/service/$APIM/apis/aoai/policies/policy"
TMP="$(mktemp -d)"
az rest --method get --url "$URL?format=rawxml&api-version=$API_VER" -o json > "$TMP/cur.json"
python3 - "$TMP/cur.json" "$TMP/new.json" "$NEW" <<'PY'
import json, re, sys
cur, out, new = sys.argv[1], sys.argv[2], sys.argv[3]
value = json.load(open(cur))["properties"]["value"]
updated, n = re.subn(r'tokens-per-minute="\d+"', 'tokens-per-minute="%s"' % new, value)
if n != 1:
    sys.exit("expected exactly one tokens-per-minute attribute, found %d - nothing changed" % n)
json.dump({"properties": {"format": "rawxml", "value": updated}}, open(out, "w"))
print("  tokens-per-minute -> %s" % new)
PY
az rest --method put --url "$URL?api-version=$API_VER" --body @"$TMP/new.json" -o none
rm -rf "$TMP"
if [ "$MODE" = "on" ]; then
  echo "DRILL ON: ask 2-3 product questions in the app within one minute (e.g. 'What drugs interact with Cardivex?')."
  echo "Expect: answers still arrive (extractive fallback); trace shows llm.complete failed (throttled)."
  echo "App Insights > Failures shows 429 on 'aoai;rev=1 - chat-completions' after 2-5 minutes."
  echo "When done: bash scripts/pfa-throttle-drill.sh off"
else
  echo "DRILL OFF: normal limit restored (${NEW} tokens per minute)."
fi
