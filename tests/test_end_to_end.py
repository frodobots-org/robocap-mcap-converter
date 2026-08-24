from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from mcap.reader import make_reader

from robocap_to_mcap.cli import main
from robocap_to_mcap.conversion import convert_segment
from robocap_to_mcap.engine.profile import profile_mcap
from robocap_to_mcap.scanner import discover_session_roots, scan_session
from robocap_to_mcap.validator import validate_session_deep


def _video(path: Path, color: str) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c={color}:s=64x64:r=30:d=1",
            "-c:v", "libx264", "-bf", "0", "-g", "30",
            "-metadata", "comment=90000000", str(path),
        ],
        check=True,
    )


def _imu(path: Path, device_offset: int) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE acc_data "
            "(id INTEGER, imuid_ INTEGER, x INTEGER, y INTEGER, z INTEGER, timestamp INTEGER)"
        )
        connection.execute(
            "CREATE TABLE gyro_data "
            "(id INTEGER, imuid_ INTEGER, x INTEGER, y INTEGER, z INTEGER, timestamp INTEGER)"
        )
        for index in range(101):
            timestamp = 90_000_000_000 + index * 10_000_000
            connection.execute(
                "INSERT INTO acc_data VALUES (?, ?, ?, ?, ?, ?)",
                (index, device_offset, device_offset, 0, 8192, timestamp),
            )
            connection.execute(
                "INSERT INTO gyro_data VALUES (?, ?, ?, ?, ?, ?)",
                (index, device_offset, device_offset, 0, 0, timestamp),
            )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
def test_real_session_converts_to_verified_mcap_without_calibration(tmp_path: Path) -> None:
    session = tmp_path / "75cd2758f7384110_20260720_034459_session6"
    session.mkdir()
    _video(session / "robocap_segment1_video_left_eye.mp4", "red")
    _video(session / "robocap_segment1_video_right_eye.mp4", "blue")
    _imu(session / "robocap_segment1_imu_left.db", 1)
    _imu(session / "robocap_segment1_imu_right.db", 2)

    assert main([str(session), "--video-workers", "2"]) == 0

    output = session / "mcap" / "75cd2758f7384110_20260720_034459_segment1.mcap"
    profile = profile_mcap(output)
    assert profile.topics["/top-left-camera/image-raw"].message_count == 30
    assert profile.topics["/top-right-camera/image-raw"].message_count == 30
    assert profile.topics["/imu/dev1"].message_count > 0
    assert profile.topics["/imu/dev2"].message_count > 0
    assert "/tf-static" not in profile.topics

    with output.open("rb") as stream:
        attachment = next(
            item for item in make_reader(stream).iter_attachments()
            if item.name == "metadata.json"
        )
    metadata = json.loads(attachment.data)
    assert metadata["calibration"] == {
        "included": False,
        "camera_intrinsics": False,
        "distortion_parameters": False,
        "extrinsics": False,
        "notice": "No device-specific calibration data is included.",
    }


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
def test_bulk_parent_discovers_and_converts_multiple_sessions(tmp_path: Path) -> None:
    roots = [
        tmp_path / "75cd2758f7384110_20260720_034459_session6",
        tmp_path / "8c94d6053f48d3e4_20260721_041500_session7",
    ]
    for index, root in enumerate(roots, start=1):
        root.mkdir()
        _video(root / "robocap_segment1_video_left_eye.mp4", "red")
        _video(root / "robocap_segment1_video_right_eye.mp4", "blue")
        _imu(root / "robocap_segment1_imu_left.db", index * 10 + 1)
        _imu(root / "robocap_segment1_imu_right.db", index * 10 + 2)

    discovered = discover_session_roots([tmp_path])
    sessions = [validate_session_deep(scan_session(root)) for root in discovered]
    results = [
        convert_segment(session, session.segments[0])
        for session in sessions
    ]

    assert discovered == [root.resolve() for root in roots]
    assert all(result.success for result in results)
    assert [result.output_path.name for result in results] == [
        "75cd2758f7384110_20260720_034459_segment1.mcap",
        "8c94d6053f48d3e4_20260721_041500_segment1.mcap",
    ]
    assert all(result.output_path.parent.name == "mcap" for result in results)
