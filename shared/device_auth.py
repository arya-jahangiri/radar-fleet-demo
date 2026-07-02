"""Optional HMAC signing for local telemetry ingest.

The production AWS path should use AWS IoT device identity. This module keeps a
small local equivalent so the runnable demo has an inspectable trust boundary
without pretending to manage a real certificate fleet.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any, Mapping

AUTH_MODE_ENV = "INGEST_AUTH_MODE"
GLOBAL_SECRET_ENV = "NODE_HMAC_SECRET"
SECRET_MAP_ENV = "NODE_HMAC_SECRETS"
MAX_SKEW_ENV = "NODE_HMAC_MAX_SKEW_S"

AUTH_MODE_OFF = "off"
AUTH_MODE_HMAC = "hmac"
DEFAULT_MAX_SKEW_S = 300

DEVICE_ID_HEADER = "x-device-id"
TIMESTAMP_HEADER = "x-timestamp"
SIGNATURE_HEADER = "x-signature"
SIGNATURE_PREFIX = "v1="


class DeviceAuthError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class DeviceAuthConfigError(Exception):
    pass


def canonical_payload_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def node_auth_mode(env: Mapping[str, str] | None = None) -> str:
    source = env or os.environ
    mode = source.get(AUTH_MODE_ENV, AUTH_MODE_HMAC).strip().lower()
    if mode in ("", "none", "disabled", AUTH_MODE_OFF):
        return AUTH_MODE_OFF
    if mode == AUTH_MODE_HMAC:
        return AUTH_MODE_HMAC
    raise DeviceAuthConfigError(f"{AUTH_MODE_ENV} must be 'off' or 'hmac'")


def node_auth_enabled(env: Mapping[str, str] | None = None) -> bool:
    return node_auth_mode(env) == AUTH_MODE_HMAC


def secret_for_device(
    device_id: str,
    *,
    default_secret: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    source = env or os.environ
    secrets = _secret_map(source)
    return secrets.get(device_id) or default_secret or source.get(GLOBAL_SECRET_ENV)


def signing_headers(
    device_id: str,
    body: bytes,
    secret: str,
    *,
    timestamp: int | None = None,
) -> dict[str, str]:
    ts = str(timestamp if timestamp is not None else int(time.time()))
    signature = _signature(device_id=device_id, timestamp=ts, body=body, secret=secret)
    return {
        "X-Device-Id": device_id,
        "X-Timestamp": ts,
        "X-Signature": signature,
    }


def signing_headers_for_payload(
    payload: Mapping[str, Any],
    *,
    body: bytes | None = None,
    default_secret: str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    device_id = payload_identity(payload)
    if not device_id:
        return {}
    secret = secret_for_device(device_id, default_secret=default_secret, env=env)
    if not secret:
        return {}
    return signing_headers(device_id, body or canonical_payload_bytes(payload), secret)


def verify_signed_payload(
    *,
    payload: Mapping[str, Any],
    body: bytes,
    headers: Mapping[str, str],
    now: float | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    if not node_auth_enabled(env):
        return

    device_id = _header(headers, DEVICE_ID_HEADER)
    timestamp = _header(headers, TIMESTAMP_HEADER)
    provided_signature = _header(headers, SIGNATURE_HEADER)
    if not device_id or not timestamp or not provided_signature:
        raise DeviceAuthError(401, "signed ingest requires X-Device-Id, X-Timestamp and X-Signature")

    expected_identity = payload_identity(payload)
    if not expected_identity:
        raise DeviceAuthError(400, "signed ingest requires payload node_id or site_id")
    if device_id != expected_identity:
        raise DeviceAuthError(403, "signed device id does not match payload identity")

    try:
        request_ts = int(timestamp)
    except ValueError as exc:
        raise DeviceAuthError(401, "invalid signed ingest timestamp") from exc

    max_skew = _max_skew_s(env or os.environ)
    if abs((now if now is not None else time.time()) - request_ts) > max_skew:
        raise DeviceAuthError(401, "signed ingest timestamp is outside the accepted clock skew")

    secret = secret_for_device(device_id, env=env)
    if not secret:
        raise DeviceAuthConfigError(
            f"{AUTH_MODE_ENV}=hmac requires {GLOBAL_SECRET_ENV} or {SECRET_MAP_ENV}"
        )

    expected = _signature(device_id=device_id, timestamp=timestamp, body=body, secret=secret)
    if not hmac.compare_digest(provided_signature, expected):
        raise DeviceAuthError(401, "invalid signed ingest signature")


def payload_identity(payload: Mapping[str, Any]) -> str:
    return str(payload.get("node_id") or payload.get("site_id") or "")


def _signature(*, device_id: str, timestamp: str, body: bytes, secret: str) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    message = f"{device_id}\n{timestamp}\n{body_hash}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def _header(headers: Mapping[str, str], name: str) -> str:
    if name in headers:
        return str(headers[name])
    canonical = name.lower()
    for key, value in headers.items():
        if key.lower() == canonical:
            return str(value)
    return ""


def _secret_map(env: Mapping[str, str]) -> dict[str, str]:
    raw = env.get(SECRET_MAP_ENV, "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DeviceAuthConfigError(f"{SECRET_MAP_ENV} must be a JSON object") from exc
    if not isinstance(data, dict):
        raise DeviceAuthConfigError(f"{SECRET_MAP_ENV} must be a JSON object")
    return {str(key): str(value) for key, value in data.items() if value}


def _max_skew_s(env: Mapping[str, str]) -> int:
    raw = env.get(MAX_SKEW_ENV, str(DEFAULT_MAX_SKEW_S))
    try:
        return max(1, int(raw))
    except ValueError as exc:
        raise DeviceAuthConfigError(f"{MAX_SKEW_ENV} must be an integer number of seconds") from exc
