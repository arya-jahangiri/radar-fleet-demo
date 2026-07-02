"""Cloud ingest contract tests: idempotency, per-home sequence gating, rollups."""

import sys
import unittest
from pathlib import Path

DEMO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEMO_ROOT))

from backend.main import FleetStore, NodeCoalescer  # noqa: E402


def summary(site="home-0001", seq=1, boot="boot-a", state="stationary", alerts=None):
    return {
        "schema_version": "home-summary.v1",
        "site_id": site,
        "boot_id": boot,
        "seq": seq,
        "idempotency_key": f"{site}:{boot}:{seq}",
        "state": state,
        "zone": "lounge",
        "track_confidence": 0.9,
        "n_nodes": 5,
        "n_nodes_detecting": 4,
        "nodes": [{"id": f"{site}-n{i}", "status": "online"} for i in range(5)],
        "alerts": alerts or [],
    }


def node_report(site="home-0001", node="home-0001-n1", seq=1, boot="boot-a",
                state="stationary", confidence=0.8, detection=True):
    return {
        "schema_version": "pi-node-summary.v1",
        "site_id": site,
        "node_id": node,
        "home_node_count": 5,
        "boot_id": f"{boot}-{node}",
        "seq": seq,
        "idempotency_key": f"{node}:{boot}:{seq}",
        "activity": {
            "state": state,
            "zone": "lounge" if state != "absent" else "none",
            "track_confidence": confidence,
        },
        "node": {
            "id": node,
            "status": "online",
            "temp_c": 42.0,
            "cpu_pct": 28.0,
            "rssi_dbm": -58,
        },
        "detection": {"range_m": 2.2, "azimuth_deg": 12.0} if detection else None,
        "gait": {
            "window_d": 7,
            "walking_minutes": 126,
            "walking_bouts": 11,
            "walking_speed_mps_avg": 0.92,
            "cadence_spm_avg": 102,
            "stride_length_m_avg": 1.08,
            "step_time_symmetry": 0.94,
            "speed_change_pct_4w": -3.2,
            "impaired": False,
            "confidence": 0.86,
        },
        "vitals": {
            "window_h": 24,
            "stationary_minutes": 640,
            "respiration_rate_bpm_avg": 14.2,
            "heart_rate_bpm_avg": 66,
            "confidence": 0.84,
        },
        "sleep": {
            "window_d": 7,
            "epoch_s": 30,
            "context_epochs": 32,
            "latest_stage": "light",
            "sleep_efficiency_pct": 82.1,
            "total_sleep_h_avg": 6.6,
            "confidence": 0.8,
        },
        "trends": {"ranges": {
            "12w": {"labels": ["-11w", "now"], "gait_speed_mps": [1.0, 0.92]},
        }},
        "alerts": [],
        "edge": {"summary_bytes": 1200, "reduction_ratio": 70000},
    }


class ContractTests(unittest.TestCase):
    def test_duplicate_idempotency_key_rejected(self):
        store = FleetStore()
        s = summary(seq=10)
        self.assertTrue(store.accept(s)["accepted"])
        store.process(s, 1.0)
        result = store.accept(summary(seq=10))
        self.assertFalse(result["accepted"])
        self.assertTrue(result["duplicate"])
        self.assertEqual(store.duplicates, 1)

    def test_stale_sequence_ignored_same_boot(self):
        store = FleetStore()
        new = summary(seq=20)
        store.accept(new)
        store.process(new, 1.0)
        old = summary(seq=15)            # lower seq, same boot -> stale
        store.accept(old)
        store.process(old, 1.0)
        self.assertEqual(store.stale, 1)
        self.assertEqual(store.last_seq["home-0001"], 20)

    def test_new_boot_resets_sequence(self):
        store = FleetStore()
        a = summary(seq=99, boot="boot-a")
        store.accept(a); store.process(a, 1.0)
        b = summary(seq=1, boot="boot-b")   # reboot -> seq resets, must accept
        store.accept(b); store.process(b, 1.0)
        self.assertEqual(store.homes["home-0001"]["boot_id"], "boot-b")
        self.assertEqual(store.stale, 0)

    def test_rollup_counts_states_and_alerts(self):
        store = FleetStore()
        for i, st in enumerate(["walking", "stationary", "absent", "walking"]):
            if i == 0:
                alerts = [{"type": "possible_fall", "severity": "critical"}]
            elif i == 1:
                alerts = [{"type": "device_silent", "severity": "warning", "nodes": ["home-0001-n3"]}]
            else:
                alerts = []
            s = summary(site=f"home-{i:04d}", state=st, alerts=alerts)
            store.accept(s); store.process(s, 0.5)
        r = store.rollup(queue_depth=0, ws_clients=1)
        self.assertEqual(r["homes_total"], 4)
        self.assertEqual(r["by_state"]["walking"], 2)
        self.assertEqual(r["by_state"]["stationary"], 1)
        self.assertEqual(r["active_alerts"], 2)
        self.assertEqual(r["warning_alerts"], 1)
        self.assertEqual(r["critical_alerts"], 1)
        self.assertEqual(r["warning_homes"], 1)
        self.assertEqual(r["critical_homes"], 1)

    def test_compacted_home_keeps_node_visibility(self):
        store = FleetStore()
        s = summary(seq=1)
        s["nodes"][2]["status"] = "silent"
        store.accept(s); store.process(s, 0.5)
        compact = store.homes["home-0001"]
        self.assertEqual(compact["n_nodes_online"], 4)
        self.assertEqual(compact["nodes"][2]["status"], "silent")

    def test_compacted_home_keeps_only_derived_outputs(self):
        store = FleetStore()
        s = summary(seq=1, state="walking")
        s["gait"] = {
            "window_d": 7,
            "walking_minutes": 126,
            "walking_bouts": 11,
            "walking_speed_mps_avg": 0.92,
            "cadence_spm_avg": 102,
            "stride_length_m_avg": 1.08,
            "step_time_symmetry": 0.94,
            "speed_change_pct_4w": -3.2,
            "impaired": False,
            "confidence": 0.86,
        }
        s["vitals"] = {
            "window_h": 24,
            "stationary_minutes": 640,
            "respiration_rate_bpm_avg": 14.2,
            "heart_rate_bpm_avg": 66,
            "confidence": 0.84,
        }
        s["sleep"] = {
            "window_d": 7,
            "epoch_s": 30,
            "context_epochs": 32,
            "latest_stage": "light",
            "sleep_efficiency_pct": 82.1,
            "total_sleep_h_avg": 6.6,
            "confidence": 0.8,
        }
        s["trends"] = {"ranges": {
            "4w": {"labels": ["-3w", "-2w", "-1w", "now"],
                   "gait_speed_mps": [0.95, 0.94, 0.93, 0.92]},
            "12w": {"labels": ["-11w", "-10w", "now"],
                    "gait_speed_mps": [1.0, 0.97, 0.92]},
        }}
        s["edge"] = {"summary_bytes": 980, "reduction_ratio": 81000}
        s["detail"] = {"floorplan": {"width_m": 7}, "doppler_range": [{"range_m": 2.1}]}
        store.accept(s); store.process(s, 0.5)
        compact = store.homes["home-0001"]
        self.assertEqual(compact["gait"]["walking_speed_mps_avg"], 0.92)
        self.assertEqual(compact["vitals"]["respiration_rate_bpm_avg"], 14.2)
        self.assertEqual(compact["sleep"]["sleep_efficiency_pct"], 82.1)
        self.assertEqual(compact["trends"]["ranges"]["4w"]["gait_speed_mps"][-1], 0.92)
        self.assertEqual(compact["gait_trend_bucket"], "review")
        self.assertEqual(compact["summary_bytes"], 980)
        self.assertNotIn("detail", compact)
        self.assertFalse(hasattr(store, "detail"))

    def test_rollup_counts_gait_trend_mix(self):
        store = FleetStore()
        profiles = {
            "home-review": [0.95, 0.88],
            "home-watch": [0.95, 0.91],
            "home-stable": [0.95, 0.94],
            "home-improving": [0.90, 0.95],
        }
        for idx, (site, series) in enumerate(profiles.items(), start=1):
            s = summary(site=site, seq=idx)
            s["trends"] = {"ranges": {"12w": {"labels": ["-11w", "now"], "gait_speed_mps": series}}}
            store.accept(s); store.process(s, 0.5)
        mix = store.rollup(queue_depth=0, ws_clients=1)["gait_trend_mix"]
        self.assertEqual(mix["review"], 1)
        self.assertEqual(mix["watch"], 1)
        self.assertEqual(mix["stable"], 1)
        self.assertEqual(mix["improving"], 1)

    def test_repeated_active_alert_is_not_logged_every_summary(self):
        store = FleetStore()
        alert = {"type": "device_silent", "severity": "warning", "nodes": ["home-0001-n3"]}
        for seq in (1, 2, 3):
            s = summary(seq=seq, alerts=[alert])
            store.accept(s); store.process(s, 0.5)
        self.assertEqual(len(store.events), 1)

    def test_node_coalescer_builds_home_summary_from_pi_reports(self):
        coalescer = NodeCoalescer()
        report_a = node_report(node="home-0001-n1", state="stationary", confidence=0.7)
        report_b = node_report(node="home-0001-n2", state="walking", confidence=0.9)
        result_a = coalescer.accept(report_a)
        result_b = coalescer.accept(report_b)
        self.assertTrue(result_a["accepted"])
        self.assertTrue(result_b["accepted"])
        home_summary = result_b["summary"]
        self.assertEqual(home_summary["schema_version"], "home-summary.v1")
        self.assertEqual(home_summary["site_id"], "home-0001")
        self.assertEqual(home_summary["state"], "walking")
        self.assertEqual(home_summary["n_nodes"], 5)
        self.assertEqual(home_summary["n_nodes_detecting"], 2)
        self.assertEqual(len(home_summary["nodes"]), 2)
        self.assertEqual(home_summary["edge"]["publisher_count"], 2)

    def test_node_coalescer_rejects_duplicate_and_stale_node_reports(self):
        coalescer = NodeCoalescer()
        first = node_report(seq=10)
        duplicate = node_report(seq=10)
        stale = node_report(seq=8)
        self.assertTrue(coalescer.accept(first)["accepted"])
        self.assertTrue(coalescer.accept(duplicate)["duplicate"])
        self.assertTrue(coalescer.accept(stale)["stale"])
        metrics = coalescer.metrics()
        self.assertEqual(metrics["node_reports_received"], 1)
        self.assertEqual(metrics["node_duplicates_ignored"], 1)
        self.assertEqual(metrics["node_stale_ignored"], 1)


if __name__ == "__main__":
    unittest.main()
