from __future__ import annotations

import json
from datetime import timezone
from pathlib import Path

from mcap.reader import make_reader

from .engine.profile import profile_mcap
from .engine.topics import topic_prefix_for

from .models import CheckResult, SegmentInput, SessionInput, Severity
from .validator import _video_probe


VIDEO_DURATION_TOLERANCE_SECONDS = 2.0
ABSOLUTE_ANCHOR_TOLERANCE_NS = 2_000_000_000


def _result(
    check_id: str,
    ok: bool,
    passed: str,
    failed: str,
    fix: str = "",
    *,
    measured: dict | None = None,
) -> CheckResult:
    return CheckResult(
        check_id,
        Severity.PASSED if ok else Severity.ERROR,
        passed if ok else failed,
        "" if ok else fix,
        measured=measured or {},
    )


def verify_mcap(
    path: Path,
    session: SessionInput,
    segment: SegmentInput,
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    try:
        with path.open("rb") as stream:
            reader = make_reader(stream, validate_crcs=True)
            summary = reader.get_summary()
            attachments = list(reader.iter_attachments())
        checks.append(_result(
            "post.mcap.footer_summary",
            summary is not None,
            "MCAP footer, summary, and CRCs are valid.",
            "The MCAP has no valid footer/summary.",
            "Regenerate this segment; never distribute this output.",
        ))
    except Exception as exc:
        return [CheckResult(
            "post.mcap.readable",
            Severity.ERROR,
            f"The generated MCAP cannot be read: {exc}.",
            "Regenerate this segment; never distribute this output.",
            (str(path),),
        )]

    profile = profile_mcap(path)
    for video in segment.videos:
        prefix = topic_prefix_for(video.camera)
        image_topic = f"{prefix}/image-raw"
        info_topic = f"{prefix}/camera-info"
        image = profile.topics.get(image_topic)
        info = profile.topics.get(info_topic)
        checks.append(_result(
            "post.camera.image_present",
            image is not None and image.message_count > 0,
            f"{video.camera} contains {image.message_count if image else 0:,} video messages.",
            f"{video.camera} has no video messages at {image_topic}.",
            "Inspect the source MP4 and regenerate this segment.",
            measured={"camera": video.camera, "topic": image_topic,
                      "message_count": image.message_count if image else 0},
        ))
        checks.append(_result(
            "post.camera.info_present",
            info is not None and info.message_count > 0,
            f"{video.camera} has a camera-info message.",
            f"{video.camera} is missing {info_topic}.",
            "Regenerate this segment; the camera topic pair is incomplete.",
            measured={"camera": video.camera, "topic": info_topic,
                      "message_count": info.message_count if info else 0},
        ))
        if image and image.first_log_time_ns is not None and session.session_start is not None:
            expected = int(session.session_start.astimezone(timezone.utc).timestamp() * 1e9)
            expected += _video_probe(video.path).clock_us * 1000
            drift = abs(image.first_log_time_ns - expected)
            checks.append(_result(
                "post.camera.absolute_anchor",
                drift <= ABSOLUTE_ANCHOR_TOLERANCE_NS,
                f"{video.camera} absolute clock anchor is within {drift / 1e6:.2f} ms.",
                f"{video.camera} absolute clock anchor drifted by {drift / 1e9:.3f}s.",
                "Verify the folder UTC timestamp and MP4 comment tag.",
                measured={"camera": video.camera, "drift_ns": drift,
                          "expected_first_log_time_ns": expected,
                          "actual_first_log_time_ns": image.first_log_time_ns},
            ))

    for device in ("1", "2"):
        topic = f"/imu/dev{device}"
        imu = profile.topics.get(topic)
        checks.append(_result(
            "post.imu.required_present",
            imu is not None and imu.message_count > 0,
            f"Required {topic} contains {imu.message_count if imu else 0:,} messages.",
            f"Required {topic} is absent or empty.",
            "Restore a usable required IMU database and regenerate.",
            measured={"device": device, "message_count": imu.message_count if imu else 0},
        ))

    non_monotonic = sorted(topic for topic, item in profile.topics.items() if not item.monotonic)
    checks.append(_result(
        "post.topics.monotonic",
        not non_monotonic,
        "Every topic is monotonic in stored log-time order.",
        f"Non-monotonic topics: {', '.join(non_monotonic)}.",
        "Regenerate the MCAP; downstream playback timing is unsafe.",
        measured={"non_monotonic_topics": non_monotonic},
    ))

    image_profiles = [
        profile.topics.get(f"{topic_prefix_for(video.camera)}/image-raw")
        for video in segment.videos
    ]
    image_durations = [
        (item.last_log_time_ns - item.first_log_time_ns) / 1e9
        for item in image_profiles
        if item and item.first_log_time_ns is not None and item.last_log_time_ns is not None
    ]
    actual_duration = max(image_durations, default=0.0)
    expected_duration = segment.duration_seconds or 0.0
    duration_delta = abs(actual_duration - expected_duration)
    checks.append(_result(
        "post.duration.matches",
        bool(image_durations) and duration_delta <= VIDEO_DURATION_TOLERANCE_SECONDS,
        f"Video duration is {actual_duration:.3f}s (expected {expected_duration:.3f}s).",
        f"Video duration is {actual_duration:.3f}s, expected {expected_duration:.3f}s.",
        "Inspect truncated cameras or normalization output before retrying.",
        measured={"actual_seconds": actual_duration, "expected_seconds": expected_duration,
                  "delta_seconds": duration_delta},
    ))

    metadata_payload = None
    for attachment in attachments:
        if attachment.name == "metadata.json" and "json" in attachment.media_type:
            try:
                metadata_payload = json.loads(attachment.data)
            except (UnicodeDecodeError, json.JSONDecodeError):
                metadata_payload = None
            break
    privacy = (
        metadata_payload.get("supplemental", {}).get("robocap", {}).get("privacy_processing", {})
        if isinstance(metadata_payload, dict)
        else {}
    )
    privacy_ok = (
        privacy.get("status") == "raw_unblurred"
        and privacy.get("face_blurred") is False
        and "unblurred" in str(privacy.get("notice", "")).lower()
    )
    checks.append(_result(
        "post.metadata.raw_unblurred",
        privacy_ok,
        "Embedded metadata explicitly marks every camera as raw and unblurred.",
        "Embedded metadata does not clearly identify this footage as raw and unblurred.",
        "Regenerate using privacy_status=raw_unblurred.",
    ))
    return checks
