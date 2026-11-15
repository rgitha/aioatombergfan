"""Async BLE client for an Atomberg fan (verified on R1 / Renesa).

Connection model (mirrors the single-operation pattern):

* Every exchange runs inside one `_operation`: operations run one at a time,
  each whole, so nothing slips in between a check and the command it decides.
* The link is opened on demand, held for the operation, and closed after a
  short idle period (connect-on-demand, not a timer poll).
* A user request gets a deadline: if it can't get its turn and a link within
  `CONNECT_TIMEOUT`, it fails with `DeviceBusy` rather than going out late.
  Background reads wait and give way to a waiting user request.
* While connected we subscribe to `CMD_CHAR` notifications, so live state
  (including IR-remote changes) arrives without polling.

Protocol note: the fan has no message number. A command's "ack" is the GATT
write-response; the state notification/read-back that follows is a separate
concern and never fails the command.

`persistent=True` keeps the link open (no idle disconnect) for always-live
state, at the cost of possibly blocking other BLE centrals (e.g. a phone).
Default is connect-on-demand.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from logging import getLogger

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from . import commands
from .const import CMD_CHAR, NAME_PREFIX, STATE_CHAR, WIFI_STATUS, WIFI_STATUS_CHAR
from .exceptions import DeviceBusy, NotConnected
from .state import FanState, decode_notification, decode_state_string

_LOGGER = getLogger(__name__)

CONNECT_TIMEOUT = 5.0  # user-request deadline: get a turn + link within this
IDLE_DISCONNECT = 5.0  # seconds idle before dropping the link (non-persistent)
CONNECT_SETTLE = 0.5  # pause after connecting before the first exchange

try:
    from bleak_retry_connector import establish_connection

    async def _raw_connect(
        dev: BLEDevice, on_disconnect: Callable[[BleakClient], None]
    ) -> BleakClient:
        return await establish_connection(
            BleakClient, dev, dev.address, disconnected_callback=on_disconnect
        )
except ImportError:  # pragma: no cover - standalone fallback

    async def _raw_connect(
        dev: BLEDevice, on_disconnect: Callable[[BleakClient], None]
    ) -> BleakClient:
        client = BleakClient(dev, disconnected_callback=on_disconnect)
        await client.connect()
        return client


def parse_advert_name(name: str | None) -> tuple[str, str] | None:
    """`Atomberg_R1_34cdb0c8cd24` -> (device_id, series)."""
    if not name or not name.startswith(NAME_PREFIX):
        return None
    parts = name.split("_")
    if len(parts) < 3:
        return None
    return parts[2], parts[1]  # device_id, series


StateCallback = Callable[[FanState], None]


class AtombergFan:
    """A BLE control session to one fan."""

    def __init__(
        self,
        device: BLEDevice,
        series: str | None = None,
        *,
        persistent: bool = False,
    ) -> None:
        self._device = device
        self.series = series
        self.persistent = persistent
        self.state: FanState | None = None

        self._client: BleakClient | None = None
        self._callbacks: list[StateCallback] = []

        # one operation at a time (re-entrant for the owning task)
        self._command_lock = asyncio.Lock()
        self._command_owner: asyncio.Task[object] | None = None

        # link lifetime
        self._link_holds = 0
        self._disconnect_task: asyncio.Task[None] | None = None

        self._subscribed = False

    # ---------------------------------------------------------------- state
    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    def add_callback(self, cb: StateCallback) -> Callable[[], None]:
        self._callbacks.append(cb)
        return lambda: self._callbacks.remove(cb)

    def _emit(self, state: FanState) -> None:
        if state.series is None:
            state.series = self.series
        self.state = state
        for cb in list(self._callbacks):
            try:
                cb(state)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("state callback failed")

    def _on_notify(self, _char: object, data: bytearray) -> None:
        st = decode_notification(bytes(data))
        if st is not None:
            self._emit(st)

    def _on_disconnect(self, _client: BleakClient) -> None:
        self._subscribed = False
        self._client = None

    # ------------------------------------------------------------ operation
    @contextlib.asynccontextmanager
    async def _operation(self, *, user: bool) -> AsyncIterator[None]:
        """Run one exchange as a single operation. See module docstring."""
        deadline: float | None = None
        if user and self._command_owner is not asyncio.current_task():
            deadline = asyncio.get_running_loop().time() + CONNECT_TIMEOUT
        async with self._command_queue(deadline), self._holding_link():
            await self._ensure_connected()
            yield

    @contextlib.asynccontextmanager
    async def _command_queue(self, deadline: float | None) -> AsyncIterator[None]:
        """One operation at a time; re-entrant for the owning task. With a
        deadline, give up waiting for a turn (raise DeviceBusy)."""
        task = asyncio.current_task()
        if task is not None and self._command_owner is task:
            yield  # a step of the operation already in progress
            return
        if deadline is None:
            await self._command_lock.acquire()
        else:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            try:
                await asyncio.wait_for(self._command_lock.acquire(), remaining)
            except (TimeoutError, asyncio.TimeoutError) as err:
                raise DeviceBusy(
                    f"{self._device.address} is busy with another command "
                    f"(no turn within {CONNECT_TIMEOUT:g}s)"
                ) from err
        self._command_owner = task
        try:
            yield
        finally:
            self._command_owner = None
            self._command_lock.release()

    @contextlib.asynccontextmanager
    async def _holding_link(self) -> AsyncIterator[None]:
        """Keep the link open for the operation; start the idle disconnect
        when the last hold ends (unless persistent)."""
        self._link_holds += 1
        self._cancel_idle_disconnect()
        try:
            yield
        finally:
            self._link_holds -= 1
            if self._link_holds == 0 and not self.persistent:
                self._schedule_idle_disconnect()

    # ------------------------------------------------------------ link mgmt
    async def _ensure_connected(self) -> None:
        if self.connected:
            return
        try:
            self._client = await _raw_connect(self._device, self._on_disconnect)
        except (BleakError, TimeoutError, asyncio.TimeoutError, OSError) as err:
            self._client = None
            raise NotConnected(f"could not connect to {self._device.address}: {err}") from err
        await asyncio.sleep(CONNECT_SETTLE)
        await self._ensure_subscribed()

    async def _ensure_subscribed(self) -> None:
        if self._subscribed or self._client is None:
            return
        await self._client.start_notify(CMD_CHAR, self._on_notify)
        self._subscribed = True

    def _cancel_idle_disconnect(self) -> None:
        if self._disconnect_task is not None:
            self._disconnect_task.cancel()
            self._disconnect_task = None

    def _schedule_idle_disconnect(self) -> None:
        self._cancel_idle_disconnect()
        self._disconnect_task = asyncio.ensure_future(self._idle_disconnect())

    async def _idle_disconnect(self) -> None:
        try:
            await asyncio.sleep(IDLE_DISCONNECT)
        except asyncio.CancelledError:
            return
        if self._link_holds == 0:
            await self.async_disconnect()

    async def async_connect(self) -> None:
        """Open the link eagerly (useful with persistent=True)."""
        async with self._operation(user=False):
            await self.async_refresh(user=False)

    async def async_disconnect(self) -> None:
        self._cancel_idle_disconnect()
        client, self._client = self._client, None
        self._subscribed = False
        if client is not None:
            with contextlib.suppress(BleakError, OSError):
                await client.disconnect()

    async def __aenter__(self) -> "AtombergFan":
        await self.async_connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.async_disconnect()

    # ------------------------------------------------------------- commands
    async def _write(self, payload: bytes) -> None:
        if self._client is None:
            raise NotConnected(f"{self._device.address} not connected")
        # ack == write-response; state read-back is separate (never fails this)
        await self._client.write_gatt_char(CMD_CHAR, payload, response=True)

    async def set_power(self, on: bool, *, user: bool = True) -> None:
        async with self._operation(user=user):
            await self._write(commands.set_power(on))

    async def set_speed(self, speed: int, *, user: bool = True) -> None:
        async with self._operation(user=user):
            await self._write(commands.set_speed(speed))  # also powers on if off

    async def set_led(self, on: bool, *, user: bool = True) -> None:
        async with self._operation(user=user):
            await self._write(commands.set_led(on))

    async def set_sleep(self, on: bool, *, user: bool = True) -> None:
        async with self._operation(user=user):
            await self._write(commands.set_sleep(on))

    async def set_timer_hours(self, hours: int, *, user: bool = True) -> None:
        async with self._operation(user=user):
            await self._write(commands.set_timer_hours(hours))

    # ---------------------------------------------------------------- reads
    async def async_refresh(self, *, user: bool = False) -> FanState | None:
        """Read the full state string and update `state`."""
        async with self._operation(user=user):
            if self._client is None:
                raise NotConnected(f"{self._device.address} not connected")
            raw = await self._client.read_gatt_char(STATE_CHAR)
            st = decode_state_string(bytes(raw).decode(errors="ignore"))
            if st is not None:
                self._emit(st)
            return st

    async def read_wifi_status(self, *, user: bool = False) -> str | None:
        async with self._operation(user=user):
            if self._client is None:
                raise NotConnected(f"{self._device.address} not connected")
            raw = await self._client.read_gatt_char(WIFI_STATUS_CHAR)
            try:
                return WIFI_STATUS.get(int(bytes(raw).decode(errors="ignore").strip()))
            except (ValueError, IndexError):
                return None
