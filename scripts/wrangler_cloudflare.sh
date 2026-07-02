#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CLOUDFLARE_DIR="${DEMO_DIR}/infra/cloudflare"
WRANGLER_BIN="${DEMO_DIR}/node_modules/.bin/wrangler"

if [[ ! -x "${WRANGLER_BIN}" ]]; then
  echo "Cloudflare Wrangler is required. Run scripts/setup.sh first." >&2
  exit 1
fi

export WRANGLER_LOG_PATH="${DEMO_DIR}/.wrangler/logs"
mkdir -p "${WRANGLER_LOG_PATH}"

pushd "${CLOUDFLARE_DIR}" >/dev/null
"${WRANGLER_BIN}" "$@"
popd >/dev/null
