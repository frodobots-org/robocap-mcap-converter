from __future__ import annotations

import hashlib
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import av
import numpy as np

from .engine.imu_reader import G, read_streams

from .models import CheckResult, SegmentInput, SessionInput, Severity

CancelCheck = Callable[[], bool]

VIDEO_CODEC_NAMES = frozenset({"h264", "hevc"})
IMU_GAP_WARNING_MS = 100.0
WITHIN_DOMAIN_WARN_US = 5_000
WITHIN_DOMAIN_ERROR_US = 500_000
CROSS_DOMAIN_WARN_US = 5_000_000
CROSS_DOMAIN_ERROR_US = 60_000_000


@dataclass(frozen=True)
class VideoProbe:
    clock_us: int
    codec: str
    width: int
    height: int
    duration_seconds: float
    has_b_frames: bool
    is_vfr: bool


@dataclass(frozen=True)
class IMUProbe:
    first_ns: int
    last_ns: int
    sample_count: int
    max_gap_ms: float
    gap_count: int
    gravity_mean: float


def _video_probe(path: Path) -> VideoProbe:
    with av.open(str(path)) as container:
        if not container.streams.video:
            raise RuntimeError("no video stream")
        stream = container.streams.video[0]
        raw_comment = container.metadata.get("comment")
        if raw_comment is None:
            raise ValueError("format.tags.comment is missing")
        try:
            clock_us = int(raw_comment.strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"format.tags.comment is not numeric: {raw_comment!r}") from exc
        if stream.duration is not None and stream.time_base is not None:
            duration = float(stream.duration * stream.time_base)
        elif container.duration is not None:
            duration = float(container.duration / av.time_base)
        else:
            raise RuntimeError("video duration is unavailable")
        average = float(stream.average_rate) if stream.average_rate else 0.0
        nominal = float(stream.base_rate) if stream.base_rate else average
        is_vfr = bool(average and nominal and abs(average - nominal) / nominal > 0.01)
        return VideoProbe(
            clock_us=clock_us,
            codec=str(stream.codec_context.name),
            width=int(stream.width),
            height=int(stream.height),
            duration_seconds=duration,
            has_b_frames=bool(stream.codec_context.has_b_frames),
            is_vfr=is_vfr,
        )


def _stored_timestamp_stats(con: sqlite3.Connection, table: str) -> tuple[int, int, int, float, int]:
    rows = con.execute(f"SELECT timestamp FROM {table} ORDER BY id").fetchall()
    if not rows:
        raise RuntimeError(f"{table} is empty")
    ts = np.asarray([int(row[0]) for row in rows], dtype=np.int64)
    diffs = np.diff(ts)
    if np.any(diffs <= 0):
        first_bad = int(np.flatnonzero(diffs <= 0)[0])
        raise RuntimeError(
            f"{table} timestamps are not strictly monotonic near row {first_bad + 1}"
        )
    warning_ns = int(IMU_GAP_WARNING_MS * 1_000_000)
    return int(ts[0]), int(ts[-1]), len(ts), float(diffs.max(initial=0) / 1e6), int(np.sum(diffs > warning_ns))


def _imu_probe(path: Path) -> IMUProbe:
    with sqlite3.connect(path) as con:
        a_first, a_last, a_count, a_max_gap, a_gaps = _stored_timestamp_stats(con, "acc_data")
        g_first, g_last, g_count, g_max_gap, g_gaps = _stored_timestamp_stats(con, "gyro_data")
    streams = read_streams(path)
    gravity_mean = float(np.linalg.norm(streams.accel_si, axis=1).mean())
    return IMUProbe(
        first_ns=max(a_first, g_first),
        last_ns=min(a_last, g_last),
        sample_count=min(a_count, g_count),
        max_gap_ms=max(a_max_gap, g_max_gap),
        gap_count=a_gaps + g_gaps,
        gravity_mean=gravity_mean,
    )


def _hash_duplicate_checks(session: SessionInput, cancel: CancelCheck) -> list[CheckResult]:
    by_size: dict[int, list[Path]] = defaultdict(list)
    all_paths = {
        path.resolve()
        for segment in session.segments
        for path in [v.path for v in segment.videos] + [i.path for i in segment.imus]
    }
    for path in all_paths:
        by_size[path.stat().st_size].append(path)
    by_digest: dict[str, list[Path]] = defaultdict(list)
    for candidates in by_size.values():
        if len(candidates) < 2:
            continue
        for path in candidates:
            if cancel():
                return []
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                    digest.update(chunk)
            by_digest[digest.hexdigest()].append(path)
    out = []
    for digest, paths in by_digest.items():
        if len(paths) > 1:
            out.append(CheckResult(
                "session.files.byte_duplicate",
                Severity.ERROR,
                f"The same source content was supplied under {len(paths)} different paths.",
                "Remove duplicate copies so one recording cannot be emitted twice.",
                tuple(str(path) for path in paths),
                {"sha256": digest},
            ))
    return out


def validate_session_deep(
    session: SessionInput,
    *,
    cancel: CancelCheck = lambda: False,
    on_segment: Callable[[SegmentInput], None] | None = None,
) -> SessionInput:
    session.checks = [c for c in session.checks if not c.check_id.startswith("session.files.byte_")]
    session.checks.extend(_hash_duplicate_checks(session, cancel))
    for segment in session.segments:
        if cancel():
            break
        validate_segment_deep(session, segment, cancel=cancel)
        if on_segment:
            on_segment(segment)
    return session


def validate_segment_deep(
    session: SessionInput,
    segment: SegmentInput,
    *,
    cancel: CancelCheck = lambda: False,
) -> SegmentInput:
    segment.checks = [c for c in segment.checks if not c.check_id.startswith("deep.")]
    video_info: dict[Path, VideoProbe] = {}
    imu_info: dict[Path, IMUProbe] = {}

    for video in segment.videos:
        if cancel():
            return segment
        try:
            info = _video_probe(video.path)
            video_info[video.path] = info
            if info.codec not in VIDEO_CODEC_NAMES:
                segment.checks.append(CheckResult(
                    "deep.video.codec_unsupported", Severity.ERROR,
                    f"{video.path.name} uses unsupported codec {info.codec!r}.",
                    "Re-encode the source as H.264 or H.265/HEVC.", (str(video.path),),
                ))
            else:
                segment.checks.append(CheckResult(
                    "deep.video.readable", Severity.PASSED,
                    f"{video.camera}: {info.codec}, {info.width}x{info.height}, {info.duration_seconds:.2f}s; clock tag {info.clock_us} us.",
                    files=(str(video.path),),
                    measured={
                        "camera": video.camera, "codec": info.codec,
                        "width": info.width, "height": info.height,
                        "duration_seconds": info.duration_seconds,
                        "device_clock_us": info.clock_us,
                    },
                ))
            if info.has_b_frames or info.is_vfr:
                issues = ", ".join(name for name, present in (
                    ("B-frames", info.has_b_frames), ("variable frame rate", info.is_vfr)
                ) if present)
                segment.checks.append(CheckResult(
                    "deep.video.normalization_required", Severity.WARNING,
                    f"{video.path.name} contains {issues}; it will be normalized during conversion.",
                    "No user action is required, but conversion will take longer.",
                    (str(video.path),),
                ))
        except Exception as exc:
            segment.checks.append(CheckResult(
                "deep.video.unreadable", Severity.ERROR,
                f"{video.path.name} cannot be converted: {exc}.",
                "Restore a readable H.264/H.265 MP4 with a numeric format.tags.comment clock tag.",
                (str(video.path),),
            ))

    durations = [probe.duration_seconds for probe in video_info.values()]
    if durations:
        segment.duration_seconds = min(durations)
        spread = max(durations) - min(durations)
        severity = Severity.WARNING if spread > 2.0 else Severity.PASSED
        segment.checks.append(CheckResult(
            "deep.video.duration_spread", severity,
            f"Camera duration spread is {spread:.3f}s (usable common duration {segment.duration_seconds:.3f}s).",
            "Check for a truncated camera recording." if severity == Severity.WARNING else "",
            measured={"spread_seconds": spread, "common_duration_seconds": segment.duration_seconds},
        ))

    _add_clock_checks(session, segment, video_info)

    for imu in segment.imus:
        if cancel():
            return segment
        severity_on_error = Severity.ERROR if imu.device in {"1", "2"} else Severity.WARNING
        try:
            info = _imu_probe(imu.path)
            imu_info[imu.path] = info
            segment.checks.append(CheckResult(
                "deep.imu.readable", Severity.PASSED,
                f"dev{imu.device}: {info.sample_count:,} aligned samples, max gap {info.max_gap_ms:.2f} ms.",
                files=(str(imu.path),),
                measured={
                    "device": imu.device, "sample_count": info.sample_count,
                    "first_ns": info.first_ns, "last_ns": info.last_ns,
                    "max_gap_ms": info.max_gap_ms, "gap_count": info.gap_count,
                    "mean_accel_magnitude": info.gravity_mean,
                },
            ))
            if info.gap_count:
                segment.checks.append(CheckResult(
                    "deep.imu.timestamp_gaps", Severity.WARNING,
                    f"dev{imu.device} has {info.gap_count} timestamp gap(s) over {IMU_GAP_WARNING_MS:.0f} ms; largest is {info.max_gap_ms:.2f} ms.",
                    "Inspect the capture for dropped IMU samples.", (str(imu.path),),
                ))
            gravity_error_pct = abs(info.gravity_mean - G) / G * 100.0
            if gravity_error_pct > 5.0:
                segment.checks.append(CheckResult(
                    "deep.imu.gravity_scale", Severity.WARNING,
                    f"dev{imu.device} mean acceleration magnitude is {info.gravity_mean:.3f} m/s^2 ({gravity_error_pct:.1f}% from gravity).",
                    "Confirm this IMU uses the expected ICM-42688-P full-scale range.",
                    (str(imu.path),),
                ))
        except Exception as exc:
            segment.checks.append(CheckResult(
                "deep.imu.unreadable", severity_on_error,
                f"{imu.path.name} is unusable: {exc}.",
                "Recover a valid SQLite DB containing non-empty acc_data and gyro_data tables.",
                (str(imu.path),),
            ))

    _add_overlap_checks(segment, video_info, imu_info)
    segment.validated_fingerprint = segment.fingerprint()
    return segment


def _add_clock_checks(
    session: SessionInput, segment: SegmentInput, probes: dict[Path, VideoProbe]
) -> None:
    by_camera = {video.camera: probes[video.path] for video in segment.videos if video.path in probes}
    domains = {
        "body": ("left", "right", "left-front", "right-front"),
        "eye": ("left-eye", "right-eye"),
    }
    medians: dict[str, float] = {}
    for domain, cameras in domains.items():
        clocks = [by_camera[c].clock_us for c in cameras if c in by_camera]
        if len(clocks) < 2:
            continue
        spread = max(clocks) - min(clocks)
        medians[domain] = float(np.median(clocks))
        severity = (
            Severity.ERROR if spread > WITHIN_DOMAIN_ERROR_US
            else Severity.WARNING if spread > WITHIN_DOMAIN_WARN_US
            else Severity.PASSED
        )
        segment.checks.append(CheckResult(
            f"deep.clock.{domain}_spread", severity,
            f"{domain.title()} clock-domain spread is {spread / 1000:.3f} ms.",
            "Check that these MP4s came from the same segment and clock domain." if severity != Severity.PASSED else "",
            measured={"spread_us": spread},
        ))
    if set(medians) == {"body", "eye"}:
        delta = abs(medians["body"] - medians["eye"])
        severity = (
            Severity.ERROR if delta > CROSS_DOMAIN_ERROR_US
            else Severity.WARNING if delta > CROSS_DOMAIN_WARN_US
            else Severity.INFO
        )
        segment.checks.append(CheckResult(
            "deep.clock.cross_domain_delta", severity,
            f"Body-to-eye clock-domain offset is {delta / 1_000_000:.3f}s and will be preserved.",
            "Confirm all camera files belong to the same capture if this exceeds one minute." if severity == Severity.ERROR else "",
            measured={"delta_us": delta},
        ))
    if session.session_start is not None and by_camera:
        max_clock = max(info.clock_us for info in by_camera.values())
        elapsed_hours = max_clock / 3_600_000_000
        severity = Severity.ERROR if elapsed_hours > 24 * 7 else Severity.WARNING if elapsed_hours > 24 else Severity.PASSED
        segment.checks.append(CheckResult(
            "deep.clock.absolute_anchor_sane", severity,
            f"Largest camera uptime offset from the folder UTC anchor is {elapsed_hours:.2f}h.",
            "Verify the folder timestamp and MP4 comment clocks belong to the same recording." if severity != Severity.PASSED else "",
            measured={"max_device_clock_us": max_clock},
        ))


def _add_overlap_checks(
    segment: SegmentInput,
    videos: dict[Path, VideoProbe],
    imus: dict[Path, IMUProbe],
) -> None:
    video_by_camera = {v.camera: videos[v.path] for v in segment.videos if v.path in videos}
    for imu in segment.imus:
        info = imus.get(imu.path)
        if info is None:
            continue
        if imu.device in {"1", "2"}:
            candidates = [p for camera, p in video_by_camera.items() if camera not in {"left_down", "right_down"}]
        elif imu.device == "3":
            candidates = [video_by_camera["left_down"]] if "left_down" in video_by_camera else []
        elif imu.device == "4":
            candidates = [video_by_camera["right_down"]] if "right_down" in video_by_camera else []
        else:
            candidates = list(video_by_camera.values())
        coverages = []
        for video in candidates:
            start_ns = video.clock_us * 1000
            end_ns = start_ns + int(video.duration_seconds * 1e9)
            overlap_ns = max(0, min(end_ns, info.last_ns) - max(start_ns, info.first_ns))
            coverages.append(overlap_ns / max(1, end_ns - start_ns))
        if not coverages:
            continue
        coverage = min(coverages)
        if coverage <= 0:
            severity = Severity.ERROR if imu.device in {"1", "2"} else Severity.WARNING
            message = f"dev{imu.device} does not overlap its camera timeline; time sync is broken."
            fix = "Confirm the IMU and MP4 files came from the same session and segment."
        elif coverage < 0.98:
            severity = Severity.WARNING
            message = f"dev{imu.device} covers only {coverage * 100:.1f}% of its camera timeline."
            fix = "Inspect the beginning/end of this recording for missing IMU samples."
        else:
            severity = Severity.PASSED
            message = f"dev{imu.device} overlaps {coverage * 100:.1f}% of its camera timeline."
            fix = ""
        segment.checks.append(CheckResult(
            "deep.imu.video_overlap", severity, message, fix, (str(imu.path),),
            {"device": imu.device, "coverage": coverage},
        ))
