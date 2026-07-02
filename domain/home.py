"""Home = floor plan + one occupant + radar nodes + the Supervisor.

The Supervisor is the per-home aggregator (their real one is FastAPI + PostgreSQL
+ Docker). It fuses node observations, classifies activity, derives gait/vitals,
and produces a compact derived summary for the cloud.
"""

from __future__ import annotations

import math
import random
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import profiles as P
from .world import (
    Detection,
    FloorPlan,
    Occupant,
    RadarNode,
    clamp,
    jitter,
    make_floorplan,
    multilaterate,
    single_node_position,
)


def now_iso(t: float | None = None) -> str:
    dt = datetime.fromtimestamp(t, UTC) if t is not None else datetime.now(UTC)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Home:
    site_id: str
    seed: int
    sim_clock_s: float = 0.0             # seconds into the simulated day
    boot_id: str = field(default_factory=lambda: secrets.token_hex(3))
    seq: int = 0
    node_seq: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)
        self.plan: FloorPlan = make_floorplan(self.rng)
        self.occupant = Occupant(plan=self.plan, rng=self.rng)
        cx, cy = self.plan.width_m / 2, self.plan.depth_m / 2
        self.nodes: list[RadarNode] = []
        for i, (nx, ny) in enumerate(self.plan.node_sites):
            boresight = math.atan2(cy - ny, cx - nx)
            self.nodes.append(
                RadarNode(id=f"{self.site_id}-n{i+1}", x=nx, y=ny,
                          boresight_rad=boresight, rng=self.rng)
            )
        self._last_dets: list[Detection] = []
        self._fix: tuple[float, float] | None = None
        self._track_conf = 0.0
        self._absent_since: float | None = None
        self._sim_clock_at_start = self.sim_clock_s
        self._metric_phase = self.rng.uniform(0, math.tau)
        self._gait_speed_change_pct_4w, self._gait_speed_change_pct_12w = self._pick_gait_trend()
        self._sleep_eff_change_pp_4w, self._sleep_eff_change_pp_12w = self._pick_sleep_trend()
        self._respiration_change_bpm_4w = self.rng.choice([-0.8, -0.4, 0.0, 0.5, 0.9])
        self._respiration_change_bpm_12w = clamp(
            self._respiration_change_bpm_4w * self.rng.uniform(1.4, 2.0) + self.rng.uniform(-0.25, 0.25),
            -1.8,
            2.0,
        )
        self._sleep_eff_base_pct = self.rng.uniform(74, 88) - (1 - self.occupant.mobility) * 8
        self._rr_base_bpm = self.rng.uniform(12.5, 16.8)
        self._hr_base_bpm = self.rng.uniform(58, 74)

    def _pick_gait_trend(self) -> tuple[float, float]:
        """Pick a plausible long-window profile for one resident.

        A monitoring fleet should surface a minority of review-worthy decline,
        not make every home trend down at once.
        """
        if self.occupant.mobility < 0.82:
            choices = [(-6.5, -11.5), (-3.0, -5.5), (-0.8, -1.5), (0.8, 1.8), (1.8, 3.8)]
            weights = [0.28, 0.22, 0.32, 0.12, 0.06]
        elif self.occupant.mobility < 0.94:
            choices = [(-5.0, -8.5), (-2.0, -3.6), (-0.4, -0.8), (0.9, 1.8), (2.0, 4.2)]
            weights = [0.14, 0.20, 0.42, 0.16, 0.08]
        else:
            choices = [(-3.2, -5.5), (-1.2, -2.2), (0.0, 0.2), (1.0, 2.2), (2.3, 4.8)]
            weights = [0.06, 0.16, 0.42, 0.24, 0.12]
        return self.rng.choices(choices, weights=weights, k=1)[0]

    def _pick_sleep_trend(self) -> tuple[float, float]:
        if self.occupant.mobility < 0.82:
            choices = [(-3.8, -6.4), (-1.8, -3.0), (-0.3, -0.6), (0.8, 1.6)]
            weights = [0.18, 0.28, 0.38, 0.16]
        else:
            choices = [(-2.4, -4.0), (-0.9, -1.5), (0.2, 0.4), (1.0, 2.0)]
            weights = [0.10, 0.24, 0.42, 0.24]
        return self.rng.choices(choices, weights=weights, k=1)[0]

    # --- per-tick update -----------------------------------------------------
    def tick(self, dt: float) -> None:
        self.sim_clock_s += dt
        occ = self.occupant
        occ.tick(dt, self.sim_clock_s)

        for node in self.nodes:
            node.tick_health()
            if (node.status == "online"
                    and self.rng.random() < dt / P.NODE_SILENCE_MEAN_INTERVAL_S):
                node.status = "silent"
                node.silent_until_s = self.sim_clock_s + self.rng.uniform(*P.NODE_SILENCE_DURATION_S)
            elif node.status == "silent" and self.sim_clock_s >= node.silent_until_s:
                node.status = "online"

        dets = [d for d in (n.observe(occ, self.sim_clock_s) for n in self.nodes) if d]
        self._last_dets = dets

        if len(dets) >= P.MIN_RADARS_FOR_FUSION:
            self._fix = multilaterate(dets)
            self._track_conf = clamp(0.7 + 0.05 * len(dets), 0, 0.98)
        elif dets:
            self._fix = single_node_position(max(dets, key=lambda d: d.snr_db))
            self._track_conf = 0.55
        else:
            self._fix = None
            self._track_conf = 0.0

        # absence tracking
        if occ.state == "absent":
            if self._absent_since is None:
                self._absent_since = self.sim_clock_s
        else:
            self._absent_since = None

    # --- derived signals -----------------------------------------------------
    def _respiration_rate(self) -> float:
        base = 13.5 if self.occupant.lying else 15.5
        return clamp(base + math.sin(self.sim_clock_s / 40) * 1.5 + jitter(0.4, self.rng), 8, 22)

    def _gait(self) -> dict[str, Any] | None:
        occ = self.occupant
        daily_phase = math.sin(self.sim_clock_s / 86400 * math.tau + self._metric_phase)
        speed = clamp(occ.base_speed_mps * occ.mobility *
                      (1 + self._gait_speed_change_pct_4w / 200) +
                      daily_phase * 0.025, 0.35, 1.35)
        cadence = clamp(0.9 + speed * 1.1, *P.CADENCE_RANGE_STEPS_PER_S)  # steps/s
        stride_len = clamp(speed / max(cadence / 2, 0.3), 0.4, 1.7)
        # step-time symmetry: healthy ~1.0; impaired lower, with a slow ON/OFF
        # medication fluctuation (ties to the Parkinson's gait paper).
        med = 0.08 * math.sin(occ.med_phase_s / 900)  # ~15 min cycle
        symmetry = clamp(occ.mobility + med + self._gait_speed_change_pct_4w / 600 + jitter(0.015, self.rng), 0.6, 1.0)
        gvi = clamp(100 - (1 - symmetry) * 180 + jitter(4, self.rng), 40, 100)
        walking_minutes = clamp(7 * (18 + occ.base_speed_mps * 14 + occ.mobility * 12 + daily_phase * 3), 35, 360)
        bouts = max(1, round(walking_minutes / clamp(8 + occ.mobility * 5, 7, 16)))
        return {
            "window_d": P.GAIT_ROLLING_WINDOW_D,
            "walking_minutes": round(walking_minutes),
            "walking_bouts": bouts,
            "walking_speed_mps_avg": round(speed, 2),
            "cadence_spm_avg": round(cadence * 60, 1),
            "stride_length_m_avg": round(stride_len, 2),
            "step_time_symmetry": round(symmetry, 3),
            "gait_variability_index": round(gvi, 1),
            "speed_change_pct_4w": round(self._gait_speed_change_pct_4w, 1),
            "speed_change_pct_12w": round(self._gait_speed_change_pct_12w, 1),
            "impaired": symmetry < 0.9,
            "confidence": round(clamp(0.76 + occ.mobility * 0.13 + daily_phase * 0.02, 0.65, 0.94), 2),
        }

    def _vitals(self) -> dict[str, Any] | None:
        phase = math.sin(self.sim_clock_s / 43200 * math.tau + self._metric_phase)
        rr = clamp(self._rr_base_bpm + self._respiration_change_bpm_4w / 2 + phase * 0.6,
                   *P.RESPIRATION_RANGE_BPM)
        hr = clamp(self._hr_base_bpm + phase * 3.5 + jitter(1.2, self.rng), *P.HEART_RATE_RANGE_BPM)
        stationary_min = clamp(24 * (28 + self.occupant.mobility * 12 + phase * 3), 300, 1100)
        return {
            "window_h": P.VITALS_ROLLING_WINDOW_H,
            "stationary_minutes": round(stationary_min),
            "respiration_rate_bpm_avg": round(rr, 1),
            "heart_rate_bpm_avg": round(hr),
            "respiration_change_bpm_4w": round(self._respiration_change_bpm_4w, 1),
            "confidence": round(clamp(0.8 + self.occupant.mobility * 0.1 + phase * 0.02, 0.68, 0.94), 2),
        }

    def _sleep(self) -> dict[str, Any] | None:
        phase = math.sin(self.sim_clock_s / 86400 * math.tau + self._metric_phase / 2)
        cycle = (self.sim_clock_s / 90) % len(P.SLEEP_STAGES)  # ~90 min cycle
        stage = P.SLEEP_STAGES[int(cycle)]
        efficiency = clamp(self._sleep_eff_base_pct + self._sleep_eff_change_pp_4w / 2 + phase * 2.0, 58, 94)
        total_sleep_h = clamp(7.8 * efficiency / 100 + phase * 0.15, 4.0, 8.8)
        waso = clamp((100 - efficiency) * 2.0 + (1 - self.occupant.mobility) * 24, 8, 110)
        rem_pct = clamp(18 + phase * 2.0 - (1 - self.occupant.mobility) * 3, 10, 26)
        deep_pct = clamp(14 - (1 - self.occupant.mobility) * 5 - phase * 1.5, 5, 23)
        light_pct = clamp(100 - rem_pct - deep_pct - (100 - efficiency) * 0.45, 35, 70)
        wake_pct = clamp(100 - efficiency, 6, 42)
        return {
            "window_d": P.SLEEP_ROLLING_WINDOW_D,
            "epoch_s": P.SLEEP_EPOCH_S,
            "context_epochs": P.SLEEP_SEQUENCE_EPOCHS,
            "latest_stage": stage,
            "sleep_efficiency_pct": round(efficiency, 1),
            "sleep_efficiency_change_pp_4w": round(self._sleep_eff_change_pp_4w, 1),
            "sleep_efficiency_change_pp_12w": round(self._sleep_eff_change_pp_12w, 1),
            "total_sleep_h_avg": round(total_sleep_h, 1),
            "waso_min_avg": round(waso),
            "rem_pct": round(rem_pct, 1),
            "deep_pct": round(deep_pct, 1),
            "light_pct": round(light_pct, 1),
            "wake_pct": round(wake_pct, 1),
            "confidence": round(clamp(0.78 + phase * 0.03 - (1 - self.occupant.mobility) * 0.08, 0.58, 0.9), 2),
        }

    def _trend_pct(self, current: float, change_pct: float, points: int,
                   *, lo: float, hi: float, decimals: int) -> list[float]:
        start = current / max(0.2, 1 + change_pct / 100)
        values: list[float] = []
        for i in range(points):
            frac = i / max(points - 1, 1)
            seasonal = math.sin(self._metric_phase + i * 1.37) * (hi - lo) * 0.008
            values.append(round(clamp(start + (current - start) * frac + seasonal, lo, hi), decimals))
        values[-1] = round(clamp(current, lo, hi), decimals)
        return values

    def _trend_delta(self, current: float, change: float, points: int,
                     *, lo: float, hi: float, decimals: int) -> list[float]:
        start = current - change
        values: list[float] = []
        for i in range(points):
            frac = i / max(points - 1, 1)
            seasonal = math.sin(self._metric_phase + i * 1.11) * (hi - lo) * 0.006
            values.append(round(clamp(start + (current - start) * frac + seasonal, lo, hi), decimals))
        values[-1] = round(clamp(current, lo, hi), decimals)
        return values

    def _trends(self, gait: dict[str, Any], vitals: dict[str, Any], sleep: dict[str, Any]) -> dict[str, Any]:
        specs = {
            "7d": {"labels": ["-6d", "-5d", "-4d", "-3d", "-2d", "-1d", "now"], "scale": 0.25},
            "4w": {"labels": ["-3w", "-2w", "-1w", "now"], "scale": 1.0},
            "12w": {"labels": ["-11w", "-10w", "-9w", "-8w", "-7w", "-6w", "-5w", "-4w", "-3w", "-2w", "-1w", "now"], "scale": 3.0},
        }
        ranges: dict[str, Any] = {}
        for key, spec in specs.items():
            labels = spec["labels"]
            points = len(labels)
            scale = float(spec["scale"])
            if key == "12w":
                gait_change = self._gait_speed_change_pct_12w
                sleep_change = self._sleep_eff_change_pp_12w
                rr_change = self._respiration_change_bpm_12w
            else:
                gait_change = self._gait_speed_change_pct_4w * scale
                sleep_change = self._sleep_eff_change_pp_4w * scale
                rr_change = self._respiration_change_bpm_4w * scale
            sym_change = gait_change * 0.32
            ranges[key] = {
                "labels": labels,
                "gait_speed_mps": self._trend_pct(gait["walking_speed_mps_avg"], gait_change, points,
                                                  lo=0.3, hi=1.5, decimals=2),
                "step_time_symmetry_pct": self._trend_delta(gait["step_time_symmetry"] * 100, sym_change, points,
                                                            lo=58, hi=100, decimals=1),
                "sleep_efficiency_pct": self._trend_delta(sleep["sleep_efficiency_pct"], sleep_change, points,
                                                          lo=50, hi=96, decimals=1),
                "respiration_rate_bpm": self._trend_delta(vitals["respiration_rate_bpm_avg"], rr_change, points,
                                                          lo=8, hi=24, decimals=1),
            }
        return {"ranges": ranges}

    def _alerts(self) -> list[dict[str, Any]]:
        alerts: list[dict[str, Any]] = []
        occ = self.occupant
        if self._absent_since is not None and self.sim_clock_s - self._absent_since > 7200:
            alerts.append({"type": "prolonged_absence",
                           "since_s": round(self.sim_clock_s - self._absent_since),
                           "severity": "warning"})
        if occ.state == "walking" and self.rng.random() < 0.0002:
            alerts.append({"type": "possible_fall", "zone": occ.zone, "severity": "critical"})
        silent = [n.id for n in self.nodes if n.status == "silent"]
        if silent:
            alerts.append({"type": "device_silent", "nodes": silent, "severity": "warning"})
        return alerts

    # --- outputs -------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """Compact derived summary the Supervisor sends onward to the cloud."""
        import json

        self.seq += 1
        occ = self.occupant
        alerts = self._alerts()
        gait = self._gait()
        vitals = self._vitals()
        sleep = self._sleep()
        body = {
            "schema_version": "home-summary.v1",
            "site_id": self.site_id,
            "boot_id": self.boot_id,
            "seq": self.seq,
            "idempotency_key": f"{self.site_id}:{self.boot_id}:{self.seq}",
            "ts": now_iso(),
            "window_s": P.SUMMARY_WINDOW_S,
            "state": occ.state,
            "zone": occ.zone if occ.present else "none",
            "track_confidence": round(self._track_conf, 2),
            "n_nodes": len(self.nodes),
            "n_nodes_detecting": len(self._last_dets),
            "gait": gait,
            "vitals": vitals,
            "sleep": sleep,
            "trends": self._trends(gait, vitals, sleep),
            "nodes": [
                {"id": n.id, "status": n.status, "temp_c": round(n.temperature_c, 1),
                 "cpu_pct": round(n.cpu_pct, 1), "rssi_dbm": round(n.rssi_dbm)}
                for n in self.nodes
            ],
            "alerts": alerts,
        }
        size = len(json.dumps(body, separators=(",", ":")).encode())
        summ_mb_h = size * P.SUMMARY_CADENCE_HZ * 3600 / 1_000_000
        body["edge"] = {
            "raw_gb_per_10h": P.RAW_GB_PER_10H,
            "ondevice_compression_pct": P.ONDEVICE_COMPRESSION_PCT,
            "summary_bytes": size,
            "reduction_ratio": round(P.RAW_MB_PER_HOUR / max(summ_mb_h, 1e-6)),
        }
        return body

    def node_summaries(self) -> list[dict[str, Any]]:
        """Reduced summaries as if each FMCW radar node were its own Pi publisher.

        The node payloads expose local detections and health, while still sending
        derived rolling metrics rather than raw range-Doppler frames. The backend
        coalesces these into the `home-summary.v1` dashboard contract.
        """
        import json

        occ = self.occupant
        gait = self._gait()
        vitals = self._vitals()
        sleep = self._sleep()
        trends = self._trends(gait, vitals, sleep)
        alerts = self._alerts()
        detections = {d.node_id: d for d in self._last_dets}
        reports: list[dict[str, Any]] = []

        for node in self.nodes:
            seq = self.node_seq.get(node.id, 0) + 1
            self.node_seq[node.id] = seq
            detection = detections.get(node.id)
            node_state = occ.state if detection or occ.state == "stationary" else "absent"
            confidence = self._track_conf if detection else (0.35 if node_state == "stationary" and node.status == "online" else 0.0)
            report_alerts = [
                alert for alert in alerts
                if not alert.get("nodes") or node.id in alert.get("nodes", [])
            ]
            body: dict[str, Any] = {
                "schema_version": "pi-node-summary.v1",
                "site_id": self.site_id,
                "node_id": node.id,
                "home_node_count": len(self.nodes),
                "boot_id": f"{self.boot_id}-{node.id}",
                "seq": seq,
                "idempotency_key": f"{node.id}:{self.boot_id}:{seq}",
                "ts": now_iso(),
                "window_s": P.SUMMARY_WINDOW_S,
                "activity": {
                    "state": node_state,
                    "zone": occ.zone if occ.present and detection else "none",
                    "track_confidence": round(confidence, 2),
                },
                "node": {
                    "id": node.id,
                    "status": node.status,
                    "temp_c": round(node.temperature_c, 1),
                    "cpu_pct": round(node.cpu_pct, 1),
                    "rssi_dbm": round(node.rssi_dbm),
                },
                "detection": self._detection_payload(detection),
                "gait": gait,
                "vitals": vitals,
                "sleep": sleep,
                "trends": trends,
                "alerts": report_alerts,
            }
            size = len(json.dumps(body, separators=(",", ":")).encode())
            summ_mb_h = size * P.SUMMARY_CADENCE_HZ * 3600 / 1_000_000
            body["edge"] = {
                "raw_gb_per_10h": P.RAW_GB_PER_10H,
                "ondevice_compression_pct": P.ONDEVICE_COMPRESSION_PCT,
                "summary_bytes": size,
                "reduction_ratio": round(P.RAW_MB_PER_HOUR / max(summ_mb_h, 1e-6)),
            }
            reports.append(body)
        return reports

    @staticmethod
    def _detection_payload(detection: Detection | None) -> dict[str, Any] | None:
        if detection is None:
            return None
        return {
            "range_m": round(detection.range_m, 2),
            "azimuth_deg": round(detection.azimuth_deg, 1),
            "radial_velocity_mps": round(detection.radial_velocity_mps, 2),
            "snr_db": round(detection.snr_db, 1),
        }
