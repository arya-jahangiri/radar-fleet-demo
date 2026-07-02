from __future__ import annotations

import json
import os
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError


TABLE = boto3.resource("dynamodb").Table(os.environ["LATEST_TABLE_NAME"])
TTL_SECONDS = int(os.environ.get("ITEM_TTL_SECONDS", "86400"))
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


def _worst_severity(alerts: list[dict[str, Any]]) -> int:
    return max((SEVERITY_RANK.get(a.get("severity", "info"), 0) for a in alerts), default=-1)


def handler(event: dict[str, Any] | str, context: Any) -> dict[str, Any]:
    if isinstance(event, str):
        event = json.loads(event)

    site_id = event.get("site_id") or event.get("site_id_from_topic")
    if not site_id:
        raise ValueError("summary requires site_id")
    node_id = event.get("node_id") or event.get("node_id_from_topic") or "_home"
    schema_version = str(event.get("schema_version", "home-summary.v1"))

    seq = int(event.get("seq", 0))
    boot_id = str(event.get("boot_id", ""))
    alerts = event.get("alerts") or []
    nodes = event.get("nodes") or []
    node = event.get("node") or {}
    activity = event.get("activity") or {}
    is_node_summary = schema_version == "pi-node-summary.v1"
    now = int(time.time())
    item = {
        "site_id": site_id,
        "node_id": str(node_id),
        "schema_version": schema_version,
        "boot_id": boot_id,
        "seq": seq,
        "state": str(activity.get("state") or event.get("state", "unknown")),
        "zone": str(activity.get("zone") or event.get("zone", "none")),
        "n_nodes": int(event.get("home_node_count") or event.get("n_nodes", len(nodes) or 1)),
        "n_nodes_online": (1 if node.get("status") == "online" else 0) if is_node_summary else sum(1 for n in nodes if n.get("status") == "online"),
        "n_detecting": (1 if event.get("detection") else 0) if is_node_summary else int(event.get("n_nodes_detecting", 0)),
        "alert_count": len(alerts),
        "worst_severity": _worst_severity(alerts),
        "track_confidence": str(activity.get("track_confidence") or event.get("track_confidence", "")),
        "updated_at": str(event.get("ts", "")),
        "ingested_at_epoch_s": now,
        "expires_at": now + TTL_SECONDS,
        "payload": json.dumps(event, separators=(",", ":"), sort_keys=True),
    }

    try:
        TABLE.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(site_id) OR boot_id <> :boot OR seq < :seq",
            ExpressionAttributeValues={":boot": boot_id, ":seq": seq},
        )
        return {"accepted": True, "site_id": site_id, "node_id": node_id, "seq": seq}
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return {"accepted": False, "stale": True, "site_id": site_id, "node_id": node_id, "seq": seq}
        raise
