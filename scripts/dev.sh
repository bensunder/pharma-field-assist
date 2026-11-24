#!/usr/bin/env bash
# Run the demo without Docker: API on :8000, UI on :5173 (Vite proxies /api -> :8000). Ctrl-C stops both.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

cd "$ROOT/backend"
[[ -d .venv ]] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --port 8000 --reload &
API_PID=$!
trap 'kill $API_PID 2>/dev/null || true' EXIT

cd "$ROOT/frontend"
[[ -d node_modules ]] || npm ci --no-audit --no-fund
npx vite --port 5173 --host
