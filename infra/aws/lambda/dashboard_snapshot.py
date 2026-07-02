from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from typing import Any


SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
GAIT_REVIEW_DROP_MPS_12W = -0.06
GAIT_WATCH_DROP_MPS_12W = -0.03
GAIT_IMPROVE_GAIN_MPS_12W = 0.03


def _now_iso(now_s: float) -> str:
    return datetime.fromtimestamp(now_s, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _clock(now_s: float) -> str:
    return datetime.fromtimestamp(now_s, UTC).strftime("%H:%M:%S")


def _alert_key(alert: dict[str, Any]) -> tuple[Any, ...]:
    nodes = tuple(sorted(alert.get("nodes") or []))
    return (alert.get("type"), alert.get("severity", "info"), alert.get("zone"), nodes)


def _gait_trend_bucket(home: dict[str, Any]) -> str:
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


def _best_report(reports: list[dict[str, Any]]) -> dict[str, Any]:
    state_rank = {"walking": 3, "stationary": 2, "moving": 2, "absent": 1}

    def score(report: dict[str, Any]) -> tuple[int, float, int]:
        activity = report.get("activity") or {}
        state = activity.get("state") or report.get("state", "absent")
        conf = float(activity.get("track_confidence") or report.get("track_confidence") or 0)
        has_detection = 1 if report.get("detection") else 0
        return (state_rank.get(state, 0), conf, has_detection)

    return max(reports, key=score) if reports else {}


def _node_view(report: dict[str, Any]) -> dict[str, Any]:
    node = report.get("node") or {}
    return {
        "id": report.get("node_id") or node.get("id", ""),
        "status": node.get("status", "unknown"),
        "temp_c": node.get("temp_c"),
        "cpu_pct": node.get("cpu_pct"),
        "rssi_dbm": node.get("rssi_dbm"),
    }


def _coalesce_node_reports(site: str, reports: list[dict[str, Any]], now_s: float) -> dict[str, Any]:
    best = _best_report(reports)
    nodes = [_node_view(report) for report in reports]
    merged_alerts: dict[tuple[Any, ...], dict[str, Any]] = {}
    for report in reports:
        node_id = report.get("node_id") or ((report.get("node") or {}).get("id"))
        for original in report.get("alerts", []):
            alert = dict(original)
            if node_id and "nodes" not in alert and alert.get("type") == "device_silent":
                alert["nodes"] = [node_id]
            merged_alerts[_alert_key(alert)] = alert

    home_count = max((int(report.get("home_node_count", 0) or 0) for report in reports), default=0)
    activity = best.get("activity") or {}
    detection_count = sum(1 for report in reports if report.get("detection"))
    seq = max((int(report.get("seq", 0) or 0) for report in reports), default=0)
    edge = _edge(reports)
    return {
        "schema_version": "home-summary.v1",
        "site_id": site,
        "boot_id": "hosted-node-coalescer",
        "seq": seq,
        "idempotency_key": f"{site}:hosted-node-coalescer:{seq}",
        "ts": _now_iso(now_s),
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
        "alerts": list(merged_alerts.values()),
        "edge": edge,
    }


def _edge(reports: list[dict[str, Any]]) -> dict[str, Any]:
    summary_bytes = sum(int((report.get("edge") or {}).get("summary_bytes") or 0) for report in reports)
    ratios = [float((report.get("edge") or {}).get("reduction_ratio") or 0) for report in reports]
    ratio = round(sum(ratios) / len(ratios)) if ratios else None
    return {
        "summary_bytes": summary_bytes or None,
        "reduction_ratio": ratio,
        "publisher_count": len(reports),
    }


def _compact(summary: dict[str, Any], now_s: float) -> dict[str, Any]:
    alerts = summary.get("alerts", [])
    worst = max((SEVERITY_RANK.get(alert.get("severity", "info"), 0) for alert in alerts), default=-1)
    nodes = summary.get("nodes", [])
    gait = summary.get("gait") or {}
    vitals = summary.get("vitals") or {}
    sleep = summary.get("sleep") or {}
    edge = summary.get("edge") or {}
    warning_count = sum(1 for alert in alerts if SEVERITY_RANK.get(alert.get("severity", "info"), 0) == 1)
    critical_count = sum(1 for alert in alerts if SEVERITY_RANK.get(alert.get("severity", "info"), 0) == 2)
    return {
        "site_id": summary["site_id"],
        "boot_id": summary.get("boot_id", ""),
        "seq": summary.get("seq", 0),
        "state": summary.get("state", "absent"),
        "zone": summary.get("zone", "none"),
        "track_confidence": summary.get("track_confidence", 0),
        "n_nodes": summary.get("n_nodes", 0),
        "n_nodes_online": sum(1 for node in nodes if node.get("status") == "online"),
        "n_detecting": summary.get("n_nodes_detecting", 0),
        "nodes": [
            {
                "id": node.get("id", ""),
                "status": node.get("status", "unknown"),
                "temp_c": node.get("temp_c"),
                "cpu_pct": node.get("cpu_pct"),
                "rssi_dbm": node.get("rssi_dbm"),
            }
            for node in nodes
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
        "trends": summary.get("trends"),
        "gait_trend_bucket": _gait_trend_bucket(summary),
        "gait_impaired": bool(gait.get("impaired")),
        "has_vitals": summary.get("vitals") is not None,
        "summary_bytes": edge.get("summary_bytes"),
        "reduction_ratio": edge.get("reduction_ratio"),
        "last_seen": summary.get("ts") or _now_iso(now_s),
    }


def _rollup(homes: list[dict[str, Any]], payload_count: int, now_s: float, ingested_at_values: list[int]) -> dict[str, Any]:
    by_state = {"walking": 0, "stationary": 0, "absent": 0}
    gait_trend_mix = {"review": 0, "watch": 0, "stable": 0, "improving": 0, "unknown": 0}
    nodes_online = nodes_total = active_alerts = warning_alerts = critical_alerts = critical_homes = 0
    for home in homes:
        by_state[home["state"]] = by_state.get(home["state"], 0) + 1
        gait_trend_mix[home.get("gait_trend_bucket") or "unknown"] += 1
        nodes_online += int(home.get("n_nodes_online") or 0)
        nodes_total += int(home.get("n_nodes") or 0)
        active_alerts += int(home.get("alert_count") or 0)
        warning_alerts += int(home.get("warning_count") or 0)
        critical_alerts += int(home.get("critical_count") or 0)
        if home.get("worst_severity") == SEVERITY_RANK["critical"]:
            critical_homes += 1

    recent_count = sum(1 for value in ingested_at_values if value >= int(now_s) - 60)
    return {
        "homes_total": len(homes),
        "by_state": by_state,
        "nodes_online": nodes_online,
        "nodes_total": nodes_total,
        "homes_with_alerts": sum(1 for home in homes if int(home.get("alert_count") or 0) > 0),
        "active_alerts": active_alerts,
        "warning_alerts": warning_alerts,
        "critical_alerts": critical_alerts,
        "warning_homes": sum(1 for home in homes if home.get("worst_severity") == SEVERITY_RANK["warning"]),
        "critical_homes": critical_homes,
        "gait_impaired_homes": sum(1 for home in homes if home.get("gait_impaired")),
        "gait_trend_mix": gait_trend_mix,
        "gait_review_homes": gait_trend_mix["review"],
        "msgs_per_s": round(recent_count / 60, 1),
        "ingest_latency_p50_ms": None,
        "ingest_latency_p95_ms": None,
        "summaries_processed": payload_count,
        "duplicates_ignored": 0,
        "stale_ignored": 0,
        "queue_depth": 0,
        "queue_capacity": 0,
        "websocket_clients": 0,
        "uptime_s": 0,
    }


def build_snapshot(
    payloads: list[dict[str, Any]],
    *,
    now_s: float | None = None,
    ingested_at_values: list[int] | None = None,
) -> dict[str, Any]:
    now_s = time.time() if now_s is None else now_s
    ingested_at_values = ingested_at_values or []
    grouped_nodes: dict[str, list[dict[str, Any]]] = {}
    home_summaries: dict[str, dict[str, Any]] = {}
    for payload in payloads:
        site = str(payload.get("site_id") or "")
        if not site:
            continue
        if payload.get("schema_version") == "pi-node-summary.v1":
            grouped_nodes.setdefault(site, []).append(payload)
        else:
            home_summaries[site] = payload

    summaries: list[dict[str, Any]] = []
    for site, reports in grouped_nodes.items():
        summaries.append(_coalesce_node_reports(site, reports, now_s))
    for site, summary in home_summaries.items():
        if site not in grouped_nodes:
            summaries.append(summary)

    events: list[dict[str, Any]] = []
    for summary in summaries:
        for alert in summary.get("alerts", []):
            events.append({
                "time": _clock(now_s),
                "site_id": summary.get("site_id"),
                "type": alert.get("type"),
                "severity": alert.get("severity", "info"),
                "zone": alert.get("zone"),
            })
    homes = sorted((_compact(summary, now_s) for summary in summaries), key=lambda home: home["site_id"])
    for home in homes:
        if home.get("gait_trend_bucket") == "review":
            events.append({
                "time": _clock(now_s),
                "site_id": home.get("site_id"),
                "type": "gait_decline_flagged",
                "severity": "warning",
                "zone": None,
            })
    events.sort(key=lambda event: (event.get("severity") != "critical", event.get("site_id") or ""))
    return {
        "type": "snapshot",
        "clock": _clock(now_s),
        "generated_at": _now_iso(now_s),
        "source": "aws-hosted-latest-state",
        "rollup": _rollup(homes, len(payloads), now_s, ingested_at_values),
        "homes": homes,
        "events": events[:60],
    }


def _scan_payloads(table_name: str, max_items: int) -> tuple[list[dict[str, Any]], list[int]]:
    import boto3

    table = boto3.resource("dynamodb").Table(table_name)
    payloads: list[dict[str, Any]] = []
    ingested_at_values: list[int] = []
    scan_args: dict[str, Any] = {
        "ProjectionExpression": "#payload, ingested_at_epoch_s",
        "ExpressionAttributeNames": {"#payload": "payload"},
        "Limit": min(max_items, 1000),
    }
    while True:
        data = table.scan(**scan_args)
        for item in data.get("Items", []):
            raw = item.get("payload")
            if raw:
                payloads.append(json.loads(raw))
            if item.get("ingested_at_epoch_s") is not None:
                ingested_at_values.append(int(item["ingested_at_epoch_s"]))
            if len(payloads) >= max_items:
                return payloads, ingested_at_values
        last_key = data.get("LastEvaluatedKey")
        if not last_key:
            return payloads, ingested_at_values
        scan_args["ExclusiveStartKey"] = last_key


def _response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    allow_origin = os.environ.get("CORS_ALLOW_ORIGIN", "*")
    return {
        "statusCode": status_code,
        "headers": {
            "access-control-allow-origin": allow_origin,
            "access-control-allow-methods": "GET,OPTIONS",
            "access-control-allow-headers": "content-type",
            "cache-control": "no-store",
            "content-type": "application/json; charset=utf-8",
        },
        "body": json.dumps(body, separators=(",", ":")),
    }


def handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    event = event or {}
    if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return _response(204, {})

    table_name = os.environ["LATEST_TABLE_NAME"]
    max_items = int(os.environ.get("DASHBOARD_MAX_ITEMS", "10000"))
    try:
        payloads, ingested_at_values = _scan_payloads(table_name, max_items)
        return _response(200, build_snapshot(payloads, ingested_at_values=ingested_at_values))
    except Exception as exc:  # pragma: no cover - exercised in AWS
        print(f"dashboard snapshot failed: {exc}")
        return _response(500, {"type": "error", "message": "dashboard snapshot unavailable"})
