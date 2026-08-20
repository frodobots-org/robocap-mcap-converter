from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .conversion import convert_segment
from .reporting import json_text, text_report
from .runtime import configure_bundled_tools
from .scanner import scan_session
from .validator import validate_session_deep


def _utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="robocap-mcap",
        description="Validate a raw RoboCap session and write one MCAP per segment.",
    )
    parser.add_argument("session_folder", type=Path)
    parser.add_argument("--session-start", type=_utc_datetime,
                        help="UTC ISO timestamp override when absent from the folder name")
    parser.add_argument("--robocap-id",
                        help="RoboCap device ID override when absent from the session path")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--segment", action="append", type=int, default=[])
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--video-workers", type=int,
                        help="parallel camera conversion workers per segment")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_bundled_tools()
    args = build_parser().parse_args(argv)
    if args.video_workers is not None:
        if args.video_workers < 1:
            raise SystemExit("--video-workers must be positive")
        os.environ["ROBOCAP_CONVERT_VIDEO_WORKERS"] = str(args.video_workers)
    session = scan_session(
        args.session_folder,
        session_start=args.session_start,
        robocap_id=args.robocap_id,
    )
    validate_session_deep(session)
    selected = set(args.segment)
    segments = [
        segment for segment in session.segments
        if not selected or segment.number in selected
    ]
    if selected - {segment.number for segment in segments}:
        raise SystemExit(f"unknown segment(s): {sorted(selected - {s.number for s in segments})}")

    if args.validate_only or session.has_session_error or any(not item.is_ready for item in segments):
        print(json_text(session) if args.json else text_report(session))
        return 0 if not session.has_session_error and all(item.is_ready for item in segments) else 2

    results = [
        convert_segment(
            session, segment, output_dir=args.output_dir, debug=args.debug,
        )
        for segment in segments
    ]
    if args.json:
        print(json_text(session, results))
    else:
        print(text_report(session), end="")
        for result in results:
            state = "OK" if result.success else "FAILED"
            print(f"{state} segment {result.segment}: {result.output_path}")
    return 0 if all(item.success for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
