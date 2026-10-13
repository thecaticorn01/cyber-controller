"""Bruce screen-mirror decoder (src.core.bruce_mirror) — pure, no Qt / no serial.

Covers the wire format pinned to the firmware's tftLogger: 0xAA-framed packets, big-endian uint16
fields in the firmware's call order, RGB565 colour conversion, sequential framing that is immune to a
0xAA byte appearing inside a payload, resync after garbage, packets split across reads, the text /
image / screen-info layouts, and the `nav` input mapping (Bruce has no coordinate injection).
"""
from __future__ import annotations

import struct

from src.core import bruce_mirror as bm


def _packet(fn: int, *u16_fields: int, trailing: bytes = b"", fs: int | None = None) -> bytes:
    """Build one framed packet exactly as the firmware does: 0xAA, total-len, fn, then payload."""
    payload = b"".join(struct.pack(">H", v & 0xFFFF) for v in u16_fields)
    if fs is not None:
        payload += bytes([fs & 0xFF])
    payload += trailing
    length = 3 + len(payload)
    return bytes([bm.HEADER, length, fn]) + payload


# ── RGB565 ───────────────────────────────────────────────────────────

def test_rgb565_endpoints_and_primaries():
    assert bm.rgb565_to_rgb888(0x0000) == (0, 0, 0)
    assert bm.rgb565_to_rgb888(0xFFFF) == (255, 255, 255)
    assert bm.rgb565_to_rgb888(0xF800) == (255, 0, 0)   # full red
    assert bm.rgb565_to_rgb888(0x07E0) == (0, 255, 0)   # full green
    assert bm.rgb565_to_rgb888(0x001F) == (0, 0, 255)   # full blue


# ── single-op decode, exact field order ──────────────────────────────

def test_fillscreen_and_rects():
    ops = bm.MirrorDecoder().feed(
        _packet(bm.FILLSCREEN, 0x001F)
        + _packet(bm.FILLRECT, 10, 20, 30, 40, 0xF800)
        + _packet(bm.DRAWROUNDRECT, 1, 2, 3, 4, 5, 0x07E0)
    )
    assert [o.fn for o in ops] == [bm.FILLSCREEN, bm.FILLRECT, bm.DRAWROUNDRECT]
    assert ops[0].fields == (0x001F,)
    assert ops[1].fields == (10, 20, 30, 40, 0xF800)
    assert ops[2].fields == (1, 2, 3, 4, 5, 0x07E0)


def test_triangle_line_circle_arc_wideline_field_counts():
    dec = bm.MirrorDecoder()
    ops = dec.feed(
        _packet(bm.FILLTRIANGLE, 1, 2, 3, 4, 5, 6, 0xFFFF)
        + _packet(bm.DRAWLINE, 7, 8, 9, 10, 0x1234)
        + _packet(bm.DRAWCIRCLE, 11, 12, 13, 0x4567)
        + _packet(bm.DRAWARC, 1, 2, 3, 4, 90, 270, 0xAAAA, 0x5555)
        + _packet(bm.DRAWWIDELINE, 1, 2, 3, 4, 5, 0x0001, 0x0002)
        + _packet(bm.DRAWFASTVLINE, 20, 21, 22, 0x0F0F)
    )
    assert ops[0].fields == (1, 2, 3, 4, 5, 6, 0xFFFF)
    assert ops[1].fields == (7, 8, 9, 10, 0x1234)
    assert ops[2].fields == (11, 12, 13, 0x4567)
    assert ops[3].fields == (1, 2, 3, 4, 90, 270, 0xAAAA, 0x5555)
    assert ops[4].fields == (1, 2, 3, 4, 5, 0x0001, 0x0002)
    assert ops[5].fields == (20, 21, 22, 0x0F0F)


def test_screen_info_reports_dimensions_and_rotation():
    (op,) = bm.MirrorDecoder().feed(_packet(bm.SCREEN_INFO, 240, 320, trailing=bytes([1])))
    assert op.fn == bm.SCREEN_INFO
    assert op.fields == (240, 320, 1)


def test_text_op_fields_then_string():
    (op,) = bm.MirrorDecoder().feed(
        _packet(bm.DRAWSTRING, 5, 6, 2, 0xFFFF, 0x0000, trailing=b"Hello")
    )
    assert op.fn == bm.DRAWSTRING
    assert op.fields == (5, 6, 2, 0xFFFF, 0x0000)   # x, y, size, color, bg
    assert op.text == "Hello"


def test_text_op_strips_control_noise_but_keeps_unicode():
    (op,) = bm.MirrorDecoder().feed(
        _packet(bm.PRINT, 0, 0, 1, 0xFFFF, 0x0000, trailing="OK\x01\x1b[0m ✓".encode("utf-8"))
    )
    assert op.text == "OK[0m ✓"   # the ESC + C0 byte are dropped; the real text and ✓ survive


def test_drawimage_carries_fs_and_path():
    (op,) = bm.MirrorDecoder().feed(
        _packet(bm.DRAWIMAGE, 0, 0, 1, 100, fs=2, trailing=b"/img/logo.jpg")
    )
    assert op.fn == bm.DRAWIMAGE
    assert op.fields == (0, 0, 1, 100)
    assert op.fs == 2
    assert op.text == "/img/logo.jpg"


# ── framing robustness ───────────────────────────────────────────────

def test_packet_split_across_many_chunks_reassembles():
    whole = _packet(bm.FILLRECT, 10, 20, 30, 40, 0xF800)
    dec = bm.MirrorDecoder()
    collected = []
    for b in whole:                      # feed one byte at a time
        collected += dec.feed(bytes([b]))
    assert len(collected) == 1
    assert collected[0].fields == (10, 20, 30, 40, 0xF800)


def test_0xAA_inside_payload_is_not_mistaken_for_a_header():
    # x=0x00AA, color=0xAAAA — three 0xAA payload bytes that a naive scanner would treat as headers.
    pkt = _packet(bm.FILLRECT, 0x00AA, 0xAA00, 0x0001, 0x0002, 0xAAAA)
    ops = bm.MirrorDecoder().feed(pkt + _packet(bm.FILLSCREEN, 0x1234))
    assert [o.fn for o in ops] == [bm.FILLRECT, bm.FILLSCREEN]
    assert ops[0].fields == (0x00AA, 0xAA00, 0x0001, 0x0002, 0xAAAA)
    assert ops[1].fields == (0x1234,)


def test_leading_garbage_before_first_header_is_skipped():
    ops = bm.MirrorDecoder().feed(b"boot log text\r\n" + _packet(bm.FILLSCREEN, 0x07E0))
    assert [o.fn for o in ops] == [bm.FILLSCREEN]
    assert ops[0].fields == (0x07E0,)


def test_resyncs_after_a_bogus_length_byte():
    # A stray 0xAA with an impossible length (< 3) must not wedge the stream; the next real packet
    # still decodes.
    dec = bm.MirrorDecoder()
    ops = dec.feed(bytes([bm.HEADER, 0x00]) + _packet(bm.FILLSCREEN, 0x1234))
    assert [o.fn for o in ops] == [bm.FILLSCREEN]


def test_unknown_opcode_is_skipped_not_fatal():
    dec = bm.MirrorDecoder()
    unknown = bytes([bm.HEADER, 5, 77, 0x00, 0x00])   # fn=77 is not a known tftFuncs code
    ops = dec.feed(unknown + _packet(bm.FILLSCREEN, 0x0001))
    assert [o.fn for o in ops] == [bm.FILLSCREEN]   # the unknown op is dropped, the stream continues


# ── nav input mapping ────────────────────────────────────────────────

def test_nav_command_only_accepts_real_bruce_verbs():
    assert bm.nav_command("up") == "nav up"
    assert bm.nav_command("select") == "nav select"
    assert bm.nav_command("nextpage") == "nav nextpage"
    assert bm.nav_command("UP") == "nav up"            # case-insensitive
    assert bm.nav_command("left") is None              # Bruce has no left/right verb
    assert bm.nav_command("bogus") is None


def test_nav_command_appends_positive_duration_only():
    assert bm.nav_command("down", 250) == "nav down 250"
    assert bm.nav_command("down", 0) == "nav down"     # non-positive -> single press, no duration
    assert bm.nav_command("down", -5) == "nav down"


def test_nav_for_key_maps_arrows_enter_esc_and_pages():
    assert bm.nav_for_key("up") == "nav up"
    assert bm.nav_for_key("left") == "nav prev"        # left/right scroll as prev/next
    assert bm.nav_for_key("right") == "nav next"
    assert bm.nav_for_key("enter") == "nav select"
    assert bm.nav_for_key("escape") == "nav esc"
    assert bm.nav_for_key("pagedown") == "nav nextpage"
    assert bm.nav_for_key("f1") is None                # unmapped key
