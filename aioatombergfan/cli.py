"""Command-line interface to drive an Atomberg fan over BLE.

    aioatombergfan scan
    aioatombergfan status [--id <device_id>]
    aioatombergfan power on|off [--id <device_id>]
    aioatombergfan speed <1-6> [--id <device_id>]
    aioatombergfan led on|off   [--id <device_id>]
    aioatombergfan sleep on|off [--id <device_id>]
    aioatombergfan timer <0|1|2|3|6> [--id <device_id>]
    aioatombergfan identify [--id <device_id>]   # blink the speed LEDs
    aioatombergfan monitor [--id <device_id>]   # stream live state until Ctrl-C

With one fan in range, --id can be omitted. Needs a BLE adapter.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from bleak.backends.device import BLEDevice

from .device import AtombergFan
from .state import FanState
from .scanner import discover


def _fmt(s: FanState | None) -> str:
    if s is None:
        return "no state"
    bits = [f"speed={s.speed}", f"power={'on' if s.power else 'off'}"]
    if s.is_boost:
        bits.append("boost")
    if s.led:
        bits.append("led")
    if s.sleep:
        bits.append("sleep")
    if s.timer_hours:
        bits.append(f"timer={s.timer_hours}h")
    if s.series:
        bits.append(f"series={s.series}")
    return " ".join(bits)


async def _pick(device_id: str | None) -> tuple[BLEDevice, str]:
    found = await discover()
    if not found:
        print("No Atomberg fans found.", file=sys.stderr)
        raise SystemExit(2)
    if device_id:
        if device_id not in found:
            print(f"Fan {device_id} not found. Seen: {', '.join(found)}", file=sys.stderr)
            raise SystemExit(2)
        return found[device_id]
    if len(found) > 1:
        print(f"Multiple fans; use --id. Seen: {', '.join(found)}", file=sys.stderr)
        raise SystemExit(2)
    return next(iter(found.values()))


async def _run(args: argparse.Namespace) -> int:
    if args.cmd == "scan":
        for dev_id, (dev, series) in (await discover()).items():
            print(f"{dev_id}  series={series}  {dev.address}")
        return 0

    dev, series = await _pick(args.id)
    async with AtombergFan(dev, series) as fan:
        if args.cmd == "status":
            print(_fmt(fan.state))
        elif args.cmd == "power":
            await fan.set_power(args.value == "on")
        elif args.cmd == "speed":
            await fan.set_speed(int(args.value))
        elif args.cmd == "led":
            await fan.set_led(args.value == "on")
        elif args.cmd == "sleep":
            await fan.set_sleep(args.value == "on")
        elif args.cmd == "timer":
            await fan.set_timer_hours(int(args.value))
        elif args.cmd == "identify":
            await fan.identify()
        elif args.cmd == "monitor":
            fan.add_callback(lambda s: print(_fmt(s), flush=True))
            print(_fmt(fan.state), flush=True)
            try:
                await asyncio.Event().wait()
            except (KeyboardInterrupt, asyncio.CancelledError):
                return 0
        if args.cmd not in ("status", "monitor"):
            await asyncio.sleep(1.0)  # let the confirming notification arrive
            print(_fmt(fan.state))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aioatombergfan", description="Local BLE control for Atomberg fans")
    p.add_argument("--id", help="fan device_id (from `scan`); optional if only one in range")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    sub.add_parser("status")
    sub.add_parser("monitor")
    sub.add_parser("identify")
    for name in ("power", "led", "sleep"):
        sp = sub.add_parser(name)
        sp.add_argument("value", choices=["on", "off"])
    sp = sub.add_parser("speed")
    sp.add_argument("value", type=int, choices=range(1, 7))
    sp = sub.add_parser("timer")
    sp.add_argument("value", type=int, choices=[0, 1, 2, 3, 6])
    args = p.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 0
    except (FileNotFoundError, OSError) as err:
        # No BLE adapter, or BlueZ/D-Bus not reachable.
        print(f"Bluetooth unavailable: {err}", file=sys.stderr)
        return 3
    except Exception as err:  # noqa: BLE001 - surface bleak errors cleanly
        from bleak.exc import BleakError

        if isinstance(err, BleakError):
            print(f"BLE error: {err}", file=sys.stderr)
            return 3
        raise


if __name__ == "__main__":
    raise SystemExit(main())
