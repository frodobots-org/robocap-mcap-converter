from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .engine.imu_reader import device_number_from_imu_key

from .models import CheckResult, IMUInput, SegmentInput, SessionInput, Severity, VideoInput

_SESSION_TIMESTAMP = re.compile(r"(?P<date>\d{8})_(?P<time>\d{6})(?:_session\d+)?")
_ROBOCAP_ID = re.compile(r"(?<![0-9a-f])(?P<id>[0-9a-f]{16})(?![0-9a-f])", re.IGNORECASE)
_VALID_ROBOCAP_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$", re.IGNORECASE)
_NEW_VIDEO = re.compile(
    r"^(?P<rig>robocap|robowrist)_segment(?P<segment>\d+)_video_(?P<camera>[a-z0-9_-]+)\.mp4$",
    re.IGNORECASE,
)
_OLD_VIDEO = re.compile(
    r"^video_dev(?P<dev>\d+)_session\d+_segment(?P<segment>\d+)_(?P<camera>[a-z0-9_-]+)\.mp4$",
    re.IGNORECASE,
)
_NEW_IMU = re.compile(
    r"^(?P<rig>robocap|robowrist)_segment(?P<segment>\d+)_imu_(?P<side>left|right)\.db$",
    re.IGNORECASE,
)
_OLD_IMU = re.compile(
    r"^IMUWriter_dev(?P<dev>\d+)_session\d+_segment(?P<segment>\d+)\.db$",
    re.IGNORECASE,
)

_HEAD_DATA_NUMBER = {
    "right-eye": 0,
    "left-front": 1,
    "left-eye": 2,
    "right": 3,
    "left": 4,
    "right-front": 5,
}
_WRIST_DATA_NUMBER = {"left_down": 6, "right_down": 7}
EXPECTED_HEAD_CAMERAS = frozenset(_HEAD_DATA_NUMBER)
EXPECTED_WRIST_CAMERAS = frozenset(_WRIST_DATA_NUMBER)


def _camera_name(raw: str, rig: str) -> str:
    value = raw.lower()
    if rig == "robocap":
        return value.replace("_", "-")
    return value.replace("-", "_")


def _session_start(root: Path) -> datetime | None:
    match = _SESSION_TIMESTAMP.search(root.name)
    if not match:
        return None
    parsed = datetime.strptime(
        match.group("date") + match.group("time"), "%Y%m%d%H%M%S"
    )
    return parsed.replace(tzinfo=timezone.utc)


def normalize_robocap_id(value: str) -> str:
    normalized = value.strip().lower()
    if not _VALID_ROBOCAP_ID.fullmatch(normalized):
        raise ValueError(
            "RoboCap ID must be 3-64 letters, numbers, underscores, or hyphens."
        )
    return normalized


def _robocap_id(root: Path) -> str | None:
    # Raw captures commonly place the 16-character device ID either in the
    # session folder name or in one of its immediate parent folders.
    for candidate in (root, *list(root.parents)[:4]):
        match = _ROBOCAP_ID.search(candidate.name)
        if match:
            return match.group("id").lower()
    return None


def _iter_input_files(root: Path, input_paths: list[Path] | None = None):
    candidates = input_paths if input_paths is not None else root.rglob("*")
    for path in candidates:
        path = path.expanduser().resolve()
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if "mcap" in {part.lower() for part in relative.parts[:-1]}:
            continue
        if path.name.endswith(".normalized.mp4") or path.name.endswith(".mcap"):
            continue
        yield path


def scan_session(
    root: str | Path,
    *,
    session_start: datetime | None = None,
    robocap_id: str | None = None,
    input_paths: list[str | Path] | None = None,
) -> SessionInput:
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)

    videos: dict[int, list[VideoInput]] = defaultdict(list)
    imus: dict[int, list[IMUInput]] = defaultdict(list)
    ignored: list[Path] = []
    checks: list[CheckResult] = []

    resolved_start = session_start or _session_start(root)
    resolved_robocap_id = normalize_robocap_id(robocap_id) if robocap_id else _robocap_id(root)
    if resolved_start is None:
        checks.append(CheckResult(
            "session.timestamp.missing",
            Severity.ERROR,
            f"The folder name {root.name!r} does not contain YYYYMMDD_HHMMSS.",
            "Rename the session folder to include its UTC recording timestamp, or enter the timestamp when selecting files.",
            (str(root),),
        ))
    else:
        checks.append(CheckResult(
            "session.timestamp.valid",
            Severity.PASSED,
            f"Session UTC anchor is {resolved_start.isoformat()}.",
            measured={"session_start_utc": resolved_start.isoformat()},
        ))

    if resolved_robocap_id is None:
        checks.append(CheckResult(
            "session.robocap_id.missing",
            Severity.ERROR,
            "The RoboCap device ID could not be determined from the session path.",
            "Enter the RoboCap ID when prompted.",
            (str(root),),
        ))
    else:
        checks.append(CheckResult(
            "session.robocap_id.valid",
            Severity.PASSED,
            f"RoboCap device ID is {resolved_robocap_id}.",
            measured={"robocap_id": resolved_robocap_id},
        ))

    selected_paths = (
        [Path(path) for path in input_paths]
        if input_paths is not None
        else None
    )
    for path in _iter_input_files(root, selected_paths):
        name = path.name
        match = _NEW_VIDEO.match(name)
        if match:
            rig = match.group("rig").lower()
            camera = _camera_name(match.group("camera"), rig)
            data_number = (
                _HEAD_DATA_NUMBER.get(camera, -1)
                if rig == "robocap"
                else _WRIST_DATA_NUMBER.get(camera, -1)
            )
            videos[int(match.group("segment"))].append(VideoInput(
                path, int(match.group("segment")), camera, rig, data_number,
            ))
            continue

        match = _OLD_VIDEO.match(name)
        if match:
            camera = _camera_name(match.group("camera"), "robocap")
            videos[int(match.group("segment"))].append(VideoInput(
                path, int(match.group("segment")), camera, "robocap", int(match.group("dev")),
            ))
            continue

        match = _NEW_IMU.match(name)
        if match:
            segment = int(match.group("segment"))
            imus[segment].append(IMUInput(
                path=path,
                segment=segment,
                device=device_number_from_imu_key(name),
                rig=match.group("rig").lower(),
                side=match.group("side").lower(),
            ))
            continue

        match = _OLD_IMU.match(name)
        if match:
            segment = int(match.group("segment"))
            imus[segment].append(IMUInput(
                path=path,
                segment=segment,
                device=match.group("dev"),
                rig="robocap",
            ))
            continue

        if path.suffix.lower() in {".mp4", ".db"}:
            ignored.append(path)

    segments: list[SegmentInput] = []
    for number in sorted(set(videos) | set(imus)):
        segment = SegmentInput(number=number, videos=videos[number], imus=imus[number])
        segment.checks.extend(_structural_checks(segment))
        segments.append(segment)

    if ignored:
        checks.append(CheckResult(
            "session.files.ignored",
            Severity.WARNING,
            f"{len(ignored)} MP4/DB file(s) do not match the supported naming rules and will be ignored.",
            "Rename capture files to the documented RoboCap grammar. Magnetometer files are expected and may be ignored.",
            tuple(str(path) for path in ignored),
        ))
    if not segments:
        checks.append(CheckResult(
            "session.segments.empty",
            Severity.ERROR,
            "No supported RoboCap segment files were found.",
            "Drop the recording session folder containing RoboCap MP4 and IMU DB files.",
        ))
    return SessionInput(
        root=root,
        session_start=resolved_start,
        segments=segments,
        checks=checks,
        ignored_files=ignored,
        robocap_id=resolved_robocap_id,
    )


def _structural_checks(segment: SegmentInput) -> list[CheckResult]:
    out: list[CheckResult] = []
    by_camera: dict[str, list[VideoInput]] = defaultdict(list)
    for video in segment.videos:
        by_camera[video.camera].append(video)
    for camera, matches in sorted(by_camera.items()):
        if len(matches) > 1:
            out.append(CheckResult(
                "segment.video.duplicate_topic",
                Severity.ERROR,
                f"Segment {segment.number} has {len(matches)} files for camera {camera}.",
                "Keep exactly one source MP4 for this camera and segment.",
                tuple(str(v.path) for v in matches),
                {"segment": segment.number, "camera": camera},
            ))

    devices = {imu.device for imu in segment.imus}
    missing = sorted({"1", "2"} - devices)
    if missing:
        out.append(CheckResult(
            "segment.imu.required_missing",
            Severity.ERROR,
            f"Segment {segment.number} is missing required IMU device(s): {', '.join('dev' + d for d in missing)}.",
            "Restore robocap imu_left/imu_right (dev1/dev2) from the recording before converting.",
            measured={"missing_devices": missing},
        ))
    else:
        out.append(CheckResult(
            "segment.imu.required_present",
            Severity.PASSED,
            "Required head IMUs dev1 and dev2 are present.",
        ))

    wrist_devices = sorted(devices & {"3", "4"})
    out.append(CheckResult(
        "segment.imu.wrist_presence",
        Severity.INFO,
        f"Optional wrist IMUs present: {', '.join('dev' + d for d in wrist_devices) if wrist_devices else 'none'}.",
        measured={"devices": wrist_devices},
    ))

    head = {v.camera for v in segment.videos if v.rig == "robocap"}
    wrist = {v.camera for v in segment.videos if v.rig == "robowrist"}
    missing_head = sorted(EXPECTED_HEAD_CAMERAS - head)
    if missing_head:
        out.append(CheckResult(
            "segment.camera.head_incomplete",
            Severity.WARNING,
            f"Segment {segment.number} has {len(head)}/6 head cameras; missing {', '.join(missing_head)}.",
            "Recover the missing MP4s if a complete six-camera rig is required. Partial rigs may still convert.",
            measured={"present": sorted(head), "missing": missing_head},
        ))
    else:
        out.append(CheckResult(
            "segment.camera.head_complete",
            Severity.PASSED,
            "All 6 head cameras are present.",
        ))

    if wrist and wrist != EXPECTED_WRIST_CAMERAS:
        out.append(CheckResult(
            "segment.camera.wrist_incomplete",
            Severity.WARNING,
            f"Segment {segment.number} has {len(wrist)}/2 wrist cameras; missing {', '.join(sorted(EXPECTED_WRIST_CAMERAS - wrist))}.",
            "Recover the missing wrist MP4, or continue with the available camera.",
        ))
    elif wrist == EXPECTED_WRIST_CAMERAS:
        out.append(CheckResult(
            "segment.camera.wrist_complete",
            Severity.PASSED,
            "Both wrist cameras are present (8-camera rig).",
        ))
    return out
