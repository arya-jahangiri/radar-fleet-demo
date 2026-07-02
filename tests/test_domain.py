"""Tests for the grounded home/Supervisor world model.

Asserts the invariants that make the demo defensible to radar engineers:
fusion accuracy matches the paper (~0.4 m RMSE), summaries stay within one
5 KB metered block, and the activity classes / schema are well-formed.
"""

import json
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain import profiles as P
from domain.home import Home


class TestHomeModel(unittest.TestCase):
    def _run(self, ticks: int = 3000):
        home = Home(site_id="home-test", seed=3, sim_clock_s=12 * 3600)
        errors, states, sizes = [], set(), []
        for i in range(ticks):
            home.tick(0.5)
            states.add(home.occupant.state)
            if home._fix and home.occupant.present:
                errors.append(math.hypot(home._fix[0] - home.occupant.x,
                                         home._fix[1] - home.occupant.y))
            if i % 20 == 0:
                sizes.append(len(json.dumps(home.summary(), separators=(",", ":")).encode()))
        return home, errors, states, sizes

    def test_activity_states_are_grounded(self):
        _, _, states, _ = self._run()
        self.assertTrue(states.issubset(set(P.ACTIVITY_STATES)))
        self.assertEqual(states, set(P.ACTIVITY_STATES))  # all three exercised

    def test_fusion_rmse_matches_paper(self):
        _, errors, _, _ = self._run()
        rmse = (sum(e * e for e in errors) / len(errors)) ** 0.5
        # Chen 2026 reports 0.40 m RMSE during movement; allow a small band.
        self.assertLess(rmse, 0.6, f"fusion RMSE {rmse:.2f} m too high")

    def test_summary_fits_one_metered_block(self):
        _, _, _, sizes = self._run()
        self.assertLess(max(sizes), 5120, "summary exceeds one 5 KB IoT block")

    def test_summary_schema(self):
        home, *_ = self._run(ticks=300)
        s = home.summary()
        for key in ("schema_version", "site_id", "idempotency_key", "state",
                    "track_confidence", "nodes", "edge", "alerts"):
            self.assertIn(key, s)
        self.assertNotIn("position_m", s)
        self.assertNotIn("uncertainty_m", s)
        self.assertGreaterEqual(s["n_nodes"], P.MIN_RADARS_FOR_FUSION)
        self.assertIn(s["state"], P.ACTIVITY_STATES)
        self.assertIn("walking_speed_mps_avg", s["gait"])
        self.assertIn("sleep_efficiency_pct", s["sleep"])
        self.assertIn("ranges", s["trends"])
        self.assertIn("4w", s["trends"]["ranges"])

    def test_pi_node_summary_schema(self):
        home, *_ = self._run(ticks=300)
        reports = home.node_summaries()
        self.assertEqual(len(reports), len(home.nodes))
        report = reports[0]
        for key in ("schema_version", "site_id", "node_id", "boot_id", "seq",
                    "idempotency_key", "activity", "node", "edge"):
            self.assertIn(key, report)
        self.assertEqual(report["schema_version"], "pi-node-summary.v1")
        self.assertEqual(report["home_node_count"], len(home.nodes))
        self.assertIn(report["activity"]["state"], P.ACTIVITY_STATES)
        self.assertIn("status", report["node"])
        self.assertLess(report["edge"]["summary_bytes"], 5120)

    def test_population_gait_trends_have_mixed_directions(self):
        deltas = []
        for seed in range(1, 120):
            home = Home(site_id=f"home-{seed:04d}", seed=seed, sim_clock_s=12 * 3600)
            home.tick(0.5)
            series = home.summary()["trends"]["ranges"]["12w"]["gait_speed_mps"]
            deltas.append(series[-1] - series[0])
        self.assertTrue(any(delta <= -0.06 for delta in deltas), "no review-worthy gait decline")
        self.assertTrue(any(abs(delta) < 0.03 for delta in deltas), "no stable gait-speed homes")
        self.assertTrue(any(delta >= 0.03 for delta in deltas), "no improving gait-speed homes")


if __name__ == "__main__":
    unittest.main()
