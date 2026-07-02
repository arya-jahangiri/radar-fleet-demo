#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${DEMO_DIR}/.venv/bin/python"
OUTPUTS="${DEMO_DIR}/infra/aws/.aws-demo-outputs.json"

. "${SCRIPT_DIR}/load_env.sh"
load_demo_env "${DEMO_DIR}/.env"

if [[ ! -f "${OUTPUTS}" ]]; then
  echo "Missing ${OUTPUTS}. Run scripts/aws_demo_up.sh first." >&2
  exit 1
fi

if [[ ! -f "${DEMO_DIR}/.env" ]]; then
  echo "Run scripts/setup.sh first." >&2
  exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Run scripts/setup.sh first." >&2
  exit 1
fi

read_output() {
  local key="$1"
  "${PYTHON_BIN}" - "${OUTPUTS}" "${key}" <<'PY'
import json
import sys
with open(sys.argv[1], "r", encoding="utf-8") as f:
    data = json.load(f)
print(data[sys.argv[2]]["value"])
PY
}

BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8765}"
BACKEND_URL="http://${BACKEND_HOST}:${BACKEND_PORT}"
AWS_REGION_VALUE="$(read_output aws_region)"
TABLE_NAME="$(read_output latest_state_table_name)"
BRIDGE_POLL_PERIOD="${BRIDGE_POLL_PERIOD:-5}"

pids=()
cleanup() {
  for pid in "${pids[@]}"; do
    if kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT INT TERM

echo "Starting local operator dashboard on ${BACKEND_URL}"
"${PYTHON_BIN}" -m uvicorn backend.main:app \
  --app-dir "${DEMO_DIR}" \
  --host "${BACKEND_HOST}" \
  --port "${BACKEND_PORT}" \
  --no-access-log &
pids+=("$!")

sleep 1

echo "Mirroring cloud-owned DynamoDB latest state into the dashboard"
"${PYTHON_BIN}" "${DEMO_DIR}/edge_simulator/aws_cloud_dashboard_bridge.py" \
  --region "${AWS_REGION_VALUE}" \
  --table-name "${TABLE_NAME}" \
  --backend-url "${BACKEND_URL}" \
  --poll-period "${BRIDGE_POLL_PERIOD}" &
pids+=("$!")

echo
echo "Open ${BACKEND_URL}/"
echo "Telemetry source: AWS Lambda -> AWS IoT Core Basic Ingest -> IoT Rule -> DynamoDB."
echo "Press Ctrl-C here to stop the local display bridge. Use scripts/aws_demo_pause.sh --yes or scripts/aws_demo_down.sh --yes when finished."

wait
