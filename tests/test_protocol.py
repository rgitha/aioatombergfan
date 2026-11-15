"""Pure-logic tests (no hardware) against the captured R1 frames."""
import pytest

from aioatombergfan import commands, decode_notification, decode_state_string, parse_advert_name
from aioatombergfan.const import TIMER_HOURS_TO_VALUE


def test_advert_name():
    assert parse_advert_name("Atomberg_R1_34cdb0c8cd24") == ("34cdb0c8cd24", "R1")
    assert parse_advert_name("Something") is None
    assert parse_advert_name(None) is None


# Real notifications captured from the fan (byte0 flags, byte2 timer).
@pytest.mark.parametrize("hexstr,power,speed,led,sleep,boost,timer", [
    ("12000000", True, 2, False, False, False, 0),   # on, speed 2
    ("04000000", False, 4, False, False, False, 0),  # off, speed 4 remembered
    ("14000000", True, 4, False, False, False, 0),   # on, speed 4
    ("56000000", True, 6, False, False, True, 0),    # boost (speed 6 + 0x40)
    ("46000000", False, 6, False, False, True, 0),   # off, boost remembered
    ("94000000", True, 4, False, True, False, 0),    # sleep
    ("34000000", True, 4, True, False, False, 0),    # led
    ("14000100", True, 4, False, False, False, 1),   # timer 1h
    ("14000600", True, 4, False, False, False, 6),   # timer 6h
])
def test_decode_notification(hexstr, power, speed, led, sleep, boost, timer):
    s = decode_notification(bytes.fromhex(hexstr))
    assert (s.power, s.speed, s.led, s.sleep, s.boost, s.timer_hours) == (
        power, speed, led, sleep, boost, timer)


def test_decode_state_string():
    # Real read: field0=20 -> on, speed4; field7=series.
    s = decode_state_string("20,1,Z,10,158.80,159,240,R1,565,4,725,END")
    assert s.power and s.speed == 4 and s.series == "R1"
    # field0=1 -> off, speed1
    s2 = decode_state_string("1,1,Z,1,82.82,158,114,R1,546,3,692,END")
    assert not s2.power and s2.speed == 1
    # non-numeric field0 -> not a state frame
    assert decode_state_string("No command detected") is None


def test_commands_one_setting_each():
    assert commands.set_speed(2) == b'{"speed":2}'
    assert commands.set_power(True) == b'{"power":true}'
    assert commands.set_led(False) == b'{"led":false}'
    assert commands.set_timer_hours(6) == b'{"timer":4}'  # 6h -> value 4
    assert commands.read_state() == b'{"read":1}'


def test_command_validation():
    with pytest.raises(ValueError):
        commands.set_speed(7)
    with pytest.raises(ValueError):
        commands.set_timer_hours(5)


def test_timer_map_matches_captures():
    # command value 4 == 6 hours (captured 14-00-06)
    assert TIMER_HOURS_TO_VALUE[6] == 4
