"""Operation-model tests using the fake device (no hardware, no HA)."""
import asyncio

import pytest

from aioatombergfan.device import AtombergFan, CONNECT_TIMEOUT
from aioatombergfan.exceptions import DeviceBusy


async def test_command_connects_writes_and_updates_state(fake_fan):
    fan, client = fake_fan
    await fan.set_speed(3)
    assert client.writes == [b'{"speed":3}']
    # notification from the fake fan updated live state
    assert fan.state is not None and fan.state.speed == 3 and fan.state.power


async def test_idle_disconnect_after_operation(fake_fan):
    fan, client = fake_fan
    await fan.set_power(True)
    assert fan.connected
    await asyncio.sleep(0.12)  # > IDLE_DISCONNECT (0.05)
    assert not fan.connected


async def test_one_operation_at_a_time_user_deadline(fake_fan, monkeypatch):
    fan, client = fake_fan
    from aioatombergfan import device as devmod

    # Make a connect that blocks, so the first op holds the lock while a second
    # user op waits past its deadline -> DeviceBusy.
    started = asyncio.Event()

    async def _slow_connect(dev, on_disconnect):
        started.set()
        await asyncio.sleep(1.0)
        client._cb = on_disconnect
        await client.connect()
        return client

    monkeypatch.setattr(devmod, "_raw_connect", _slow_connect)
    monkeypatch.setattr(devmod, "CONNECT_TIMEOUT", 0.1)

    first = asyncio.ensure_future(fan.set_speed(1))
    await started.wait()
    with pytest.raises(DeviceBusy):
        await fan.set_speed(2)  # user request, can't get a turn in 0.1s
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first


async def test_persistent_keeps_link(make_fan):
    fan, _client = make_fan(persistent=True)
    await fan.set_power(True)
    await asyncio.sleep(0.12)
    assert fan.connected  # persistent: no idle disconnect
    await fan.async_disconnect()
    assert not fan.connected
