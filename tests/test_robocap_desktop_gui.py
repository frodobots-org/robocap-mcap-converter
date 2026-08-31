from __future__ import annotations

import os
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel

import robocap_to_mcap.gui as gui_module
from robocap_to_mcap.gui import ConversionWorker, MainWindow, _display_status, _result_key
from robocap_to_mcap.models import (
    CheckResult,
    ConversionResult,
    SegmentInput,
    SessionInput,
    Severity,
)


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_normal_mode_hides_privacy_notice_and_diagnostics(app: QApplication) -> None:
    window = MainWindow()

    visible_text = " ".join(label.text() for label in window.findChildren(QLabel))
    assert "PII" not in visible_text
    assert "UNBLURRED" not in visible_text
    assert window.details.isHidden()
    assert window.copy_button.isHidden()

    window.close()


def test_debug_mode_is_required_for_technical_details(app: QApplication, tmp_path: Path) -> None:
    segment = SegmentInput(
        number=1,
        checks=[
            CheckResult(
                "deep.imu.timestamp_gaps",
                Severity.WARNING,
                "dev1 has 3 timestamp gaps over 100 ms.",
                "Inspect the recorder clock.",
            )
        ],
        validated_fingerprint=(),
    )
    window = MainWindow()
    window.session = SessionInput(root=tmp_path, session_start=None, segments=[segment])
    window._refresh_table()
    window.table.selectRow(0)

    assert _display_status(segment, None) == "Ready with warnings"
    assert window.details.isHidden()
    assert window.copy_button.isHidden()

    window._set_debug_mode(True)
    assert not window.details.isHidden()
    assert not window.copy_button.isHidden()
    assert "timestamp gaps" in window.details.toPlainText()

    window._set_debug_mode(False)
    assert window.details.isHidden()
    assert window.copy_button.isHidden()

    window.close()


def test_plain_language_statuses() -> None:
    checking = SegmentInput(number=1)
    blocked = SegmentInput(
        number=2,
        checks=[CheckResult("structural.required", Severity.ERROR, "missing input")],
    )
    ready = SegmentInput(number=3, validated_fingerprint=())

    assert _display_status(checking, None) == "Checking..."
    assert _display_status(blocked, None) == "Cannot convert"
    assert _display_status(ready, None) == "Ready"


def test_bulk_table_keeps_same_segment_number_separate_by_session(
    app: QApplication,
    tmp_path: Path,
) -> None:
    first = SessionInput(
        root=tmp_path / "device_a_20260801_010203_session1",
        session_start=None,
        segments=[SegmentInput(number=1, validated_fingerprint=())],
    )
    second = SessionInput(
        root=tmp_path / "device_b_20260801_020304_session2",
        session_start=None,
        segments=[SegmentInput(number=1, validated_fingerprint=())],
    )
    first_result = ConversionResult(1, first.root / "mcap" / "first.mcap", True, ())
    second_result = ConversionResult(1, second.root / "mcap" / "second.mcap", True, ())

    window = MainWindow()
    window.sessions = [first, second]
    window.results = {
        _result_key(first, 1): first_result,
        _result_key(second, 1): second_result,
    }
    window._refresh_table()

    assert window.table.rowCount() == 2
    assert window.table.item(0, 0).text() == first.root.name
    assert window.table.item(1, 0).text() == second.root.name
    assert window.table.item(0, 5).text() == "Complete"
    assert window.table.item(1, 6).text().endswith("second.mcap")
    window.close()


def test_parent_folder_drop_loads_multiple_sessions(
    app: QApplication,
    tmp_path: Path,
) -> None:
    roots = [
        tmp_path / "75cd2758f7384110_20260801_010203_session1",
        tmp_path / "8c94d6053f48d3e4_20260801_020304_session2",
    ]
    for root in roots:
        root.mkdir()
        (root / "robocap_segment1_video_left_eye.mp4").write_bytes(b"video")
        (root / "robocap_segment1_imu_left.db").write_bytes(b"imu")
        (root / "robocap_segment1_imu_right.db").write_bytes(b"imu")

    window = MainWindow()
    window.deep_on_drop = False
    window._handle_drop([tmp_path])

    assert [session.root for session in window.sessions] == [
        root.resolve() for root in roots
    ]
    assert window.table.rowCount() == 2
    assert "2 session(s)" in window.summary.text()
    window.close()


def test_bulk_conversion_continues_after_one_job_raises(
    app: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = SessionInput(tmp_path / "first", None, [SegmentInput(number=1)])
    second = SessionInput(tmp_path / "second", None, [SegmentInput(number=1)])
    calls = 0

    def fake_convert(
        session: SessionInput,
        segment: SegmentInput,
        *,
        debug: bool,
    ) -> ConversionResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("bad session")
        return ConversionResult(
            segment.number,
            session.root / "mcap" / "ok.mcap",
            True,
            (),
        )

    monkeypatch.setattr(gui_module, "convert_segment", fake_convert)
    emitted: list[list[tuple[SessionInput, ConversionResult]]] = []
    worker = ConversionWorker(
        [(first, first.segments[0]), (second, second.segments[0])],
        False,
        threading.Event(),
    )
    worker.finished.connect(emitted.append)
    worker.run()

    assert len(emitted[0]) == 2
    assert emitted[0][0][1].success is False
    assert emitted[0][1][1].success is True


def test_large_bulk_validation_enables_and_queues_conversion_without_table_rebuild(
    app: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = [
        SessionInput(
            root=tmp_path / f"device{i:02d}_20260801_010203_session{i}",
            session_start=None,
            segments=[SegmentInput(number=1)],
        )
        for i in range(90)
    ]
    window = MainWindow()
    window.sessions = sessions
    window._refresh_table()
    window.operation = "validation"
    window.thread = object()  # type: ignore[assignment]

    refresh_calls = 0

    def count_refresh() -> None:
        nonlocal refresh_calls
        refresh_calls += 1

    monkeypatch.setattr(window, "_refresh_table", count_refresh)
    first = sessions[0].segments[0]
    first.validated_fingerprint = ()
    window._validation_progress(sessions[0], 1, 1, 90)

    assert refresh_calls == 0
    assert window.table.item(0, 5).text() == "Ready"
    assert window.convert_button.isEnabled()

    window._start_conversion()

    assert window.conversion_requested is True
    assert not window.convert_button.isEnabled()
    assert "Conversion queued" in window.summary.text()
    window.thread = None
    window.close()
