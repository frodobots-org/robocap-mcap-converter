"""Convert one validated local RoboCap segment into a self-contained MCAP."""
from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timezone
from pathlib import Path

from robocap_to_mcap import __version__
from robocap_to_mcap.models import SegmentInput, SessionInput
from robocap_to_mcap.validator import _video_probe

from .imu_reader import (
    InsufficientIMUError,
    device_number_from_imu_key,
    missing_required_imu,
    samples_in_range,
)
from .mcap import MCAPBundle, build_camera_info, build_imu, build_video
from .topics import topic_prefix_for
from .video import iter_packets, probe
from .video_normalize import normalize_for_mcap


def _worker_count(camera_count: int) -> int:
    requested = int(os.environ.get("ROBOCAP_CONVERT_VIDEO_WORKERS", "6"))
    return max(1, min(requested, max(1, camera_count)))


def _convert_video(
    video,
    *,
    upload_ns: int,
    bundle: MCAPBundle,
    normalization_dir: Path,
) -> dict:
    source_probe = _video_probe(video.path)
    normalized = normalize_for_mcap(video.path, cache_dir=normalization_dir)
    stream = probe(normalized)
    packets = list(iter_packets(normalized))
    if not packets:
        raise RuntimeError(f"{video.path.name} contains no video packets")
    base_ns = upload_ns + source_probe.clock_us * 1_000
    first_seconds = packets[0].effective_dts_seconds
    prefix = topic_prefix_for(video.camera)
    image_topic = f"{prefix}/image-raw"
    info_topic = f"{prefix}/camera-info"
    first_ns = base_ns + int(first_seconds * 1_000_000_000)
    bundle.add(*build_camera_info(
        info_topic, video.camera, first_ns, stream.width, stream.height,
    ))
    for packet in packets:
        packet_ns = base_ns + int(packet.effective_dts_seconds * 1_000_000_000)
        bundle.add(*build_video(
            image_topic, video.camera, packet_ns, packet.codec, packet.data,
        ))
    return {
        "camera": video.camera,
        "topic": image_topic,
        "codec": "h265" if stream.codec == "hevc" else stream.codec,
        "width": stream.width,
        "height": stream.height,
        "message_count": len(packets),
        "device_clock_us": source_probe.clock_us,
    }


def _convert_imu(
    imu,
    *,
    upload_ns: int,
    duration_seconds: float,
    bundle: MCAPBundle,
) -> dict:
    sample_range = samples_in_range(imu.path, 0.0, duration_seconds)
    device = device_number_from_imu_key(imu.path.name)
    topic = f"/imu/dev{device}"
    for sample in sample_range.samples:
        bundle.add(*build_imu(
            topic,
            f"imu_dev{device}",
            upload_ns + sample.t_ns,
            sample.accel_xyz,
            sample.gyro_xyz,
        ))
    return {
        "device": device,
        "topic": topic,
        "message_count": len(sample_range.samples),
    }


def convert(
    session: SessionInput,
    segment: SegmentInput,
    *,
    out_path: Path,
    normalization_dir: Path,
    conversion_warnings: list[dict] | None = None,
) -> dict:
    if session.session_start is None:
        raise ValueError("session UTC timestamp is required")
    if session.robocap_id is None:
        raise ValueError("RoboCap device ID is required")
    if not segment.duration_seconds or segment.duration_seconds <= 0:
        raise ValueError("segment duration is unavailable")

    upload_ns = int(
        session.session_start.astimezone(timezone.utc).timestamp() * 1_000_000_000
    )
    bundle = MCAPBundle()
    videos: list[dict] = []
    with ThreadPoolExecutor(max_workers=_worker_count(len(segment.videos))) as pool:
        futures = {
            pool.submit(
                _convert_video,
                video,
                upload_ns=upload_ns,
                bundle=bundle,
                normalization_dir=normalization_dir,
            ): video
            for video in segment.videos
        }
        for future in as_completed(futures):
            videos.append(future.result())

    imus: list[dict] = []
    usable_devices: set[str] = set()
    for imu in segment.imus:
        try:
            stats = _convert_imu(
                imu,
                upload_ns=upload_ns,
                duration_seconds=segment.duration_seconds,
                bundle=bundle,
            )
        except (sqlite3.DatabaseError, RuntimeError) as exc:
            if imu.device in {"1", "2"}:
                raise
            imus.append({"device": imu.device, "skipped": str(exc)})
            continue
        imus.append(stats)
        if stats["message_count"]:
            usable_devices.add(stats["device"])

    missing = missing_required_imu(usable_devices)
    if missing:
        raise InsufficientIMUError(
            "missing usable required IMU device(s): "
            + ", ".join(f"dev{device}" for device in sorted(missing))
        )

    metadata = {
        "schema_version": 1,
        "converter": {"name": "robocap-mcap-converter", "version": __version__},
        "session": {
            "robocap_id": session.robocap_id,
            "session_start_utc": session.session_start.astimezone(timezone.utc).isoformat(),
            "segment": segment.number,
            "duration_seconds": segment.duration_seconds,
        },
        "streams": {"cameras": sorted(videos, key=lambda row: row["camera"]), "imus": imus},
        "calibration": {
            "included": False,
            "camera_intrinsics": False,
            "distortion_parameters": False,
            "extrinsics": False,
            "notice": "No device-specific calibration data is included.",
        },
        "supplemental": {
            "robocap": {
                "privacy_processing": {
                    "status": "raw_unblurred",
                    "face_blurred": False,
                    "notice": "Camera streams are raw and unblurred.",
                },
                "conversion_warnings": conversion_warnings or [],
            }
        },
    }
    metadata_bytes = json.dumps(metadata, indent=2, sort_keys=True).encode()
    first_ns = min((row[0] for row in bundle.messages), default=upload_ns)
    bundle.add_attachment("metadata.json", "application/json", metadata_bytes, first_ns)
    bundle.add_metadata("robocap", {
        "robocap_id": session.robocap_id,
        "segment": str(segment.number),
        "session_start_utc": metadata["session"]["session_start_utc"],
        "calibration_included": "false",
        "privacy_status": "raw_unblurred",
    })
    bundle.write(out_path)
    return {
        "segment": segment.number,
        "duration_seconds": segment.duration_seconds,
        "output_size_bytes": out_path.stat().st_size,
        "videos": sorted(videos, key=lambda row: row["camera"]),
        "imus": imus,
        "calibration_included": False,
    }
