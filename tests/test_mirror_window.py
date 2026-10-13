"""Screen-mirror window (src.ui.qt.mirror_window) — serial lifecycle with a fake connection. Offscreen.

Checks the full round trip without hardware: Start raw-modes the link and sends the start command,
received bytes decode and paint the canvas, a key press writes the matching `nav` command, and
Stop/close sends the stop command and restores the connection's previous mode.
"""
from __future__ import annotations

import os
import struct

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtGui import QKeyEvent  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.core import bruce_mirror as bm  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _packet(fn, *u16, trailing=b""):
    payload = b"".join(struct.pack(">H", v & 0xFFFF) for v in u16) + trailing
    return bytes([bm.HEADER, 3 + len(payload), fn]) + payload


class _FakeConn:
    def __init__(self):
        self.raw = False
        self.writes = []
        self._byte_cbs = []

    def on_bytes(self, cb):
        self._byte_cbs.append(cb)

    def remove_byte_callback(self, cb):
        if cb in self._byte_cbs:
            self._byte_cbs.remove(cb)

    def write(self, data):
        self.writes.append(data)

    def push(self, data):          # simulate the reader thread delivering bytes
        for cb in list(self._byte_cbs):
            cb(data)


class _FakeDM:
    def __init__(self, conn):
        self._conn = conn

    def get_connection(self, port):
        return self._conn


def _window(firmware="bruce"):
    from src.ui.qt.mirror_window import MirrorWindow
    conn = _FakeConn()
    win = MirrorWindow(_FakeDM(conn), "/dev/cu.usbserial-10", firmware=firmware)
    return win, conn


def test_start_rawmodes_and_sends_start_command(qapp):
    win, conn = _window()
    win.start()
    assert conn.raw is True                       # byte-delivery mode engaged
    assert conn.writes == ["display start"]       # Bruce mirror-start command sent
    assert conn._byte_cbs                          # subscribed to the raw stream


def test_received_bytes_paint_the_canvas(qapp):
    win, conn = _window()
    win.start()
    conn.push(_packet(bm.SCREEN_INFO, 64, 64, trailing=bytes([0])))
    conn.push(_packet(bm.FILLSCREEN, 0x001F))     # blue
    img = win._canvas.image()
    assert img.width() == 64 and img.height() == 64
    c = img.pixelColor(10, 10)
    assert (c.red(), c.green(), c.blue()) == (0, 0, 255)


def test_key_press_writes_nav_command(qapp):
    win, conn = _window()
    win.start()
    conn.writes.clear()
    win._canvas.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Down, Qt.NoModifier))
    win._canvas.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
    assert conn.writes == ["nav down", "nav select"]


def test_stop_sends_stop_command_and_restores_mode(qapp):
    win, conn = _window()
    assert conn.raw is False
    win.start()
    assert conn.raw is True
    win.stop()
    assert conn.writes[-1] == "display stop"
    assert conn.raw is False                       # previous (text) mode restored
    assert not conn._byte_cbs                       # callback detached


def test_unsupported_firmware_does_not_start(qapp):
    win, conn = _window(firmware="marauder")       # marauder has no mirror support
    win.start()
    assert conn.raw is False
    assert conn.writes == []
    assert "not support" in win._status.text().lower()
