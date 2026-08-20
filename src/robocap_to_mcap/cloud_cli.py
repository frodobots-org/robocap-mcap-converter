from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import boto3

from .cli import _utc_datetime
from .conversion import convert_segment
from .reporting import conversion_report, session_report
from .runtime import configure_bundled_tools
from .scanner import scan_session
from .validator import validate_session_deep


SUPPORTED_INPUT_SUFFIXES = {".mp4", ".db"}


@dataclass(frozen=True)
class S3Location:
    bucket: str
    prefix: str

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}" if self.prefix else f"s3://{self.bucket}"


def parse_s3_uri(value: str) -> S3Location:
    if not value.startswith("s3://"):
        raise argparse.ArgumentTypeError("must start with s3://")
    remainder = value[5:]
    bucket, separator, prefix = remainder.partition("/")
    if not bucket or bucket in {".", ".."}:
        raise argparse.ArgumentTypeError("S3 bucket is missing")
    normalized = prefix.strip("/") if separator else ""
    if any(part in {"", ".", ".."} for part in PurePosixPath(normalized).parts):
        raise argparse.ArgumentTypeError("S3 prefix contains an unsafe path component")
    return S3Location(bucket, normalized)


def _join_prefix(prefix: str, *parts: str) -> str:
    return "/".join(item.strip("/") for item in (prefix, *parts) if item.strip("/"))


def _relative_key(key: str, prefix: str) -> PurePosixPath:
    relative = key[len(prefix):].lstrip("/") if prefix else key
    path = PurePosixPath(relative)
    if not relative or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe input object key: {key}")
    return path


def _download_inputs(client: Any, source: S3Location, root: Path) -> list[dict[str, Any]]:
    paginator = client.get_paginator("list_objects_v2")
    downloaded: list[dict[str, Any]] = []
    listing_prefix = f"{source.prefix}/" if source.prefix else ""
    for page in paginator.paginate(Bucket=source.bucket, Prefix=listing_prefix):
        for item in page.get("Contents", []):
            key = item["Key"]
            if key.endswith("/") or Path(key).suffix.lower() not in SUPPORTED_INPUT_SUFFIXES:
                continue
            relative = _relative_key(key, source.prefix)
            destination = root.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(source.bucket, key, str(destination))
            downloaded.append({"key": key, "size": int(item.get("Size", destination.stat().st_size))})
    return downloaded


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _upload_file(client: Any, path: Path, destination: S3Location, key: str) -> dict[str, Any]:
    client.upload_file(str(path), destination.bucket, key)
    return {
        "uri": f"s3://{destination.bucket}/{key}",
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="robocap-mcap-cloud s3",
        description="Convert one raw RoboCap S3 session prefix into verified MCAP files.",
    )
    parser.add_argument("--input-uri", required=True, type=parse_s3_uri)
    parser.add_argument("--output-uri", required=True, type=parse_s3_uri)
    parser.add_argument("--session-start", type=_utc_datetime,
                        help="UTC ISO timestamp override when absent from the input prefix")
    parser.add_argument("--robocap-id",
                        help="RoboCap device ID override when absent from the input prefix")
    parser.add_argument("--segment", action="append", type=int, default=[])
    parser.add_argument("--video-workers", type=int, default=4)
    parser.add_argument("--job-id", default=os.environ.get("AWS_BATCH_JOB_ID"))
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def _base_report(args: argparse.Namespace, job_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "job_id": job_id,
        "input_uri": args.input_uri.uri,
        "output_uri": args.output_uri.uri,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "downloaded_objects": [],
        "outputs": [],
        "errors": [],
    }


def run(args: argparse.Namespace, *, s3_client: Any | None = None) -> tuple[int, dict[str, Any]]:
    if args.video_workers < 1:
        raise ValueError("--video-workers must be positive")
    source, destination = args.input_uri, args.output_uri
    if source.bucket == destination.bucket and source.prefix == destination.prefix:
        raise ValueError("input and output S3 locations must differ")

    os.environ["ROBOCAP_CONVERT_VIDEO_WORKERS"] = str(args.video_workers)
    client = s3_client or boto3.client("s3")
    job_id = args.job_id or f"local-{uuid.uuid4().hex[:12]}"
    report = _base_report(args, job_id)
    started = time.monotonic()
    exit_code = 1

    with tempfile.TemporaryDirectory(prefix="robocap-mcap-cloud-") as temporary:
        work_root = Path(temporary)
        session_name = PurePosixPath(source.prefix).name or "session"
        input_root = work_root / "input" / session_name
        output_root = work_root / "output"
        input_root.mkdir(parents=True)
        output_root.mkdir(parents=True)
        try:
            report["downloaded_objects"] = _download_inputs(client, source, input_root)
            if not report["downloaded_objects"]:
                raise ValueError("input prefix contains no supported .mp4 or .db files")

            session = scan_session(
                input_root,
                session_start=args.session_start,
                robocap_id=getattr(args, "robocap_id", None),
            )
            validate_session_deep(session)
            report["validation"] = session_report(session)
            selected = set(args.segment)
            segments = [item for item in session.segments if not selected or item.number in selected]
            unknown = selected - {item.number for item in segments}
            if unknown:
                raise ValueError(f"unknown segment(s): {sorted(unknown)}")
            if session.has_session_error or any(not item.is_ready for item in segments):
                report["status"] = "validation_failed"
                exit_code = 2
            else:
                results = [
                    convert_segment(session, segment, output_dir=output_root, debug=args.debug)
                    for segment in segments
                ]
                report["conversions"] = [conversion_report(item) for item in results]
                for result in results:
                    if not result.success:
                        continue
                    key = _join_prefix(destination.prefix, result.output_path.name)
                    uploaded = _upload_file(client, result.output_path, destination, key)
                    uploaded["segment"] = result.segment
                    report["outputs"].append(uploaded)
                    diagnostics = result.output_path.with_suffix(".diagnostics.json")
                    if args.debug and diagnostics.is_file():
                        diagnostic_key = _join_prefix(destination.prefix, "_diagnostics", diagnostics.name)
                        _upload_file(client, diagnostics, destination, diagnostic_key)
                report["status"] = "complete" if all(item.success for item in results) else "failed"
                exit_code = 0 if report["status"] == "complete" else 1
        except Exception as exc:
            report["status"] = "failed"
            report["errors"].append({"type": type(exc).__name__, "message": str(exc)})
            exit_code = 1

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["wall_seconds"] = round(time.monotonic() - started, 3)
    report_key = _join_prefix(destination.prefix, "_reports", f"{job_id}.json")
    try:
        client.put_object(
            Bucket=destination.bucket,
            Key=report_key,
            Body=(json.dumps(report, indent=2, sort_keys=True) + "\n").encode(),
            ContentType="application/json",
        )
        report["report_uri"] = f"s3://{destination.bucket}/{report_key}"
    except Exception as exc:
        report["errors"].append({"type": type(exc).__name__, "message": f"report upload failed: {exc}"})
        if exit_code == 0:
            report["status"] = "failed"
            exit_code = 1
    return exit_code, report


def main(argv: list[str] | None = None) -> int:
    configure_bundled_tools()
    args = build_parser().parse_args(argv)
    try:
        exit_code, report = run(args)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"{report['status']}: {len(report['outputs'])} MCAP(s)")
        if report.get("report_uri"):
            print(f"Report: {report['report_uri']}")
        for error in report["errors"]:
            print(f"ERROR: {error['type']}: {error['message']}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
