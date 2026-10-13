"""Screen-mirror window — a host-side virtual display for a supported firmware (Bruce).

Opens a live view of a connected device that streams its screen over serial, so a board with no
physical display can be watched and driven from the computer's keyboard. On Start it puts the serial
connection into raw-byte mode, sends the firmware's mirror-start command (Bruce: ``display start``),
and feeds every received byte into :class:`src.core.bruce_mirror.MirrorDecoder`; the decoded draw ops
are replayed on a :class:`src.ui.qt.mirror_canvas.MirrorCanvas`. Arrow keys / Enter / Esc / PageUp /
PageDown on the canvas are translated into the firmware's navigation commands (Bruce: ``nav <dir>``)
and written back. On Stop (or close) it sends the mirror-stop command and restores the connection's
previous mode, so the Devices-tab console and parsers resume normally.

The serial read callback runs on the reader thread, so received bytes cross back to the Qt thread
through a signal (never touching a widget off-thread). All writes are best-effort and guarded — a
device unplugged mid-session must never abort the slot or the app.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from PyQt5.QtCore import QObject, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.core import bruce_mirror as bm
from src.protocols import mirror_commands_for, supports_display_mirror
from src.ui.qt.mirror_canvas import MirrorCanvas

log = logging.getLogger(__name__)


class _BytesSignal(QObject):
    """Bridge raw byte chunks from the serial reader thread onto the Qt thread."""

    received = pyqtSignal(bytes)


class MirrorWindow(QWidget):
    """Virtual-display window for one device. Construct with the DeviceManager and the device port;
    call :meth:`start` (done automatically on first show) to begin mirroring."""

    def __init__(self, dm: Any, port: str, firmware: str = "",
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, Qt.Window)
        self._dm = dm
        self._port = port
        self._firmware = firmware or ""
        self._decoder = bm.MirrorDecoder()
        self._active = False
        self._prev_raw: Optional[bool] = None
        self._byte_cb = None                     # the on_bytes callback we registered (to remove it)
        self._start_cmd, self._stop_cmd = mirror_commands_for(self._firmware)
        self._started_once = False

        self.setWindowTitle(f"Screen Mirror — {port}")
        self._sig = _BytesSignal()
        self._sig.received.connect(self._on_bytes_main)

        self._canvas = MirrorCanvas(on_key=self._on_key)
        self._status = QLabel("")
        self._status.setStyleSheet("color:#8b949e;font-size:9pt;")
        self._btn_start = QPushButton("Start")
        self._btn_stop = QPushButton("Stop")
        self._btn_start.clicked.connect(self.start)
        self._btn_stop.clicked.connect(self.stop)

        root = QVBoxLayout(self)
        controls = QHBoxLayout()
        controls.addWidget(self._btn_start)
        controls.addWidget(self._btn_stop)
        controls.addWidget(self._status, 1)
        root.addLayout(controls)
        root.addWidget(self._canvas, 1)
        hint = QLabel("Click the view, then use arrow keys to navigate, Enter to select, "
                      "Esc to go back, PageUp/PageDown to page.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8b949e;font-size:9pt;")
        root.addWidget(hint)
        self.resize(360, 560)
        self._update_buttons()

    # ── lifecycle ──
    def showEvent(self, ev) -> None:  # noqa: N802 (Qt override)
        super().showEvent(ev)
        if not self._started_once:
            self._started_once = True
            self.start()
        self._canvas.setFocus()

    def closeEvent(self, ev) -> None:  # noqa: N802 (Qt override)
        self.stop()
        super().closeEvent(ev)

    def _conn(self):
        try:
            return self._dm.get_connection(self._port)
        except Exception:  # noqa: BLE001
            return None

    def start(self) -> None:
        """Begin mirroring: raw-mode the connection, send the start command, and subscribe to bytes."""
        if self._active:
            return
        if not supports_display_mirror(self._firmware):
            self._status.setText("This firmware does not support screen mirroring.")
            return
        conn = self._conn()
        if conn is None:
            self._status.setText("Device is not connected — connect it on the Devices tab first.")
            return
        self._decoder.reset()
        self._prev_raw = bool(getattr(conn, "raw", False))
        cb = lambda data: self._sig.received.emit(bytes(data))  # noqa: E731 (reader-thread -> signal)
        try:
            conn.on_bytes(cb)
        except Exception:  # noqa: BLE001
            self._status.setText("Could not attach to the device's serial stream.")
            return
        self._byte_cb = cb
        conn.raw = True                      # the reader checks this each loop; flips to byte delivery
        self._active = True
        if not self._write(self._start_cmd):
            # The write failed — unwind so we don't leave the connection in raw mode with no stream.
            self.stop()
            self._status.setText("Could not send the mirror-start command.")
            return
        self._status.setText("Mirroring… click the view and use the keyboard to navigate.")
        self._update_buttons()

    def stop(self) -> None:
        """End mirroring: send the stop command, detach, and restore the connection's previous mode."""
        if not self._active:
            return
        self._active = False
        self._write(self._stop_cmd)          # best-effort; the device stops streaming
        conn = self._conn()
        if conn is not None:
            if self._byte_cb is not None:
                try:
                    conn.remove_byte_callback(self._byte_cb)
                except Exception:  # noqa: BLE001
                    log.debug("mirror: remove_byte_callback failed", exc_info=True)
            try:
                conn.raw = bool(self._prev_raw)   # restore text/line mode for the console + parsers
            except Exception:  # noqa: BLE001
                log.debug("mirror: restoring raw mode failed", exc_info=True)
        self._byte_cb = None
        self._status.setText("Stopped.")
        self._update_buttons()

    # ── serial <-> canvas ──
    def _on_bytes_main(self, data: bytes) -> None:
        """Qt-thread slot: decode the received bytes and replay the resulting ops on the canvas."""
        if not self._active:
            return
        try:
            ops = self._decoder.feed(data)
        except Exception:  # noqa: BLE001 — a decode glitch must never crash the UI
            log.debug("mirror: decode error", exc_info=True)
            return
        if ops:
            self._canvas.apply_many(ops)

    def _on_key(self, key_name: str) -> None:
        """Canvas key press -> the firmware's navigation command (Bruce: `nav <dir>`), written back."""
        if not self._active:
            return
        cmd = bm.nav_for_key(key_name)
        if cmd:
            self._write(cmd)

    def _write(self, cmd: str) -> bool:
        """Write a command to the device, guarded. Returns True on a clean write."""
        conn = self._conn()
        if conn is None:
            return False
        try:
            conn.write(cmd)
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("mirror: write %r failed: %s", cmd, exc)
            return False

    def _update_buttons(self) -> None:
        self._btn_start.setEnabled(not self._active)
        self._btn_stop.setEnabled(self._active)
