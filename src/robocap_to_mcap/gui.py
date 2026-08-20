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
from .models import ConversionResult, SegmentInput, SessionInput, Severity
from .reporting import text_report
from .runtime import configure_bundled_tools
from .scanner import normalize_robocap_id, scan_session
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


class DropZone(QWidget):
    dropped = Signal(list)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("dropZone")
        self.setAcceptDrops(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(5)
        title = QLabel("Drop a RoboCap session folder here")
        title.setObjectName("dropTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint = QLabel("One verified MCAP will be written for each valid segment")
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
    segment_ready = Signal(int)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, session: SessionInput, cancel: threading.Event) -> None:
        super().__init__()
        self.session = session
        self.cancel = cancel

    def run(self) -> None:
        try:
            validate_session_deep(
                self.session,
                cancel=self.cancel.is_set,
                on_segment=lambda segment: self.segment_ready.emit(segment.number),
            )
            self.finished.emit(self.session)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class ConversionWorker(QObject):
    progress = Signal(int, int, object)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        session: SessionInput,
        segments: list[SegmentInput],
        debug: bool,
        cancel: threading.Event,
    ) -> None:
        super().__init__()
        self.session = session
        self.segments = segments
        self.debug = debug
        self.cancel = cancel

    def run(self) -> None:
        results: list[ConversionResult] = []
        try:
            for index, segment in enumerate(self.segments, start=1):
                if self.cancel.is_set():
                    break
                result = convert_segment(self.session, segment, debug=self.debug)
                results.append(result)
                self.progress.emit(index, len(self.segments), result)
            self.finished.emit(results)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("RoboCap to MCAP")
        self.resize(1040, 760)
        self.session: SessionInput | None = None
        self.results: dict[int, ConversionResult] = {}
        self.thread: QThread | None = None
        self.worker: QObject | None = None
        self.cancel_event = threading.Event()
        self.deep_on_drop = True
        self.debug_mode = False
        self.video_workers = min(4, os.cpu_count() or 1)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)

        top = QHBoxLayout()
        heading = QVBoxLayout()
        title = QLabel("RoboCap to MCAP")
        title.setObjectName("title")
        subtitle = QLabel("Raw session validation and time-synchronized Foxglove export")
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
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(("Segment", "Cameras", "IMUs", "Duration", "Status", "Output"))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._show_selected_details)
        self.details = QTextBrowser()
        self.details.setHtml("<b>No session loaded.</b><br>Drop a timestamped recording folder to begin.")
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
        self.summary = QLabel("Waiting for a session folder")
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
        self.copy_button.setEnabled(enabled and self.session is not None)
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
        selected_files: list[Path] | None = None
        if folders:
            root = folders[0]
        else:
            parents = {path.parent.resolve() for path in paths if path.is_file()}
            if len(parents) != 1:
                QMessageBox.warning(self, "One session required", "Drop one folder, or files from one folder.")
                return
            root = parents.pop()
            selected_files = [path for path in paths if path.is_file()]
        try:
            session = scan_session(root, input_paths=selected_files)
        except Exception as exc:
            QMessageBox.critical(self, "Cannot scan folder", str(exc))
            return
        session_start = session.session_start
        robocap_id = session.robocap_id
        if session_start is None:
            value, ok = QInputDialog.getText(
                self, "Session UTC timestamp",
                "Folder name has no timestamp. Enter UTC as YYYY-MM-DDTHH:MM:SSZ:",
            )
            if not ok:
                return
            try:
                session_start = _parse_timestamp(value)
            except ValueError as exc:
                QMessageBox.warning(self, "Invalid timestamp", str(exc))
                return
        if robocap_id is None:
            value, ok = QInputDialog.getText(
                self,
                "RoboCap device ID",
                "Enter the RoboCap device ID for this recording:",
            )
            if not ok:
                return
            try:
                robocap_id = normalize_robocap_id(value)
            except ValueError as exc:
                QMessageBox.warning(self, "Invalid RoboCap ID", str(exc))
                return
        session = scan_session(
            root,
            session_start=session_start,
            robocap_id=robocap_id,
            input_paths=selected_files,
        )
        self.session = session
        self.results.clear()
        self.recheck_button.setEnabled(True)
        self.copy_button.setEnabled(self.debug_mode)
        self._refresh_table()
        if self.deep_on_drop:
            self._start_validation()
        else:
            self._update_summary("Structural scan complete; deep checks will run before conversion.")

    def _refresh_table(self) -> None:
        if self.session is None:
            return
        self.table.setRowCount(len(self.session.segments))
        for row, segment in enumerate(self.session.segments):
            result = self.results.get(segment.number)
            duration = f"{segment.duration_seconds:.1f}s" if segment.duration_seconds is not None else "Checking..."
            status = _display_status(segment, result)
            output = str(result.output_path) if result else ""
            values = (
                str(segment.number), str(len(segment.videos)), str(len(segment.imus)),
                duration, status, output,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, segment.number)
                self.table.setItem(row, column, item)
        self._update_buttons()

    def _start_validation(self) -> None:
        if self.session is None or self.thread is not None:
            return
        self.cancel_event = threading.Event()
        self._set_busy(True, "Running deep file and synchronization checks...")
        worker = ValidationWorker(self.session, self.cancel_event)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.segment_ready.connect(lambda _number: self._refresh_table())
        worker.finished.connect(self._validation_finished)
        worker.failed.connect(self._worker_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._thread_finished)
        self.thread = thread
        self.worker = worker
        thread.start()

    def _validation_finished(self, _session: SessionInput) -> None:
        self._refresh_table()
        ready = sum(segment.is_ready for segment in self.session.segments) if self.session else 0
        total = len(self.session.segments) if self.session else 0
        self._set_busy(False, f"{ready} of {total} segments ready")

    def _start_conversion(self) -> None:
        if self.session is None or self.thread is not None:
            return
        if any(segment.validated_fingerprint != segment.fingerprint() for segment in self.session.segments):
            self._start_validation()
            return
        segments = [segment for segment in self.session.segments if segment.is_ready]
        if not segments:
            return
        os.environ["ROBOCAP_CONVERT_VIDEO_WORKERS"] = str(self.video_workers)
        self.cancel_event = threading.Event()
        self.progress.setRange(0, len(segments))
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self._set_busy(True, f"Converting {len(segments)} segment(s)...")
        worker = ConversionWorker(self.session, segments, self.debug_mode, self.cancel_event)
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

    def _conversion_progress(self, current: int, total: int, result: ConversionResult) -> None:
        self.results[result.segment] = result
        self.progress.setValue(current)
        self._refresh_table()
        self._update_summary(f"Converted {current} of {total} segments")

    def _conversion_finished(self, results: list[ConversionResult]) -> None:
        for result in results:
            self.results[result.segment] = result
        succeeded = sum(result.success for result in results)
        self._refresh_table()
        self._set_busy(False, f"{succeeded} of {len(results)} conversions passed post-write QA")
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
        self.recheck_button.setEnabled(not busy and self.session is not None)
        self.settings_button.setEnabled(not busy)
        self._update_summary(message)
        self._update_buttons()

    def _update_buttons(self) -> None:
        ready = bool(self.session and any(segment.is_ready for segment in self.session.segments))
        self.convert_button.setEnabled(ready and self.thread is None)
        self.copy_button.setEnabled(self.debug_mode and self.session is not None)

    def _update_summary(self, message: str) -> None:
        self.summary.setText(message)

    def _selected_segment(self) -> SegmentInput | None:
        if self.session is None or not self.table.selectedItems():
            return None
        number = self.table.selectedItems()[0].data(Qt.ItemDataRole.UserRole)
        return next((segment for segment in self.session.segments if segment.number == number), None)

    def _show_selected_details(self) -> None:
        if not self.debug_mode:
            return
        segment = self._selected_segment()
        if segment is None:
            return
        groups = (
            (Severity.ERROR, "Errors"), (Severity.WARNING, "Warnings"),
            (Severity.INFO, "Information"), (Severity.PASSED, "Passed"),
        )
        html = [f"<h2>Segment {segment.number}</h2>"]
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
        result = self.results.get(segment.number)
        if result:
            html.append(f"<h3>Output</h3><p>{escape(str(result.output_path))}</p>")
            html.append('<p><a href="reveal://output">Reveal in Explorer</a></p>')
        self.details.setHtml("".join(html))

    def _reveal_output(self, _url: QUrl) -> None:
        segment = self._selected_segment()
        result = self.results.get(segment.number) if segment else None
        if result is None:
            return
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(result.output_path)])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(result.output_path.parent)))

    def _copy_report(self) -> None:
        if self.session is not None:
            QApplication.clipboard().setText(text_report(self.session))
            self._update_summary("Validation report copied to clipboard")

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        if self.session and self.thread is None:
            stale = any(
                segment.validated_fingerprint is not None
                and segment.validated_fingerprint != segment.fingerprint()
                for segment in self.session.segments
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
