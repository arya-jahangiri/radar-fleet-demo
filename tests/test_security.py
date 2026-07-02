import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

DEMO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEMO_ROOT))

from backend.main import app  # noqa: E402
from shared.device_auth import canonical_payload_bytes, signing_headers  # noqa: E402


AUTH_ENV = {
    "INGEST_AUTH_MODE": "hmac",
    "NODE_HMAC_SECRET": "local-demo-secret",
    "NODE_HMAC_SECRETS": "",
}


def auth_report(site: str = "home-auth", node: str = "home-auth-n1", seq: int = 1) -> dict:
    return {
        "schema_version": "pi-node-summary.v1",
        "site_id": site,
        "node_id": node,
        "home_node_count": 3,
        "boot_id": f"boot-auth-{node}",
        "seq": seq,
        "idempotency_key": f"{node}:boot-auth:{seq}",
        "activity": {"state": "stationary", "zone": "lounge", "track_confidence": 0.82},
        "node": {"id": node, "status": "online", "temp_c": 41.0, "cpu_pct": 24.0, "rssi_dbm": -57},
        "detection": {"range_m": 2.2, "azimuth_deg": 12.0},
        "gait": {"walking_speed_mps_avg": 0.92, "confidence": 0.86},
        "vitals": {"respiration_rate_bpm_avg": 14.2, "heart_rate_bpm_avg": 66, "confidence": 0.84},
        "sleep": {"sleep_efficiency_pct": 82.1, "confidence": 0.8},
        "trends": {"ranges": {"12w": {"labels": ["-11w", "now"], "gait_speed_mps": [1.0, 0.94]}}},
        "alerts": [],
        "edge": {"summary_bytes": 1200, "reduction_ratio": 70000},
    }


def home_summary(site: str = "home-auth-summary", seq: int = 1) -> dict:
    return {
        "schema_version": "home-summary.v1",
        "site_id": site,
        "boot_id": f"boot-{site}",
        "seq": seq,
        "idempotency_key": f"{site}:boot:{seq}",
        "state": "stationary",
        "zone": "lounge",
        "track_confidence": 0.82,
        "n_nodes": 3,
        "n_nodes_detecting": 2,
        "nodes": [{"id": f"{site}-n1", "status": "online"}],
        "alerts": [],
    }


class NodeIngestAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_explicit_off_allows_unsigned_node_ingest(self):
        with patch.dict(os.environ, {"INGEST_AUTH_MODE": "off"}, clear=False):
            response = self.client.post(
                "/api/ingest/node",
                json=auth_report(site="home-auth-default", node="home-auth-default-n1", seq=101),
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["accepted"])

    def test_hmac_is_default_for_unsigned_node_ingest(self):
        with patch.dict(os.environ, {"NODE_HMAC_SECRET": "local-demo-secret"}, clear=False):
            os.environ.pop("INGEST_AUTH_MODE", None)
            response = self.client.post(
                "/api/ingest/node",
                json=auth_report(site="home-auth-default-hmac", node="home-auth-default-hmac-n1", seq=106),
            )
        self.assertEqual(response.status_code, 401)

    def test_hmac_mode_rejects_unsigned_node_ingest(self):
        with patch.dict(os.environ, AUTH_ENV, clear=False):
            response = self.client.post(
                "/api/ingest/node",
                json=auth_report(site="home-auth-unsigned", node="home-auth-unsigned-n1", seq=102),
            )
        self.assertEqual(response.status_code, 401)

    def test_hmac_mode_accepts_signed_node_ingest(self):
        report = auth_report(site="home-auth-signed", node="home-auth-signed-n1", seq=103)
        body = canonical_payload_bytes(report)
        headers = {"content-type": "application/json"}
        headers.update(signing_headers(report["node_id"], body, AUTH_ENV["NODE_HMAC_SECRET"]))

        with patch.dict(os.environ, AUTH_ENV, clear=False):
            response = self.client.post("/api/ingest/node", content=body, headers=headers)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["accepted"])

    def test_hmac_mode_rejects_payload_identity_mismatch(self):
        report = auth_report(site="home-auth-mismatch", node="home-auth-mismatch-n1", seq=104)
        body = canonical_payload_bytes(report)
        headers = {"content-type": "application/json"}
        headers.update(signing_headers("home-auth-n2", body, AUTH_ENV["NODE_HMAC_SECRET"]))

        with patch.dict(os.environ, AUTH_ENV, clear=False):
            response = self.client.post("/api/ingest/node", content=body, headers=headers)

        self.assertEqual(response.status_code, 403)

    def test_hmac_mode_rejects_bad_signature(self):
        report = auth_report(site="home-auth-bad", node="home-auth-bad-n1", seq=105)
        body = canonical_payload_bytes(report)
        headers = {"content-type": "application/json"}
        headers.update(signing_headers(report["node_id"], body, "wrong-secret"))

        with patch.dict(os.environ, AUTH_ENV, clear=False):
            response = self.client.post("/api/ingest/node", content=body, headers=headers)

        self.assertEqual(response.status_code, 401)

    def test_hmac_mode_rejects_unsigned_home_summary_ingest(self):
        with patch.dict(os.environ, AUTH_ENV, clear=False):
            response = self.client.post(
                "/api/ingest",
                json=home_summary(site="home-auth-summary-unsigned", seq=201),
            )

        self.assertEqual(response.status_code, 401)

    def test_hmac_mode_accepts_signed_home_summary_ingest(self):
        summary = home_summary(site="home-auth-summary-signed", seq=202)
        body = canonical_payload_bytes(summary)
        headers = {"content-type": "application/json"}
        headers.update(signing_headers(summary["site_id"], body, AUTH_ENV["NODE_HMAC_SECRET"]))

        with patch.dict(os.environ, AUTH_ENV, clear=False):
            response = self.client.post("/api/ingest", content=body, headers=headers)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["accepted"])


if __name__ == "__main__":
    unittest.main()
