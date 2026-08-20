from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from robocap_to_mcap.models import Severity
from robocap_to_mcap.scanner import normalize_robocap_id, scan_session


HEAD_CAMERAS = (
    "left_eye", "right_eye", "left_front", "right_front", "left", "right",
)


def _touch(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_scan_groups_new_rig_files_and_parses_full_utc_anchor(tmp_path: Path) -> None:
    root = tmp_path / "75cd2758f7384110_20260720_034459_session6"
    for camera in HEAD_CAMERAS:
        _touch(root / f"robocap_segment1_video_{camera}.mp4")
    for camera in ("left_down", "right_down"):
        _touch(root / "robowrist" / f"robowrist_segment1_video_{camera}.mp4")
    for rig in ("robocap", "robowrist"):
        for side in ("left", "right"):
            _touch(root / rig / f"{rig}_segment1_imu_{side}.db")
    _touch(root / "robocap_segment1_mag_middle.db")

    session = scan_session(root)

    assert session.session_start == datetime(2026, 7, 20, 3, 44, 59, tzinfo=timezone.utc)
    assert session.robocap_id == "75cd2758f7384110"
    assert len(session.segments) == 1
    segment = session.segments[0]
    assert {video.camera for video in segment.videos} == {
        "left-eye", "right-eye", "left-front", "right-front", "left", "right",
        "left_down", "right_down",
    }
    assert {imu.device for imu in segment.imus} == {"1", "2", "3", "4"}
    assert not segment.errors
    assert any(check.check_id == "session.files.ignored" for check in session.checks)


def test_scan_reports_missing_timestamp_duplicate_camera_and_required_imu(tmp_path: Path) -> None:
    root = tmp_path / "input"
    _touch(root / "a" / "robocap_segment2_video_left_eye.mp4")
    _touch(root / "b" / "robocap_segment2_video_left_eye.mp4")
    _touch(root / "robocap_segment2_imu_left.db")

    session = scan_session(root)
    checks = session.checks + session.segments[0].checks
    errors = {check.check_id for check in checks if check.severity == Severity.ERROR}

    assert "session.timestamp.missing" in errors
    assert "session.robocap_id.missing" in errors
    assert "segment.video.duplicate_topic" in errors
    assert "segment.imu.required_missing" in errors


def test_scan_supports_legacy_names_and_multiple_segments(tmp_path: Path) -> None:
    root = tmp_path / "75cd2758f7384110_20260102_030405_session7"
    for segment in (1, 3):
        _touch(root / f"video_dev2_session7_segment{segment}_left-eye.mp4")
        _touch(root / f"IMUWriter_dev1_session7_segment{segment}.db")
        _touch(root / f"IMUWriter_dev2_session7_segment{segment}.db")

    session = scan_session(root)

    assert [segment.number for segment in session.segments] == [1, 3]
    assert all(not segment.errors for segment in session.segments)


def test_scan_multi_file_drop_uses_only_selected_files(tmp_path: Path) -> None:
    root = tmp_path / "75cd2758f7384110" / "20260720_034459_session6"
    selected = [
        _touch(root / "robocap_segment1_video_left_eye.mp4"),
        _touch(root / "robocap_segment1_imu_left.db"),
        _touch(root / "robocap_segment1_imu_right.db"),
    ]
    _touch(root / "robocap_segment2_video_right_eye.mp4")

    session = scan_session(root, input_paths=selected)

    assert [segment.number for segment in session.segments] == [1]
    assert [video.camera for video in session.segments[0].videos] == ["left-eye"]
    assert session.robocap_id == "75cd2758f7384110"


def test_robocap_id_override_is_normalized_and_validated(tmp_path: Path) -> None:
    root = tmp_path / "20260720_034459_session6"
    _touch(root / "robocap_segment1_video_left_eye.mp4")
    _touch(root / "robocap_segment1_imu_left.db")
    _touch(root / "robocap_segment1_imu_right.db")

    session = scan_session(root, robocap_id="ROBOCAP_DEVICE_7")

    assert session.robocap_id == "robocap_device_7"
    assert not session.has_session_error

    try:
        normalize_robocap_id("not valid!")
    except ValueError as exc:
        assert "RoboCap ID" in str(exc)
    else:
        raise AssertionError("invalid RoboCap ID was accepted")
