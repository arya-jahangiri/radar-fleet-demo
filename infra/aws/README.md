# AWS hosted fleet

This Terraform stack provisions a cost-controlled AWS IoT mapping for the
Imperial radar demo:

```text
Raspberry Pi radar-node summaries
  -> optional local publisher or disabled-by-default Lambda load generator
  -> AWS IoT Core Basic Ingest
  -> IoT Rule
  -> S3 cold audit copy
  -> Lambda hot ingest
  -> DynamoDB latest-node-state table
  -> Lambda Function URL dashboard snapshot
  -> Cloudflare Worker dashboard
```

It deliberately avoids always-on EC2/ECS. The only runtime cost while the
publisher is stopped is small storage/registry state; `scripts/aws_demo_down.sh`
destroys those resources too.

## Why Basic Ingest

AWS documents Basic Ingest as a way to send device data to IoT Rule actions
without normal publish/subscribe messaging cost. It uses reserved topics:

```text
$aws/rules/<rule-name>/<ordinary-topic>
```

The ordinary direct-node topic here is:

```text
imperial-demo/sites/<site-id>/nodes/<node-id>/summary
```

## Apply

From the demo root:

```bash
scripts/aws_demo_up.sh --yes
```

The script writes Terraform outputs to:

```text
infra/aws/.aws-demo-outputs.json
```

## Pause, Resume, Destroy

Pause ingestion but keep resources:

```bash
scripts/aws_demo_pause.sh --yes
```

Resume ingestion:

```bash
scripts/aws_demo_resume.sh --yes
```

Destroy everything when finished:

```bash
scripts/aws_demo_down.sh --yes
```

## Cloud run

After `aws_demo_up.sh`, run:

```bash
scripts/run_cloud_demo.sh
```

Default shape:

- 1 AWS IoT Thing registry marker, kept deliberately small so the stack applies
  quickly in a fresh account.
- The scheduled cloud load generator is disabled by default; set
  `CLOUD_SIMULATOR_ENABLED=true` only for an AWS-side smoke test or short hosted
  run.
- S3 cold copy is disabled by default; set `S3_COLD_COPY_ENABLED=true` only when
  demonstrating the audit-copy path.
- `run_hybrid_demo.sh` publishes direct `pi-node-summary.v1` payloads from the
  local simulator to AWS IoT and mirrors them into the local dashboard.
- The local laptop runs only the operator dashboard and a small bridge that
  mirrors DynamoDB latest-state into that dashboard.

Open:

```text
http://127.0.0.1:8765/
```

## Public dashboard

The stack outputs `dashboard_snapshot_url`, a public read-only endpoint over the
synthetic latest-state table. It is intended to sit behind the Cloudflare Worker
in `infra/cloudflare`, which serves the static dashboard and exposes
same-origin `/api/snapshot` and `/api/stream` routes.

From the demo root:

```bash
scripts/deploy_cloudflare_dashboard.sh
```

This keeps the live dashboard independent of any laptop. The endpoint returns
synthetic demo state only; a production system would add identity, throttling,
and stronger public API controls.

## Cost sanity

If enabled, the optional cloud load generator runs once per minute and publishes
one latest node summary per node each invocation:

```text
homes * nodes_per_home * 60 summaries/hour
```

Each summary triggers one IoT rule and the Lambda hot-ingest action. If S3 cold
copy is enabled, each summary also triggers one S3 action and one S3 object write.
Lifecycle expiry controls storage duration, not the number of write requests.
To cut spend during practice runs, pause or destroy the fleet when you are done:

```bash
scripts/aws_demo_pause.sh --yes
```

`aws_demo_pause.sh` disables both EventBridge simulator scheduling and IoT rule
processing without deleting resources. `aws_demo_down.sh --yes` removes the
fleet entirely.

## Credentials

Terraform can use standard AWS provider auth: `AWS_PROFILE`, environment
credentials, or another supported provider mechanism.

The cloud simulator runs inside AWS Lambda. The hosted dashboard reads through
the public snapshot endpoint and Cloudflare Worker. The local dashboard bridge
and fallback Python publisher do not depend on the `aws` CLI. They support:

- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / optional `AWS_SESSION_TOKEN`
- static shared credentials in `~/.aws/credentials`

For SSO profiles, export temporary environment credentials before running the
dashboard bridge or fallback publisher.

## Security Boundary

The lightweight local publisher signs AWS IoT Data Plane HTTPS publish requests
with AWS Signature Version 4. Terraform also scopes publish permission to the
Basic Ingest topic shape used by the direct-node contract.

For a physical Raspberry Pi fleet, device identity should move to AWS IoT device
provisioning: per-device certificates, topic-scoped IoT policies, key rotation,
revocation, and audit logging. This stack does not generate or store device
private keys.
