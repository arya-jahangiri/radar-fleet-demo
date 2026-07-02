#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${DEMO_DIR}"

if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  echo "Python 3 is required. Install Python, then rerun scripts/setup.sh." >&2
  exit 1
fi

if [[ ! -d ".venv" ]]; then
  "${PYTHON_BIN}" -m venv .venv
fi

VENV_PYTHON=".venv/bin/python"
"${VENV_PYTHON}" -m pip install --upgrade pip
"${VENV_PYTHON}" -m pip install -r requirements.txt

if command -v npm >/dev/null 2>&1; then
  npm install
else
  echo "npm was not found; Cloudflare deployment requires Node.js/npm." >&2
fi

if [[ ! -f ".env" ]]; then
  SECRET="$("${VENV_PYTHON}" -c "import secrets; print(secrets.token_urlsafe(32))")"
  {
    echo "INGEST_AUTH_MODE=hmac"
    echo "NODE_HMAC_SECRET=${SECRET}"
    echo "BACKEND_URL=http://127.0.0.1:8765"
    echo ""
    echo "# Local simulator defaults"
    echo "HOMES=100"
    echo "POST_PERIOD=5"
    echo "SIM_SPEED=6"
    echo ""
    echo "# Optional AWS demo defaults"
    echo "AWS_REGION=eu-west-2"
    echo "HOSTED_HOMES=100"
    echo "CLOUD_SIMULATOR_ENABLED=false"
    echo "CLOUD_SIMULATOR_HOME_COUNT=100"
    echo "CLOUD_SIMULATOR_PERIOD_SECONDS=60"
    echo "S3_COLD_COPY_ENABLED=false"
    echo "S3_RETENTION_DAYS=1"
    echo "LATEST_STATE_TTL_SECONDS=86400"
    echo "DASHBOARD_SNAPSHOT_MAX_ITEMS=10000"
    echo "DASHBOARD_CORS_ALLOW_ORIGIN=*"
    echo "CLOUDFLARE_STREAM_INTERVAL_MS=3000"
    echo "CLOUDFLARE_SNAPSHOT_CACHE_SECONDS=3"
  } > .env
  chmod 600 .env 2>/dev/null || true
else
  echo "Keeping existing .env"
fi

echo
echo "Setup complete."
echo "Run the local signed demo with:"
echo "  scripts/run_local_demo.sh"
echo "Validate the Cloudflare Worker bundle with:"
echo "  npm run cloudflare:check"
