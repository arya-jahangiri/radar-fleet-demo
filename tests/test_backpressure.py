"""Overload behaviour: a 503 must leave no trace, so the advertised retry works."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

DEMO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEMO_ROOT))

from backend import main as backend  # noqa: E402

AUTH_OFF = {"INGEST_AUTH_MODE": "off"}


def node_report(seq: int) -> dict:
    return {
        "schema_version": "pi-node-summary.v1",
        "site_id": "home-bp",
        "node_id": "home-bp-n1",
        "home_node_count": 1,
        "boot_id": "boot-bp",
        "seq": seq,
        "idempotency_key": f"home-bp-n1:boot-bp:{seq}",
        "activity": {"state": "stationary", "zone": "lounge", "track_confidence": 0.8},
        "node": {"id": "home-bp-n1", "status": "online"},
        "alerts": [],
    }


def home_summary(seq: int) -> dict:
    return {
        "schema_version": "home-summary.v1",
        "site_id": "home-bp-summary",
        "boot_id": "boot-bp",
        "seq": seq,
        "idempotency_key": f"home-bp-summary:boot-bp:{seq}",
        "state": "stationary",
        "zone": "lounge",
        "n_nodes": 1,
        "nodes": [{"id": "home-bp-summary-n1", "status": "online"}],
        "alerts": [],
    }


class QueueFullRetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(backend.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def _fill_queue(self):
        while not backend.queue.full():
            backend.queue.put_nowait(backend.QueuedSummary(summary={}, queued_at=0.0))

    def _drain_queue(self):
        while not backend.queue.empty():
            backend.queue.get_nowait()

    def test_node_retry_after_503_is_not_treated_as_duplicate(self):
        report = node_report(seq=1)
        with patch.dict(os.environ, AUTH_OFF, clear=False):
            self._fill_queue()
            try:
                first = self.client.post("/api/ingest/node", json=report)
            finally:
                self._drain_queue()
            self.assertEqual(first.status_code, 503)

            retry = self.client.post("/api/ingest/node", json=report)
        self.assertEqual(retry.status_code, 200)
        self.assertTrue(retry.json()["accepted"])
        self.assertFalse(retry.json().get("duplicate", False))
        self.assertFalse(retry.json().get("stale", False))

    def test_home_summary_retry_after_503_is_not_treated_as_duplicate(self):
        summary = home_summary(seq=1)
        with patch.dict(os.environ, AUTH_OFF, clear=False):
            self._fill_queue()
            try:
                first = self.client.post("/api/ingest", json=summary)
            finally:
                self._drain_queue()
            self.assertEqual(first.status_code, 503)

            retry = self.client.post("/api/ingest", json=summary)
        self.assertEqual(retry.status_code, 200)
        self.assertTrue(retry.json()["accepted"])
        self.assertFalse(retry.json().get("duplicate", False))


if __name__ == "__main__":
    unittest.main()
