from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import pytest

from robocap_to_mcap import cloud_cli
from robocap_to_mcap.models import ConversionResult, SegmentInput, SessionInput


class _Paginator:
    def __init__(self, objects):
        self.objects = objects

    def paginate(self, **kwargs):
        prefix = kwargs["Prefix"]
        yield {"Contents": [item for item in self.objects if item["Key"].startswith(prefix)]}


class FakeS3:
    def __init__(self):
        self.objects = [
            {"Key": "raw/20260720_034459_session6/robocap_segment1_video_left_eye.mp4", "Size": 5},
            {"Key": "raw/20260720_034459_session6/notes.txt", "Size": 7},
        ]
        self.uploads = []
        self.puts = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return _Paginator(self.objects)

    def download_file(self, bucket, key, destination):
        Path(destination).write_bytes(b"video")

    def upload_file(self, source, bucket, key):
        self.uploads.append((Path(source).read_bytes(), bucket, key))

    def put_object(self, **kwargs):
        self.puts.append(kwargs)


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        input_uri=cloud_cli.parse_s3_uri("s3://source/raw/20260720_034459_session6/"),
        output_uri=cloud_cli.parse_s3_uri("s3://destination/converted/job-1/"),
        session_start=datetime(2026, 7, 20, 3, 44, 59, tzinfo=timezone.utc),
        segment=[],
        video_workers=2,
        job_id="job-123",
        debug=False,
        json=True,
    )


@pytest.mark.parametrize("value", ["bucket/prefix", "s3:///prefix", "s3://bucket/a/../b"])
def test_parse_s3_uri_rejects_invalid_values(value):
    with pytest.raises(argparse.ArgumentTypeError):
        cloud_cli.parse_s3_uri(value)


def test_parse_s3_uri_normalizes_slashes():
    location = cloud_cli.parse_s3_uri("s3://bucket/path/to/session/")
    assert location.bucket == "bucket"
    assert location.prefix == "path/to/session"
    assert location.uri == "s3://bucket/path/to/session"


def test_s3_run_uploads_only_verified_outputs(monkeypatch):
    client = FakeS3()
    segment = SegmentInput(number=1, validated_fingerprint=())
    session = SessionInput(
        root=Path("/unused"),
        session_start=datetime(2026, 7, 20, tzinfo=timezone.utc),
        segments=[segment],
    )

    monkeypatch.setattr(cloud_cli, "scan_session", lambda *args, **kwargs: session)
    monkeypatch.setattr(cloud_cli, "validate_session_deep", lambda value: None)

    def fake_convert(session, segment, *, output_dir, debug):
        output = output_dir / "segment_1.mcap"
        output.write_bytes(b"verified-mcap")
        return ConversionResult(1, output, True, (), {"messages": 42})

    monkeypatch.setattr(cloud_cli, "convert_segment", fake_convert)
    code, report = cloud_cli.run(_args(), s3_client=client)

    assert code == 0
    assert report["status"] == "complete"
    assert [item["key"] for item in report["downloaded_objects"]] == [
        "raw/20260720_034459_session6/robocap_segment1_video_left_eye.mp4"
    ]
    assert client.uploads == [
        (b"verified-mcap", "destination", "converted/job-1/segment_1.mcap")
    ]
    assert report["outputs"][0]["sha256"] == (
        "9dc9cf2b2c12853e4acdb32363cb685c97cfce1c567b6d582d24f014c4a5b97e"
    )
    assert client.puts[0]["Key"] == "converted/job-1/_reports/job-123.json"


def test_s3_run_reports_validation_failure_without_output(monkeypatch):
    client = FakeS3()
    segment = SegmentInput(number=1, validated_fingerprint=None)
    session = SessionInput(Path("/unused"), None, [segment])
    monkeypatch.setattr(cloud_cli, "scan_session", lambda *args, **kwargs: session)
    monkeypatch.setattr(cloud_cli, "validate_session_deep", lambda value: None)

    code, report = cloud_cli.run(_args(), s3_client=client)

    assert code == 2
    assert report["status"] == "validation_failed"
    assert client.uploads == []
    assert len(client.puts) == 1


def test_s3_run_rejects_same_input_and_output():
    args = _args()
    args.output_uri = args.input_uri
    with pytest.raises(ValueError, match="must differ"):
        cloud_cli.run(args, s3_client=FakeS3())
