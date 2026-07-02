#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ ! -f "${DEMO_DIR}/.env" || ! -d "${DEMO_DIR}/.venv" ]]; then
  echo "Run scripts/setup.sh first." >&2
  exit 1
fi

. "${SCRIPT_DIR}/load_env.sh"
load_demo_env "${DEMO_DIR}/.env"

BACKEND_URL="${BACKEND_URL:-http://127.0.0.1:8765}"
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8765}"
HOMES="${HOMES:-100}"
POST_PERIOD="${POST_PERIOD:-5}"
SIM_SPEED="${SIM_SPEED:-6}"

pids=()
cleanup() {
  for pid in "${pids[@]}"; do
    if kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT INT TERM

echo "Starting signed local backend on ${BACKEND_URL}"
"${DEMO_DIR}/.venv/bin/python" -m uvicorn backend.main:app \
  --app-dir "${DEMO_DIR}" \
  --host "${BACKEND_HOST}" \
  --port "${BACKEND_PORT}" \
  --no-access-log &
pids+=("$!")

sleep 1

echo "Starting ${HOMES} homes with HMAC-signed direct-node ingest"
"${DEMO_DIR}/.venv/bin/python" "${DEMO_DIR}/edge_simulator/simulator.py" \
  --homes "${HOMES}" \
  --backend-url "${BACKEND_URL}" \
  --speed "${SIM_SPEED}" \
  --post-period "${POST_PERIOD}" \
  --topology direct-node &
pids+=("$!")

echo
echo "Open ${BACKEND_URL}/"
echo "Press Ctrl-C here to stop local processes."

wait
