# RoboCap MCAP Converter

Open-source tools for validating and converting raw RoboCap and RoboWrist
recordings into self-contained [MCAP](https://mcap.dev/) files.

The converter accepts synchronized camera MP4s and IMU SQLite databases from
one recording session. It validates filenames, required streams, clock tags,
timestamp synchronization, video codecs, and IMU integrity, then writes one
MCAP per segment.

## Features

- Six RoboCap cameras, with two optional RoboWrist cameras
- Required head IMUs and optional wrist IMUs
- MP4 `comment` clock anchoring and preserved per-camera offsets
- H.264/H.265 Annex-B payloads for compatible playback
- Automatic B-frame and incompatible SPS normalization
- IMU conversion to SI units with gyro interpolation
- Automatic structural, timing, and post-write MCAP validation
- Windows drag-and-drop app, local CLI, Docker, S3, AWS Batch, and Kubernetes
- No database, FrodoBots service, telemetry, or inbound network listener

## Input Layout

Drop or mount a timestamped session directory:

```text
75cd2758f7384110_20260720_034459_session6/
  robocap_segment1_video_left_eye.mp4
  robocap_segment1_video_right_eye.mp4
  robocap_segment1_video_left_front.mp4
  robocap_segment1_video_right_front.mp4
  robocap_segment1_video_left.mp4
  robocap_segment1_video_right.mp4
  robocap_segment1_imu_left.db
  robocap_segment1_imu_right.db
  robowrist_segment1_video_left_down.mp4       # optional
  robowrist_segment1_video_right_down.mp4      # optional
  robowrist_segment1_imu_left.db               # optional
  robowrist_segment1_imu_right.db              # optional
```

Each MP4 must contain a numeric `format.tags.comment` value holding its
`device_clock_us`. Legacy RoboCap filenames are also supported.

## Docker Quick Start

```bash
docker pull bitrobot/robocap-mcap-converter:0.2.1
mkdir -p sessions output
# The container runs as non-root UID/GID 10001.
sudo chown 10001:10001 output

docker run --rm \
  -v "$PWD/sessions:/work/sessions:ro" \
  -v "$PWD/output:/work/output" \
  bitrobot/robocap-mcap-converter:0.2.1 \
  local /work/sessions/75cd2758f7384110_20260720_034459_session6 \
  --output-dir /work/output
```

Mount the parent directory, not only the session contents. The timestamped
session folder name is part of the clock anchor and must remain visible inside
the container.

Build directly from source:

```bash
docker build -t robocap-mcap-converter .
```

## Local CLI

Requires Python 3.12+, `uv`, FFmpeg, and FFprobe:

```bash
uv sync --extra desktop --group dev
uv run robocap-mcap ./75cd2758f7384110_20260720_034459_session6 --validate-only
uv run robocap-mcap ./75cd2758f7384110_20260720_034459_session6
```

Outputs are written under `SESSION/mcap/` using:

```text
<robocap_id>_<YYYYMMDD_HHMMSS>_segment<N>.mcap
```

## S3 Mode

The container uses the standard AWS credential chain. Prefer workload roles:

```bash
docker run --rm bitrobot/robocap-mcap-converter:0.2.1 \
  s3 \
  --input-uri s3://customer-raw/session/20260720_034459_session6/ \
  --output-uri s3://customer-derived/mcap/job-001/ \
  --video-workers 4 \
  --json
```

See [`docs/cloud.md`](docs/cloud.md) and [`docs/format.md`](docs/format.md).

## Development

```bash
uv sync --extra desktop --group dev
uv run pytest -q
```

## License

Apache License 2.0. FFmpeg and other dependencies retain their own licenses;
see [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
