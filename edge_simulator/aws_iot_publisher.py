"""Publish the hosted home slice to AWS IoT Core Basic Ingest.

This intentionally avoids a local aws-cli/boto3 dependency. It signs the IoT
Data Plane HTTPS Publish request with AWS Signature Version 4 using either
environment credentials or a static profile in ~/.aws/credentials.

Example:
    .venv/bin/python edge_simulator/aws_iot_publisher.py \
      --endpoint a1234567890-ats.iot.eu-west-2.amazonaws.com \
      --region eu-west-2 \
      --rule-name imperial_radar_demo_summary_ingest \
      --homes 100 \
      --mirror-backend-url http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import asyncio
import configparser
import hashlib
import hmac
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from domain.home import Home  # noqa: E402
from shared.device_auth import canonical_payload_bytes, signing_headers_for_payload  # noqa: E402


SERVICE = "iotdevicegateway"


@dataclass(frozen=True)
class AwsCredentials:
    access_key: str
    secret_key: str
    session_token: str | None = None


def build_basic_ingest_topic(rule_name: str, site_id: str, node_id: str | None = None) -> str:
    if node_id:
        return f"$aws/rules/{rule_name}/imperial-demo/sites/{site_id}/nodes/{node_id}/summary"
    return f"$aws/rules/{rule_name}/imperial-demo/sites/{site_id}/supervisor/summary"


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _load_profile_credentials(profile: str) -> AwsCredentials | None:
    paths = [Path.home() / ".aws" / "credentials", Path.home() / ".aws" / "config"]
    parser = configparser.ConfigParser()
    parser.read([str(p) for p in paths])
    sections = [profile, f"profile {profile}"] if profile != "default" else ["default"]
    for section in sections:
        if not parser.has_section(section):
            continue
        access_key = parser.get(section, "aws_access_key_id", fallback="")
        secret_key = parser.get(section, "aws_secret_access_key", fallback="")
        token = parser.get(section, "aws_session_token", fallback=None)
        if access_key and secret_key:
            return AwsCredentials(access_key, secret_key, token)
    return None


def load_credentials(profile: str | None = None) -> AwsCredentials:
    access_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    token = os.environ.get("AWS_SESSION_TOKEN")
    if access_key and secret_key:
        return AwsCredentials(access_key, secret_key, token)

    selected_profile = profile or os.environ.get("AWS_PROFILE")
    if selected_profile:
        creds = _load_profile_credentials(selected_profile)
        if creds:
            return creds

    raise RuntimeError(
        "AWS credentials not found. Export AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY "
        "or use a static profile in ~/.aws/credentials. SSO profiles need exported "
        "temporary credentials for this lightweight publisher."
    )


def sign_iot_publish_request(
    *,
    endpoint: str,
    region: str,
    topic: str,
    payload: bytes,
    credentials: AwsCredentials,
) -> urllib.request.Request:
    host = endpoint.removeprefix("https://").removeprefix("http://").rstrip("/")
    encoded_topic = urllib.parse.quote(topic, safe="/")
    canonical_uri = f"/topics/{encoded_topic}"
    canonical_query = "qos=1"
    url = f"https://{host}{canonical_uri}?{canonical_query}"
    now = datetime.now(UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(payload).hexdigest()

    headers: dict[str, str] = {
        "content-type": "application/octet-stream",
        "host": host,
        "x-amz-date": amz_date,
    }
    if credentials.session_token:
        headers["x-amz-security-token"] = credentials.session_token

    signed_header_names = sorted(headers)
    canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in signed_header_names)
    signed_headers = ";".join(signed_header_names)
    canonical_request = "\n".join([
        "POST",
        canonical_uri,
        canonical_query,
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
    headers["Authorization"] = (
        "AWS4-HMAC-SHA256 "
        f"Credential={credentials.access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return urllib.request.Request(url, data=payload, headers=headers, method="POST")


class HostedAwsPublisher:
    def __init__(
        self,
        *,
        endpoint: str,
        region: str,
        rule_name: str,
        n_homes: int,
        site_offset: int,
        speed: float,
        post_period_s: float,
        tick_s: float,
        credentials: AwsCredentials | None,
        mirror_backend_url: str | None,
        node_auth_secret: str | None,
        dry_run: bool,
        max_publishes: int | None,
        topology: str,
    ) -> None:
        self.endpoint = endpoint
        self.region = region
        self.rule_name = rule_name
        self.speed = speed
        self.post_period_s = post_period_s
        self.tick_s = tick_s
        self.credentials = credentials
        self.mirror_backend_url = mirror_backend_url.rstrip("/") if mirror_backend_url else None
        self.node_auth_secret = node_auth_secret
        self.dry_run = dry_run
        self.max_publishes = max_publishes
        self.topology = topology
        rng = random.Random(42)
        self.homes = [
            Home(site_id=f"home-{i + site_offset:04d}", seed=i + site_offset,
                 sim_clock_s=rng.uniform(0, 86400))
            for i in range(n_homes)
        ]
        self.stride = max(1, round(post_period_s / tick_s))
        self.published = 0
        self.failures = 0
        self.mirrored = 0

    async def run(self) -> None:
        async with httpx.AsyncClient(timeout=5) as client:
            tick = 0
            while True:
                t0 = time.perf_counter()
                self._advance(self.tick_s * self.speed)
                batch = self.homes[tick % self.stride :: self.stride]
                for home in batch:
                    payloads = [home.summary()] if self.topology == "home-summary" else home.node_summaries()
                    for summary in payloads:
                        await self._publish_one(client, summary)
                        if self.max_publishes and self.published >= self.max_publishes:
                            return
                tick += 1
                elapsed = time.perf_counter() - t0
                await asyncio.sleep(max(0.0, self.tick_s - elapsed))

    def _advance(self, dt_sim: float) -> None:
        n = max(1, math.ceil(dt_sim / 0.5))
        step = dt_sim / n
        for _ in range(n):
            for home in self.homes:
                home.tick(step)

    async def _publish_one(self, client: httpx.AsyncClient, summary: dict[str, Any]) -> None:
        node_id = summary.get("node_id") if summary.get("schema_version") == "pi-node-summary.v1" else None
        topic = build_basic_ingest_topic(self.rule_name, summary["site_id"], node_id)
        payload = canonical_payload_bytes(summary)
        if self.dry_run:
            if self.published < 5:
                print(f"DRY {topic} {len(payload)} bytes")
            self.published += 1
        else:
            assert self.credentials is not None
            request = sign_iot_publish_request(
                endpoint=self.endpoint,
                region=self.region,
                topic=topic,
                payload=payload,
                credentials=self.credentials,
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    if response.status >= 300:
                        raise urllib.error.HTTPError(
                            request.full_url, response.status, response.reason, response.headers, None
                        )
                self.published += 1
            except Exception as exc:
                self.failures += 1
                print(f"AWS publish failed for {summary['site_id']}: {exc}", file=sys.stderr)

        if self.mirror_backend_url:
            path = "/api/ingest/node" if summary.get("schema_version") == "pi-node-summary.v1" else "/api/ingest"
            headers = {"content-type": "application/json"}
            headers.update(
                signing_headers_for_payload(
                    summary,
                    body=payload,
                    default_secret=self.node_auth_secret,
                )
            )
            try:
                r = await client.post(f"{self.mirror_backend_url}{path}", content=payload, headers=headers)
                r.raise_for_status()
                self.mirrored += 1
            except httpx.HTTPError as exc:
                print(f"Mirror ingest failed for {summary['site_id']}: {exc}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Publish hosted radar home summaries to AWS IoT Core")
    p.add_argument("--endpoint", required=True, help="AWS IoT data endpoint, without https://")
    p.add_argument("--region", default=os.environ.get("AWS_REGION", "eu-west-2"))
    p.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    p.add_argument("--rule-name", required=True, help="IoT topic rule name used in $aws/rules/<rule>")
    p.add_argument("--homes", type=int, default=100, help="number of hosted homes")
    p.add_argument("--site-offset", type=int, default=0, help="first numeric hosted site id")
    p.add_argument("--speed", type=float, default=6.0, help="sim-time multiplier")
    p.add_argument("--post-period", type=float, default=10.0, help="seconds between each hosted home's summaries")
    p.add_argument("--tick", type=float, default=0.5, help="real seconds per loop")
    p.add_argument(
        "--topology",
        choices=("direct-node", "home-summary"),
        default=os.environ.get("SIM_TOPOLOGY", "direct-node"),
        help="publish independent Pi-node reports or legacy per-home summaries",
    )
    p.add_argument("--mirror-backend-url", help="optional local backend URL for the operator dashboard")
    p.add_argument(
        "--node-auth-secret",
        default=os.environ.get("NODE_HMAC_SECRET"),
        help="optional HMAC secret for signed local mirror ingest; can also use NODE_HMAC_SECRET",
    )
    p.add_argument("--dry-run", action="store_true", help="print topics without calling AWS")
    p.add_argument("--max-publishes", type=int, help="stop after N publishes, useful for smoke tests")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    creds = None if args.dry_run else load_credentials(args.profile)
    pub = HostedAwsPublisher(
        endpoint=args.endpoint,
        region=args.region,
        rule_name=args.rule_name,
        n_homes=args.homes,
        site_offset=args.site_offset,
        speed=args.speed,
        post_period_s=args.post_period,
        tick_s=args.tick,
        credentials=creds,
        mirror_backend_url=args.mirror_backend_url,
        node_auth_secret=args.node_auth_secret,
        dry_run=args.dry_run,
        max_publishes=args.max_publishes,
        topology=args.topology,
    )
    print(
        f"Publishing {args.homes} hosted homes with {args.topology} payloads "
        f"-> AWS IoT Basic Ingest rule {args.rule_name} ({args.region}); "
        f"mirror={args.mirror_backend_url or 'off'} dry_run={args.dry_run}"
    )
    try:
        asyncio.run(pub.run())
    except KeyboardInterrupt:
        pass
    finally:
        print(f"Stopped. published={pub.published} mirrored={pub.mirrored} failures={pub.failures}")
