# Cost Model

This demo is designed to stay cheap when the hosted path is used deliberately.
The main cost driver is not storage volume; it is request count.

## Default Safe Posture

The checked-in AWS defaults are conservative:

- cloud-side simulator disabled unless explicitly enabled;
- S3 cold copy disabled by default;
- S3 lifecycle expiry set to 1 day when cold copy is enabled;
- DynamoDB latest-state TTL set to 1 day;
- CloudWatch log retention set to 3 days;
- AWS IoT Basic Ingest used so reserved-topic publishes avoid normal IoT
  messaging charges.

## Current Cloud Simulator Rate

The AWS Lambda cloud simulator is triggered by EventBridge once per minute. Each
invocation publishes:

```text
cloud_simulator_home_count * nodes_per_home
```

With the example settings:

```text
100 homes * 5 nodes = 500 node summaries per minute
500 * 60 = 30,000 node summaries per hour
720,000 node summaries per day
```

`cloud_simulator_period_seconds` controls the sequence/idempotency window used
inside the payloads; it does not make EventBridge run more frequently.

## Local Publisher Rate

The local AWS publisher uses `--post-period` as a real cadence:

```text
(homes * nodes_per_home) / post_period_seconds
```

For `100` homes, `5` nodes, and `--post-period 5`, that is about `100` node
summaries per second. For `--post-period 1`, it is about `500` node summaries per
second.

Default local scripts:

- `scripts/run_local_demo.sh`: `100` homes, `5` nodes, `POST_PERIOD=5` -> about
  `100` local node summaries per second, with no AWS spend.
- `scripts/run_hybrid_demo.sh`: hosted publisher `HOSTED_POST_PERIOD=10` -> about
  `50` AWS node summaries per second for `100` hosted homes.
- AWS cloud simulator: one EventBridge invocation per minute -> about `8.3`
  AWS node summaries per second on average for `100` homes and `5` nodes.

## Why S3 Cold Copy Is Optional

Lifecycle expiry reduces how long objects remain stored, but it does not remove
the request cost of creating those objects. When publishing frequent tiny
summaries, per-object writes can dominate the storage cost.

Keep `S3_COLD_COPY_ENABLED=false` for a multi-day hosted demo. Turn it on only
when showing the cold-audit-copy architecture explicitly:

```bash
S3_COLD_COPY_ENABLED=true scripts/aws_demo_up.sh --yes
```

## Hosted Dashboard Rate

The Cloudflare dashboard stream is a display channel, not the ingest firehose.
The Worker reads the AWS dashboard snapshot endpoint through a short cache and
emits Server-Sent Events at a default display cadence of 3 seconds:

```text
CLOUDFLARE_STREAM_INTERVAL_MS=3000
CLOUDFLARE_SNAPSHOT_CACHE_SECONDS=3
```

Increasing the display cadence mostly increases dashboard read traffic. The
larger spend lever is still how often AWS generates and writes node summaries
into DynamoDB.

## Recommended Few-Day Demo Setting

For a few days of hosted activity, prefer:

```text
HOSTED_HOMES=100
CLOUD_SIMULATOR_ENABLED=true
CLOUD_SIMULATOR_HOME_COUNT=100
CLOUDFLARE_STREAM_INTERVAL_MS=3000
CLOUDFLARE_SNAPSHOT_CACHE_SECONDS=3
S3_COLD_COPY_ENABLED=false
S3_RETENTION_DAYS=1
LATEST_STATE_TTL_SECONDS=86400
```

That keeps data flowing in AWS even when every laptop is off. The public
dashboard can be deployed with:

```bash
scripts/deploy_cloudflare_dashboard.sh
```

The local dashboard can still be started later with:

```bash
scripts/run_cloud_demo.sh
```

Use `scripts/aws_demo_pause.sh --yes` to stop ingestion without deleting
resources, and `scripts/aws_demo_down.sh --yes` to destroy the hosted slice.
