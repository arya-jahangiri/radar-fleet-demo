# Security Model

This repo includes a small, runnable trust-boundary model for radar telemetry
publishing. It is meant to show how the backend treats publisher identity at
ingest time, while keeping production device provisioning outside the demo.

## Local Ingest

Local ingest uses HMAC mode by default. Run setup to create a local `.env` with
a generated `NODE_HMAC_SECRET`:

```bash
scripts/setup.sh
```

On native Windows PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
```

Each ingest request then carries:

```text
X-Device-Id: <node-id-or-site-id>
X-Timestamp: <unix-seconds>
X-Signature: v1=<hmac-sha256>
```

The signature covers the device id, timestamp, and JSON body hash. The backend
checks:

- required signing headers are present;
- the signed device id matches `payload.node_id` for direct-node summaries;
- the signed device id matches `payload.site_id` for legacy home summaries;
- the timestamp is inside the accepted clock-skew window;
- the HMAC matches the configured node secret.

For per-node demo secrets, set `NODE_HMAC_SECRETS` to a JSON object:

```bash
export NODE_HMAC_SECRETS='{"home-0000-n1":"secret-for-n1"}'
```

`NODE_HMAC_SECRET` remains available as a shared fallback for quick local runs
and for the legacy home-summary comparison path.

For isolated local debugging only, HMAC can be disabled explicitly:

```text
INGEST_AUTH_MODE=off
```

## AWS Boundary

The optional AWS path sends the same `pi-node-summary.v1` contract through AWS
IoT Core Basic Ingest, an IoT Rule, Lambda, and DynamoDB. The local AWS publisher
signs IoT Data Plane HTTPS publish requests with AWS Signature Version 4, and
Terraform scopes publish permissions to the Basic Ingest topic shape.

That is enough to make the infrastructure mapping concrete, but it is not a
full production device-identity system.

## Hosted Dashboard Read Boundary

The hosted dashboard uses a public AWS Lambda Function URL behind a Cloudflare
Worker. It returns synthetic latest-state snapshots only; it does not expose raw
radar frames, device credentials, write operations, or private operator data.

This is acceptable for a public demo, but it is not a production access-control
model. A production dashboard would add authenticated operators, rate limiting,
audit logging, tenant scoping, and private network or API-gateway controls.

## Production Device Identity

A production Raspberry Pi fleet would move identity and authentication to AWS
IoT device primitives, for example:

- per-device X.509 certificates or fleet provisioning;
- topic-scoped IoT policies bound to device identity;
- secure private-key storage on the Pi;
- certificate rotation and revocation;
- separate operator, service, and device roles;
- audit logs and alerting for rejected or anomalous publishes.

Those controls are deliberately not mocked here. A fake certificate authority or
checked-in device keys would add ceremony without proving safe provisioning, and
Terraform-managed private keys can easily leak through local files or state.

## What This Demo Proves

The local HMAC mode proves that the backend has an explicit publisher identity
check before accepting telemetry payloads. The stream-level protections then
take over: idempotency keys, monotonic sequence gates, bounded queues, and
node-to-home coalescing.

It does not prove physical device hardening, production key management, clinical
security requirements, or a complete privacy programme.
