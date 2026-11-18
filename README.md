# aioatombergfan

Local **Bluetooth LE** control for Atomberg smart fans. No cloud, no account.

Verified on an **Atomberg R1 (Renesa)**. The protocol was reverse-engineered from a
real device; it should fit other fans in the same family but only the R1 is confirmed.

## Install
```bash
pip install aioatombergfan
```

## CLI
```bash
aioatombergfan scan                 # list fans in range (device_id, series, address)
aioatombergfan status               # current state
aioatombergfan speed 3              # speed 1-6 (6 = boost); also powers on
aioatombergfan power on|off
aioatombergfan led on|off
aioatombergfan sleep on|off
aioatombergfan timer 0|1|2|3|6      # hours
aioatombergfan identify             # blink the speed LEDs, then restore them
aioatombergfan monitor              # stream live state, including remote-control changes
```
Add `--id <device_id>` when more than one fan is in range.

## Library
```python
import asyncio
from aioatombergfan import AtombergFan, discover

async def main():
    fans = await discover()
    dev, series = next(iter(fans.values()))
    async with AtombergFan(dev, series) as fan:
        fan.add_callback(print)
        await fan.set_speed(3)
        await asyncio.sleep(2)

asyncio.run(main())
```

## How it works
- Commands are plain JSON written to the command characteristic. Each command sets **one**
  setting; the fan ignores a command that carries two. `set_speed` powers the fan on if it
  was off.
- Connect-on-demand by default: the link opens for an operation, reads the state and closes
  after a short idle period. It doesn't subscribe to notifications: the fan stops answering
  subscription requests after a few links that each subscribed, until it's power-cycled.
- Pass `persistent=True` for an always-open link: it subscribes once, so every change,
  including those made with the physical remote, arrives at once.

## Limits
- **Untested:** whether the fan accepts a second BLE connection while one is open. If your
  phone cannot connect while this holds the link, use the default connect-on-demand mode.
- Wi-Fi control is intentionally left out: the R1 drops its Wi-Fi unless it reaches the
  vendor cloud, so BLE is the local path.

## Development
```bash
pip install -e .[dev]
mypy --strict aioatombergfan
pytest -q
```

## License
LGPL-3.0-or-later.
