# Cloudflare hosted dashboard

This Worker removes the laptop from the live demo path:

```text
Cloudflare Worker static assets + SSE
  -> AWS Lambda Function URL snapshot endpoint
  -> DynamoDB latest node state
```

The Worker serves the same dashboard files as the local demo. `/api/snapshot`
briefly caches the AWS snapshot response, and `/api/stream` emits Server-Sent
Events at a small display cadence. The data source is still AWS-hosted telemetry;
Cloudflare is the public web edge, not the simulator.

Default public URL after deployment:

```text
https://radar-care-fleet-demo.jahangiri-arya.workers.dev/
```

## Deploy

From the demo root:

```bash
npm install
npm run cloudflare:check
scripts/deploy_cloudflare_dashboard.sh
```

`deploy_cloudflare_dashboard.sh` reads `infra/aws/.aws-demo-outputs.json`, sets
the `AWS_SNAPSHOT_URL` Worker secret from the Terraform
`dashboard_snapshot_url` output, and runs the project-local Wrangler binary.

Wrangler must be authenticated before deployment. Either run an interactive
login in a terminal:

```bash
npm run cloudflare:login
npm run cloudflare:whoami
```

and follow Wrangler's login prompt if needed, or set `CLOUDFLARE_API_TOKEN` in
your shell or local `.env`. For token-based deployment, use an account token
that can edit Workers scripts.

To deploy manually:

```bash
cd infra/cloudflare
printf "%s" "<dashboard_snapshot_url>" | WRANGLER_LOG_PATH="../../.wrangler/logs" ../../node_modules/.bin/wrangler secret put AWS_SNAPSHOT_URL
WRANGLER_LOG_PATH="../../.wrangler/logs" ../../node_modules/.bin/wrangler deploy
```

The public page should connect to same-origin `/api/stream`. If the cloud
snapshot endpoint is unavailable, the dashboard reports cloud unavailability
instead of switching to local preview data.

## GitHub Actions Deployment

The CI workflow can also deploy the Worker after checks pass. Manual deployment
is available from the Actions tab. Automatic deployment from `main` is disabled
until the repository variable `CLOUDFLARE_DEPLOY_ENABLED` is set to `true`.

Required repository secrets:

```text
CLOUDFLARE_API_TOKEN
CLOUDFLARE_ACCOUNT_ID
AWS_SNAPSHOT_URL
```

`AWS_SNAPSHOT_URL` should be the Lambda Function URL snapshot endpoint from the
AWS Terraform output `dashboard_snapshot_url`. The workflow stages static
assets, writes `AWS_SNAPSHOT_URL` as a Worker secret, and deploys with the
project-local Wrangler version from `package-lock.json`.
