"""Per-home world model: floor plan, one occupant, radar nodes, and Supervisor.

The default run models one occupant per home, multiple radar nodes, coarse
azimuth, 2D azimuth-plane tracking, and reduced cloud summaries.
"""

from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .profiles import (
    LOCALISATION_RMSE_M,
    MIN_RADARS_FOR_FUSION,
    PER_NODE_RANGE_SIGMA_M,
    RADAR_PROFILE_FMCW,
    RAW_GB_PER_10H,
    RAW_MB_PER_HOUR,
    ONDEVICE_COMPRESSION_PCT,
    WALKING_SPEED_THRESHOLD_MPS,
)

AZIMUTH_FOV_RAD = math.radians(RADAR_PROFILE_FMCW["azimuth_fov_deg"])
MAX_RANGE_M = RADAR_PROFILE_FMCW["max_range_m"]
RANGE_RES_M = RADAR_PROFILE_FMCW["range_resolution_m"]


# --- small math helpers ------------------------------------------------------
def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def jitter(amount: float, rng: random.Random | None = None) -> float:
    source = rng if rng is not None else random
    return (source.random() - 0.5) * amount


def angle_diff(a: float, b: float) -> float:
    d = a - b
    while d > math.pi:
        d -= 2 * math.pi
    while d < -math.pi:
        d += 2 * math.pi
    return d


# --- floor plan --------------------------------------------------------------
@dataclass
class Zone:
    name: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def centre(self) -> tuple[float, float]:
        return ((self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2)

    def contains(self, x: float, y: float) -> bool:
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1


@dataclass
class FloorPlan:
    width_m: float
    depth_m: float
    zones: list[Zone]
    node_sites: list[tuple[float, float]]  # wall-mounted radar positions

    def zone_at(self, x: float, y: float) -> str:
        for z in self.zones:
            if z.contains(x, y):
                return z.name
        return "hallway"


def make_floorplan(rng: random.Random) -> FloorPlan:
    """A studio/1-bed apartment with mild per-home variation (Chen 2026 Fig 6)."""
    w = round(rng.uniform(5.5, 8.5), 1)
    d = round(rng.uniform(4.5, 6.5), 1)
    zones = [
        Zone("kitchen", 0.0, 0.0, w * 0.42, d * 0.5),
        Zone("lounge", w * 0.42, 0.0, w, d * 0.55),
        Zone("bedroom", w * 0.45, d * 0.55, w, d),
        Zone("bathroom", 0.0, d * 0.55, w * 0.30, d),
        Zone("entry", w * 0.30, d * 0.5, w * 0.45, d),
    ]
    # radar nodes mounted high on walls overlooking the space (>=3 for fusion)
    node_sites = [
        (0.1, 0.1),
        (w - 0.1, 0.1),
        (w - 0.1, d - 0.1),
        (0.1, d - 0.1),
        (w / 2, 0.05),
    ]
    return FloorPlan(width_m=w, depth_m=d, zones=zones, node_sites=node_sites)


# --- occupant ----------------------------------------------------------------
@dataclass
class Occupant:
    """One resident performing activities of daily living in the floor plane."""

    plan: FloorPlan
    rng: random.Random
    x: float = 0.0
    y: float = 0.0
    vx: float = 0.0
    vy: float = 0.0
    speed_mps: float = 0.0
    base_speed_mps: float = 0.9          # elderly normal pace
    target: tuple[float, float] = (0.0, 0.0)
    dwell_s: float = 0.0
    present: bool = True
    absent_until_s: float = 0.0
    lying: bool = False
    # mobility 1.0 = healthy; <1 = impaired gait (asymmetry). Ties to the
    # Parkinson's gait/medication-fluctuation work.
    mobility: float = 1.0
    med_phase_s: float = 0.0             # slow ON/OFF medication cycle

    def __post_init__(self) -> None:
        cx, cy = self.plan.zones[0].centre
        self.x, self.y, self.target = cx, cy, (cx, cy)
        self.base_speed_mps = round(self.rng.uniform(0.6, 1.15), 2)
        self.mobility = round(self.rng.choice([1.0, 1.0, 1.0, 0.85, 0.7]), 2)
        self.med_phase_s = self.rng.uniform(0, 1800)

    def _pick_target(self, now_s: float) -> None:
        hour = (now_s / 3600) % 24
        if 0 <= hour < 7 or 22 <= hour < 24:           # night -> bed
            zone = self.plan.zones[2]                   # bedroom
            self.lying = True
            self.dwell_s = self.rng.uniform(120, 600)
        else:
            self.lying = False
            # small chance of leaving the home entirely
            if self.rng.random() < 0.04:
                self.present = False
                self.absent_until_s = now_s + self.rng.uniform(60, 900)
                return
            zone = self.rng.choice(self.plan.zones)
            self.dwell_s = self.rng.uniform(4, 40)
        cx, cy = zone.centre
        self.target = (
            clamp(cx + jitter(1.2, self.rng), 0.2, self.plan.width_m - 0.2),
            clamp(cy + jitter(1.0, self.rng), 0.2, self.plan.depth_m - 0.2),
        )

    def tick(self, dt: float, now_s: float) -> None:
        self.med_phase_s += dt
        if not self.present:
            self.vx = self.vy = self.speed_mps = 0.0
            if now_s >= self.absent_until_s:
                self.present = True
                self._pick_target(now_s)
            return

        if self.dwell_s > 0:                            # stationary at a waypoint
            self.dwell_s -= dt
            self.vx = self.vy = self.speed_mps = 0.0
            return

        dx, dy = self.target[0] - self.x, self.target[1] - self.y
        dist = math.hypot(dx, dy)
        if dist < 0.15:
            self._pick_target(now_s)
            return
        speed = self.base_speed_mps * self.mobility
        step = min(speed * dt, dist)
        self.x += dx / dist * step
        self.y += dy / dist * step
        self.vx, self.vy = dx / dist * speed, dy / dist * speed
        self.speed_mps = speed

    @property
    def state(self) -> str:
        if not self.present:
            return "absent"
        if self.speed_mps < WALKING_SPEED_THRESHOLD_MPS:
            return "stationary"
        return "walking"

    @property
    def zone(self) -> str:
        return self.plan.zone_at(self.x, self.y)


# --- radar node --------------------------------------------------------------
@dataclass
class RadarNode:
    id: str
    x: float
    y: float
    boresight_rad: float
    rng: random.Random
    status: str = "online"
    temperature_c: float = field(default=0.0)
    cpu_pct: float = field(default=0.0)
    rssi_dbm: float = field(default=0.0)
    silent_until_s: float = 0.0

    def __post_init__(self) -> None:
        self.temperature_c = 42 + self.rng.random() * 8
        self.cpu_pct = 20 + self.rng.random() * 12      # Chen: ~26% on Pi 5
        self.rssi_dbm = -52 - self.rng.random() * 12

    def tick_health(self) -> None:
        self.temperature_c = clamp(self.temperature_c + jitter(0.5, self.rng), 38, 60)
        self.cpu_pct = clamp(self.cpu_pct + jitter(3, self.rng), 12, 70)
        self.rssi_dbm = clamp(self.rssi_dbm + jitter(2, self.rng), -82, -40)

    def observe(self, occ: Occupant, now_s: float) -> "Detection | None":
        if self.status != "online" or not occ.present:
            return None
        dx, dy = occ.x - self.x, occ.y - self.y
        rng_m = math.hypot(dx, dy)
        if rng_m > MAX_RANGE_M:
            return None
        bearing = math.atan2(dy, dx)
        az = angle_diff(bearing, self.boresight_rad)
        if abs(az) > AZIMUTH_FOV_RAD:
            return None
        # radial velocity = projection of occupant velocity on line-of-sight
        if rng_m > 1e-3:
            radial_v = (occ.vx * dx + occ.vy * dy) / rng_m
        else:
            radial_v = 0.0
        snr = clamp(30 - rng_m * 2.6 + self.rng.random() * 4, 6, 32)
        if snr < 8:
            return None
        # per-node range error -> fused RMSE ~ 0.4 m after multilateration
        meas = rng_m + self.rng.gauss(0, PER_NODE_RANGE_SIGMA_M)
        return Detection(
            node_id=self.id,
            node_x=self.x,
            node_y=self.y,
            node_boresight_rad=self.boresight_rad,
            true_range_m=rng_m,
            range_m=max(0.1, meas),
            azimuth_deg=math.degrees(az),
            radial_velocity_mps=radial_v,
            snr_db=snr,
        )


@dataclass
class Detection:
    node_id: str
    node_x: float
    node_y: float
    node_boresight_rad: float
    true_range_m: float
    range_m: float
    azimuth_deg: float
    radial_velocity_mps: float
    snr_db: float


# --- fusion ------------------------------------------------------------------
def multilaterate(dets: list[Detection]) -> tuple[float, float] | None:
    """Weighted least-squares multilateration from >=3 noisy node ranges.

    Linearises r_i^2 = (x-x_i)^2 + (y-y_i)^2 by subtracting a reference node,
    then solves the 2x2 weighted normal equations. Weights 1/(r+1) down-weight
    distant (path-loss-degraded) nodes (Hadjipanayi 2024).
    """
    if len(dets) < MIN_RADARS_FOR_FUSION:
        return None
    ref = min(dets, key=lambda d: d.range_m)
    x0, y0, r0 = ref.node_x, ref.node_y, ref.range_m
    saa = sab = sbb = sac = sbc = 0.0
    for d in dets:
        if d is ref:
            continue
        a = 2 * (d.node_x - x0)
        b = 2 * (d.node_y - y0)
        c = (d.node_x**2 + d.node_y**2 - x0**2 - y0**2) - (d.range_m**2 - r0**2)
        w = 1.0 / (d.range_m + 1.0)
        saa += w * a * a
        sab += w * a * b
        sbb += w * b * b
        sac += w * a * c
        sbc += w * b * c
    det = saa * sbb - sab * sab
    if abs(det) < 1e-9:
        return None
    x = (sac * sbb - sab * sbc) / det
    y = (saa * sbc - sab * sac) / det
    return x, y


def single_node_position(d: Detection) -> tuple[float, float]:
    """Range + azimuth fix from one node (digital-beamforming equivalent).

    Used when fewer than MIN_RADARS_FOR_FUSION nodes see the occupant; lower
    confidence than multilateration.
    """
    bearing = d.node_boresight_rad + math.radians(d.azimuth_deg)
    return (
        d.node_x + d.range_m * math.cos(bearing),
        d.node_y + d.range_m * math.sin(bearing),
    )
