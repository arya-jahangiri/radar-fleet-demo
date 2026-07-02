"""Cloud tier: ingest of Pi-node or per-home summaries + fleet aggregation.

The local demo defaults to direct publishing: each Raspberry Pi radar node
publishes reduced node summaries directly. The backend coalesces those node
reports into the stable `home-summary.v1` dashboard contract. The legacy
per-home Supervisor summary endpoint remains available because a gateway can
still be a useful topology when upstream traffic needs to be reduced.

The cloud:
  - ingests summaries asynchronously (idempotency + sequence gating),
  - coalesces direct Pi-node reports into per-home state,
  - maintains fleet state + rollups (states, alerts, throughput, latency),
  - stores only derived summaries, never raw range-Doppler frames,
  - broadcasts a fleet snapshot to the dashboard at a FIXED rate (decoupled from
    ingest rate, so cost is O(homes) per tick, not O(messages) -- no O(N^2)).

The stores are in-memory behind small boundaries; each field maps cleanly onto
tables a production service would keep for latest node state, latest home state,
and alert transitions.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT_DIR = Path(__file__).resolve().parents[1]

from shared.device_auth import (  # noqa: E402
    DeviceAuthConfigError,
    DeviceAuthError,
    node_auth_mode,
    verify_signed_payload,
)

QUEUE_MAX_SIZE = 8192
IDEMPOTENCY_CACHE_SIZE = 16384
BROADCAST_HZ = 3.0
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
GAIT_REVIEW_DROP_MPS_12W = -0.06
GAIT_WATCH_DROP_MPS_12W = -0.03
GAIT_IMPROVE_GAIN_MPS_12W = 0.03


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def gait_trend_bucket(home: dict[str, Any]) -> str:
    series = (((home.get("trends") or {}).get("ranges") or {}).get("12w") or {}).get("gait_speed_mps") or []
    if len(series) < 2:
        return "unknown"
    delta = float(series[-1]) - float(series[0])
    if delta <= GAIT_REVIEW_DROP_MPS_12W:
        return "review"
    if delta <= GAIT_WATCH_DROP_MPS_12W:
        return "watch"
    if delta >= GAIT_IMPROVE_GAIN_MPS_12W:
        return "improving"
    return "stable"


class BoundedKeySet:
    """Fixed-size FIFO membership set: rejects near-term duplicate retries while
    keeping memory flat under a continuous stream."""

    def __init__(self, maxlen: int) -> None:
        self._maxlen = maxlen
        self._keys: set[str] = set()
        self._order: deque[str] = deque()

    def __contains__(self, key: str) -> bool:
        return key in self._keys

    def add(self, key: str) -> None:
        if key in self._keys:
            return
        self._keys.add(key)
        self._order.append(key)
        while len(self._order) > self._maxlen:
            self._keys.discard(self._order.popleft())


@dataclass
class QueuedSummary:
    summary: dict[str, Any]
    queued_at: float


class FleetStore:
    """In-memory fleet state. Field names mirror the would-be PostgreSQL tables:
    `homes` (latest per-site derived status) and `events` (alert log)."""

    def __init__(self) -> None:
        self.homes: dict[str, dict[str, Any]] = {}
        self.last_seq: dict[str, int] = {}
        self.events: deque[dict[str, Any]] = deque(maxlen=60)
        self.active_alert_keys: dict[str, set[tuple[Any, ...]]] = {}
        self.trend_buckets: dict[str, str] = {}
        self.seen = BoundedKeySet(IDEMPOTENCY_CACHE_SIZE)
        # counters
        self.received = 0
        self.processed = 0
        self.duplicates = 0
        self.stale = 0
        self.rejected = 0
        self._latencies: deque[float] = deque(maxlen=2000)
        self._ingest_times: deque[float] = deque(maxlen=4000)
        self.started_at = time.time()

    # --- ingest path ---------------------------------------------------------
    def accept(self, summary: dict[str, Any]) -> dict[str, Any]:
        site = summary.get("site_id")
        key = summary.get("idempotency_key")
        if not site or not key:
            raise HTTPException(400, "summary requires site_id and idempotency_key")
        if key in self.seen:
            self.duplicates += 1
            return {"accepted": False, "duplicate": True}
        self.seen.add(key)
        self.received += 1
        return {"accepted": True, "duplicate": False}

    def process(self, summary: dict[str, Any], latency_ms: float) -> None:
        site = summary["site_id"]
        seq = int(summary.get("seq", 0))
        boot = summary.get("boot_id", "")
        prev = self.homes.get(site)
        self.processed += 1
        self._latencies.append(latency_ms)
        self._ingest_times.append(time.time())
        # per-home sequence gate: ignore stale late summaries from the same boot
        if prev and prev.get("boot_id") == boot and seq <= self.last_seq.get(site, -1):
            self.stale += 1
            return
        self.last_seq[site] = seq
        compacted = self._compact(summary)
        self.homes[site] = compacted
        self._track_gait_trend(site, compacted["gait_trend_bucket"])
        previous_alerts = self.active_alert_keys.get(site, set())
        current_alerts = {self._alert_key(a) for a in summary.get("alerts", [])}
        for alert in summary.get("alerts", []):
            if self._alert_key(alert) in previous_alerts:
                continue
            self.events.appendleft({
                "time": datetime.now(UTC).strftime("%H:%M:%S"),
                "site_id": site, "type": alert.get("type"),
                "severity": alert.get("severity", "info"),
                "zone": alert.get("zone"),
            })
        self.active_alert_keys[site] = current_alerts

    def _track_gait_trend(self, site: str, bucket: str) -> None:
        """Surface gait-trend transitions in the event feed: clinically useful
        signal the fleet already computes, not just device-health churn."""
        prev = self.trend_buckets.get(site)
        if bucket == prev:
            return
        self.trend_buckets[site] = bucket
        if bucket == "review":
            self.events.appendleft({
                "time": datetime.now(UTC).strftime("%H:%M:%S"),
                "site_id": site, "type": "gait_decline_flagged",
                "severity": "warning", "zone": None,
            })
        elif prev == "review" and bucket in ("stable", "improving"):
            self.events.appendleft({
                "time": datetime.now(UTC).strftime("%H:%M:%S"),
                "site_id": site, "type": "gait_decline_cleared",
                "severity": "info", "zone": None,
            })

    @staticmethod
    def _alert_key(alert: dict[str, Any]) -> tuple[Any, ...]:
        nodes = tuple(sorted(alert.get("nodes") or []))
        return (alert.get("type"), alert.get("severity", "info"), alert.get("zone"), nodes)

    @staticmethod
    def _compact(s: dict[str, Any]) -> dict[str, Any]:
        alerts = s.get("alerts", [])
        worst = max((SEVERITY_RANK.get(a.get("severity", "info"), 0) for a in alerts), default=-1)
        nodes = s.get("nodes", [])
        gait = s.get("gait") or {}
        vitals = s.get("vitals") or {}
        sleep = s.get("sleep") or {}
        edge = s.get("edge") or {}
        warning_count = sum(1 for a in alerts if SEVERITY_RANK.get(a.get("severity", "info"), 0) == 1)
        critical_count = sum(1 for a in alerts if SEVERITY_RANK.get(a.get("severity", "info"), 0) == 2)
        return {
            "site_id": s["site_id"],
            "boot_id": s.get("boot_id", ""),
            "seq": s.get("seq", 0),
            "state": s.get("state", "absent"),
            "zone": s.get("zone", "none"),
            "track_confidence": s.get("track_confidence", 0),
            "n_nodes": s.get("n_nodes", 0),
            "n_nodes_online": sum(1 for n in nodes if n.get("status") == "online"),
            "n_detecting": s.get("n_nodes_detecting", 0),
            "nodes": [
                {
                    "id": n.get("id", ""),
                    "status": n.get("status", "unknown"),
                    "temp_c": n.get("temp_c"),
                    "cpu_pct": n.get("cpu_pct"),
                    "rssi_dbm": n.get("rssi_dbm"),
                }
                for n in nodes
            ],
            "alert_count": len(alerts),
            "warning_count": warning_count,
            "critical_count": critical_count,
            "worst_severity": worst,
            "gait": {
                "window_d": gait.get("window_d"),
                "walking_minutes": gait.get("walking_minutes"),
                "walking_bouts": gait.get("walking_bouts"),
                "walking_speed_mps_avg": gait.get("walking_speed_mps_avg"),
                "cadence_spm_avg": gait.get("cadence_spm_avg"),
                "stride_length_m_avg": gait.get("stride_length_m_avg"),
                "step_time_symmetry": gait.get("step_time_symmetry"),
                "gait_variability_index": gait.get("gait_variability_index"),
                "speed_change_pct_4w": gait.get("speed_change_pct_4w"),
                "speed_change_pct_12w": gait.get("speed_change_pct_12w"),
                "impaired": bool(gait.get("impaired")),
                "confidence": gait.get("confidence"),
            } if gait else None,
            "vitals": {
                "window_h": vitals.get("window_h"),
                "stationary_minutes": vitals.get("stationary_minutes"),
                "respiration_rate_bpm_avg": vitals.get("respiration_rate_bpm_avg"),
                "heart_rate_bpm_avg": vitals.get("heart_rate_bpm_avg"),
                "respiration_change_bpm_4w": vitals.get("respiration_change_bpm_4w"),
                "confidence": vitals.get("confidence"),
            } if vitals else None,
            "sleep": {
                "window_d": sleep.get("window_d"),
                "epoch_s": sleep.get("epoch_s"),
                "context_epochs": sleep.get("context_epochs"),
                "latest_stage": sleep.get("latest_stage"),
                "sleep_efficiency_pct": sleep.get("sleep_efficiency_pct"),
                "sleep_efficiency_change_pp_4w": sleep.get("sleep_efficiency_change_pp_4w"),
                "sleep_efficiency_change_pp_12w": sleep.get("sleep_efficiency_change_pp_12w"),
                "total_sleep_h_avg": sleep.get("total_sleep_h_avg"),
                "waso_min_avg": sleep.get("waso_min_avg"),
                "rem_pct": sleep.get("rem_pct"),
                "deep_pct": sleep.get("deep_pct"),
                "light_pct": sleep.get("light_pct"),
                "wake_pct": sleep.get("wake_pct"),
                "confidence": sleep.get("confidence"),
            } if sleep else None,
            "trends": s.get("trends"),
            "gait_trend_bucket": gait_trend_bucket(s),
            "gait_impaired": bool(gait.get("impaired")),
            "has_vitals": s.get("vitals") is not None,
            "summary_bytes": edge.get("summary_bytes"),
            "reduction_ratio": edge.get("reduction_ratio"),
            "last_seen": now_iso(),
        }

    # --- rollups -------------------------------------------------------------
    def _percentile(self, vals: list[float], p: float) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        return round(s[min(len(s) - 1, int(len(s) * p))], 2)

    def _msgs_per_s(self) -> float:
        cutoff = time.time() - 5
        recent = [t for t in self._ingest_times if t >= cutoff]
        return round(len(recent) / 5, 1)

    def rollup(self, queue_depth: int, ws_clients: int) -> dict[str, Any]:
        homes = list(self.homes.values())
        by_state = {"walking": 0, "stationary": 0, "absent": 0}
        gait_trend_mix = {"review": 0, "watch": 0, "stable": 0, "improving": 0, "unknown": 0}
        nodes_online = nodes_total = active_alerts = critical = warning_alerts = critical_alerts = 0
        for h in homes:
            by_state[h["state"]] = by_state.get(h["state"], 0) + 1
            gait_trend_mix[h.get("gait_trend_bucket") or gait_trend_bucket(h)] += 1
            nodes_online += h["n_nodes_online"]
            nodes_total += h["n_nodes"]
            active_alerts += h["alert_count"]
            warning_alerts += h.get("warning_count", 0)
            critical_alerts += h.get("critical_count", 0)
            if h["worst_severity"] == SEVERITY_RANK["critical"]:
                critical += 1
        lat = list(self._latencies)
        return {
            "homes_total": len(homes),
            "by_state": by_state,
            "nodes_online": nodes_online,
            "nodes_total": nodes_total,
            "homes_with_alerts": sum(1 for h in homes if h["alert_count"] > 0),
            "active_alerts": active_alerts,
            "warning_alerts": warning_alerts,
            "critical_alerts": critical_alerts,
            "warning_homes": sum(1 for h in homes if h["worst_severity"] == SEVERITY_RANK["warning"]),
            "critical_homes": critical,
            "gait_impaired_homes": sum(1 for h in homes if h["gait_impaired"]),
            "gait_trend_mix": gait_trend_mix,
            "gait_review_homes": gait_trend_mix["review"],
            "msgs_per_s": self._msgs_per_s(),
            "ingest_latency_p50_ms": self._percentile(lat, 0.5),
            "ingest_latency_p95_ms": self._percentile(lat, 0.95),
            "summaries_processed": self.processed,
            "duplicates_ignored": self.duplicates,
            "stale_ignored": self.stale,
            "queue_depth": queue_depth,
            "queue_capacity": QUEUE_MAX_SIZE,
            "websocket_clients": ws_clients,
            "uptime_s": round(time.time() - self.started_at, 1),
        }

    def snapshot(self, queue_depth: int, ws_clients: int) -> dict[str, Any]:
        return {
            "type": "snapshot",
            "clock": datetime.now(UTC).strftime("%H:%M:%S"),
            "rollup": self.rollup(queue_depth, ws_clients),
            "homes": list(self.homes.values()),
            "events": list(self.events),
        }


@dataclass
class LatestNodeReport:
    report: dict[str, Any]
    received_at: float


class NodeCoalescer:
    """Coalesce independent Pi-node summaries into the home summary contract.

    Each node stream has its own boot/sequence/idempotency boundary; the
    dashboard still sees one current home row so it does not become a per-packet
    firehose.
    """

    def __init__(self) -> None:
        self.latest_by_site: dict[str, dict[str, LatestNodeReport]] = {}
        self.last_seq: dict[tuple[str, str, str], int] = {}
        self.home_seq: dict[str, int] = {}
        self.seen = BoundedKeySet(IDEMPOTENCY_CACHE_SIZE)
        self.received = 0
        self.duplicates = 0
        self.stale = 0
        self.coalesced = 0

    def accept(self, report: dict[str, Any]) -> dict[str, Any]:
        site = str(report.get("site_id") or "")
        node = str(report.get("node_id") or ((report.get("node") or {}).get("id") or ""))
        boot = str(report.get("boot_id") or "")
        key = str(report.get("idempotency_key") or "")
        if not site or not node or not boot or not key:
            raise HTTPException(400, "node summary requires site_id, node_id, boot_id and idempotency_key")

        if key in self.seen:
            self.duplicates += 1
            return {"accepted": False, "duplicate": True}

        seq = int(report.get("seq", 0))
        stream = (site, node, boot)
        if seq <= self.last_seq.get(stream, -1):
            self.stale += 1
            self.seen.add(key)
            return {"accepted": False, "stale": True}

        self.seen.add(key)
        self.last_seq[stream] = seq
        self.received += 1
        self.latest_by_site.setdefault(site, {})[node] = LatestNodeReport(report=report, received_at=time.time())

        summary = self._coalesce(site)
        self.coalesced += 1
        return {"accepted": True, "duplicate": False, "summary": summary}

    def metrics(self) -> dict[str, Any]:
        return {
            "node_reports_received": self.received,
            "node_duplicates_ignored": self.duplicates,
            "node_stale_ignored": self.stale,
            "homes_with_node_reports": len(self.latest_by_site),
            "latest_node_reports": sum(len(nodes) for nodes in self.latest_by_site.values()),
            "home_summaries_coalesced": self.coalesced,
        }

    def _coalesce(self, site: str) -> dict[str, Any]:
        wrapped_reports = list(self.latest_by_site.get(site, {}).values())
        reports = [w.report for w in wrapped_reports]
        best = self._best_report(reports)
        nodes = [self._node_view(r) for r in reports]
        alerts = self._alerts(reports)
        home_count = max((int(r.get("home_node_count", 0) or 0) for r in reports), default=0)
        next_seq = self.home_seq.get(site, 0) + 1
        self.home_seq[site] = next_seq
        edge = self._edge(reports)
        activity = best.get("activity") or {}
        detection_count = sum(1 for r in reports if r.get("detection"))
        return {
            "schema_version": "home-summary.v1",
            "site_id": site,
            "boot_id": "node-coalescer",
            "seq": next_seq,
            "idempotency_key": f"{site}:node-coalescer:{next_seq}",
            "ts": now_iso(),
            "window_s": best.get("window_s"),
            "state": activity.get("state") or best.get("state", "absent"),
            "zone": activity.get("zone") or best.get("zone", "none"),
            "track_confidence": activity.get("track_confidence", best.get("track_confidence", 0)),
            "n_nodes": home_count or len(nodes),
            "n_nodes_detecting": detection_count,
            "gait": best.get("gait"),
            "vitals": best.get("vitals"),
            "sleep": best.get("sleep"),
            "trends": best.get("trends"),
            "nodes": nodes,
            "alerts": alerts,
            "edge": edge,
        }

    @staticmethod
    def _best_report(reports: list[dict[str, Any]]) -> dict[str, Any]:
        state_rank = {"walking": 3, "stationary": 2, "moving": 2, "absent": 1}

        def score(report: dict[str, Any]) -> tuple[int, float, int]:
            activity = report.get("activity") or {}
            state = activity.get("state") or report.get("state", "absent")
            conf = float(activity.get("track_confidence") or report.get("track_confidence") or 0)
            has_detection = 1 if report.get("detection") else 0
            return (state_rank.get(state, 0), conf, has_detection)

        return max(reports, key=score) if reports else {}

    @staticmethod
    def _node_view(report: dict[str, Any]) -> dict[str, Any]:
        node = report.get("node") or {}
        return {
            "id": report.get("node_id") or node.get("id", ""),
            "status": node.get("status", "unknown"),
            "temp_c": node.get("temp_c"),
            "cpu_pct": node.get("cpu_pct"),
            "rssi_dbm": node.get("rssi_dbm"),
        }

    @staticmethod
    def _alerts(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: dict[tuple[Any, ...], dict[str, Any]] = {}
        for report in reports:
            node_id = report.get("node_id") or ((report.get("node") or {}).get("id"))
            for alert in report.get("alerts", []):
                alert = dict(alert)
                if node_id and "nodes" not in alert and alert.get("type") == "device_silent":
                    alert["nodes"] = [node_id]
                merged[FleetStore._alert_key(alert)] = alert
        return list(merged.values())

    @staticmethod
    def _edge(reports: list[dict[str, Any]]) -> dict[str, Any]:
        summary_bytes = sum(int((r.get("edge") or {}).get("summary_bytes") or 0) for r in reports)
        ratios = [float((r.get("edge") or {}).get("reduction_ratio") or 0) for r in reports]
        ratio = round(sum(ratios) / len(ratios)) if ratios else None
        return {
            "summary_bytes": summary_bytes or None,
            "reduction_ratio": ratio,
            "publisher_count": len(reports),
        }


class ConnectionManager:
    def __init__(self) -> None:
        self._conns: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._conns.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._conns.discard(ws)

    @property
    def count(self) -> int:
        return len(self._conns)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        stale = []
        for ws in list(self._conns):
            try:
                await ws.send_json(payload)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.disconnect(ws)


store = FleetStore()
node_coalescer = NodeCoalescer()
connections = ConnectionManager()
queue: asyncio.Queue[QueuedSummary] = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)


@asynccontextmanager
async def lifespan(app: FastAPI):
    worker = asyncio.create_task(_worker())
    caster = asyncio.create_task(_broadcaster())
    try:
        yield
    finally:
        for task in (worker, caster):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Imperial Radar Fleet Cloud", version="1.0.0", lifespan=lifespan)
# Open CORS is deliberate: this backend serves a public synthetic-data demo and
# ingest is guarded by HMAC signatures, not by origin. A production deployment
# would pin origins and put ingest behind device identity (see SECURITY_MODEL.md).
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/assets", StaticFiles(directory=ROOT_DIR / "assets"), name="assets")


def _json_request_body(body: bytes, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"{label} body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, f"{label} body must be a JSON object")
    return payload


def _verify_ingest_signature(payload: dict[str, Any], body: bytes, request: Request) -> None:
    try:
        verify_signed_payload(payload=payload, body=body, headers=request.headers)
    except DeviceAuthConfigError as exc:
        raise HTTPException(500, str(exc)) from exc
    except DeviceAuthError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(ROOT_DIR / "index.html")


@app.get("/styles.css")
async def styles() -> FileResponse:
    return FileResponse(ROOT_DIR / "styles.css")


@app.get("/app.js")
async def app_js() -> FileResponse:
    return FileResponse(ROOT_DIR / "app.js")


@app.post("/api/ingest")
async def ingest(request: Request) -> dict[str, Any]:
    body = await request.body()
    summary = _json_request_body(body, "summary")
    _verify_ingest_signature(summary, body, request)
    # Reject on overload BEFORE committing idempotency state, so the 503 retry
    # the supervisor is told to make is not misread as a duplicate. No await
    # happens between this check and put_nowait, so the capacity check holds.
    if queue.full():
        store.rejected += 1
        raise HTTPException(503, "ingest queue full; supervisor should retry")
    result = store.accept(summary)
    if result.get("duplicate"):
        return {**result, "queued": False}
    queue.put_nowait(QueuedSummary(summary=summary, queued_at=time.perf_counter()))
    return {**result, "queued": True, "queue_depth": queue.qsize()}


@app.post("/api/ingest/node")
async def ingest_node(request: Request) -> dict[str, Any]:
    body = await request.body()
    report = _json_request_body(body, "node summary")
    _verify_ingest_signature(report, body, request)

    # Reject on overload BEFORE the coalescer commits the idempotency key and
    # sequence gate; otherwise the retry we ask the node to make would be
    # dropped as duplicate/stale. No await between this check and put_nowait.
    if queue.full():
        store.rejected += 1
        raise HTTPException(503, "ingest queue full; Pi node should retry")
    result = node_coalescer.accept(report)
    summary = result.pop("summary", None)
    if not result.get("accepted"):
        return {**result, "queued": False}
    queue.put_nowait(QueuedSummary(summary=summary, queued_at=time.perf_counter()))
    return {**result, "queued": True, "queue_depth": queue.qsize()}


@app.get("/api/edge/control")
async def edge_control() -> dict[str, Any]:
    return {"privacy_mode": "cloud_summary_only"}


@app.get("/api/home/{site_id}")
async def home(site_id: str) -> dict[str, Any]:
    if site_id not in store.homes:
        raise HTTPException(404, "unknown site")
    return {"summary": store.homes[site_id]}


@app.get("/api/snapshot")
async def snapshot() -> dict[str, Any]:
    return store.snapshot(queue.qsize(), connections.count)


@app.get("/api/stream")
async def snapshot_stream() -> StreamingResponse:
    async def events():
        interval = 1.0 / BROADCAST_HZ
        while True:
            payload = json.dumps(store.snapshot(queue.qsize(), connections.count), separators=(",", ":"))
            yield f"event: snapshot\ndata: {payload}\n\n"
            await asyncio.sleep(interval)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "homes": len(store.homes),
            "queue_depth": queue.qsize(), "processed": store.processed,
            "websocket_clients": connections.count,
            "node_ingest": node_coalescer.metrics(),
            "security": {"ingest_auth": node_auth_mode()}}


@app.get("/api/metrics")
async def metrics() -> dict[str, Any]:
    return {**store.rollup(queue.qsize(), connections.count),
            "node_ingest": node_coalescer.metrics()}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await connections.connect(ws)
    try:
        await ws.send_json(store.snapshot(queue.qsize(), connections.count))
        while True:
            await ws.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        connections.disconnect(ws)


async def _worker() -> None:
    while True:
        item = await queue.get()
        try:
            latency_ms = (time.perf_counter() - item.queued_at) * 1000
            store.process(item.summary, latency_ms)
        finally:
            queue.task_done()


async def _broadcaster() -> None:
    """Fixed-rate fleet snapshot -> dashboard. O(homes) per tick, independent of
    ingest message rate (this is what kills the old per-frame O(N^2) broadcast)."""
    interval = 1.0 / BROADCAST_HZ
    while True:
        await asyncio.sleep(interval)
        if connections.count:
            await connections.broadcast(store.snapshot(queue.qsize(), connections.count))
