from __future__ import annotations

import json
import math
import os
import random
import time
from datetime import UTC, datetime
from typing import Any


TOPIC_PREFIX = "imperial-demo/sites"
STATES = ("walking", "stationary", "absent")
ZONES = ("lounge", "kitchen", "hall", "bedroom", "bathroom")
SLEEP_STAGES = ("wake", "light", "deep", "rem")


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _now_iso(now_s: float) -> str:
    return datetime.fromtimestamp(now_s, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _trend(current: float, delta: float, points: int, *, lo: float, hi: float, digits: int) -> list[float]:
    start = _clamp(current - delta, lo, hi)
    values = []
    for i in range(points):
        frac = i / max(points - 1, 1)
        values.append(round(_clamp(start + (current - start) * frac, lo, hi), digits))
    return values


def build_basic_ingest_topic(rule_name: str, site_id: str, node_id: str | None = None) -> str:
    if node_id:
        return f"$aws/rules/{rule_name}/{TOPIC_PREFIX}/{site_id}/nodes/{node_id}/summary"
    return f"$aws/rules/{rule_name}/{TOPIC_PREFIX}/{site_id}/supervisor/summary"


def summary_for_home(index: int, seq: int, now_s: float, nodes_per_home: int) -> dict[str, Any]:
    rng = random.Random(index * 1000003 + seq)
    site_id = f"home-{index:04d}"
    phase = math.sin((seq / 17.0) + index * 0.19)
    mobility = _clamp(0.72 + (index % 23) / 100 + rng.uniform(-0.04, 0.04), 0.62, 0.98)
    state = STATES[(seq + index) % len(STATES)]
    zone = ZONES[(seq + index * 3) % len(ZONES)]
    if state == "absent":
        zone = "none"
    detecting = 0 if state == "absent" else max(1, nodes_per_home - (1 if index % 19 == 0 else 0))
    silent_node = index % 37 == seq % 37
    nodes = []
    for n in range(nodes_per_home):
        status = "silent" if silent_node and n == nodes_per_home - 1 else "online"
        nodes.append({
            "id": f"{site_id}-n{n + 1}",
            "status": status,
            "temp_c": round(38 + rng.random() * 8, 1),
            "cpu_pct": round(24 + rng.random() * 44, 1),
            "rssi_dbm": round(-72 + rng.random() * 18, 1),
        })

    gait_speed = _clamp(0.62 + mobility * 0.45 + phase * 0.04, 0.35, 1.35)
    gait_delta_12w = -0.10 if index % 13 == 0 else (-0.04 if index % 7 == 0 else 0.02)
    gait_delta_4w = gait_delta_12w / 3.0
    # Symmetry impairment tracks the declining cohorts, not the mobility scalar,
    # so the fleet shows a plausible minority of impaired homes rather than most.
    if index % 13 == 0:
        symmetry_base = 0.84
    elif index % 7 == 0:
        symmetry_base = 0.885
    else:
        symmetry_base = 0.925 + (index % 6) * 0.011
    symmetry = _clamp(symmetry_base + rng.uniform(-0.012, 0.012), 0.6, 1.0)
    sleep_efficiency = _clamp(77 + mobility * 10 + phase * 2.0, 58, 94)
    respiration = _clamp(14.5 + phase * 1.3 + rng.uniform(-0.3, 0.3), 8, 22)
    heart_rate = round(_clamp(63 + phase * 6 + rng.uniform(-2, 2), 45, 105))
    alerts = []
    if silent_node:
        alerts.append({
            "type": "device_silent",
            "severity": "warning",
            "zone": zone,
            "nodes": [nodes[-1]["id"]],
        })
    if index % 211 == seq % 211 and state != "absent":
        alerts.append({"type": "possible_fall", "severity": "critical", "zone": zone})

    gait_series_12w = _trend(gait_speed, gait_delta_12w, 12, lo=0.3, hi=1.5, digits=2)
    gait_series_4w = _trend(gait_speed, gait_delta_4w, 4, lo=0.3, hi=1.5, digits=2)
    sleep_series_12w = _trend(sleep_efficiency, -1.5 if index % 11 == 0 else 0.8, 12, lo=55, hi=95, digits=1)
    sleep_series_4w = sleep_series_12w[-4:]
    return {
        "schema_version": "home-summary.v1",
        "site_id": site_id,
        "boot_id": datetime.fromtimestamp(now_s, UTC).strftime("cloud-%Y%m%d"),
        "seq": seq,
        "idempotency_key": f"{site_id}:cloud:{seq}",
        "ts": _now_iso(now_s),
        "state": state,
        "zone": zone,
        "track_confidence": round(0.72 + max(phase, 0) * 0.16, 2) if state != "absent" else 0.0,
        "n_nodes": nodes_per_home,
        "n_nodes_detecting": detecting,
        "nodes": nodes,
        "alerts": alerts,
        "gait": {
            "window_d": 7,
            "walking_minutes": round(_clamp(120 + mobility * 110 + phase * 20, 35, 360)),
            "walking_bouts": round(_clamp(8 + mobility * 8 + phase * 2, 1, 30)),
            "walking_speed_mps_avg": round(gait_speed, 2),
            "cadence_spm_avg": round(_clamp(82 + gait_speed * 28, 45, 150), 1),
            "stride_length_m_avg": round(_clamp(gait_speed / 1.55, 0.4, 1.7), 2),
            "step_time_symmetry": round(symmetry, 3),
            "gait_variability_index": round(_clamp(100 - (1 - symmetry) * 180, 40, 100), 1),
            "speed_change_pct_4w": round(gait_delta_4w / max(gait_speed, 0.1) * 100, 1),
            "speed_change_pct_12w": round(gait_delta_12w / max(gait_speed, 0.1) * 100, 1),
            "impaired": symmetry < 0.9,
            "confidence": round(_clamp(0.76 + mobility * 0.14, 0.65, 0.94), 2),
        },
        "vitals": {
            "window_h": 24,
            "stationary_minutes": round(_clamp(560 + mobility * 280 - phase * 45, 300, 1100)),
            "respiration_rate_bpm_avg": round(respiration, 1),
            "heart_rate_bpm_avg": heart_rate,
            "respiration_change_bpm_4w": round(-0.4 if index % 9 == 0 else 0.3, 1),
            "confidence": round(_clamp(0.79 + mobility * 0.12, 0.68, 0.94), 2),
        },
        "sleep": {
            "window_d": 7,
            "epoch_s": 30,
            "context_epochs": 32,
            "latest_stage": SLEEP_STAGES[(seq + index) % len(SLEEP_STAGES)],
            "sleep_efficiency_pct": round(sleep_efficiency, 1),
            "sleep_efficiency_change_pp_4w": round(sleep_series_4w[-1] - sleep_series_4w[0], 1),
            "sleep_efficiency_change_pp_12w": round(sleep_series_12w[-1] - sleep_series_12w[0], 1),
            "total_sleep_h_avg": round(_clamp(7.8 * sleep_efficiency / 100, 4.0, 8.8), 1),
            "waso_min_avg": round(_clamp((100 - sleep_efficiency) * 2.0, 8, 110)),
            "rem_pct": round(_clamp(18 + phase * 2, 10, 26), 1),
            "deep_pct": round(_clamp(14 - phase * 1.5, 5, 23), 1),
            "light_pct": round(_clamp(52 + phase * 2, 35, 70), 1),
            "wake_pct": round(_clamp(100 - sleep_efficiency, 6, 42), 1),
            "confidence": round(_clamp(0.76 + mobility * 0.12, 0.58, 0.9), 2),
        },
        "trends": {
            "ranges": {
                "4w": {
                    "labels": ["-3w", "-2w", "-1w", "now"],
                    "gait_speed_mps": gait_series_4w,
                    "sleep_efficiency_pct": sleep_series_4w,
                },
                "12w": {
                    "labels": ["-11w", "-10w", "-9w", "-8w", "-7w", "-6w", "-5w", "-4w", "-3w", "-2w", "-1w", "now"],
                    "gait_speed_mps": gait_series_12w,
                    "sleep_efficiency_pct": sleep_series_12w,
                },
            }
        },
        "edge": {
            "summary_bytes": 980 + (index % 190),
            "reduction_ratio": 81000 + (index % 9000),
        },
    }


def node_summary_for_home(index: int, node_index: int, seq: int, now_s: float, nodes_per_home: int) -> dict[str, Any]:
    home = summary_for_home(index, seq, now_s, nodes_per_home)
    node = home["nodes"][node_index]
    node_id = node["id"]
    online = node.get("status") == "online"
    state = home["state"] if online else "absent"
    detection = None
    if online and state != "absent":
        rng = random.Random(index * 1000003 + node_index * 9176 + seq)
        detection = {
            "range_m": round(_clamp(1.2 + rng.random() * 5.8, 0.2, 9.6), 2),
            "azimuth_deg": round(rng.uniform(-42, 42), 1),
            "radial_velocity_mps": round(rng.uniform(-1.1, 1.1), 2),
            "snr_db": round(rng.uniform(12, 29), 1),
        }
    alerts = [
        alert for alert in home["alerts"]
        if not alert.get("nodes") or node_id in alert.get("nodes", [])
    ]
    report: dict[str, Any] = {
        "schema_version": "pi-node-summary.v1",
        "site_id": home["site_id"],
        "node_id": node_id,
        "home_node_count": nodes_per_home,
        "boot_id": f"{home['boot_id']}-{node_id}",
        "seq": seq,
        "idempotency_key": f"{node_id}:cloud:{seq}",
        "ts": home["ts"],
        "window_s": home.get("window_s", 10),
        "activity": {
            "state": state,
            "zone": home["zone"] if detection else "none",
            "track_confidence": home["track_confidence"] if detection else 0.0,
        },
        "node": node,
        "detection": detection,
        "gait": home["gait"],
        "vitals": home["vitals"],
        "sleep": home["sleep"],
        "trends": home["trends"],
        "alerts": alerts,
        "edge": home["edge"],
    }
    payload_size = len(json.dumps(report, separators=(",", ":")).encode("utf-8"))
    report["edge"] = {
        **home["edge"],
        "summary_bytes": payload_size,
    }
    return report


def _int_value(event: dict[str, Any], key: str, env_key: str, default: int) -> int:
    raw = event.get(key) if key in event else os.environ.get(env_key, default)
    return int(raw)


def handler(event: dict[str, Any] | str | None, context: Any) -> dict[str, Any]:
    if event is None:
        event = {}
    if isinstance(event, str):
        event = json.loads(event)

    import boto3

    endpoint = os.environ["IOT_ENDPOINT"].removeprefix("https://").rstrip("/")
    rule_name = os.environ["IOT_RULE_NAME"]
    home_count = _int_value(event, "home_count", "HOME_COUNT", 100)
    nodes_per_home = _int_value(event, "nodes_per_home", "NODES_PER_HOME", 5)
    period_s = max(1, _int_value(event, "post_period_seconds", "POST_PERIOD_SECONDS", 60))
    now_s = time.time()
    seq = int(now_s // period_s)
    client = boto3.client("iot-data", endpoint_url=f"https://{endpoint}")

    published = 0
    failures = 0
    out_of_time = False
    for index in range(home_count):
        for node_index in range(nodes_per_home):
            if context is not None and context.get_remaining_time_in_millis() < 1500:
                out_of_time = True
                break
            summary = node_summary_for_home(index, node_index, seq, now_s, nodes_per_home)
            payload = json.dumps(summary, separators=(",", ":")).encode("utf-8")
            try:
                client.publish(
                    topic=build_basic_ingest_topic(rule_name, summary["site_id"], summary["node_id"]),
                    qos=1,
                    payload=payload,
                )
                published += 1
            except Exception as exc:  # pragma: no cover - exercised in AWS
                failures += 1
                print(f"publish failed for {summary['node_id']}: {exc}")
        if out_of_time:
            break

    return {
        "published": published,
        "failures": failures,
        "requested_home_count": home_count,
        "requested_node_count": home_count * nodes_per_home,
        "seq": seq,
    }
