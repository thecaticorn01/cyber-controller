"""Web firmware detection + manual override (src/ui/web/app.py).

Regression for "connected but no firmware detected" on macOS: a board that resets when the port opens
is still booting during the one-shot connect probe, so the web path left it unidentified for the whole
session. These cover the two fixes — live auto-detection from the serial stream, and the manual
override endpoint/list — without real hardware (a fake connection stands in for the serial link).
"""
from __future__ import annotations

import pytest

pytest.importorskip("flask")

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.models.device import Device
from src.ui.web.app import create_app


@pytest.fixture(autouse=True)
def _creds(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")


class _FakeConn:
    """A silent, injectable serial link: records on_line callbacks and lets the test push lines.
    Its write() emits nothing, so the connect probe finds no reply (as a still-booting board would)."""

    def __init__(self):
        self.is_connected = True
        self.raw = False
        self._cbs = []
        self.writes = []

    def on_line(self, cb):
        self._cbs.append(cb)

    def remove_line_callback(self, cb):
        if cb in self._cbs:
            self._cbs.remove(cb)

    def write(self, data):
        self.writes.append(data)       # silent board: no reply emitted

    def push(self, line):
        for cb in list(self._cbs):
            cb(line)


def _app_and_dm(monkeypatch):
    dm = DeviceManager()
    app, _sio = create_app(dm, FlashEngine(), EventBus(), TargetPool())
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["cred_gen"] = client.application.extensions["cc_web_credentials"].generation
        sess["csrf"] = "tok"
    return app, dm, client


_H = {"X-CSRF-Token": "tok"}


# ── manual override endpoints ────────────────────────────────────────

def test_firmwares_list_includes_ghost_esp(monkeypatch):
    _app, _dm, client = _app_and_dm(monkeypatch)
    data = client.get("/api/firmwares").get_json()
    names = {f["name"] for f in data["firmwares"]}
    assert "ghost-esp" in names and "marauder" in names
    assert "generic" not in names and "raw" not in names   # the passthroughs aren't user-selectable


def test_set_firmware_forces_and_returns_commands(monkeypatch):
    _app, dm, client = _app_and_dm(monkeypatch)
    dm.add_device(Device(port="COM9", firmware="", connected=True))
    resp = client.post("/api/firmware", json={"port": "COM9", "firmware": "ghost-esp"}, headers=_H)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["firmware"] == "ghost-esp"
    assert body["groups"], "forcing GhostESP must yield its one-tap command set"
    dev = dm.get_device("COM9")
    assert dev.firmware == "ghost-esp" and dev.firmware_forced is True


def test_set_firmware_rejects_unknown(monkeypatch):
    _app, dm, client = _app_and_dm(monkeypatch)
    dm.add_device(Device(port="COM9", firmware="", connected=True))
    resp = client.post("/api/firmware", json={"port": "COM9", "firmware": "nope-fw"}, headers=_H)
    assert resp.status_code == 400


def test_set_firmware_accepts_loose_name(monkeypatch):
    # The override resolves separator/display drift, so "GhostESP" / "ghost_esp" land on "ghost-esp".
    _app, dm, client = _app_and_dm(monkeypatch)
    dm.add_device(Device(port="COM9", firmware="", connected=True))
    resp = client.post("/api/firmware", json={"port": "COM9", "firmware": "ghost_esp"}, headers=_H)
    assert resp.status_code == 200
    assert resp.get_json()["firmware"] == "ghost-esp"


# ── live auto-detection on connect ───────────────────────────────────

def test_live_detect_identifies_ghostesp_after_boot(monkeypatch):
    # Simulate the macOS case: connect runs while the board is silent (still booting) so the probe finds
    # nothing; once GhostESP speaks, the live detector must identify it from its own output.
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)  # don't wait the re-probe backoff

    _app, dm, client = _app_and_dm(monkeypatch)
    dm.add_device(Device(port="COM9", firmware="", connected=True))
    conn = _FakeConn()
    # Inject the fake link in place of a real serial open, and keep the device marked connected.
    monkeypatch.setattr(dm, "open_connection",
                        lambda port, **kw: (dm._connections.__setitem__(port, conn), conn)[1])
    monkeypatch.setattr(dm, "get_connection", lambda port: dm._connections.get(port))

    resp = client.post("/api/connect", json={"port": "COM9"}, headers=_H)
    assert resp.status_code == 200
    assert resp.get_json()["firmware"] == ""          # silent board: nothing detected at connect yet

    conn.push("[Ghost] Wi-Fi scan started")            # the board finishes booting and prints output
    assert dm.get_device("COM9").firmware == "ghost-esp"   # identified live, from its own text


def test_live_detect_does_not_override_forced_firmware(monkeypatch):
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)

    _app, dm, client = _app_and_dm(monkeypatch)
    dm.add_device(Device(port="COM9", firmware="", connected=True))
    conn = _FakeConn()
    monkeypatch.setattr(dm, "open_connection",
                        lambda port, **kw: (dm._connections.__setitem__(port, conn), conn)[1])
    monkeypatch.setattr(dm, "get_connection", lambda port: dm._connections.get(port))
    client.post("/api/connect", json={"port": "COM9"}, headers=_H)

    dm.set_firmware("COM9", "marauder", forced=True)   # operator forced Marauder
    conn.push("[Ghost] Wi-Fi scan started")            # a stray GhostESP-looking line
    assert dm.get_device("COM9").firmware == "marauder"  # the forced choice is not clobbered
