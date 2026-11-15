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
    state_string = "20,1,Z,10,158.80,159,240,R1,565,4,725,END"

    def __init__(self, *_, disconnected_callback=None, **__):
        self._cb = disconnected_callback
        self.is_connected = False
        self.writes: list[bytes] = []
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

    async def read_gatt_char(self, _uuid):
        return self.state_string.encode()


@pytest.fixture
def fake_fan(monkeypatch):
    """Return an AtombergFan wired to a FakeClient, with fast timings."""
    from aioatombergfan import device as devmod

    client = FakeClient()

    async def _fake_raw_connect(dev, on_disconnect):
        client._cb = on_disconnect
        await client.connect()
        return client

    monkeypatch.setattr(devmod, "_raw_connect", _fake_raw_connect)
    monkeypatch.setattr(devmod, "CONNECT_SETTLE", 0.0)
    monkeypatch.setattr(devmod, "IDLE_DISCONNECT", 0.05)
    fan = devmod.AtombergFan(FakeBLEDevice(), series="R1")
    return fan, client
