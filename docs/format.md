# MCAP Format

Each input segment produces one MCAP containing:

- One `foxglove.CompressedVideo` image topic and one placeholder
  `foxglove.CameraCalibration` topic per discovered camera.
- `/imu/devN` messages using the published [`imu.proto`](../imu.proto) schema.
- An embedded `metadata.json` attachment describing the session and streams.
- A flat `robocap` MCAP metadata record.

Camera packet times are anchored to:

```text
session_start_utc + MP4 format.tags.comment device_clock_us + packet DTS
```

IMU samples use their device-clock nanosecond timestamp with the same session
UTC base. The converter validates temporal overlap before writing.
