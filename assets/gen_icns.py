#!/usr/bin/env python3
"""Generate the macOS app-bundle icon (``assets/icon.icns``) from the square app icon.

macOS shows a ``.icns`` for a ``.app`` bundle (the Dock, Finder, the switcher); a Windows ``.ico``
is not understood there. PyInstaller's ``--windowed`` build on macOS embeds whatever ``--icon`` it is
given into the bundle's ``Info.plist`` (``CFBundleIconFile``), so the flasher's Mac ``.app`` needs this
file to carry the real icon instead of the generic blank-document placeholder.

Source is ``assets/icon.ico`` (a 256x256 RGBA master — the same art as the Windows icon), so both
platforms show one coherent mark. Pillow writes the multi-resolution ``.icns`` directly; no macOS-only
``iconutil`` is required, so the icon regenerates on any platform the build runs on.

Run:  python assets/gen_icns.py        # writes assets/icon.icns
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

_ASSETS = Path(__file__).resolve().parent
_SRC = _ASSETS / "icon.ico"
_OUT = _ASSETS / "icon.icns"

# The icon slots macOS actually uses. Pillow upsamples/downsamples the master to each; the master is
# 256px, so larger slots don't gain detail but keep the bundle valid on Retina displays.
_ICNS_SIZES = [(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)]


def generate() -> Path:
    """Write ``assets/icon.icns`` from the square master icon and return its path."""
    master = Image.open(_SRC).convert("RGBA")
    # Square the master defensively so a non-square source can never skew the rounded app icon.
    side = max(master.size)
    if master.size != (side, side):
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        canvas.paste(master, ((side - master.width) // 2, (side - master.height) // 2))
        master = canvas
    master.save(_OUT, format="ICNS", sizes=_ICNS_SIZES)
    return _OUT


if __name__ == "__main__":
    out = generate()
    print(f"wrote {out} ({out.stat().st_size} bytes)")
