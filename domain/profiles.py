"""Paper-grounded radar profiles and constants.

Values in this module are based on public biomedical radar papers. Demo design
choices without a paper figure are marked DESIGN.
"""

from __future__ import annotations

# --- FMCW sensor: Infineon BGT60TR13C (Chen 2026, Table I) -------------------
RADAR_PROFILE_FMCW = {
    "profile_id": "infineon_bgt60tr13c_fmcw",
    "manufacturer": "Infineon Technologies",
    "model": "BGT60TR13C",
    "source": "Chen et al. 2026, Table I",
    "modality": "60 GHz FMCW mmWave",
    "start_frequency_ghz": 58,
    "end_frequency_ghz": 61,
    "centre_frequency_ghz": 60,
    "bandwidth_mhz": 3000,
    "max_range_m": 9.6,
    "range_resolution_m": 0.05,
    "max_unambiguous_doppler_mps": 6.25,
    "velocity_resolution_mps": 0.065,
    "sampling_rate_mhz": 2,
    "chirps_per_frame": 64,
    "samples_per_chirp": 360,
    "chirp_duration_us": 188.96,
    "frame_duration_ms": 12.8,
    "processing_rate_hz": 26,        # 3 frames concatenated (N=192) -> 26 Hz
    "activated_receivers": "RX2,RX3",  # 2 of 3 RX, azimuth plane only
    "azimuth_fov_deg": 45,           # +/-45 deg
    "mount_height_m": 1.7,
    "edge_compute": "Raspberry Pi 5 @ ~26% CPU",
}

# --- UWB sensor: Novelda XeThru X4 family (Bannon/Yin/Hadjipanayi) ------------
RADAR_PROFILE_UWB = {
    "profile_id": "novelda_x4m03_uwb",
    "manufacturer": "Novelda",
    "model": "XeThru X4M03",
    "source": "Hadjipanayi 2024; Yin 2024/2025; Bannon 2021/2025",
    "modality": "IR-UWB",
    "centre_frequency_ghz": 7.29,
    "bandwidth_mhz": 1400,
    "max_range_m": 9.87,
    "range_resolution_m": 0.05,
    "max_unambiguous_doppler_mps": 5.15,
    "frame_rate_hz": 500,            # 800 Hz in gait-ID; up to 300-800 fps micro-Doppler
    "edge_node": "ESP32-S3 (FreeRTOS/C) + on-device compression",
}

# --- Edge data volume / compression (Chen 2026 p.2; Bannon 2025 p.4-5) --------
RAW_GB_PER_10H = 80                  # ~80 GB compressed raw per 10-hour period
RAW_MB_PER_HOUR = RAW_GB_PER_10H * 1000 / 10
ONDEVICE_COMPRESSION_PCT = 80       # 76-84% binning + Huffman

# --- Localisation / fusion (Chen 2026 p.7; Hadjipanayi 2024) -----------------
LOCALISATION_RMSE_M = 0.40          # median 0.24, MAE 0.30, 90% < 0.59
MIN_RADARS_FOR_FUSION = 3           # Hadjipanayi 2024 multilateration
PER_NODE_RANGE_SIGMA_M = 0.35       # DESIGN: tuned so fused RMSE ~ 0.4 m

# --- Gait (Hadjipanayi 2024 RDT/asymmetric) ----------------------------------
CADENCE_RANGE_STEPS_PER_S = (0.5, 3.3)   # slow pathological -> brisk healthy
STEP_TIME_SYMMETRY_ACCURACY = 0.956      # 95.6 +/- 2.8%
GAIT_CYCLE = {"stance_pct": 60, "swing_pct": 40, "double_support_pct": 20}

# --- Vitals (Yin 2025 sleep) -------------------------------------------------
RESPIRATION_BAND_HZ = (0.1, 0.6)
RESPIRATION_RANGE_BPM = (6, 36)
HEART_RATE_RANGE_BPM = (42, 120)
SLEEP_EPOCH_S = 30
SLEEP_STAGES = ("wake", "rem", "light", "deep")
SLEEP_SEQUENCE_EPOCHS = 32              # Yin 2026: 32*30 s = 16 min context
SLEEP_WRLD_ACCURACY_2025 = 0.744        # wake / REM / light / deep
SLEEP_WRLD_ACCURACY_2026 = 0.795        # transfer learning + domain adaptation
SLEEP_HIGH_CONFIDENCE_ACCURACY = 0.85   # 2025: after dropping lowest 20%

# --- Rolling derived summaries (DESIGN: cloud-visible aggregate layer) --------
ROLLING_SAMPLE_PERIOD_S = 30
GAIT_ROLLING_WINDOW_D = 7
GAIT_TREND_WINDOW_WEEKS = 4
VITALS_ROLLING_WINDOW_H = 24
SLEEP_ROLLING_WINDOW_D = 7

# --- Activity classification (Chen 2026) -------------------------------------
ACTIVITY_STATES = ("absent", "stationary", "walking")
WALKING_SPEED_THRESHOLD_MPS = 0.10       # |v| < 0.1 -> stationary (Chen Fig 2b)
ABSENCE_ONSET_DELAY_S = 25               # inherent absence-confirmation delay
WALKING_MIN_DURATION_S = 1.5             # confirmed walking bout
WALKING_MIN_DISPLACEMENT_M = 1.5

# --- Device resilience (DESIGN: demonstrates local buffering/replay) ----------
# Brief node-silent periods should be visible in the demo without making the
# whole fleet look broken. Mean interval is per node in simulated time.
NODE_SILENCE_MEAN_INTERVAL_S = 6 * 3600
NODE_SILENCE_DURATION_S = (20, 90)

# --- Cloud cadence (DESIGN: justified by "orders of magnitude" reduction) ----
SUMMARY_CADENCE_HZ = 0.1                 # one home-derived summary / 10 s
SUMMARY_WINDOW_S = 10

# --- Deployment scale (Chen 2026 p.2; Bannon 2025) ---------------------------
MINDER_HOMES_TODAY = 100
NODES_PER_HOME_PROTOTYPE = 8             # 8-node prototype; living lab ran 5
