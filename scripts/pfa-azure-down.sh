#!/usr/bin/env bash
# Delete EVERYTHING the demo created on Azure and purge soft-deleted resources so nothing keeps billing.
# ASCII-only, bash 3.2 safe (macOS). Usage: bash scripts/pfa-azure-down.sh   (add YES=1 to skip the prompt)
set -euo pipefail
RG="${RG:-pfa-demo-rg}"
export PATH="$HOME/.local/bin:$HOME/.local/pfa-bin:$PATH"
command -v az >/dev/null || { echo "[fail] az not found"; exit 1; }
SUB="$(az account show --query id -o tsv)"
if ! az group show -n "$RG" -o none 2>/dev/null; then echo "Resource group $RG does not exist - nothing billing there."; exit 0; fi
echo "Resources in $RG:"; az resource list -g "$RG" --query "[].{name:name,type:type}" -o table
if [[ "${YES:-0}" != "1" ]]; then
  printf 'Delete ALL of the above permanently? type yes: '; read -r a; [[ "$a" == "yes" ]] || { echo "aborted"; exit 1; }
fi
KV="$(az keyvault list -g "$RG" --query "[].name" -o tsv)"
AOAI="$(az cognitiveservices account list -g "$RG" --query "[].{n:name,l:location}" -o tsv)"
APIM="$(az resource list -g "$RG" --resource-type Microsoft.ApiManagement/service --query "[].{n:name,l:location}" -o tsv)"
echo "Deleting resource group $RG (5-15 minutes)..."
az group delete -n "$RG" --yes
# Soft-deleted resources keep names reserved; purge them so a re-deploy starts clean
for k in $KV; do az keyvault purge -n "$k" -o none 2>/dev/null && echo "purged key vault $k" || true; done
echo "$AOAI" | while read -r n l; do [[ -n "${n:-}" ]] && az cognitiveservices account purge -g "$RG" -n "$n" -l "$l" -o none 2>/dev/null && echo "purged openai $n" || true; done
echo "$APIM" | while read -r n l; do [[ -n "${n:-}" ]] && az rest --method delete --url "https://management.azure.com/subscriptions/$SUB/providers/Microsoft.ApiManagement/locations/$l/deletedservices/$n?api-version=2024-05-01" -o none 2>/dev/null && echo "purged apim $n" || true; done
echo "Done. Nothing from this demo is left billing in subscription $SUB."
