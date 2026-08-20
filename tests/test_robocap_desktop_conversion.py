from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from robocap_to_mcap import conversion
from robocap_to_mcap.models import (
    CheckResult,
    IMUInput,
    SegmentInput,
    SessionInput,
    Severity,
    VideoInput,
)


def _ready_session(tmp_path: Path) -> tuple[SessionInput, SegmentInput]:
    root = tmp_path / "75cd2758f7384110_20260720_034459_session6"
    root.mkdir()
    videos = []
    for index, camera in enumerate(("left-eye", "right-eye")):
        path = root / f"robocap_segment1_video_{camera.replace('-', '_')}.mp4"
        path.write_bytes(f"video-{camera}".encode())
        videos.append(VideoInput(path, 1, camera, "robocap", index))
    imus = []
    for device, side in (("1", "left"), ("2", "right")):
        path = root / f"robocap_segment1_imu_{side}.db"
        path.write_bytes(f"imu-{device}".encode())
        imus.append(IMUInput(path, 1, device, "robocap", side))
    segment = SegmentInput(1, videos, imus, duration_seconds=10.0)
    segment.validated_fingerprint = segment.fingerprint()
    session = SessionInput(
        root,
        datetime(2026, 7, 20, 3, 44, 59, tzinfo=timezone.utc),
        [segment],
        robocap_id="75cd2758f7384110",
    )
    return session, segment


def test_output_filename_carries_device_timestamp_and_segment(tmp_path: Path) -> None:
    session, segment = _ready_session(tmp_path)
    assert conversion.output_filename(session, segment) == (
        "75cd2758f7384110_20260720_034459_segment1.mcap"
    )


def test_convert_segment_publishes_only_after_post_checks(
    monkeypatch, tmp_path: Path,
) -> None:
    session, segment = _ready_session(tmp_path)

    def fake_convert(_session, _segment, *, out_path, **kwargs):
        assert kwargs["normalization_dir"].parent == out_path.parent
        assert kwargs["conversion_warnings"] == []
        out_path.write_bytes(b"valid-mcap")
        return {"duration_seconds": 10.0}

    monkeypatch.setattr(conversion, "convert", fake_convert)
    monkeypatch.setattr(conversion, "verify_mcap", lambda *_args: [
        CheckResult("post.ok", Severity.PASSED, "valid"),
    ])

    result = conversion.convert_segment(session, segment)

    assert result.success
    assert result.output_path == (
        session.root / "mcap" / "75cd2758f7384110_20260720_034459_segment1.mcap"
    )
    assert result.output_path.read_bytes() == b"valid-mcap"
    assert list((session.root / "mcap").glob(".robocap-mcap-*")) == []
    assert not (
        session.root / "mcap" /
        "75cd2758f7384110_20260720_034459_segment1.mcap.metadata.json"
    ).exists()


def test_convert_segment_embeds_preflight_warnings(
    monkeypatch, tmp_path: Path,
) -> None:
    session, segment = _ready_session(tmp_path)
    segment.checks.append(CheckResult(
        "deep.video.normalization_required",
        Severity.WARNING,
        "A repair pass is required.",
        "No user action is required.",
    ))
    captured = {}

    def fake_convert(_session, _segment, *, out_path, **kwargs):
        captured.update(kwargs)
        out_path.write_bytes(b"valid-mcap")
        return {}

    monkeypatch.setattr(conversion, "convert", fake_convert)
    monkeypatch.setattr(conversion, "verify_mcap", lambda *_args: [
        CheckResult("post.ok", Severity.PASSED, "valid"),
    ])

    result = conversion.convert_segment(session, segment)

    assert result.success
    assert captured["conversion_warnings"] == [{
        "id": "deep.video.normalization_required",
        "severity": "warning",
        "message": "A repair pass is required.",
        "fix": "No user action is required.",
        "files": [],
        "measured": {},
    }]


def test_convert_segment_quarantines_failed_post_check(
    monkeypatch, tmp_path: Path,
) -> None:
    session, segment = _ready_session(tmp_path)
    monkeypatch.setattr(conversion, "convert", lambda _session, _segment, *, out_path, **_kwargs: (
        out_path.write_bytes(b"bad-mcap") or {}
    ))
    monkeypatch.setattr(conversion, "verify_mcap", lambda *_args: [
        CheckResult("post.failed", Severity.ERROR, "bad output"),
    ])

    result = conversion.convert_segment(session, segment)

    assert not result.success
    assert result.output_path.name == (
        "75cd2758f7384110_20260720_034459_segment1.mcap.invalid"
    )
    assert result.output_path.read_bytes() == b"bad-mcap"
    assert not (
        session.root / "mcap" / "75cd2758f7384110_20260720_034459_segment1.mcap"
    ).exists()
