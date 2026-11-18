"""A fake BLE device + patched connector, so device.py is testable without hardware."""
import asyncio
from unittest.mock import patch

import pytest


class FakeBLEDevice:
    def __init__(self, address="34:CD:B0:C8:CD:26", name="Atomberg_R1_34cdb0c8cd24"):
        self.address = address
        self.name = name


class FakeClient:
    """Minimal stand-in for BleakClient covering what device.py uses."""

    # class-level knobs the tests can tweak
    connect_delay = 0.0
    state_string = (
        "20,1,Z,10,158.80,159,240,R1,565,4,725,11541641,156802869,27979229,0,0.93,24.00,1,"
        "77142859,0,0,0,323,0,321,END"
    )
    state_bytes = "14000300"  # on, speed 4, timer 3 h

    def __init__(self, *_, disconnected_callback=None, **__):
        self._cb = disconnected_callback
        self.is_connected = False
        self.writes: list[bytes] = []
        self.reads: list[str] = []
        self.notify_handler = None

    async def connect(self):
        if self.connect_delay:
            await asyncio.sleep(self.connect_delay)
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False
        if self._cb:
            self._cb(self)

    async def start_notify(self, _uuid, handler):
        self.notify_handler = handler

    async def write_gatt_char(self, _uuid, data, response=True):
        self.writes.append(bytes(data))
        # emulate the fan: notify fresh state after a command (speed 3, on)
        if self.notify_handler:
            self.notify_handler(None, bytearray.fromhex("13000000"))

    async def read_gatt_char(self, uuid):
        from aioatombergfan.const import CMD_CHAR

        self.reads.append(uuid)
        if uuid == CMD_CHAR:
            return bytearray.fromhex(self.state_bytes)
        return self.state_string.encode()


@pytest.fixture
def make_fan(monkeypatch):
    """Factory: build an AtombergFan wired to a FakeClient, with fast timings.

    Returns a callable ``make(persistent=False, series="R1") -> (fan, client)``.
    """
    from aioatombergfan import device as devmod

    def _make(persistent: bool = False, series: str | None = "R1"):
        client = FakeClient()

        async def _fake_raw_connect(dev, on_disconnect):
            client._cb = on_disconnect
            await client.connect()
            return client

        monkeypatch.setattr(devmod, "_raw_connect", _fake_raw_connect)
        monkeypatch.setattr(devmod, "CONNECT_SETTLE", 0.0)
        monkeypatch.setattr(devmod, "IDLE_DISCONNECT", 0.05)
        fan = devmod.AtombergFan(FakeBLEDevice(), series=series, persistent=persistent)
        return fan, client

    return _make


@pytest.fixture
def fake_fan(make_fan):
    """Convenience: a non-persistent fan + client."""
    return make_fan()
