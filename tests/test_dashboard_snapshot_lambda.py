import sys
import unittest
from pathlib import Path

LAMBDA_ROOT = Path(__file__).resolve().parents[1] / "infra" / "aws" / "lambda"
sys.path.insert(0, str(LAMBDA_ROOT))

from cloud_simulator import node_summary_for_home, summary_for_home  # noqa: E402
from dashboard_snapshot import build_snapshot  # noqa: E402


class DashboardSnapshotLambdaTests(unittest.TestCase):
    def test_snapshot_coalesces_direct_node_reports_for_dashboard(self):
        payloads = [
            node_summary_for_home(index=7, node_index=node_index, seq=123, now_s=1_800_000_000, nodes_per_home=5)
            for node_index in range(5)
        ]

        snapshot = build_snapshot(payloads, now_s=1_800_000_030, ingested_at_values=[1_800_000_000] * 5)

        self.assertEqual(snapshot["type"], "snapshot")
        self.assertEqual(snapshot["source"], "aws-hosted-latest-state")
        self.assertEqual(snapshot["rollup"]["homes_total"], 1)
        self.assertEqual(snapshot["rollup"]["nodes_total"], 5)
        self.assertEqual(snapshot["rollup"]["summaries_processed"], 5)
        self.assertEqual(snapshot["rollup"]["msgs_per_s"], 0.1)
        self.assertEqual(len(snapshot["homes"]), 1)
        home = snapshot["homes"][0]
        self.assertEqual(home["site_id"], "home-0007")
        self.assertEqual(home["n_nodes"], 5)
        self.assertEqual(len(home["nodes"]), 5)
        self.assertIn(home["state"], {"walking", "stationary", "absent"})
        self.assertIn("gait", home)
        self.assertIn("vitals", home)
        self.assertIn("sleep", home)

    def test_snapshot_surfaces_gait_review_homes_in_events(self):
        # index 13 is a declining-cohort home in the cloud simulator (-0.10 m/s over 12w)
        payload = summary_for_home(index=13, seq=55, now_s=1_800_000_000, nodes_per_home=5)

        snapshot = build_snapshot([payload], now_s=1_800_000_030, ingested_at_values=[1_800_000_000])

        self.assertEqual(snapshot["homes"][0]["gait_trend_bucket"], "review")
        gait_events = [e for e in snapshot["events"] if e["type"] == "gait_decline_flagged"]
        self.assertEqual(len(gait_events), 1)
        self.assertEqual(gait_events[0]["site_id"], "home-0013")
        self.assertEqual(gait_events[0]["severity"], "warning")

    def test_snapshot_accepts_legacy_home_summary_payload(self):
        payload = summary_for_home(index=2, seq=55, now_s=1_800_000_000, nodes_per_home=5)

        snapshot = build_snapshot([payload], now_s=1_800_000_030, ingested_at_values=[1_800_000_000])

        self.assertEqual(snapshot["rollup"]["homes_total"], 1)
        self.assertEqual(snapshot["homes"][0]["site_id"], "home-0002")
        self.assertEqual(snapshot["homes"][0]["boot_id"], payload["boot_id"])


if __name__ == "__main__":
    unittest.main()
