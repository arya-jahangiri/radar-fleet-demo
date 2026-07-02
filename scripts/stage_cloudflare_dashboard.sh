#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DIST_DIR="${DEMO_DIR}/infra/cloudflare/dist"

mkdir -p "${DIST_DIR}/assets"
cp "${DEMO_DIR}/index.html" "${DIST_DIR}/index.html"
cp "${DEMO_DIR}/styles.css" "${DIST_DIR}/styles.css"
cp "${DEMO_DIR}/app.js" "${DIST_DIR}/app.js"

echo "Staged Cloudflare dashboard assets in ${DIST_DIR}"
