from __future__ import annotations

import os
import subprocess
import sys
import threading
from html import escape
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QDragEnterEvent, QDropEvent, QFont
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .conversion import convert_segment
from .models import CheckResult, ConversionResult, SegmentInput, SessionInput, Severity
from .reporting import text_report
from .runtime import configure_bundled_tools
from .scanner import discover_session_roots, normalize_robocap_id, scan_session
from .validator import validate_session_deep


APP_STYLE = """
QMainWindow, QWidget { background: #f4f0e7; color: #172532; }
QLabel#title { font-size: 25px; font-weight: 700; color: #102c3a; }
QLabel#subtitle { color: #53636a; font-size: 12px; }
QWidget#dropZone { background: #fffaf0; border: 2px dashed #d09a28; border-radius: 14px; }
QWidget#dropZone[active="true"] { background: #fff1c9; border-color: #a86e00; }
QLabel#dropTitle { font-size: 18px; font-weight: 650; color: #173f4e; }
QLabel#dropHint { color: #66747a; }
QPushButton { background: #173f4e; color: white; border: 0; border-radius: 7px; padding: 8px 14px; font-weight: 600; }
QPushButton:hover { background: #23596b; }
QPushButton:disabled { background: #aab3b2; color: #e8e8e8; }
QPushButton#secondary { background: transparent; color: #173f4e; border: 1px solid #87979a; }
QTableWidget { background: #fffdf8; border: 1px solid #d9d2c4; border-radius: 8px; gridline-color: #e7e0d5; }
QHeaderView::section { background: #e8e1d3; color: #263b43; border: 0; padding: 7px; font-weight: 650; }
QTextBrowser { background: #fffdf8; border: 1px solid #d9d2c4; border-radius: 8px; padding: 8px; }
QProgressBar { border: 0; border-radius: 5px; background: #ded8cc; text-align: center; }
QProgressBar::chunk { background: #d09a28; border-radius: 5px; }
"""


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _display_status(
    segment: SegmentInput,
    result: ConversionResult | None,
) -> str:
    if result is not None:
        return "Complete" if result.success else "Conversion failed"
    if segment.errors:
        return "Cannot convert"
    if segment.validated_fingerprint is None:
        return "Checking..."
    if segment.warnings:
        return "Ready with warnings"
    return "Ready"


def _result_key(session: SessionInput, segment: SegmentInput | int) -> tuple[str, int]:
    number = segment if isinstance(segment, int) else segment.number
    return str(session.root), number


def _requires_validation(segment: SegmentInput) -> bool:
    if segment.validated_fingerprint is None:
        return True
    try:
        return segment.validated_fingerprint != segment.fingerprint()
    except OSError:
        return True


class DropZone(QWidget):
    dropped = Signal(list)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("dropZone")
        self.setAcceptDrops(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(5)
        title = QLabel("Drop session folders or a parent folder here")
        title.setObjectName("dropTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint = QLabel("Sessions are discovered, validated, and converted as one batch")
        hint.setObjectName("dropHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        layout.addWidget(hint)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            self.setProperty("active", True)
            self.style().unpolish(self)
            self.style().polish(self)
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:
        self.setProperty("active", False)
        self.style().unpolish(self)
        self.style().polish(self)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self.setProperty("active", False)
        self.style().unpolish(self)
        self.style().polish(self)
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.dropped.emit(paths)
            event.acceptProposedAction()


class ValidationWorker(QObject):
    segment_ready = Signal(object, int, int, int)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, sessions: list[SessionInput], cancel: threading.Event) -> None:
        super().__init__()
        self.sessions = sessions
        self.cancel = cancel

    def run(self) -> None:
        try:
            total = sum(len(session.segments) for session in self.sessions)
            completed = 0
            for session in self.sessions:
                if self.cancel.is_set():
                    break

                def on_segment(segment: SegmentInput) -> None:
                    nonlocal completed
                    completed += 1
                    self.segment_ready.emit(session, segment.number, completed, total)

                try:
                    validate_session_deep(
                        session,
                        cancel=self.cancel.is_set,
                        on_segment=on_segment,
                    )
                except Exception as exc:
                    session.checks.append(CheckResult(
                        "batch.validation.failed",
                        Severity.ERROR,
                        f"This session could not be validated: {type(exc).__name__}: {exc}",
                        "Enable Debug mode, inspect this session, and retry it separately.",
                        (str(session.root),),
                    ))
            self.finished.emit(self.sessions)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class ConversionWorker(QObject):
    progress = Signal(int, int, object)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        jobs: list[tuple[SessionInput, SegmentInput]],
        debug: bool,
        cancel: threading.Event,
    ) -> None:
        super().__init__()
        self.jobs = jobs
        self.debug = debug
        self.cancel = cancel

    def run(self) -> None:
        results: list[tuple[SessionInput, ConversionResult]] = []
        try:
            for index, (session, segment) in enumerate(self.jobs, start=1):
                if self.cancel.is_set():
                    break
                try:
                    result = convert_segment(session, segment, debug=self.debug)
                except Exception as exc:
                    result = ConversionResult(
                        segment=segment.number,
                        output_path=session.root / "mcap" / f"segment{segment.number}.mcap.invalid",
                        success=False,
                        checks=(CheckResult(
                            "batch.conversion.failed",
                            Severity.ERROR,
                            f"This segment could not be converted: {type(exc).__name__}: {exc}",
                            "Enable Debug mode, inspect this session, and retry it separately.",
                            (str(session.root),),
                        ),),
                    )
                results.append((session, result))
                self.progress.emit(index, len(self.jobs), (session, result))
            self.finished.emit(results)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("RoboCap to MCAP")
        self.resize(1040, 760)
        self.sessions: list[SessionInput] = []
        self.results: dict[tuple[str, int], ConversionResult] = {}
        self.row_inputs: dict[tuple[str, int], tuple[SessionInput, SegmentInput]] = {}
        self.thread: QThread | None = None
        self.worker: QObject | None = None
        self.cancel_event = threading.Event()
        self.deep_on_drop = True
        self.debug_mode = False
        self.video_workers = min(4, os.cpu_count() or 1)
        self._build_ui()

    @property
    def session(self) -> SessionInput | None:
        """Compatibility accessor for callers loading one session."""
        return self.sessions[0] if self.sessions else None

    @session.setter
    def session(self, value: SessionInput | None) -> None:
        self.sessions = [value] if value is not None else []

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)

        top = QHBoxLayout()
        heading = QVBoxLayout()
        title = QLabel("RoboCap to MCAP")
        title.setObjectName("title")
        subtitle = QLabel("Bulk raw-session validation and timestamp-synchronized MCAP export")
        subtitle.setObjectName("subtitle")
        heading.addWidget(title)
        heading.addWidget(subtitle)
        top.addLayout(heading)
        top.addStretch()
        self.settings_button = QPushButton("Settings")
        self.settings_button.setObjectName("secondary")
        self.settings_button.clicked.connect(self._settings_menu)
        self.recheck_button = QPushButton("Re-check")
        self.recheck_button.setObjectName("secondary")
        self.recheck_button.setEnabled(False)
        self.recheck_button.clicked.connect(self._start_validation)
        top.addWidget(self.settings_button)
        top.addWidget(self.recheck_button)
        layout.addLayout(top)

        self.drop_zone = DropZone()
        self.drop_zone.dropped.connect(self._handle_drop)
        layout.addWidget(self.drop_zone)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ("Session", "Segment", "Cameras", "IMUs", "Duration", "Status", "Output")
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._show_selected_details)
        self.details = QTextBrowser()
        self.details.setHtml(
            "<b>No sessions loaded.</b><br>Drop session folders or a parent folder to begin."
        )
        self.details.setVisible(False)
        self.details.setOpenLinks(False)
        self.details.anchorClicked.connect(self._reveal_output)
        splitter.addWidget(self.table)
        splitter.addWidget(self.details)
        splitter.setSizes([360, 220])
        layout.addWidget(splitter, 1)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        bottom = QHBoxLayout()
        self.summary = QLabel("Waiting for session folders")
        bottom.addWidget(self.summary, 1)
        self.copy_button = QPushButton("Copy report")
        self.copy_button.setObjectName("secondary")
        self.copy_button.setEnabled(False)
        self.copy_button.setVisible(False)
        self.copy_button.clicked.connect(self._copy_report)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("secondary")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_event.set)
        self.convert_button = QPushButton("Convert ready segments")
        self.convert_button.setEnabled(False)
        self.convert_button.clicked.connect(self._start_conversion)
        bottom.addWidget(self.copy_button)
        bottom.addWidget(self.cancel_button)
        bottom.addWidget(self.convert_button)
        layout.addLayout(bottom)
        self.setCentralWidget(root)

    def _settings_menu(self) -> None:
        menu = QMenu(self)
        deep = QAction("Deep checks on drop", menu, checkable=True)
        deep.setChecked(self.deep_on_drop)
        deep.toggled.connect(lambda value: setattr(self, "deep_on_drop", value))
        debug = QAction("Debug mode", menu, checkable=True)
        debug.setStatusTip("Show technical validation details and write diagnostic reports")
        debug.setChecked(self.debug_mode)
        debug.toggled.connect(self._set_debug_mode)
        workers = QAction(f"Camera workers: {self.video_workers}", menu)
        workers.triggered.connect(self._choose_workers)
        menu.addAction(deep)
        menu.addAction(debug)
        menu.addSeparator()
        menu.addAction(workers)
        menu.exec(self.settings_button.mapToGlobal(self.settings_button.rect().bottomLeft()))

    def _set_debug_mode(self, enabled: bool) -> None:
        self.debug_mode = enabled
        self.details.setVisible(enabled)
        self.copy_button.setVisible(enabled)
        self.copy_button.setEnabled(enabled and bool(self.sessions))
        if enabled:
            if self.table.rowCount() and not self.table.selectedItems():
                self.table.selectRow(0)
            self._show_selected_details()

    def _choose_workers(self) -> None:
        value, ok = QInputDialog.getInt(
            self, "Camera workers", "Parallel camera workers per segment:",
            self.video_workers, 1, 16,
        )
        if ok:
            self.video_workers = value

    def _handle_drop(self, paths: list[Path]) -> None:
        folders = [path for path in paths if path.is_dir()]
        files = [path.resolve() for path in paths if path.is_file()]
        roots = discover_session_roots(paths)
        if not roots:
            QMessageBox.warning(
                self,
                "No sessions found",
                "No supported RoboCap session files were found in the dropped paths.",
            )
            return

        sessions: list[SessionInput] = []
        for root in roots:
            selected_files = None
            if not folders and files:
                selected_files = [path for path in files if path.is_relative_to(root)]
            try:
                session = self._scan_session_with_prompts(root, selected_files)
            except Exception as exc:
                QMessageBox.critical(self, "Cannot scan folder", f"{root}\n\n{exc}")
                continue
            sessions.append(session)
        if not sessions:
            return

        self.sessions = sessions
        self.results.clear()
        self.recheck_button.setEnabled(True)
        self.copy_button.setEnabled(self.debug_mode)
        self._refresh_table()
        total = sum(len(session.segments) for session in sessions)
        if self.deep_on_drop:
            self._start_validation()
        else:
            self._update_summary(
                f"Found {len(sessions)} session(s) and {total} segment(s); "
                "deep checks will run before conversion."
            )

    def _scan_session_with_prompts(
        self,
        root: Path,
        selected_files: list[Path] | None,
    ) -> SessionInput:
        session = scan_session(root, input_paths=selected_files)
        session_start = session.session_start
        robocap_id = session.robocap_id
        if session_start is None:
            value, ok = QInputDialog.getText(
                self, "Session UTC timestamp",
                f"{root.name} has no timestamp. Enter UTC as YYYY-MM-DDTHH:MM:SSZ:",
            )
            if not ok:
                return session
            try:
                session_start = _parse_timestamp(value)
            except ValueError as exc:
                QMessageBox.warning(self, "Invalid timestamp", str(exc))
                return session
        if robocap_id is None:
            value, ok = QInputDialog.getText(
                self,
                "RoboCap device ID",
                f"Enter the RoboCap device ID for {root.name}:",
            )
            if not ok:
                return session
            try:
                robocap_id = normalize_robocap_id(value)
            except ValueError as exc:
                QMessageBox.warning(self, "Invalid RoboCap ID", str(exc))
                return session
        return scan_session(
            root,
            session_start=session_start,
            robocap_id=robocap_id,
            input_paths=selected_files,
        )

    def _refresh_table(self) -> None:
        if not self.sessions:
            return
        selected_key = None
        if self.table.selectedItems():
            selected_key = self.table.selectedItems()[0].data(Qt.ItemDataRole.UserRole)
        rows = [
            (session, segment)
            for session in self.sessions
            for segment in session.segments
        ]
        self.row_inputs.clear()
        self.table.setRowCount(len(rows))
        selected_row = None
        for row, (session, segment) in enumerate(rows):
            key = _result_key(session, segment)
            self.row_inputs[key] = (session, segment)
            result = self.results.get(key)
            duration = (
                f"{segment.duration_seconds:.1f}s"
                if segment.duration_seconds is not None
                else "Checking..."
            )
            values = (
                session.root.name,
                str(segment.number),
                str(len(segment.videos)),
                str(len(segment.imus)),
                duration,
                _display_status(segment, result),
                str(result.output_path) if result else "",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, key)
                if column == 0:
                    item.setToolTip(str(session.root))
                self.table.setItem(row, column, item)
            if key == selected_key:
                selected_row = row
        if selected_row is not None:
            self.table.selectRow(selected_row)
        self._update_buttons()

    def _start_validation(self) -> None:
        if not self.sessions or self.thread is not None:
            return
        self.cancel_event = threading.Event()
        total = sum(len(session.segments) for session in self.sessions)
        self.progress.setRange(0, total)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self._set_busy(
            True,
            f"Validating {total} segment(s) across {len(self.sessions)} session(s)...",
        )
        worker = ValidationWorker(self.sessions, self.cancel_event)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.segment_ready.connect(self._validation_progress)
        worker.finished.connect(self._validation_finished)
        worker.failed.connect(self._worker_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._thread_finished)
        self.thread = thread
        self.worker = worker
        thread.start()

    def _validation_progress(
        self,
        _session: SessionInput,
        _number: int,
        completed: int,
        total: int,
    ) -> None:
        self.progress.setValue(completed)
        self._refresh_table()
        self._update_summary(f"Validated {completed} of {total} segments")

    def _validation_finished(self, _sessions: list[SessionInput]) -> None:
        self._refresh_table()
        segments = [segment for session in self.sessions for segment in session.segments]
        ready = sum(segment.is_ready for segment in segments)
        self.progress.setVisible(False)
        self._set_busy(
            False,
            f"{ready} of {len(segments)} segments ready across {len(self.sessions)} session(s)",
        )

    def _start_conversion(self) -> None:
        if not self.sessions or self.thread is not None:
            return
        candidates = [
            segment
            for session in self.sessions
            if not session.has_session_error
            for segment in session.segments
            if not segment.errors
        ]
        if any(_requires_validation(segment) for segment in candidates):
            self._start_validation()
            return
        jobs = [
            (session, segment)
            for session in self.sessions
            for segment in session.segments
            if segment.is_ready
        ]
        if not jobs:
            return
        os.environ["ROBOCAP_CONVERT_VIDEO_WORKERS"] = str(self.video_workers)
        self.cancel_event = threading.Event()
        self.progress.setRange(0, len(jobs))
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self._set_busy(
            True,
            f"Converting {len(jobs)} segment(s) across {len(self.sessions)} session(s)...",
        )
        worker = ConversionWorker(jobs, self.debug_mode, self.cancel_event)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._conversion_progress)
        worker.finished.connect(self._conversion_finished)
        worker.failed.connect(self._worker_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._thread_finished)
        self.thread = thread
        self.worker = worker
        thread.start()

    def _conversion_progress(
        self,
        current: int,
        total: int,
        payload: tuple[SessionInput, ConversionResult],
    ) -> None:
        session, result = payload
        self.results[_result_key(session, result.segment)] = result
        self.progress.setValue(current)
        self._refresh_table()
        self._update_summary(f"Converted {current} of {total} segments")

    def _conversion_finished(
        self,
        results: list[tuple[SessionInput, ConversionResult]],
    ) -> None:
        for session, result in results:
            self.results[_result_key(session, result.segment)] = result
        succeeded = sum(result.success for _session, result in results)
        failed = len(results) - succeeded
        total_segments = sum(
            len(session.segments) for session in self.sessions
        )
        skipped = total_segments - len(results)
        self._refresh_table()
        self._set_busy(
            False,
            f"Batch complete: {succeeded} converted, {failed} failed, "
            f"{skipped} skipped across {len(self.sessions)} session(s)",
        )
        self.progress.setVisible(False)

    def _thread_finished(self) -> None:
        self.thread = None
        self.worker = None
        self._update_buttons()

    def _worker_failed(self, message: str) -> None:
        self._set_busy(False, "Operation failed")
        if self.debug_mode:
            body = message
        else:
            body = (
                "The operation could not be completed. Turn on Debug mode and "
                "re-check the session, or contact support."
            )
        QMessageBox.critical(self, "RoboCap to MCAP", body)

    def _set_busy(self, busy: bool, message: str) -> None:
        self.cancel_button.setEnabled(busy)
        self.recheck_button.setEnabled(not busy and bool(self.sessions))
        self.settings_button.setEnabled(not busy)
        self._update_summary(message)
        self._update_buttons()

    def _update_buttons(self) -> None:
        convertible = any(
            segment.is_ready or not segment.errors
            for session in self.sessions
            if not session.has_session_error
            for segment in session.segments
        )
        self.convert_button.setEnabled(convertible and self.thread is None)
        self.copy_button.setEnabled(self.debug_mode and bool(self.sessions))

    def _update_summary(self, message: str) -> None:
        self.summary.setText(message)

    def _selected_segment(self) -> tuple[SessionInput, SegmentInput] | None:
        if not self.sessions or not self.table.selectedItems():
            return None
        key = self.table.selectedItems()[0].data(Qt.ItemDataRole.UserRole)
        return self.row_inputs.get(tuple(key))

    def _show_selected_details(self) -> None:
        if not self.debug_mode:
            return
        selected = self._selected_segment()
        if selected is None:
            return
        session, segment = selected
        groups = (
            (Severity.ERROR, "Errors"), (Severity.WARNING, "Warnings"),
            (Severity.INFO, "Information"), (Severity.PASSED, "Passed"),
        )
        html = [
            f"<h2>{escape(session.root.name)} · Segment {segment.number}</h2>",
            f"<p>{escape(str(session.root))}</p>",
        ]
        html.append("<h3>Input files</h3><ul>")
        for video in sorted(segment.videos, key=lambda item: item.camera):
            html.append(
                f"<li>Camera {escape(video.camera)}: {escape(str(video.path))}</li>"
            )
        for imu in sorted(segment.imus, key=lambda item: item.device):
            html.append(f"<li>IMU dev{escape(imu.device)}: {escape(str(imu.path))}</li>")
        html.append("</ul>")
        for severity, title in groups:
            checks = [check for check in segment.checks if check.severity == severity]
            if not checks:
                continue
            html.append(f"<h3>{title}</h3><ul>")
            for check in checks:
                fix = f"<br><i>Fix: {escape(check.fix)}</i>" if check.fix else ""
                html.append(
                    f"<li><b>{escape(check.check_id)}</b>: "
                    f"{escape(check.message)}{fix}</li>"
                )
            html.append("</ul>")
        result = self.results.get(_result_key(session, segment))
        if result:
            html.append(f"<h3>Output</h3><p>{escape(str(result.output_path))}</p>")
            html.append('<p><a href="reveal://output">Reveal in Explorer</a></p>')
        self.details.setHtml("".join(html))

    def _reveal_output(self, _url: QUrl) -> None:
        selected = self._selected_segment()
        result = (
            self.results.get(_result_key(*selected))
            if selected is not None
            else None
        )
        if result is None:
            return
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(result.output_path)])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(result.output_path.parent)))

    def _copy_report(self) -> None:
        if self.sessions:
            report = "\n\n".join(text_report(session).rstrip() for session in self.sessions)
            QApplication.clipboard().setText(report + "\n")
            self._update_summary(
                f"Validation report for {len(self.sessions)} session(s) copied to clipboard"
            )

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        if self.sessions and self.thread is None:
            stale = any(
                segment.validated_fingerprint is not None
                and _requires_validation(segment)
                for session in self.sessions
                for segment in session.segments
            )
            if stale:
                QTimer.singleShot(0, self._start_validation)


def main() -> int:
    configure_bundled_tools()
    app = QApplication(sys.argv)
    app.setApplicationName("RoboCap to MCAP")
    app.setOrganizationName("BitRobot")
    app.setFont(QFont("Segoe UI Variable", 10))
    app.setStyleSheet(APP_STYLE)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
