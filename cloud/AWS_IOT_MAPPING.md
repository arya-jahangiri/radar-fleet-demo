# AWS IoT mapping

This file sketches the AWS IoT mapping implemented in `../infra/aws`.

## Local demo to AWS shape

```text
Raspberry Pi radar node
  FMCW acquisition + local reduction
  |
  | MQTT QoS 1 with device identity
  v
AWS IoT Core
  |
  v
IoT Rules Engine
  |
  +--> S3 cold node summaries
  |
  +--> Lambda or backend ingest
         idempotency + ordering
         |
         v
      DynamoDB or Timestream
         latest node state + time series
         |
      backend coalescer
         home summaries + alert transitions
         |
         v
      Operator dashboard

AWS IoT Device Shadow
  desired config -> edge container

CloudWatch
  logs + metrics + alarms
```

## What the current prototype already exercises

- Edge service publishes Raspberry Pi node summaries independently of the browser.
- Backend ingestion is asynchronous: HTTP requests enqueue summaries, and a worker processes them.
- Idempotency uses `{node_id}:{boot_id}:{seq}` for direct-node reports.
- Node streams use `seq` as the sequence authority, so stale late summaries cannot move the dashboard backwards.
- Pi publishers buffer locally during connectivity loss, then replay with the original idempotency keys.
- Desired configuration is versioned and pulled by nodes, matching the mental model of AWS IoT Device Shadow.
- The dashboard receives backend-owned snapshots over WebSocket rather than talking directly to radar nodes.

## What AWS would add

- mTLS device identity through AWS IoT Core certificates.
- Per-node least-privilege IoT policies.
- MQTT QoS 1 delivery and retained/shadow state where appropriate.
- IoT Rules for routing reduced summaries to hot storage, cold storage, and processing.
- CloudWatch metrics and alarms for queue depth, ingestion failures, stale summaries, duplicate summaries, node silence, and replay backlog.
- Infrastructure as code through Terraform once the target account, naming, retention, and security boundaries are agreed.

## Implemented optional AWS mapping

The Terraform stack in `infra/aws` provisions a deliberately cheap direct-node
mapping:

```text
Raspberry Pi node summaries
  optional local publisher or disabled-by-default Lambda load generator
  |
  | AWS IoT data-plane publish API
  v
AWS IoT Core Basic Ingest
  |
  v
IoT Rule: summary_ingest
  |
  +--> S3 cold copy
  |
  +--> Lambda hot ingest
         sequence gate
         |
         v
      DynamoDB latest node state
         |
      hosted dashboard snapshot API
         |
      Cloudflare Worker dashboard
         |
      Operator dashboard display
```

The default development path is local and direct-node. The AWS path is opt-in:
`scripts/aws_demo_up.sh --yes` provisions the IaC slice without starting a large
scheduled fleet. `scripts/run_hybrid_demo.sh` can publish the same direct-node
payloads to AWS IoT and mirror them into the local dashboard. The Lambda load
generator can also run inside AWS so the public dashboard does not depend on a
laptop.

Cost-control posture:

- no EC2/ECS/RDS runtime;
- AWS IoT Basic Ingest reserved topics to avoid normal pub/sub messaging charges;
- DynamoDB on-demand and Lambda/EventBridge only run while the optional load generator is enabled;
- S3 cold-copy is disabled by default and retention defaults to 1 day when enabled;
- `scripts/aws_demo_pause.sh` disables the simulator schedule and IoT rule;
- `scripts/aws_demo_down.sh --yes` destroys the hosted fleet.

## Topic contract

The concrete MQTT topic proposal lives in:

- `cloud/topic_contracts.json`

The local demo uses HTTP for ease of local setup and inspection. The AWS path
uses AWS IoT Core Basic Ingest topics. The local publisher can send the same
direct-node packets to AWS using HTTPS Publish signed with SigV4 by
`edge_simulator/aws_iot_publisher.py`; the optional Lambda load generator exists
only for cloud-side smoke testing.
