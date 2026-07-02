"""Grounded radar home-monitoring domain model.

The domain model supports both a per-home Supervisor/gateway topology and the
default direct-node topology, where Raspberry Pi radar nodes publish reduced
summaries and the backend coalesces them into home summaries.

Both paths use the same paper-grounded home, occupant, FMCW profile, activity,
gait, vitals, sleep, and alert model.
"""
