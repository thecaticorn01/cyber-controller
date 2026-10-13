"""Bruce screen-mirror decoder — turn Bruce's serial display stream into drawing ops.

Bruce firmware can mirror its TFT over USB serial: the ``display start`` serial command enables an
"async serial" display log, and from then on the firmware streams every drawing primitive it issues
(fill, rects, circles, lines, text, images) as small binary packets. This is a VECTOR / display-list
stream, not a raw framebuffer — the host rebuilds the screen by replaying the same draw calls onto a
canvas. ``display stop`` turns it back off. Input goes back the other way as ``nav`` commands (there is
no coordinate/touch injection in Bruce), so a keyboard drives the on-device UI.

Wire format (firmware ``include/tftLogger.h`` / ``src/core/tftLogger/tftLogger.cpp``, pinned to the
source the decoder was written against):

    byte 0   : 0xAA                      -- LOG_PACKET_HEADER (packet start marker)
    byte 1   : len                       -- TOTAL packet length in bytes, INCLUDING these 3 header bytes
    byte 2   : fn                         -- a tftFuncs code (see the constants below)
    byte 3.. : payload                   -- big-endian uint16 fields in call order; text/image ops append
                                            trailing bytes (the string / the file path) after the fields

Colours are 16-bit RGB565. Framing is SEQUENTIAL: once synced on a 0xAA header the decoder trusts
``len`` and consumes exactly that many bytes, so a 0xAA that merely appears inside a payload field is
never mistaken for a header. It only rescans for 0xAA at stream start or after a malformed length/short
packet, so a glitch self-heals without wedging the stream. This module is PURE (no Qt, no serial) so the
whole decode path is unit-testable from synthetic bytes; rendering and I/O live in the UI layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ── packet constants (verbatim from the firmware) ─────────────────────────────────────────────────
HEADER = 0xAA           # LOG_PACKET_HEADER
_MIN_PACKET = 3         # header + len + fn
_MAX_PACKET = 255       # the length field is a single byte

# tftFuncs enum — "DO NOT CHANGE THE ORDER" in the firmware, so these integer values are stable.
FILLSCREEN = 0
DRAWRECT = 1
FILLRECT = 2
DRAWROUNDRECT = 3
FILLROUNDRECT = 4
DRAWCIRCLE = 5
FILLCIRCLE = 6
DRAWTRIANGLE = 7        # firmware spells the code "DRAWTRIAGLE"
FILLTRIANGLE = 8
DRAWELLIPSE = 9         # firmware spells the code "DRAWELIPSE"
FILLELLIPSE = 10
DRAWLINE = 11
DRAWARC = 12
DRAWWIDELINE = 13
DRAWCENTRESTRING = 14
DRAWRIGHTSTRING = 15
DRAWSTRING = 16
PRINT = 17
DRAWIMAGE = 18
DRAWPIXEL = 19
DRAWFASTVLINE = 20
DRAWFASTHLINE = 21
SCREEN_INFO = 99

# Number of big-endian uint16 fields for each PURELY-NUMERIC op, in the firmware's checkAndLog() order:
#   DRAWRECT/FILLRECT            x, y, w, h, color
#   DRAWROUNDRECT/FILLROUNDRECT  x, y, w, h, r, color
#   DRAWCIRCLE/FILLCIRCLE        x, y, r, color
#   DRAW/FILLTRIANGLE            x1, y1, x2, y2, x3, y3, color
#   DRAW/FILLELLIPSE             x, y, rx, ry, color
#   DRAWLINE                     x, y, x1, y1, color
#   DRAWARC                      x, y, r, ir, startAngle, endAngle, fg, bg
#   DRAWWIDELINE                 ax, ay, bx, by, wd, fg, bg
#   DRAWFASTVLINE                x, y, h, color
#   DRAWFASTHLINE                x, y, w, color
#   DRAWPIXEL                    x, y, color
#   FILLSCREEN                   color
_NUMERIC_FIELDS = {
    FILLSCREEN: 1,
    DRAWRECT: 5, FILLRECT: 5,
    DRAWROUNDRECT: 6, FILLROUNDRECT: 6,
    DRAWCIRCLE: 4, FILLCIRCLE: 4,
    DRAWTRIANGLE: 7, FILLTRIANGLE: 7,
    DRAWELLIPSE: 5, FILLELLIPSE: 5,
    DRAWLINE: 5,
    DRAWARC: 8,
    DRAWWIDELINE: 7,
    DRAWFASTVLINE: 4, DRAWFASTHLINE: 4,
    DRAWPIXEL: 3,
}
# Text ops: 5 uint16 fields (x, y, textSize, textColor, bgColor) then the raw string bytes.
_TEXT_OPS = frozenset({DRAWSTRING, DRAWCENTRESTRING, DRAWRIGHTSTRING, PRINT})
_TEXT_FIELD_BYTES = 10  # 5 x uint16


@dataclass
class DrawOp:
    """One decoded drawing primitive from the stream.

    ``fn`` is the tftFuncs code. ``fields`` holds the numeric uint16 arguments in firmware call order
    (for SCREEN_INFO it is ``(width, height, rotation)``). ``text`` carries the trailing string for a
    text op or the file path for DRAWIMAGE; ``fs`` is DRAWIMAGE's filesystem byte (0 = SD, 2 = LittleFS).
    """

    fn: int
    fields: tuple[int, ...] = ()
    text: str = ""
    fs: int | None = None


def rgb565_to_rgb888(value: int) -> tuple[int, int, int]:
    """Convert a 16-bit RGB565 colour to an 8-bit-per-channel (r, g, b) tuple.

    Channels are rescaled (not just left-shifted) so full-scale 5/6-bit values map to exactly 255 —
    e.g. 0xFFFF -> (255, 255, 255) and 0x0000 -> (0, 0, 0)."""
    value &= 0xFFFF
    r5 = (value >> 11) & 0x1F
    g6 = (value >> 5) & 0x3F
    b5 = value & 0x1F
    return (r5 * 255 + 15) // 31, (g6 * 255 + 31) // 63, (b5 * 255 + 15) // 31


def _decode_text(raw: bytes) -> str:
    """Decode a trailing text/path field as UTF-8, replacing undecodable bytes and dropping the C0/C1
    control noise that would render as stray glyphs (the device pads/omits no terminator)."""
    s = raw.decode("utf-8", "replace")
    return "".join(ch for ch in s if ch >= " " and ch != "\x7f" or ch == "\t").replace("�", "")


class MirrorDecoder:
    """Incremental decoder for Bruce's ``display start`` byte stream.

    Feed it raw serial chunks as they arrive (any size, split anywhere); it returns the complete
    :class:`DrawOp`s that became available, buffering any partial trailing packet until the rest
    arrives. One instance per mirroring session; it holds only the unparsed byte tail and a sync flag.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._synced = False

    def reset(self) -> None:
        """Drop all buffered bytes and sync state (call when (re)starting a mirror session)."""
        self._buf.clear()
        self._synced = False

    def feed(self, chunk: bytes) -> list[DrawOp]:
        """Add received bytes and return every DrawOp that is now fully decoded (possibly none)."""
        if chunk:
            self._buf.extend(chunk)
        ops: list[DrawOp] = []
        buf = self._buf
        while True:
            if not self._synced:
                i = buf.find(HEADER)
                if i < 0:
                    buf.clear()          # no header in sight — nothing to keep
                    break
                if i:
                    del buf[:i]          # discard the pre-header garbage, keep from the 0xAA
                self._synced = True
            if len(buf) < 2:
                break                    # need the length byte
            length = buf[1]
            if length < _MIN_PACKET or length > _MAX_PACKET:
                del buf[0]               # bogus length — this 0xAA was not a real header; rescan
                self._synced = False
                continue
            if len(buf) < length:
                break                    # full packet not here yet
            packet = bytes(buf[:length])
            del buf[:length]
            op = _parse_packet(packet)
            if op is not None:
                ops.append(op)
            # Sequential framing: the next byte must be another header, else we lost sync — rescan.
            if buf and buf[0] != HEADER:
                self._synced = False
        return ops


def _u16(payload: bytes, offset: int) -> int:
    return (payload[offset] << 8) | payload[offset + 1]


def _parse_packet(packet: bytes) -> DrawOp | None:
    """Turn one framed packet (0xAA, len, fn, payload) into a DrawOp, or None if it is malformed."""
    fn = packet[2]
    payload = packet[3:]

    if fn == SCREEN_INFO:
        if len(payload) < 5:
            return None
        return DrawOp(fn, (_u16(payload, 0), _u16(payload, 2), payload[4]))

    if fn in _TEXT_OPS:
        if len(payload) < _TEXT_FIELD_BYTES:
            return None
        fields = tuple(_u16(payload, 2 * i) for i in range(5))
        return DrawOp(fn, fields, text=_decode_text(payload[_TEXT_FIELD_BYTES:]))

    if fn == DRAWIMAGE:
        # x, y, center, Ms (4 x uint16 = 8 bytes), then a 1-byte filesystem id, then the path bytes.
        if len(payload) < 9:
            return None
        fields = tuple(_u16(payload, 2 * i) for i in range(4))
        return DrawOp(fn, fields, text=_decode_text(payload[9:]), fs=payload[8])

    count = _NUMERIC_FIELDS.get(fn)
    if count is None:
        return None                      # unknown op code — skip it, keep the stream going
    if len(payload) < count * 2:
        return None
    fields = tuple(_u16(payload, 2 * i) for i in range(count))
    return DrawOp(fn, fields)


# ── input (host -> device): Bruce's `nav` command ──────────────────────────────────────────────────
# Bruce injects navigation as `nav <command> [duration_ms]`; there is NO coordinate/touch injection, so
# a mirror drives the device with the keyboard. These are the only command words the firmware's
# navCallback accepts (src/core/serial_commands/util_commands.cpp); anything else it rejects.
NAV_COMMANDS = frozenset({
    "up", "down", "next", "prev", "select", "sel", "esc", "nextpage", "prevpage",
})

# Map neutral key names (the UI translates a Qt key event to one of these) to the nav word to send.
# Bruce has no left/right verbs: left/right scroll as prev/next, which is how its own UI treats them.
KEY_TO_NAV = {
    "up": "up",
    "down": "down",
    "left": "prev",
    "right": "next",
    "enter": "select",
    "return": "select",
    "space": "select",
    "escape": "esc",
    "esc": "esc",
    "backspace": "esc",
    "pageup": "prevpage",
    "pagedown": "nextpage",
}


def nav_command(word: str, duration_ms: int | None = None) -> str | None:
    """Build the ``nav`` serial command for a navigation *word*, or None if it is not a Bruce verb.

    ``duration_ms`` (a long-press hold) is appended only when a positive value is given; Bruce defaults
    an absent/non-positive duration to a single short press."""
    word = (word or "").strip().lower()
    if word not in NAV_COMMANDS:
        return None
    if duration_ms is not None and duration_ms > 0:
        return f"nav {word} {int(duration_ms)}"
    return "nav {}".format(word)


def nav_for_key(key_name: str, duration_ms: int | None = None) -> str | None:
    """Build the ``nav`` command for a neutral key name (see :data:`KEY_TO_NAV`), or None if unmapped."""
    word = KEY_TO_NAV.get((key_name or "").strip().lower())
    return nav_command(word, duration_ms) if word else None
