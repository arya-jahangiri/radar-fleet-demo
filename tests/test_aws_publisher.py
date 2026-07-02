"""Tests for the lightweight AWS IoT publisher helpers.

These do not call AWS; they protect the Basic Ingest topic shape and request
signing inputs used by the optional hosted mapping.
"""

import sys
import unittest
from pathlib import Path

DEMO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEMO_ROOT))

from edge_simulator.aws_iot_publisher import (  # noqa: E402
    AwsCredentials,
    build_basic_ingest_topic,
    sign_iot_publish_request,
)


class AwsPublisherTests(unittest.TestCase):
    def test_basic_ingest_topic_uses_direct_node_summary_contract(self):
        topic = build_basic_ingest_topic(
            "imperial_radar_demo_summary_ingest",
            "home-0042",
            "home-0042-n1",
        )
        self.assertEqual(
            topic,
            "$aws/rules/imperial_radar_demo_summary_ingest/"
            "imperial-demo/sites/home-0042/nodes/home-0042-n1/summary",
        )

    def test_signed_publish_request_targets_iot_data_plane(self):
        req = sign_iot_publish_request(
            endpoint="abc123-ats.iot.eu-west-2.amazonaws.com",
            region="eu-west-2",
            topic=build_basic_ingest_topic("rule_name", "home-0001", "home-0001-n1"),
            payload=b'{"ok":true}',
            credentials=AwsCredentials("AKIAEXAMPLE", "secret"),
        )
        self.assertEqual(req.get_method(), "POST")
        self.assertIn("/topics/%24aws/rules/rule_name/", req.full_url)
        self.assertIn("qos=1", req.full_url)
        self.assertIn("AWS4-HMAC-SHA256", req.headers["Authorization"])


if __name__ == "__main__":
    unittest.main()
