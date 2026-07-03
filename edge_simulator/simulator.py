"""Multi-home edge simulator: drives a fleet of Pi radar-node publishers.

Each Home (domain.home.Home) contains multiple simulated FMCW radar nodes. The
default topology posts each node's reduced Pi summary to the backend, which then
coalesces nodes into one home-level dashboard contract. The older per-home
Supervisor summary topology is still available with `--topology home-summary`.

Run (from repo root), e.g. 100 homes:
    .venv/bin/python edge_simulator/simulator.py \
        --homes 100 --backend-url http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import random
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from domain.home import Home  # noqa: E402
from shared.device_auth import canonical_payload_bytes, signing_headers_for_payload  # noqa: E402


class FleetSimulator:
    def __init__(self, backend_url: str, n_homes: int, *, site_offset: int, speed: float,
                 post_period_s: float, tick_s: float, topology: str,
                 max_posts: int | None, node_auth_secret: str | None) -> None:
        self.backend_url = backend_url.rstrip("/")
        self.tick_s = tick_s
        self.speed = speed
        self.post_period_s = post_period_s
        self.topology = topology
        self.node_auth_secret = node_auth_secret
        rng = random.Random(42)
        # stagger each home's start time across a 24 h day so the fleet shows a
        # realistic live mix of walking / stationary / absent / sleeping homes.
        self.homes = [
            Home(site_id=f"home-{i + site_offset:04d}", seed=i + site_offset,
                 sim_clock_s=rng.uniform(0, 86400))
            for i in range(n_homes)
        ]
        self.stride = max(1, round(post_period_s / tick_s))
        self.buffer: list[dict] = []          # failed summaries awaiting replay
        self.posted = 0
        self.failures = 0
        self.max_posts = max_posts

    async def run(self) -> None:
        limits = httpx.Limits(max_connections=64, max_keepalive_connections=64)
        async with httpx.AsyncClient(timeout=5, limits=limits) as client:
            tick = 0
            while True:
                t0 = time.perf_counter()
                self._advance(self.tick_s * self.speed)

                batch = self.homes[tick % self.stride :: self.stride]
                await self._post_batch(client, batch)
                await self._flush_buffer(client)
                if self.max_posts and self.posted >= self.max_posts:
                    return

                tick += 1
                elapsed = time.perf_counter() - t0
                await asyncio.sleep(max(0.0, self.tick_s - elapsed))

    def _advance(self, dt_sim: float) -> None:
        # sub-step so fast-forwarded motion stays smooth
        n = max(1, math.ceil(dt_sim / 0.5))
        step = dt_sim / n
        for _ in range(n):
            for home in self.homes:
                home.tick(step)

    async def _post_batch(self, client: httpx.AsyncClient, batch: list[Home]) -> None:
        sem = asyncio.Semaphore(64)

        async def one(payload: dict) -> None:
            async with sem:
                await self._post(client, payload)

        payloads: list[dict] = []
        for home in batch:
            if self.topology == "home-summary":
                payloads.append(home.summary())
            else:
                payloads.extend(home.node_summaries())
        await asyncio.gather(*(one(payload) for payload in payloads))

    async def _post(self, client: httpx.AsyncClient, payload: dict) -> None:
        path = "/api/ingest" if payload.get("schema_version") == "home-summary.v1" else "/api/ingest/node"
        body = canonical_payload_bytes(payload)
        headers = {"content-type": "application/json"}
        headers.update(
            signing_headers_for_payload(
                payload,
                body=body,
                default_secret=self.node_auth_secret,
            )
        )
        try:
            r = await client.post(f"{self.backend_url}{path}", content=body, headers=headers)
            r.raise_for_status()
            self.posted += 1
        except httpx.HTTPError:
            self.failures += 1
            if len(self.buffer) < 20000:            # bounded edge buffer
                self.buffer.append(payload)

    async def _flush_buffer(self, client: httpx.AsyncClient) -> None:
        if not self.buffer:
            return
        for _ in range(min(200, len(self.buffer))):  # drain gradually (replay)
            payload = self.buffer.pop(0)
            await self._post(client, payload)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Multi-home radar edge simulator")
    p.add_argument("--backend-url", default=os.environ.get("BACKEND_URL", "http://127.0.0.1:8765"))
    p.add_argument("--homes", type=int, default=100, help="number of homes to simulate")
    p.add_argument("--site-offset", type=int, default=0, help="first numeric site id, e.g. 100 -> home-0100")
    p.add_argument("--speed", type=float, default=6.0, help="sim-time multiplier")
    p.add_argument("--post-period", type=float, default=5.0, help="seconds between a home's summaries")
    p.add_argument("--tick", type=float, default=0.5, help="real seconds per loop")
    p.add_argument(
        "--topology",
        choices=("direct-node", "home-summary"),
        default=os.environ.get("SIM_TOPOLOGY", "direct-node"),
        help="publish independent Pi-node reports or legacy per-home summaries",
    )
    p.add_argument("--max-posts", type=int, help="stop after N successful publishes, useful for smoke tests")
    p.add_argument(
        "--node-auth-secret",
        default=os.environ.get("NODE_HMAC_SECRET"),
        help="optional HMAC secret for signed direct-node ingest; can also use NODE_HMAC_SECRET",
    )
    return p.parse_args()


if __name__ == "__main__":
    a = parse_args()
    sim = FleetSimulator(a.backend_url, a.homes, site_offset=a.site_offset, speed=a.speed,
                         post_period_s=a.post_period, tick_s=a.tick, topology=a.topology,
                         max_posts=a.max_posts, node_auth_secret=a.node_auth_secret)
    print(f"Simulating {a.homes} homes ({sum(len(h.nodes) for h in sim.homes)} Pi radar nodes) "
          f"with {a.topology} publishing -> {a.backend_url}")
    try:
        asyncio.run(sim.run())
    except KeyboardInterrupt:
        print()
    finally:
        print(f"Stopped. posted={sim.posted} failures={sim.failures}")
