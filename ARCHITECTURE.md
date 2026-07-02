# Architecture

This prototype models a direct-publishing biomedical radar fleet. Each simulated
Raspberry Pi-like radar node emits a compact `pi-node-summary.v1` packet. The
backend applies stream-level reliability rules, coalesces node reports into a
home-level `home-summary.v1` view, and broadcasts operator snapshots at a fixed
rate.

## Hosted Runtime

```text
Pi radar-node summaries
  -> AWS IoT Core Basic Ingest
  -> IoT Rule
  -> Lambda hot ingest
  -> DynamoDB latest node state
  -> Lambda Function URL snapshot API
  -> Cloudflare Worker static dashboard + SSE
```

The hosted path is the primary demo path. AWS keeps the latest synthetic node
state, while Cloudflare serves the public dashboard and streams cached snapshots.
This keeps the visible demo independent of any laptop.

## Local Development Runtime

```text
Pi radar-node simulators
  -> pi-node-summary.v1
  -> FastAPI ingest
  -> bounded async queue
  -> node coalescer
  -> fleet state + alert transitions
  -> WebSocket dashboard snapshots
```

The local path exists as a deterministic development harness for inspecting the
same payload contract, coalescing logic, and dashboard states without cloud
credentials.

## Ingest Guarantees

- `node_id + boot_id + seq` forms the direct-node idempotency boundary.
- Optional HMAC headers bind local ingest requests to `payload.node_id` or
  `payload.site_id`.
- Each node stream has a monotonic sequence gate, so stale late packets do not
  move state backwards.
- The ingest queue is bounded; callers receive a retryable failure when the
  backend is overloaded.
- Replayed packets keep their original idempotency keys.
- Dashboard broadcasting is fixed-rate and decoupled from packet arrival rate.

## Coalescing Model

The dashboard should not treat every node packet as an operator event. The
backend keeps latest node reports per home and emits a compact home snapshot:

- current activity state and zone;
- node reporting status;
- alert counts and alert transitions;
- rolling gait, vitals, sleep, and trend summaries;
- ingest health metrics such as queue depth, latency, duplicate drops, and stale
  drops.

The legacy per-home summary path is still available with:

```bash
python edge_simulator/simulator.py --topology home-summary
```

That mode is useful for comparing direct-node publishing with a gateway or
Supervisor pattern.

## AWS Mapping

The Terraform stack maps the same reduced telemetry contract onto AWS:

```text
Pi radar node
  -> AWS IoT Core Basic Ingest
  -> IoT Rule
  +-> optional S3 cold copy
  +-> Lambda hot ingest
      -> DynamoDB latest node state
      -> Lambda Function URL snapshot API
      -> Cloudflare Worker dashboard / SSE
```

The AWS path deliberately avoids always-on compute. The scheduled load generator
is disabled by default; local publishing into AWS is available through
`edge_simulator/aws_iot_publisher.py` and `scripts/run_hybrid_demo.sh`.

When the cloud simulator is enabled, AWS keeps generating synthetic node
summaries. Cloudflare serves the dashboard and streams cached snapshots from the
AWS latest-state endpoint. The local dashboard bridge remains useful for
development and debugging.

## Production Gaps

This repo intentionally leaves out:

- raw radar signal processing;
- clinical validation;
- physical device provisioning;
- production certificate lifecycle management;
- long-term history, replay, and retention design;
- production observability, privacy, and security controls.
