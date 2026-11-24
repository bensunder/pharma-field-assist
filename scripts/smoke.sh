#!/usr/bin/env bash
# Post-deploy smoke test against a revision URL. Exercises probes + an authenticated chat turn.
# In Entra environments SMOKE_TOKEN is a client-credentials token for the smoke-test app registration
# (assigned the FieldRep app role in non-prod only), fetched by the pipeline from Key Vault.
set -euo pipefail
BASE="${1%/}"
check() { local code; code="$(curl -s -o /dev/null -w '%{http_code}' "$@")"; echo "$code"; }

[[ "$(check "$BASE/health/live")" == 200 ]] || { echo "live probe failed"; exit 1; }
[[ "$(check "$BASE/health/ready")" == 200 ]] || { echo "ready probe failed"; exit 1; }

TOKEN="${SMOKE_TOKEN:-}"
if [[ -z "$TOKEN" ]]; then  # demo-mode environments
  TOKEN="$(curl -s -X POST "$BASE/api/auth/demo-token" -H 'Content-Type: application/json' -d '{"persona":"rep"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')"
fi
RESP="$(curl -s -X POST "$BASE/api/chat" -H "X-PFA-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"message":"What is the starting dose of Cardivex?"}')"
echo "$RESP" | python3 -c '
import sys, json
raw = sys.stdin.read()
try:
    r = json.loads(raw)
except ValueError:
    sys.exit("chat did not return JSON: " + raw[:300])
assert r["agent"] == "product_qa", r
assert r["citations"], "no citations"
assert any(v["rule_id"] == "CMP-103" for v in r["verdicts"]), "fair balance check missing"
print("smoke ok: audit seq", r["audit_seq"], "latency_ms", r["latency_ms"])
'
