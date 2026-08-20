from __future__ import annotations

import json

from .models import CheckResult, ConversionResult, SessionInput, Severity


def session_report(session: SessionInput) -> dict:
    return {
        "root": str(session.root),
        "session_start_utc": session.session_start.isoformat() if session.session_start else None,
        "checks": [check.to_dict() for check in session.checks],
        "segments": [
            {
                "segment": segment.number,
                "status": segment.status,
                "ready": segment.is_ready,
                "duration_seconds": segment.duration_seconds,
                "camera_count": len(segment.videos),
                "imu_count": len(segment.imus),
                "checks": [check.to_dict() for check in segment.checks],
            }
            for segment in session.segments
        ],
    }


def conversion_report(result: ConversionResult) -> dict:
    return {
        "segment": result.segment,
        "success": result.success,
        "output": str(result.output_path),
        "checks": [check.to_dict() for check in result.checks],
        "stats": result.stats,
    }


def text_report(session: SessionInput) -> str:
    lines = [
        "RoboCap to MCAP validation report",
        f"Session: {session.root}",
        f"UTC anchor: {session.session_start.isoformat() if session.session_start else 'missing'}",
        "",
    ]
    severity_order = (Severity.ERROR, Severity.WARNING, Severity.INFO, Severity.PASSED)
    for check in session.checks:
        lines.append(f"[{check.severity.value.upper()}] {check.check_id}: {check.message}")
        if check.fix:
            lines.append(f"  Fix: {check.fix}")
    for segment in session.segments:
        lines.extend(("", f"Segment {segment.number}: {segment.status}"))
        for severity in severity_order:
            for check in segment.checks:
                if check.severity != severity:
                    continue
                lines.append(f"[{check.severity.value.upper()}] {check.check_id}: {check.message}")
                if check.fix:
                    lines.append(f"  Fix: {check.fix}")
    return "\n".join(lines) + "\n"


def json_text(session: SessionInput, conversions: list[ConversionResult] | None = None) -> str:
    payload = session_report(session)
    if conversions is not None:
        payload["conversions"] = [conversion_report(item) for item in conversions]
    return json.dumps(payload, indent=2, sort_keys=True)
