"""Virtual-display canvas — replay Bruce's decoded draw ops onto a Qt surface.

:mod:`src.core.bruce_mirror` turns Bruce's serial stream into :class:`~src.core.bruce_mirror.DrawOp`
records; this module paints them. The screen is rebuilt as a persistent QImage (Bruce streams a
display LIST, not a framebuffer, so each op is applied on top of the last and ``FILLSCREEN`` clears
it). :func:`paint_op` is the pure painter — it applies ONE op to a QPainter and is unit-testable by
rendering to an off-screen QImage and reading pixels back. :class:`MirrorCanvas` wraps that image,
scales it to the widget (aspect-preserved, nearest-neighbour so pixels stay crisp), and turns key
presses into neutral key names for the host to translate into Bruce ``nav`` commands.

Text and vector primitives reproduce Bruce's drawing calls; a ``DRAWIMAGE`` references a file on the
device's own SD/flash that the host does not have, so it is drawn as a labelled placeholder rather
than guessed at. The result is a faithful, interactive view of the Bruce UI with no physical display.
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt5.QtCore import QRect, Qt
from PyQt5.QtGui import QColor, QFont, QImage, QPainter, QPen
from PyQt5.QtWidgets import QWidget

from src.core import bruce_mirror as bm

# Default panel size until a SCREEN_INFO packet reports the real one (a common Bruce TFT is 240x320).
DEFAULT_W = 240
DEFAULT_H = 320


def qcolor_from565(value: int) -> QColor:
    """QColor for a 16-bit RGB565 value."""
    r, g, b = bm.rgb565_to_rgb888(value)
    return QColor(r, g, b)


def _text_font(size: int) -> QFont:
    """A monospaced font approximating Bruce's built-in GLCD font at text-size *size* (~8px/level)."""
    f = QFont("Menlo")
    f.setStyleHint(QFont.Monospace)
    f.setPixelSize(max(8, int(size) * 8))
    return f


def paint_op(painter: QPainter, op: bm.DrawOp) -> None:
    """Apply a single decoded DrawOp to *painter*. The painter's device gives the canvas bounds (used
    by FILLSCREEN). Unknown or malformed ops are ignored. Pure apart from the painter it is handed."""
    fn = op.fn
    f = op.fields
    dev = painter.device()
    w = dev.width()
    h = dev.height()

    if fn == bm.FILLSCREEN and len(f) >= 1:
        painter.fillRect(0, 0, w, h, qcolor_from565(f[0]))
        return

    # ── vector primitives: last field is the colour unless noted ──
    if fn in (bm.DRAWRECT, bm.FILLRECT) and len(f) >= 5:
        x, y, rw, rh, color = f[:5]
        if fn == bm.FILLRECT:
            painter.fillRect(x, y, rw, rh, qcolor_from565(color))
        else:
            painter.setPen(QPen(qcolor_from565(color)))
            painter.drawRect(x, y, rw, rh)
        return
    if fn in (bm.DRAWROUNDRECT, bm.FILLROUNDRECT) and len(f) >= 6:
        x, y, rw, rh, r, color = f[:6]
        c = qcolor_from565(color)
        painter.setPen(QPen(c))
        painter.setBrush(c if fn == bm.FILLROUNDRECT else Qt.NoBrush)
        painter.drawRoundedRect(x, y, rw, rh, r, r)
        painter.setBrush(Qt.NoBrush)
        return
    if fn in (bm.DRAWCIRCLE, bm.FILLCIRCLE) and len(f) >= 4:
        x, y, r, color = f[:4]
        c = qcolor_from565(color)
        painter.setPen(QPen(c))
        painter.setBrush(c if fn == bm.FILLCIRCLE else Qt.NoBrush)
        painter.drawEllipse(x - r, y - r, 2 * r, 2 * r)
        painter.setBrush(Qt.NoBrush)
        return
    if fn in (bm.DRAWELLIPSE, bm.FILLELLIPSE) and len(f) >= 5:
        x, y, rx, ry, color = f[:5]
        c = qcolor_from565(color)
        painter.setPen(QPen(c))
        painter.setBrush(c if fn == bm.FILLELLIPSE else Qt.NoBrush)
        painter.drawEllipse(x - rx, y - ry, 2 * rx, 2 * ry)
        painter.setBrush(Qt.NoBrush)
        return
    if fn in (bm.DRAWTRIANGLE, bm.FILLTRIANGLE) and len(f) >= 7:
        from PyQt5.QtGui import QPolygon
        from PyQt5.QtCore import QPoint
        x1, y1, x2, y2, x3, y3, color = f[:7]
        c = qcolor_from565(color)
        poly = QPolygon([QPoint(x1, y1), QPoint(x2, y2), QPoint(x3, y3)])
        painter.setPen(QPen(c))
        painter.setBrush(c if fn == bm.FILLTRIANGLE else Qt.NoBrush)
        painter.drawPolygon(poly)
        painter.setBrush(Qt.NoBrush)
        return
    if fn == bm.DRAWLINE and len(f) >= 5:
        x, y, x1, y1, color = f[:5]
        painter.setPen(QPen(qcolor_from565(color)))
        painter.drawLine(x, y, x1, y1)
        return
    if fn == bm.DRAWWIDELINE and len(f) >= 7:
        ax, ay, bx, by, wd, fg, _bg = f[:7]
        pen = QPen(qcolor_from565(fg))
        pen.setWidth(max(1, wd))
        painter.setPen(pen)
        painter.drawLine(ax, ay, bx, by)
        return
    if fn == bm.DRAWFASTVLINE and len(f) >= 4:
        x, y, ln, color = f[:4]
        painter.setPen(QPen(qcolor_from565(color)))
        painter.drawLine(x, y, x, y + ln)
        return
    if fn == bm.DRAWFASTHLINE and len(f) >= 4:
        x, y, ln, color = f[:4]
        painter.setPen(QPen(qcolor_from565(color)))
        painter.drawLine(x, y, x + ln, y)
        return
    if fn == bm.DRAWPIXEL and len(f) >= 3:
        x, y, color = f[:3]
        painter.setPen(QPen(qcolor_from565(color)))
        painter.drawPoint(x, y)
        return
    if fn == bm.DRAWARC and len(f) >= 8:
        x, y, r, ir, start, end, fg, _bg = f[:8]
        # TFT_eSPI drawArc(x, y, r, ir, start, end, ...) fills the RING between inner radius ir and
        # outer radius r, with angle 0 at 6 o'clock increasing CLOCKWISE. Qt's drawArc strokes a thin
        # ellipse arc with angle 0 at 3 o'clock increasing COUNTER-clockwise, in 1/16°. So stroke a
        # path at the mid radius with a pen as wide as the ring (recognisable Wi-Fi / RF signal bands,
        # not hairlines), and convert the angles: q = 270 - tft_angle (mod 360).
        rmid = (r + ir) / 2.0 if 0 <= ir < r else r
        width = max(1, r - ir)
        pen = QPen(qcolor_from565(fg))
        pen.setWidth(int(width))
        painter.setPen(pen)
        q_start = int((270 - end) % 360)
        q_span = int((end - start) % 360) or 360
        painter.drawArc(int(x - rmid), int(y - rmid), int(2 * rmid), int(2 * rmid),
                        q_start * 16, q_span * 16)
        return

    # ── text ──
    if fn in (bm.DRAWSTRING, bm.DRAWCENTRESTRING, bm.DRAWRIGHTSTRING, bm.PRINT) and len(f) >= 5:
        x, y, size, color, _bg = f[:5]
        if not op.text:
            return
        painter.setPen(QPen(qcolor_from565(color)))
        painter.setFont(_text_font(size))
        if fn == bm.DRAWCENTRESTRING:
            painter.drawText(QRect(x - w, y, 2 * w, max(8, size * 8)),
                             Qt.AlignHCenter | Qt.AlignTop, op.text)
        elif fn == bm.DRAWRIGHTSTRING:
            painter.drawText(QRect(0, y, x, max(8, size * 8)),
                             Qt.AlignRight | Qt.AlignTop, op.text)
        else:
            painter.drawText(QRect(x, y, w - x, max(8, size * 8)),
                             Qt.AlignLeft | Qt.AlignTop, op.text)
        return

    # ── image: a file on the device's own SD/flash the host doesn't have, so draw a labelled
    # placeholder (same as the official Bruce app). This is ONLY hit for file-based images — a
    # custom THEME icon pack or an app wallpaper. Stock Bruce draws its tool icons (WiFi/BLE/RF/…)
    # with the vector primitives above, so those render for real; only themed image icons fall here.
    if fn == bm.DRAWIMAGE and len(f) >= 2:
        x, y = f[0], f[1]
        box = QRect(x, y, 48, 48)
        painter.setPen(QPen(QColor(120, 120, 120)))
        painter.drawRect(box)
        painter.setFont(_text_font(1))
        name = (op.text or "img").rsplit("/", 1)[-1][:10]
        painter.drawText(box, Qt.AlignCenter | Qt.TextWordWrap, name)
        return


class MirrorCanvas(QWidget):
    """A persistent virtual display: decoded ops are painted onto an off-screen image which is then
    scaled to the widget. Key presses are reported through ``on_key`` as neutral key names (see
    :data:`src.core.bruce_mirror.KEY_TO_NAV`) for the owner to translate into ``nav`` commands."""

    # Qt.Key -> the neutral key name bruce_mirror.nav_for_key understands.
    _KEY_NAMES = {
        Qt.Key_Up: "up", Qt.Key_Down: "down", Qt.Key_Left: "left", Qt.Key_Right: "right",
        Qt.Key_Return: "enter", Qt.Key_Enter: "enter", Qt.Key_Space: "space",
        Qt.Key_Escape: "escape", Qt.Key_Backspace: "backspace",
        Qt.Key_PageUp: "pageup", Qt.Key_PageDown: "pagedown",
    }

    def __init__(self, on_key: Optional[Callable[[str], None]] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._on_key = on_key
        self._rotation = 0
        self._img = QImage(DEFAULT_W, DEFAULT_H, QImage.Format_RGB32)
        self._img.fill(QColor(0, 0, 0))
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(DEFAULT_W, DEFAULT_H)

    # ── incoming ops ──
    def reset_screen(self, width: int, height: int, rotation: int = 0) -> None:
        """Resize the backing image to the device's reported resolution and clear it."""
        width = max(1, min(width, 4096))
        height = max(1, min(height, 4096))
        self._rotation = rotation
        self._img = QImage(width, height, QImage.Format_RGB32)
        self._img.fill(QColor(0, 0, 0))
        self.setMinimumSize(width, height)
        self.update()

    def apply(self, op: bm.DrawOp) -> None:
        """Apply one DrawOp to the backing image (SCREEN_INFO resizes/clears; everything else paints)."""
        if op.fn == bm.SCREEN_INFO:
            if len(op.fields) >= 3:
                self.reset_screen(op.fields[0], op.fields[1], op.fields[2])
            return
        painter = QPainter(self._img)
        try:
            paint_op(painter, op)
        finally:
            painter.end()

    def apply_many(self, ops) -> None:
        """Apply a batch of ops, then request a single repaint."""
        info = [o for o in ops if o.fn == bm.SCREEN_INFO]
        if info:
            last = info[-1]
            if len(last.fields) >= 3:
                self.reset_screen(last.fields[0], last.fields[1], last.fields[2])
        painter = QPainter(self._img)
        try:
            for op in ops:
                if op.fn != bm.SCREEN_INFO:
                    paint_op(painter, op)
        finally:
            painter.end()
        self.update()

    def image(self) -> QImage:
        """The current backing image (for tests / snapshots)."""
        return self._img

    # ── rendering ──
    def paintEvent(self, _ev) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), QColor(0, 0, 0))
            if self._img.isNull():
                return
            scaled = self._img.scaled(self.size(), Qt.KeepAspectRatio, Qt.FastTransformation)
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            painter.drawImage(x, y, scaled)
        finally:
            painter.end()

    # ── input ──
    def keyPressEvent(self, ev) -> None:  # noqa: N802 (Qt override)
        name = self._KEY_NAMES.get(ev.key())
        if name and self._on_key is not None:
            self._on_key(name)
            ev.accept()
            return
        super().keyPressEvent(ev)
