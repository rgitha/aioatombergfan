"""Decode Atomberg fan state (verified on R1 / Renesa)."""

from __future__ import annotations

from dataclasses import dataclass

from .const import (
    BIT_BOOST,
    BIT_LED,
    BIT_POWER,
    BIT_SLEEP,
    MASK_SPEED,
)


@dataclass
class FanState:
    power: bool
    speed: int  # 1-6; 6 == boost
    boost: bool
    led: bool
    sleep: bool
    timer_hours: int  # 0/1/2/3/6
    series: str | None = None
    raw0: int = 0  # byte 0 / state-string field 0

    @property
    def is_boost(self) -> bool:
        return self.boost or self.speed == 6


def _decode_flags(field0: int, timer_hours: int, series: str | None) -> FanState:
    return FanState(
        power=bool(field0 & BIT_POWER),
        speed=field0 & MASK_SPEED,
        boost=bool(field0 & BIT_BOOST),
        led=bool(field0 & BIT_LED),
        sleep=bool(field0 & BIT_SLEEP),
        timer_hours=timer_hours,
        series=series,
        raw0=field0,
    )


def decode_notification(data: bytes) -> FanState | None:
    """Decode a 4-byte notification/read from CMD_CHAR.

    byte0 = flags, byte1 = brightness (R1: always 0), byte2 = timer hours, byte3 = 0.
    """
    if len(data) < 3:
        return None
    return _decode_flags(data[0], data[2], None)


def decode_state_string(text: str) -> FanState | None:
    """Decode the comma-separated string from STATE_CHAR.

    Field 0 = same flags as notification byte 0; field 7 = series code.
    Returns None for non-state replies (e.g. an error string).
    """
    parts = text.split(",")
    field0 = parts[0].strip()
    if not field0.lstrip("-").isdigit():
        return None
    v = int(field0)
    series = parts[7].strip() if len(parts) > 7 else None
    # Timer hours aren't in the flag byte of the string; use 0 unless a
    # notification supplies it. (The string carries timer in later fields that
    # we don't rely on here.)
    return _decode_flags(v, 0, series)
