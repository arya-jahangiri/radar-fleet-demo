#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
INFRA_DIR="${DEMO_DIR}/infra/aws"
YES=0
EXTRA_ARGS=()

. "${SCRIPT_DIR}/load_env.sh"
load_demo_env "${DEMO_DIR}/.env"

for arg in "$@"; do
  case "${arg}" in
    --yes) YES=1 ;;
    *) EXTRA_ARGS+=("${arg}") ;;
  esac
done

AWS_REGION_VALUE="${AWS_REGION:-eu-west-2}"
HOSTED_HOMES_VALUE="${HOSTED_HOMES:-1}"
CLOUD_SIMULATOR_HOME_COUNT_VALUE="${CLOUD_SIMULATOR_HOME_COUNT:-${HOSTED_HOMES_VALUE}}"
S3_COLD_COPY_ENABLED_VALUE="${S3_COLD_COPY_ENABLED:-false}"
S3_RETENTION_DAYS_VALUE="${S3_RETENTION_DAYS:-1}"
LATEST_STATE_TTL_SECONDS_VALUE="${LATEST_STATE_TTL_SECONDS:-86400}"
DASHBOARD_SNAPSHOT_MAX_ITEMS_VALUE="${DASHBOARD_SNAPSHOT_MAX_ITEMS:-10000}"
DASHBOARD_CORS_ALLOW_ORIGIN_VALUE="${DASHBOARD_CORS_ALLOW_ORIGIN:-*}"

TF_ARGS=(
  -var "aws_region=${AWS_REGION_VALUE}"
  -var "hosted_home_count=${HOSTED_HOMES_VALUE}"
  -var "iot_rule_enabled=false"
  -var "cloud_simulator_enabled=false"
  -var "cloud_simulator_home_count=${CLOUD_SIMULATOR_HOME_COUNT_VALUE}"
  -var "s3_cold_copy_enabled=${S3_COLD_COPY_ENABLED_VALUE}"
  -var "s3_retention_days=${S3_RETENTION_DAYS_VALUE}"
  -var "latest_state_ttl_seconds=${LATEST_STATE_TTL_SECONDS_VALUE}"
  -var "dashboard_snapshot_max_items=${DASHBOARD_SNAPSHOT_MAX_ITEMS_VALUE}"
  -var "dashboard_cors_allow_origin=${DASHBOARD_CORS_ALLOW_ORIGIN_VALUE}"
)

if [[ -n "${AWS_PROFILE:-}" ]]; then
  TF_ARGS+=(-var "aws_profile=${AWS_PROFILE}")
fi

if [[ "${YES}" == "1" ]]; then
  TF_ARGS+=(-auto-approve)
fi

terraform -chdir="${INFRA_DIR}" init
if [[ "${#EXTRA_ARGS[@]}" -gt 0 ]]; then
  terraform -chdir="${INFRA_DIR}" apply "${TF_ARGS[@]}" "${EXTRA_ARGS[@]}"
else
  terraform -chdir="${INFRA_DIR}" apply "${TF_ARGS[@]}"
fi
terraform -chdir="${INFRA_DIR}" output -json > "${INFRA_DIR}/.aws-demo-outputs.json"

echo "AWS IoT ingest and cloud simulator paused. Use aws_demo_resume.sh to re-enable or aws_demo_down.sh to destroy."
