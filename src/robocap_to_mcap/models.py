from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class Severity(StrEnum):
    PASSED = "passed"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    severity: Severity
    message: str
    fix: str = ""
    files: tuple[str, ...] = ()
    measured: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.check_id,
            "severity": self.severity.value,
            "message": self.message,
            "fix": self.fix,
            "files": list(self.files),
            "measured": self.measured,
        }


@dataclass(frozen=True)
class VideoInput:
    path: Path
    segment: int
    camera: str
    rig: str
    data_number: int


@dataclass(frozen=True)
class IMUInput:
    path: Path
    segment: int
    device: str
    rig: str
    side: str | None = None


@dataclass
class SegmentInput:
    number: int
    videos: list[VideoInput] = field(default_factory=list)
    imus: list[IMUInput] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    duration_seconds: float | None = None
    validated_fingerprint: tuple[tuple[str, int, int], ...] | None = None

    @property
    def errors(self) -> list[CheckResult]:
        return [c for c in self.checks if c.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[CheckResult]:
        return [c for c in self.checks if c.severity == Severity.WARNING]

    @property
    def status(self) -> str:
        if self.errors:
            return "FAIL"
        if self.warnings:
            return "WARN"
        return "OK"

    @property
    def is_ready(self) -> bool:
        return self.validated_fingerprint is not None and not self.errors

    def fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        rows = []
        for path in [v.path for v in self.videos] + [i.path for i in self.imus]:
            stat = path.stat()
            rows.append((str(path), stat.st_size, stat.st_mtime_ns))
        return tuple(sorted(rows))


@dataclass
class SessionInput:
    root: Path
    session_start: datetime | None
    segments: list[SegmentInput]
    checks: list[CheckResult] = field(default_factory=list)
    ignored_files: list[Path] = field(default_factory=list)
    robocap_id: str | None = None

    @property
    def has_session_error(self) -> bool:
        return any(c.severity == Severity.ERROR for c in self.checks)


@dataclass(frozen=True)
class ConversionResult:
    segment: int
    output_path: Path
    success: bool
    checks: tuple[CheckResult, ...]
    stats: dict[str, Any] = field(default_factory=dict)
