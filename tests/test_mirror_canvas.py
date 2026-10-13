"""Virtual-display canvas (src.ui.qt.mirror_canvas) — offscreen render + input mapping.

Applies decoded Bruce draw ops to the backing image and reads pixels back, and checks that key
presses are reported as the neutral key names the host maps to `nav` commands. Offscreen.
"""
from __future__ import annotations

import os

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


def _rgb(img, x, y):
    c = img.pixelColor(x, y)
    return (c.red(), c.green(), c.blue())


def test_fillscreen_then_fillrect_paints_expected_pixels(qapp):
    from src.ui.qt.mirror_canvas import MirrorCanvas

    cv = MirrorCanvas()
    cv.reset_screen(240, 320, 0)
    cv.apply_many([
        bm.DrawOp(bm.FILLSCREEN, (0xF800,)),             # whole screen red
        bm.DrawOp(bm.FILLRECT, (10, 10, 20, 20, 0x001F)),  # blue 20x20 block at (10,10)
    ])
    img = cv.image()
    assert _rgb(img, 0, 0) == (255, 0, 0)        # background red
    assert _rgb(img, 15, 15) == (0, 0, 255)      # inside the blue rect
    assert _rgb(img, 200, 300) == (255, 0, 0)    # still red far from the rect


def test_screen_info_op_resizes_the_canvas(qapp):
    from src.ui.qt.mirror_canvas import MirrorCanvas

    cv = MirrorCanvas()
    cv.apply(bm.DrawOp(bm.SCREEN_INFO, (128, 160, 1)))
    assert cv.image().width() == 128
    assert cv.image().height() == 160


def test_draw_fast_hline_paints_a_row(qapp):
    from src.ui.qt.mirror_canvas import MirrorCanvas

    cv = MirrorCanvas()
    cv.reset_screen(64, 64, 0)
    cv.apply(bm.DrawOp(bm.FILLSCREEN, (0x0000,)))
    cv.apply(bm.DrawOp(bm.DRAWFASTHLINE, (5, 8, 20, 0x07E0)))  # green run y=8, x=5..25
    img = cv.image()
    assert _rgb(img, 10, 8) == (0, 255, 0)
    assert _rgb(img, 10, 9) == (0, 0, 0)


def test_key_press_reports_neutral_name(qapp):
    from src.ui.qt.mirror_canvas import MirrorCanvas

    seen = []
    cv = MirrorCanvas(on_key=seen.append)
    cv.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Up, Qt.NoModifier))
    cv.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
    cv.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    assert seen == ["up", "enter", "escape"]
    # And those names round-trip through the core mapping to real Bruce nav verbs.
    assert [bm.nav_for_key(n) for n in seen] == ["nav up", "nav select", "nav esc"]
