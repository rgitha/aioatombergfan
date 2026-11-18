"""Operation-model tests using the fake device (no hardware, no HA)."""
import asyncio

import pytest

from aioatombergfan.device import AtombergFan, CONNECT_TIMEOUT
from aioatombergfan.exceptions import DeviceBusy


async def test_command_connects_and_writes_without_subscribing(fake_fan):
    fan, client = fake_fan
    await fan.set_speed(3)
    assert client.writes == [b'{"speed":3}']
    assert client.notify_handler is None  # connect-on-demand never subscribes


async def test_persistent_subscribes_once_and_follows_notifications(make_fan):
    fan, client = make_fan(persistent=True)
    subscribes = 0
    real = client.start_notify

    async def _counting(uuid, handler):
        nonlocal subscribes
        subscribes += 1
        await real(uuid, handler)

    client.start_notify = _counting
    await fan.set_speed(3)
    await fan.set_led(True)
    assert subscribes == 1
    # notification from the fake fan updated live state
    assert fan.state is not None and fan.state.speed == 3 and fan.state.power
    await fan.async_shutdown()


async def test_idle_disconnect_after_operation(fake_fan):
    fan, client = fake_fan
    await fan.set_power(True)
    assert fan.connected
    await asyncio.sleep(0.12)  # > IDLE_DISCONNECT (0.05)
    assert not fan.connected


async def test_one_operation_at_a_time_user_deadline(fake_fan, monkeypatch):
    """A user request that can't get its turn in time fails with DeviceBusy."""
    fan, client = fake_fan
    from aioatombergfan import device as devmod

    started = asyncio.Event()

    async def _slow_connect(dev, on_disconnect):
        started.set()
        await asyncio.sleep(1.0)
        client._cb = on_disconnect
        await client.connect()
        return client

    monkeypatch.setattr(devmod, "_raw_connect", _slow_connect)
    monkeypatch.setattr(devmod, "CONNECT_TIMEOUT", 0.1)

    first = asyncio.ensure_future(fan.async_refresh())  # background: waits the whole attempt
    await started.wait()
    with pytest.raises(DeviceBusy):
        await fan.set_speed(2)  # user request, can't get a turn in 0.1s
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first


async def test_user_deadline_covers_the_connect_and_a_late_link_is_closed(fake_fan, monkeypatch):
    from aioatombergfan import device as devmod
    from aioatombergfan.exceptions import NotConnected

    fan, client = fake_fan
    connected = asyncio.Event()

    async def _slow_connect(dev, on_disconnect):
        await asyncio.sleep(0.2)
        client._cb = on_disconnect
        await client.connect()
        connected.set()
        return client

    monkeypatch.setattr(devmod, "_raw_connect", _slow_connect)
    monkeypatch.setattr(devmod, "CONNECT_TIMEOUT", 0.05)
    with pytest.raises(NotConnected, match="no connection within"):
        await fan.set_power(True)
    await connected.wait()  # the attempt carried on (not cut short)...
    await asyncio.sleep(0.01)
    assert not client.is_connected  # ...and its late link was closed
    assert client.writes == []


async def test_user_request_joins_a_background_attempt(fake_fan, monkeypatch):
    from aioatombergfan import device as devmod

    fan, client = fake_fan
    attempts = 0

    async def _slow_connect(dev, on_disconnect):
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(0.05)
        client._cb = on_disconnect
        await client.connect()
        return client

    monkeypatch.setattr(devmod, "_raw_connect", _slow_connect)
    background = asyncio.ensure_future(fan.async_refresh())
    await asyncio.sleep(0.01)
    await fan.set_speed(3)
    await background
    assert attempts == 1


async def test_one_attempt_per_connection(monkeypatch):
    from aioatombergfan import device as devmod

    seen = {}

    async def _establish(*args, **kwargs):
        seen.update(kwargs)
        raise devmod.BleakError("nope")

    monkeypatch.setattr(devmod, "establish_connection", _establish)
    with pytest.raises(devmod.BleakError):
        await devmod._raw_connect(type("D", (), {"address": "x"})(), lambda c: None)
    assert seen["max_attempts"] == 1


async def test_failure_after_connecting_closes_the_link(make_fan):
    from aioatombergfan.device import BleakError

    fan, client = make_fan(persistent=True)

    async def _broken_notify(_uuid, _handler):
        raise BleakError("notify failed")

    client.start_notify = _broken_notify
    with pytest.raises(BleakError):
        await fan.set_power(True)
    assert not client.is_connected and not fan.connected


async def test_idle_disconnect_never_overlaps_a_new_operation(fake_fan, monkeypatch):
    """An operation arriving while the idle disconnect runs waits for it,
    then connects afresh: connect and disconnect never overlap."""
    fan, client = fake_fan
    events: list[str] = []
    gate = asyncio.Event()
    real_disconnect = client.disconnect

    async def _slow_disconnect():
        events.append("disconnect start")
        await gate.wait()
        await real_disconnect()
        events.append("disconnect end")

    client.disconnect = _slow_disconnect
    real_connect = client.connect

    async def _connect():
        events.append("connect")
        await real_connect()

    client.connect = _connect
    await fan.set_power(True)
    await asyncio.sleep(0.07)  # idle disconnect starts (0.05)
    assert events[-1] == "disconnect start"
    op = asyncio.ensure_future(fan.set_led(True))
    await asyncio.sleep(0.02)
    assert events[-1] == "disconnect start"  # the new operation waits
    gate.set()
    await op
    assert events[-3:] == ["disconnect start", "disconnect end", "connect"]


async def test_stuck_warning_after_three_failures_while_heard(fake_fan, monkeypatch, caplog):
    from aioatombergfan import device as devmod
    from aioatombergfan.exceptions import NotConnected

    fan, _client = fake_fan
    fan._heard_recently = lambda: "esp32-proxy"

    async def _fail(dev, on_disconnect):
        raise devmod.BleakError("timeout")

    monkeypatch.setattr(devmod, "_raw_connect", _fail)
    for _ in range(3):
        with pytest.raises(NotConnected):
            await fan.async_refresh()
    assert "can't be connected to (3 attempts in a row) through esp32-proxy" in caplog.text


async def test_no_stuck_warning_when_not_heard(fake_fan, monkeypatch, caplog):
    from aioatombergfan import device as devmod
    from aioatombergfan.exceptions import NotConnected

    fan, _client = fake_fan
    fan._heard_recently = lambda: None

    async def _fail(dev, on_disconnect):
        raise devmod.BleakError("timeout")

    monkeypatch.setattr(devmod, "_raw_connect", _fail)
    for _ in range(4):
        with pytest.raises(NotConnected):
            await fan.async_refresh()
    assert "can't be connected" not in caplog.text


async def test_background_read_gives_way_to_a_waiting_user(fake_fan):
    from aioatombergfan.const import STATE_CHAR

    fan, client = fake_fan
    await fan.set_power(True)  # connect
    client.reads.clear()
    async with fan._command_queue(None):  # hold the queue
        background = asyncio.ensure_future(fan.async_refresh())
        await asyncio.sleep(0)
        user = asyncio.ensure_future(fan.set_speed(2))
        await asyncio.sleep(0)
    await background
    await user
    assert STATE_CHAR not in client.reads  # the background read gave way
    assert client.writes[-1] == b'{"speed":2}'


async def test_shutdown_disconnects(fake_fan):
    fan, client = fake_fan
    await fan.set_power(True)
    fan.persistent = True
    await fan.async_shutdown()
    assert not client.is_connected


async def test_persistent_keeps_link(make_fan):
    fan, _client = make_fan(persistent=True)
    await fan.set_power(True)
    await asyncio.sleep(0.12)
    assert fan.connected  # persistent: no idle disconnect
    await fan.async_disconnect()
    assert not fan.connected


async def test_refresh_keeps_timer(fake_fan):
    """A refresh reads the 4-byte state, which carries the timer."""
    from aioatombergfan.const import CMD_CHAR, STATE_CHAR

    fan, client = fake_fan
    st = await fan.async_refresh()
    assert st is not None and st.timer_hours == 3 and st.speed == 4 and st.power
    assert client.reads == [STATE_CHAR, CMD_CHAR]
    assert fan.details is not None and fan.details.rpm == 240


async def test_refresh_learns_series(make_fan):
    from aioatombergfan.const import CMD_CHAR, STATE_CHAR

    fan, client = make_fan(series=None)
    st = await fan.async_refresh()
    assert fan.series == "R1" and st is not None and st.series == "R1"
    assert client.reads == [STATE_CHAR, CMD_CHAR]


async def test_identify_blinks_and_restores_the_led(fake_fan, monkeypatch):
    from aioatombergfan import device as devmod

    monkeypatch.setattr(devmod, "IDENTIFY_INTERVAL", 0.0)
    fan, client = fake_fan
    await fan.identify(blinks=2)
    # state read says LED off: on, off, on, off
    assert client.writes == [b'{"led":true}', b'{"led":false}'] * 2


async def test_link_callback_follows_the_link(fake_fan):
    fan, _client = fake_fan
    seen: list[bool] = []
    fan.add_link_callback(seen.append)
    await fan.set_power(True)
    await asyncio.sleep(0.12)  # idle disconnect
    assert seen == [True, False]


async def test_failed_exchange_closes_the_link_and_the_next_reconnects(fake_fan):
    from aioatombergfan.device import BleakError

    fan, client = fake_fan
    connects = 0
    real_connect = client.connect

    async def _counting_connect():
        nonlocal connects
        connects += 1
        await real_connect()

    client.connect = _counting_connect
    real_read = client.read_gatt_char

    async def _stuck_read(uuid):
        raise BleakError("read timed out")

    client.read_gatt_char = _stuck_read
    with pytest.raises(BleakError):
        await fan.async_refresh()
    assert not client.is_connected and not fan.connected  # closed at once, not left idle

    client.read_gatt_char = real_read
    await fan.async_refresh()
    assert connects == 2  # a fresh link


async def test_bad_value_never_touches_the_link(fake_fan):
    fan, client = fake_fan
    with pytest.raises(ValueError):
        await fan.set_speed(7)
    assert not client.is_connected and client.writes == []


async def test_last_read_follows_successful_reads(fake_fan, monkeypatch):
    from aioatombergfan import device as devmod

    monkeypatch.setattr(devmod, "monotonic", lambda: 123.0)
    fan, _client = fake_fan
    assert fan.last_read is None
    await fan.set_power(True)
    assert fan.last_read is None  # a command alone isn't a read
    await fan.async_refresh(user=True)
    assert fan.last_read == 123.0
