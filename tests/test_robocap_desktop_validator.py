from __future__ import annotations

from pathlib import Path

from robocap_to_mcap.engine.imu_reader import G
from robocap_to_mcap import validator
from robocap_to_mcap.models import Severity
from robocap_to_mcap.scanner import scan_session
from robocap_to_mcap.validator import IMUProbe, VideoProbe, validate_session_deep


def _touch(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _session(tmp_path: Path):
    root = tmp_path / "75cd2758f7384110_20260720_034459_session6"
    _touch(root / "robocap_segment1_video_left_eye.mp4", b"video-left")
    _touch(root / "robocap_segment1_video_right_eye.mp4", b"video-right")
    _touch(root / "robocap_segment1_imu_left.db", b"imu-left")
    _touch(root / "robocap_segment1_imu_right.db", b"imu-right")
    return scan_session(root)


def test_deep_validation_makes_segment_ready(monkeypatch, tmp_path: Path) -> None:
    session = _session(tmp_path)
    clocks = {"left_eye": 90_000_000, "right_eye": 90_000_100}

    def video_probe(path: Path) -> VideoProbe:
        return VideoProbe(
            clock_us=clocks["left_eye" if "left_eye" in path.name else "right_eye"],
            codec="h264", width=1920, height=1080, duration_seconds=10.0,
            has_b_frames=False, is_vfr=False,
        )

    monkeypatch.setattr(validator, "_video_probe", video_probe)
    monkeypatch.setattr(validator, "_imu_probe", lambda _path: IMUProbe(
        first_ns=90_000_000_000, last_ns=100_000_000_000,
        sample_count=1000, max_gap_ms=10.0, gap_count=0, gravity_mean=G,
    ))

    validate_session_deep(session)

    segment = session.segments[0]
    assert segment.is_ready
    assert segment.duration_seconds == 10.0
    assert not segment.errors
    assert any(check.check_id == "deep.imu.video_overlap" for check in segment.checks)


def test_deep_validation_reports_unreadable_video_and_no_overlap(
    monkeypatch, tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    left = session.segments[0].videos[0].path

    def video_probe(path: Path) -> VideoProbe:
        if path == left:
            raise ValueError("format.tags.comment is missing")
        return VideoProbe(0, "h264", 1920, 1080, 10.0, False, False)

    monkeypatch.setattr(validator, "_video_probe", video_probe)
    monkeypatch.setattr(validator, "_imu_probe", lambda _path: IMUProbe(
        first_ns=20_000_000_000, last_ns=30_000_000_000,
        sample_count=1000, max_gap_ms=10.0, gap_count=0, gravity_mean=G,
    ))

    validate_session_deep(session)
    errors = [c for c in session.segments[0].checks if c.severity == Severity.ERROR]

    assert any(c.check_id == "deep.video.unreadable" for c in errors)
    assert any(c.check_id == "deep.imu.video_overlap" for c in errors)
    assert not session.segments[0].is_ready


def test_deep_validation_detects_byte_identical_files(monkeypatch, tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.segments[0].videos[1].path.write_bytes(
        session.segments[0].videos[0].path.read_bytes()
    )
    monkeypatch.setattr(validator, "_video_probe", lambda _path: VideoProbe(
        90_000_000, "h264", 1920, 1080, 10.0, False, False,
    ))
    monkeypatch.setattr(validator, "_imu_probe", lambda _path: IMUProbe(
        90_000_000_000, 100_000_000_000, 1000, 10.0, 0, G,
    ))

    validate_session_deep(session)

    assert any(
        check.check_id == "session.files.byte_duplicate"
        and check.severity == Severity.ERROR
        for check in session.checks
    )
