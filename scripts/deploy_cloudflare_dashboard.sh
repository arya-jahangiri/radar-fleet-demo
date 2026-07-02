#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CLOUDFLARE_DIR="${DEMO_DIR}/infra/cloudflare"
OUTPUTS="${DEMO_DIR}/infra/aws/.aws-demo-outputs.json"
PYTHON_BIN="${DEMO_DIR}/.venv/bin/python"
WRANGLER_BIN="${DEMO_DIR}/node_modules/.bin/wrangler"
export WRANGLER_LOG_PATH="${DEMO_DIR}/.wrangler/logs"

. "${SCRIPT_DIR}/load_env.sh"
load_demo_env "${DEMO_DIR}/.env"

if [[ ! -f "${OUTPUTS}" ]]; then
  echo "Missing ${OUTPUTS}. Run scripts/aws_demo_up.sh --yes first." >&2
  exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Run scripts/setup.sh first." >&2
  exit 1
fi

if [[ ! -x "${WRANGLER_BIN}" ]]; then
  echo "Cloudflare Wrangler is required. Run scripts/setup.sh first." >&2
  exit 1
fi

mkdir -p "${WRANGLER_LOG_PATH}"

SNAPSHOT_URL="$("${PYTHON_BIN}" - "${OUTPUTS}" <<'PY'
import json
import sys
with open(sys.argv[1], "r", encoding="utf-8") as f:
    data = json.load(f)
print(data["dashboard_snapshot_url"]["value"])
PY
)"

"${SCRIPT_DIR}/stage_cloudflare_dashboard.sh"

pushd "${CLOUDFLARE_DIR}" >/dev/null
printf "%s" "${SNAPSHOT_URL}" | "${WRANGLER_BIN}" secret put AWS_SNAPSHOT_URL
"${WRANGLER_BIN}" deploy
popd >/dev/null
