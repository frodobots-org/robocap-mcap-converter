from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel

from robocap_to_mcap.gui import MainWindow, _display_status
from robocap_to_mcap.models import CheckResult, SegmentInput, SessionInput, Severity


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
