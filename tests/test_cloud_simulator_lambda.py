import sys
import unittest
from pathlib import Path

LAMBDA_ROOT = Path(__file__).resolve().parents[1] / "infra" / "aws" / "lambda"
sys.path.insert(0, str(LAMBDA_ROOT))

from cloud_simulator import build_basic_ingest_topic, node_summary_for_home, summary_for_home  # noqa: E402


class CloudSimulatorLambdaTests(unittest.TestCase):
    def test_summary_has_dashboard_contract_fields(self):
        summary = summary_for_home(index=42, seq=123, now_s=1_800_000_000, nodes_per_home=5)
        self.assertEqual(summary["schema_version"], "home-summary.v1")
        self.assertEqual(summary["site_id"], "home-0042")
        self.assertEqual(summary["n_nodes"], 5)
        self.assertEqual(len(summary["nodes"]), 5)
        self.assertIn("idempotency_key", summary)
        self.assertIn("gait", summary)
        self.assertIn("vitals", summary)
        self.assertIn("sleep", summary)
        self.assertIn("trends", summary)
        self.assertIn("edge", summary)

    def test_cloud_simulator_uses_basic_ingest_topic(self):
        self.assertEqual(
            build_basic_ingest_topic("imperial_radar_demo_summary_ingest", "home-0007", "home-0007-n1"),
            "$aws/rules/imperial_radar_demo_summary_ingest/"
            "imperial-demo/sites/home-0007/nodes/home-0007-n1/summary",
        )

    def test_gait_impairment_stays_a_plausible_minority(self):
        impaired = sum(
            1
            for index in range(100)
            if summary_for_home(index=index, seq=7, now_s=1_800_000_000, nodes_per_home=5)["gait"]["impaired"]
        )
        self.assertLess(impaired, 30, "hosted fleet should not look mostly gait-impaired")
        self.assertGreater(impaired, 5, "some homes should still be flagged for review")

    def test_speed_change_is_expressed_as_percent_of_speed(self):
        gait = summary_for_home(index=13, seq=7, now_s=1_800_000_000, nodes_per_home=5)["gait"]
        expected = round(-0.10 / max(gait["walking_speed_mps_avg"], 0.1) * 100, 1)
        self.assertAlmostEqual(gait["speed_change_pct_12w"], expected, places=1)

    def test_cloud_simulator_can_emit_direct_node_summary(self):
        summary = node_summary_for_home(index=42, node_index=0, seq=123, now_s=1_800_000_000, nodes_per_home=5)
        self.assertEqual(summary["schema_version"], "pi-node-summary.v1")
        self.assertEqual(summary["site_id"], "home-0042")
        self.assertEqual(summary["node_id"], "home-0042-n1")
        self.assertEqual(summary["home_node_count"], 5)
        self.assertIn("activity", summary)
        self.assertIn("node", summary)
        self.assertIn("idempotency_key", summary)


if __name__ == "__main__":
    unittest.main()
