from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import timezone
from pathlib import Path

from .engine.converter import convert
from .models import CheckResult, ConversionResult, SegmentInput, SessionInput, Severity
from .validator import validate_segment_deep
from .verifier import verify_mcap


def output_filename(session: SessionInput, segment: SegmentInput) -> str:
    if session.session_start is None:
        raise ValueError("session UTC timestamp is required")
    if session.robocap_id is None:
        raise ValueError("RoboCap device ID is required")
    timestamp = session.session_start.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{session.robocap_id}_{timestamp}_segment{segment.number}.mcap"


def convert_segment(
    session: SessionInput,
    segment: SegmentInput,
    *,
    output_dir: Path | None = None,
    debug: bool = False,
) -> ConversionResult:
    output_dir = output_dir or session.root / "mcap"
    try:
        filename = output_filename(session, segment)
    except ValueError:
        filename = f"unknown_{segment.number}.mcap"
    if session.has_session_error:
        return ConversionResult(
            segment.number, output_dir / filename, False, tuple(session.checks),
        )
    if segment.validated_fingerprint != segment.fingerprint():
        validate_segment_deep(session, segment)
    if not segment.is_ready:
        return ConversionResult(
            segment.number, output_dir / filename, False, tuple(segment.checks),
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    final_path = output_dir / filename
    invalid_path = final_path.with_suffix(final_path.suffix + ".invalid")
    checks: list[CheckResult] = []
    stats = {}
    with tempfile.TemporaryDirectory(prefix=".robocap-mcap-", dir=output_dir) as temporary:
        temporary_root = Path(temporary)
        candidate = temporary_root / final_path.name
        normalization_dir = temporary_root / "normalized"
        try:
            stats = convert(
                session,
                segment,
                out_path=candidate,
                normalization_dir=normalization_dir,
                conversion_warnings=[check.to_dict() for check in segment.warnings],
            )
            checks = verify_mcap(candidate, session, segment)
            if any(check.severity == Severity.ERROR for check in checks):
                shutil.move(candidate, invalid_path)
                return ConversionResult(
                    segment.number, invalid_path, False, tuple(checks), stats,
                )
            os.replace(candidate, final_path)
        except Exception as exc:
            if candidate.exists():
                shutil.move(candidate, invalid_path)
            checks.append(CheckResult(
                "convert.failed",
                Severity.ERROR,
                f"Conversion failed: {type(exc).__name__}: {exc}",
                "Use Copy report and send the failure details to support.",
            ))
            return ConversionResult(
                segment.number, invalid_path, False, tuple(checks), stats,
            )

    if debug:
        report = {
            "segment": segment.number,
            "output": str(final_path),
            "stats": stats,
            "checks": [check.to_dict() for check in checks],
        }
        final_path.with_suffix(".diagnostics.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
    return ConversionResult(segment.number, final_path, True, tuple(checks), stats)
