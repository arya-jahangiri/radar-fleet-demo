"""Mirror latest AWS DynamoDB cloud state into the local operator dashboard.

The simulator can run through AWS IoT and DynamoDB. This bridge is a local
display adapter: it scans the latest-state table and posts new cloud-owned
summaries into the existing FastAPI dashboard.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aws_iot_publisher import AwsCredentials, load_credentials
from shared.device_auth import canonical_payload_bytes, signing_headers_for_payload


SERVICE = "dynamodb"


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _dynamodb_request(
    *,
    credentials: AwsCredentials,
    region: str,
    target: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    host = f"dynamodb.{region}.amazonaws.com"
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    now = dt.datetime.now(dt.UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()
    headers = {
        "content-type": "application/x-amz-json-1.0",
        "host": host,
        "x-amz-date": amz_date,
        "x-amz-target": target,
    }
    if credentials.session_token:
        headers["x-amz-security-token"] = credentials.session_token

    signed_header_names = sorted(headers)
    canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in signed_header_names)
    signed_headers = ";".join(signed_header_names)
    canonical_request = "\n".join([
        "POST",
        "/",
        "",
        canonical_headers,
        signed_headers,
        payload_hash,
    ])
    credential_scope = f"{date_stamp}/{region}/{SERVICE}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signing_key = _sign(
        _sign(_sign(_sign(f"AWS4{credentials.secret_key}".encode("utf-8"), date_stamp), region), SERVICE),
        "aws4_request",
    )
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["authorization"] = (
        "AWS4-HMAC-SHA256 "
        f"Credential={credentials.access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    req = urllib.request.Request(f"https://{host}/", data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _scan_payloads_sigv4(credentials: AwsCredentials, region: str, table_name: str) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    last_key: dict[str, Any] | None = None
    while True:
        payload: dict[str, Any] = {
            "TableName": table_name,
            "ProjectionExpression": "#payload",
            "ExpressionAttributeNames": {"#payload": "payload"},
        }
        if last_key:
            payload["ExclusiveStartKey"] = last_key
        data = _dynamodb_request(
            credentials=credentials,
            region=region,
            target="DynamoDB_20120810.Scan",
            payload=payload,
        )
        for item in data.get("Items", []):
            raw = item.get("payload", {}).get("S")
            if not raw:
                continue
            summaries.append(json.loads(raw))
        last_key = data.get("LastEvaluatedKey")
        if not last_key:
            return summaries


def _scan_payloads_boto3(region: str, table_name: str) -> list[dict[str, Any]]:
    import boto3

    client = boto3.client("dynamodb", region_name=region)
    summaries: list[dict[str, Any]] = []
    scan_args: dict[str, Any] = {
        "TableName": table_name,
        "ProjectionExpression": "#payload",
        "ExpressionAttributeNames": {"#payload": "payload"},
    }
    while True:
        data = client.scan(**scan_args)
        for item in data.get("Items", []):
            raw = item.get("payload", {}).get("S")
            if raw:
                summaries.append(json.loads(raw))
        last_key = data.get("LastEvaluatedKey")
        if not last_key:
            return summaries
        scan_args["ExclusiveStartKey"] = last_key


def _post_summary(backend_url: str, summary: dict[str, Any]) -> None:
    path = "/api/ingest/node" if summary.get("schema_version") == "pi-node-summary.v1" else "/api/ingest"
    body = canonical_payload_bytes(summary)
    headers = {"content-type": "application/json"}
    headers.update(signing_headers_for_payload(summary, body=body))
    req = urllib.request.Request(
        f"{backend_url.rstrip('/')}{path}",
        data=body,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        if response.status >= 300:
            raise RuntimeError(f"backend returned HTTP {response.status}")


def run(args: argparse.Namespace) -> None:
    credentials: AwsCredentials | None = None
    use_boto3 = False
    try:
        credentials = load_credentials(args.profile)
    except RuntimeError:
        use_boto3 = True

    seen_by_stream: dict[str, str] = {}
    while True:
        try:
            if use_boto3:
                summaries = _scan_payloads_boto3(args.region, args.table_name)
            else:
                assert credentials is not None
                summaries = _scan_payloads_sigv4(credentials, args.region, args.table_name)
            mirrored = 0
            for summary in summaries:
                site_id = str(summary.get("site_id", ""))
                node_id = str(summary.get("node_id", "_home"))
                key = str(summary.get("idempotency_key", ""))
                stream_id = f"{site_id}:{node_id}"
                if not site_id or not key or seen_by_stream.get(stream_id) == key:
                    continue
                _post_summary(args.backend_url, summary)
                seen_by_stream[stream_id] = key
                mirrored += 1
            print(
                f"cloud bridge mirrored {mirrored} new summaries "
                f"from {len(summaries)} DynamoDB items; tracked={len(seen_by_stream)}",
                flush=True,
            )
        except (urllib.error.URLError, RuntimeError, json.JSONDecodeError) as exc:
            print(f"cloud bridge error: {exc}", file=sys.stderr, flush=True)
        if args.once:
            return
        time.sleep(args.poll_period)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mirror AWS latest-home-state into the local dashboard")
    parser.add_argument("--table-name", required=True)
    parser.add_argument("--region", default="eu-west-2")
    parser.add_argument("--backend-url", default="http://127.0.0.1:8765")
    parser.add_argument("--profile")
    parser.add_argument("--poll-period", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    run(parse_args())
