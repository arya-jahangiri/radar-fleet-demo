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
HOSTED_HOMES="${HOSTED_HOMES:-$(read_output hosted_home_count)}"
LOCAL_HOMES="${LOCAL_HOMES:-0}"
HOSTED_POST_PERIOD="${HOSTED_POST_PERIOD:-10}"
LOCAL_POST_PERIOD="${LOCAL_POST_PERIOD:-5}"
SIM_SPEED="${SIM_SPEED:-6}"
IOT_ENDPOINT="$(read_output iot_data_endpoint)"
IOT_RULE_NAME="$(read_output iot_rule_name)"
AWS_REGION_VALUE="$(read_output aws_region)"

pids=()
cleanup() {
  for pid in "${pids[@]}"; do
    if kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT INT TERM

echo "Starting local dashboard backend on ${BACKEND_URL}"
"${PYTHON_BIN}" -m uvicorn backend.main:app \
  --app-dir "${DEMO_DIR}" \
  --host "${BACKEND_HOST}" \
  --port "${BACKEND_PORT}" \
  --no-access-log &
pids+=("$!")

sleep 1

if [[ "${LOCAL_HOMES}" -gt 0 ]]; then
  echo "Starting ${LOCAL_HOMES} local-only homes at offset ${HOSTED_HOMES}"
  "${PYTHON_BIN}" "${DEMO_DIR}/edge_simulator/simulator.py" \
    --homes "${LOCAL_HOMES}" \
    --site-offset "${HOSTED_HOMES}" \
    --backend-url "${BACKEND_URL}" \
    --speed "${SIM_SPEED}" \
    --post-period "${LOCAL_POST_PERIOD}" \
    --topology direct-node &
  pids+=("$!")
fi

echo "Publishing ${HOSTED_HOMES} hosted homes to AWS IoT and mirroring them to the local dashboard"
"${PYTHON_BIN}" "${DEMO_DIR}/edge_simulator/aws_iot_publisher.py" \
  --endpoint "${IOT_ENDPOINT}" \
  --region "${AWS_REGION_VALUE}" \
  --rule-name "${IOT_RULE_NAME}" \
  --homes "${HOSTED_HOMES}" \
  --site-offset 0 \
  --speed "${SIM_SPEED}" \
  --post-period "${HOSTED_POST_PERIOD}" \
  --topology direct-node \
  --mirror-backend-url "${BACKEND_URL}" &
pids+=("$!")

echo
echo "Open ${BACKEND_URL}/"
echo "Local-only homes: ${LOCAL_HOMES}. Hosted publisher uses direct Pi-node summaries."
echo "Press Ctrl-C here to stop local processes. Use scripts/aws_demo_down.sh --yes to destroy AWS resources."

wait
