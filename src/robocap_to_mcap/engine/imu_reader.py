"""Convert RoboCap SQLite IMU dumps to SI-unit samples.

Source schema:
  acc_data(id, imuid_, x, y, z, timestamp)        — int counts; ts in ns
  gyro_data(id, imuid_, x, y, z, timestamp)       — independent timing
  metadata(key, value)                            — firmware/chip ids

Per omni-specs README + gravity-validated:
  ICM-42688-P  accel: 8192 LSB/g   (FSR ±4g)
  ICM-42688-P  gyro:  65.5 LSB/(°/s) (FSR ±500 dps)

Sanity check at rest: ‖accel_si‖ ≈ 9.81 m/s².
"""
from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ACCEL_LSB_PER_G = 8192.0
GYRO_LSB_PER_DPS = 65.5
G = 9.80665


@dataclass(frozen=True)
class IMUSample:
    t_ns: int          # source nanosecond timestamp (device-monotonic)
    accel_xyz: tuple[float, float, float]   # m/s²
    gyro_xyz:  tuple[float, float, float]   # rad/s


@dataclass(frozen=True)
class IMUStreams:
    accel_t_ns: np.ndarray   # int64 [N]
    accel_si:   np.ndarray   # float64 [N, 3]
    gyro_t_ns:  np.ndarray   # int64 [M]
    gyro_si:    np.ndarray   # float64 [M, 3]


@dataclass(frozen=True)
class IMUSampleRange:
    origin_t_ns: int
    samples: list[IMUSample]


# --- IMU device-completeness policy -------------------------------------
#
# Hardware invariant (verified against the cap fleet, 2026-05):
#   * dev0 is OPTIONAL — newer caps physically ship WITHOUT a dev0 IMU, so an
#     absent OR corrupt dev0 is normal and never a reason to fail/exclude an
#     episode.
#   * dev1 + dev2 are REQUIRED — every generation carries both. An episode
#     lacking a *usable* dev1 or dev2 has an incomplete IMU set and must be
#     cleanly rejected (not crash, not ship).
#
# Device numbers are parsed from the IMU filename (`IMUWriter_dev{N}_...`, or
# the newer `robocap_segment{N}_imu_{side}.db` mapped below).
# This policy lives in core so any vendor converter shares the same rule.
OPTIONAL_IMU_DEVICES: frozenset[str] = frozenset({"0"})
REQUIRED_IMU_DEVICES: frozenset[str] = frozenset({"1", "2"})

# `IMUWriter_dev2_session1_...` -> "2". The digit run right after `_dev`.
_IMU_DEV_RE = re.compile(r"_dev(\d+)")

# Newer 2-IMU capture layout names files `robocap_segment{N}_imu_{side}.db`
# (head) and `robowrist_segment{N}_imu_{side}.db` (wrist) instead of carrying a
# `_dev{N}` token. This module owns the CANONICAL side→device mapping: head
# left/right ARE the required dev1/dev2; wrist left/right the optional dev3/dev4.
# (src/cli/export_local.py hardcodes the same values today; it should import
# these once it is committed.)
_NEWER_IMU_RE = re.compile(r"(robocap|robowrist)_segment\d+_imu_(left|right)\.db$")
_NEWER_IMU_DEVICE: dict[tuple[str, str], str] = {
    ("robocap", "left"): "1",
    ("robocap", "right"): "2",
    ("robowrist", "left"): "3",
    ("robowrist", "right"): "4",
}

# The head IMUs of the newer layout — the required pair (→ dev1/dev2). Exported
# so the resolver builds candidate keys against the same grammar this module
# parses, rather than re-spelling `robocap_segment{N}_imu_{side}.db` elsewhere.
NEWER_IMU_REQUIRED_SIDES: tuple[str, ...] = ("left", "right")


def newer_imu_filename(segment: str | int, side: str, *, rig: str = "robocap") -> str:
    """Filename for a newer-layout IMU: `{rig}_segment{N}_imu_{side}.db`.

    The inverse of `_NEWER_IMU_RE` / `device_number_from_imu_key` — kept beside
    them so build and parse share one grammar."""
    return f"{rig}_segment{segment}_imu_{side}.db"


class InsufficientIMUError(RuntimeError):
    """Raised when an episode is missing a *usable* required IMU device
    (dev1 or dev2). Callers treat this as a clean per-episode rejection —
    the episode is skipped, not shipped, and the batch continues."""


def device_number_from_imu_key(key: str) -> str:
    """Parse the device number from an IMU s3_key/filename.

    `.../IMUWriter_dev2_session1_segment1.db` -> "2". Also maps the newer
    `robocap_segment{N}_imu_left.db` -> "1" / `_imu_right.db` -> "2" (head) and
    `robowrist_..._imu_left/right.db` -> "3"/"4" (wrist). Returns "?" when the
    filename carries neither naming (e.g. `mag_middle`), so topic/frame_id and
    the completeness policy agree on the device label. The `_dev` token must be
    immediately followed by digits — guarding against substrings like
    `no_device` that merely contain `_dev`."""
    fname = key.rsplit("/", 1)[-1]
    match = _IMU_DEV_RE.search(fname)
    if match:
        return match.group(1)
    newer = _NEWER_IMU_RE.search(fname)
    if newer:
        return _NEWER_IMU_DEVICE[(newer.group(1), newer.group(2))]
    return "?"


def missing_required_imu(usable_dev_ns: set[str]) -> set[str]:
    """Required IMU devices (dev1, dev2) that are absent or unusable.

    `usable_dev_ns` is the set of device numbers with a successfully-read,
    non-empty IMU stream. An empty result means the required set is
    satisfied; a non-empty result names the devices that make the episode's
    IMU set incomplete."""
    return set(REQUIRED_IMU_DEVICES) - usable_dev_ns


def _to_si_accel(raw: np.ndarray) -> np.ndarray:
    return (raw.astype(np.float64) / ACCEL_LSB_PER_G) * G


def _to_si_gyro(raw: np.ndarray) -> np.ndarray:
    return (raw.astype(np.float64) / GYRO_LSB_PER_DPS) * (math.pi / 180.0)


def read_streams(path: Path) -> IMUStreams:
    con = sqlite3.connect(path)
    try:
        accel = np.array(
            con.execute("SELECT timestamp, x, y, z FROM acc_data ORDER BY timestamp").fetchall(),
            dtype=np.int64,
        )
        gyro = np.array(
            con.execute("SELECT timestamp, x, y, z FROM gyro_data ORDER BY timestamp").fetchall(),
            dtype=np.int64,
        )
    finally:
        con.close()
    if len(accel) == 0 or len(gyro) == 0:
        raise RuntimeError(f"empty IMU stream in {path}")
    return IMUStreams(
        accel_t_ns=accel[:, 0],
        accel_si=_to_si_accel(accel[:, 1:]),
        gyro_t_ns=gyro[:, 0],
        gyro_si=_to_si_gyro(gyro[:, 1:]),
    )


def interp_gyro_to_accel(streams: IMUStreams) -> tuple[np.ndarray, np.ndarray]:
    """Linearly interpolate gyro to accel timestamps. Out-of-range entries are NaN.

    Returns (accel_t_ns, gyro_xyz_aligned). Caller filters NaN rows.
    """
    a_t, g_t, g_xyz = streams.accel_t_ns, streams.gyro_t_ns, streams.gyro_si
    out = np.empty((len(a_t), 3), dtype=np.float64)
    in_range = (a_t >= g_t[0]) & (a_t <= g_t[-1])
    for i in range(3):
        out[:, i] = np.interp(a_t, g_t, g_xyz[:, i], left=np.nan, right=np.nan)
    out[~in_range] = np.nan
    return a_t, out


def _samples_from_streams(streams: IMUStreams) -> list[IMUSample]:
    accel_t, gyro_aligned = interp_gyro_to_accel(streams)
    samples: list[IMUSample] = []
    for i in range(len(accel_t)):
        if np.any(np.isnan(gyro_aligned[i])):
            continue
        samples.append(IMUSample(
            t_ns=int(accel_t[i]),
            accel_xyz=tuple(streams.accel_si[i]),
            gyro_xyz=tuple(gyro_aligned[i]),
        ))
    return samples


def iter_samples(path: Path) -> list[IMUSample]:
    return _samples_from_streams(read_streams(path))


def read_device_metadata(path: Path) -> dict[str, str]:
    """Read the SQLite `metadata` table — firmware-stamped key/value pairs.

    Sample contents on a 2026 cap (RoboCap firmware 1.1.7):
        product=robocap   author=frodobots   version=1.1.7
        imu=icm42688p     camera=sc230ai     mag=mmc5983ma
        deviceid=...      subdevices=...     host=...

    Returns whatever is present; tolerates missing table (e.g. older firmware).
    """
    con = sqlite3.connect(path)
    try:
        cur = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='metadata'"
        )
        if cur.fetchone() is None:
            return {}
        rows = con.execute("SELECT key, value FROM metadata").fetchall()
    finally:
        con.close()
    return {str(k): str(v) for k, v in rows if k is not None}


def samples_in_range(
    path: Path, start_seconds: float, end_seconds: float,
) -> IMUSampleRange:
    """
    Like `iter_samples`, but returns the IMU stream origin plus only samples
    whose timestamp falls in [start_seconds, end_seconds] relative to that
    origin.

    The IMU SQLite holds 10-min device-clock segments; the start time within
    the segment is t=0 by convention (first acc_data row). We trim against
    that origin so the returned sample times line up with the video PTS scale.
    """
    if end_seconds <= start_seconds:
        raise ValueError(f"end_seconds ({end_seconds}) must be > start_seconds ({start_seconds})")
    streams = read_streams(path)
    base_ns = int(streams.accel_t_ns[0])
    all_samples = _samples_from_streams(streams)
    if not all_samples:
        return IMUSampleRange(origin_t_ns=base_ns, samples=[])
    start_ns = int(start_seconds * 1_000_000_000)
    end_ns = int(end_seconds * 1_000_000_000)
    samples = [
        s for s in all_samples
        if start_ns <= (s.t_ns - base_ns) <= end_ns
    ]
    return IMUSampleRange(origin_t_ns=base_ns, samples=samples)


def iter_samples_in_range(
    path: Path, start_seconds: float, end_seconds: float,
) -> list[IMUSample]:
    """Backward-compatible sample-only wrapper around `samples_in_range`."""
    return samples_in_range(path, start_seconds, end_seconds).samples


def sanity_check_at_rest(streams: IMUStreams, tolerance_pct: float = 5.0) -> bool:
    """For an IMU at rest, ‖a‖ should be ≈ 9.81 m/s². Returns True if mean is within tolerance."""
    mean_norm = float(np.linalg.norm(streams.accel_si, axis=1).mean())
    return abs(mean_norm - G) / G * 100.0 <= tolerance_pct
