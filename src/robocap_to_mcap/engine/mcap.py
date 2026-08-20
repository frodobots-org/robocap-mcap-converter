"""Foxglove MCAP message construction with no calibration dependencies."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from foxglove_schemas_protobuf.CameraCalibration_pb2 import CameraCalibration
from foxglove_schemas_protobuf.CompressedVideo_pb2 import CompressedVideo
from google.protobuf.timestamp_pb2 import Timestamp
from mcap_protobuf.writer import Writer

from robocap_to_mcap.proto import imu_pb2


def timestamp(ns: int) -> Timestamp:
    value = Timestamp()
    value.seconds = ns // 1_000_000_000
    value.nanos = int(ns % 1_000_000_000)
    return value


@dataclass(frozen=True)
class Attachment:
    name: str
    media_type: str
    data: bytes
    timestamp_ns: int


@dataclass
class MCAPBundle:
    messages: list[tuple[int, str, Any]] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    metadata: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def add(self, log_time_ns: int, topic: str, message: Any) -> None:
        self.messages.append((log_time_ns, topic, message))

    def add_attachment(
        self, name: str, media_type: str, data: bytes, timestamp_ns: int,
    ) -> None:
        self.attachments.append(Attachment(name, media_type, data, timestamp_ns))

    def add_metadata(self, name: str, values: dict[str, str]) -> None:
        self.metadata.append((name, dict(values)))

    def write(self, output: Path) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        self.messages.sort(key=lambda item: item[0])
        with output.open("wb") as stream, Writer(stream) as writer:
            inner = writer._writer  # mcap-protobuf exposes attachments through its wrapped writer.
            for item in self.attachments:
                inner.add_attachment(
                    create_time=item.timestamp_ns,
                    log_time=item.timestamp_ns,
                    name=item.name,
                    media_type=item.media_type,
                    data=item.data,
                )
            for name, values in self.metadata:
                inner.add_metadata(name=name, data=values)
            for log_time_ns, topic, message in self.messages:
                writer.write_message(
                    topic=topic,
                    message=message,
                    log_time=log_time_ns,
                    publish_time=log_time_ns,
                )


def build_camera_info(
    topic: str, frame_id: str, ns: int, width: int, height: int,
) -> tuple[int, str, CameraCalibration]:
    """Build non-device-specific placeholder camera metadata.

    Real intrinsics, distortion coefficients, extrinsics, calibration URIs,
    and transforms are deliberately out of scope for this public converter.
    """
    message = CameraCalibration()
    message.timestamp.CopyFrom(timestamp(ns))
    message.frame_id = frame_id
    message.width = width
    message.height = height
    message.distortion_model = "plumb_bob"
    fx = float(width)
    fy = float(width)
    cx = width / 2.0
    cy = height / 2.0
    message.K[:] = [fx, 0.0, cx, 0.0, fy, cy, 0.0, 0.0, 1.0]
    message.D[:] = [0.0] * 5
    message.R[:] = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    message.P[:] = [fx, 0.0, cx, 0.0, 0.0, fy, cy, 0.0, 0.0, 0.0, 1.0, 0.0]
    return ns, topic, message


def build_video(
    topic: str, frame_id: str, ns: int, codec: str, payload: bytes,
) -> tuple[int, str, CompressedVideo]:
    message = CompressedVideo()
    message.timestamp.CopyFrom(timestamp(ns))
    message.frame_id = frame_id
    message.format = "h265" if codec == "hevc" else codec
    message.data = payload
    return ns, topic, message


def build_imu(
    topic: str,
    frame_id: str,
    ns: int,
    accel_xyz: tuple[float, float, float],
    gyro_xyz: tuple[float, float, float],
) -> tuple[int, str, imu_pb2.IMU]:
    message = imu_pb2.IMU()
    message.timestamp.CopyFrom(timestamp(ns))
    message.frame_id = frame_id
    message.accel_x, message.accel_y, message.accel_z = accel_xyz
    message.gyro_x, message.gyro_y, message.gyro_z = gyro_xyz
    return ns, topic, message
