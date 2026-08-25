"""Safety checks run before a config is written.

Each returns a message or None. They are shared by the CLI and the web UI, so
they must stay free of printing and of HTTP.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .codes import (
    ALL_CODES,
    GAMEPAD_FIRST_CODE,
    P2_FIRST_CODE,
    code_to_name,
    control_span,
    is_game_code,
)
from .firmware import firmware_note, firmware_supports_gamepad
from .identity import MODE_KEYBOARD, VENDOR_XINPUT
from .pins import PIN_ORDER, PIN_TABLE
from .protocol import SHIFT_BIT, XINPUT_BIT

if TYPE_CHECKING:  # a type hint only - importing the device layer for it
    from .device import DeviceInfo  # would make every check depend on hardware

def config_kind(raw: bytes) -> str:
    """Which of keyboard, gamepad or mixed a download reads as.

    Multi-mode firmware picks its mode from the config it is sent rather than
    from a field in it. Ultimarc document three rules: a keyboard-only
    download moves a board in mode 4 to keyboard mode 1, a gamepad-only one
    moves a board in mode 1 to Dinput mode 4, and a gamepad-only one carrying
    an Xbox HOME key moves it to Xinput mode 5.

    Everything else is "mixed", and mixed is the case worth naming, because it
    is easy to produce by accident. A profile that assigns gamepad buttons but
    leaves the stick pins alone is not gamepad-only once it is encoded: the
    unassigned pins keep whatever the board already had, which on a factory
    board is keycodes. The download is then mixed and the board stays where it
    is - which looks exactly like the write having been ignored.
    """
    data = raw[4:]
    kinds = set()
    for name in PIN_ORDER:
        ai, alt_i, _ = PIN_TABLE[name]
        for index in (ai, alt_i):
            code = data[index]
            if not code:
                continue  # unassigned - no opinion
            if is_game_code(code):
                kinds.add("gamepad")
            elif code_to_name(code) is None:
                continue  # a macro or a byte we do not recognise - no opinion
            else:
                kinds.add("keyboard")
    if len(kinds) == 1:
        return kinds.pop()
    return "mixed"


# Start1 held with one of these for ten seconds selects a mode. They are
# ordinary pins, so a profile can assign them anything - including actions
# that do not exist in the mode the board is currently in.
MODE_SELECT_PINS = ("1sw1", "1sw2", "1sw3", "1sw4", "1sw5")


def hotkey_warning(raw: bytes):
    """Whether this config would disarm the board's mode-switch hotkeys.

    Mode switching is Start1 (as the I-PAC shift control) held with P1SW1-5.
    Those are the same six pins a profile is free to reassign, and a gamepad
    action on them is inert while the board is in keyboard mode - so a gamepad
    profile written in keyboard mode can leave the board with no working way
    to get to the gamepad mode it was written for.

    Recoverable, but only by the one route that ignores the config entirely:
    holding P1SW1 while plugging in usb. Worth saying before the write, not
    after.
    """
    data = raw[4:]
    shift_pins = [
        name for name in PIN_ORDER
        if data[PIN_TABLE[name][2]] & SHIFT_BIT
    ]
    if not shift_pins:
        return (
            "this config sets no I-PAC shift key. Ultimarc document mode "
            "switching as requiring one, so Start1+P1SW1-5 will stop working "
            "and the only way back is holding P1SW1 while plugging in usb."
        )

    def inert(name):
        return is_game_code(data[PIN_TABLE[name][0]])

    dead = [n for n in shift_pins + list(MODE_SELECT_PINS) if inert(n)]
    if not dead:
        return None
    return (
        "this config puts gamepad actions on %s, which the mode-switch "
        "hotkeys use (%s as the shift key, held with P1SW1-5). Gamepad "
        "actions do nothing while the board is in keyboard mode, so after "
        "this write Start1+P1SW1-5 will not switch modes and the only way "
        "back is holding P1SW1 while plugging in usb.\n"
        "Leave those pins on keycodes if you want the hotkeys to keep "
        "working." % (", ".join(dead), ", ".join(shift_pins))
    )


def unconfirmed_code_warning(raw: bytes):
    """Pins on codes inside a player's block but not a button or a hat.

    Each block is 25 codes: eleven buttons, four hat directions, and ten more
    that are only known to be axis controls - 0x9d was seen moving ABS_X, and
    nothing else in that stretch has been pressed. Assigned to a stick
    direction one of them moves an axis instead of the d-pad, which looks like
    a direction that simply does not work.
    """
    data = raw[4:]
    axis_codes = set()
    for first in (GAMEPAD_FIRST_CODE, P2_FIRST_CODE):
        axis_codes.update(control_span(first)[2])

    hits = []
    for name in PIN_ORDER:
        for index, field in ((PIN_TABLE[name][0], ""), (PIN_TABLE[name][1], " alt")):
            code = data[index]
            if code in axis_codes:
                hits.append("%s%s=0x%02x" % (name, field, code))
    if not hits:
        return None
    return (
        "%s sit in the axis part of a player's block. Each player has eleven "
        "buttons then four hat directions then ten codes that are only known "
        "to move axes; on a stick direction one of those moves an axis rather "
        "than the d-pad, which reads as a direction that does nothing."
        % ", ".join(hits)
    )


def xinput_warning(raw: bytes, info: DeviceInfo):
    """Whether this write hands the board to Xinput, or takes it out.

    Worth saying out loud in both directions. Xinput has no config interface,
    so a write that sets the bit is the last one this tool can make until the
    board is brought back by hand.
    """
    setting = bool(raw[3] & XINPUT_BIT)
    if setting:
        return (
            "this config has the Xinput bit set, so the board will come up in "
            "Xinput - as 045e:028e, wearing an Xbox 360 pad's identity. There "
            "is no hid interface in Xinput, so this tool cannot reach the "
            "board again until you bring it back: hold Start1+P1SW4 for 10s "
            "for Dinput, Start1+P1SW1 for keyboard, or hold P1SW1 while "
            "plugging in usb.\n"
            "Pass --no-xinput to clear the bit instead."
        )
    if info.mode != MODE_KEYBOARD and info.vendor == VENDOR_XINPUT:
        return (
            "this config clears the Xinput bit, so the board will leave "
            "Xinput on its next mode change."
        )
    return None


def mode_switch_note(raw: bytes, info: DeviceInfo):
    """What the board is expected to do with its mode after this download."""
    kind = config_kind(raw)
    if kind == "gamepad" and info.mode == MODE_KEYBOARD:
        return (
            "this is a gamepad-only config. Ultimarc document the board as "
            "switching itself to a gamepad mode when it is sent one, but that "
            "did not happen on a 1.55 board - with or without --reconfigure. "
            "Expect to switch by hand: hold Start1 with P1SW2..P1SW5 for 10s "
            "and check `lsusb` for which one gives d209:0421 (Dinput). The "
            "config you just wrote is what those modes will use."
        )
    if kind == "keyboard" and info.mode != MODE_KEYBOARD:
        return (
            "this is a keyboard-only config, so the board is expected to "
            "switch itself back to keyboard mode 1."
        )
    if kind == "mixed":
        return (
            "this config mixes keyboard and gamepad actions. Pins the profile "
            "leaves unassigned keep what the board already had, and alternate "
            "actions count too, which is how a gamepad profile ends up mixed "
            "without anyone meaning it to - profiles/gamepad.json assigns "
            "every pin and clears every alternate. Note that on a 1.55 board "
            "even a properly gamepad-only download did not switch the mode by "
            "itself, so expect to use the hotkeys either way."
        )
    return None


def _gamepad_warning(profile: dict, info: DeviceInfo):
    def is_game_action(value):
        code = ALL_CODES.get(str(value).strip()) or ALL_CODES.get(str(value).strip().upper())
        return code is not None and is_game_code(code)

    uses_gamepad = any(
        is_game_action(pin.get(field))
        for pin in profile.get("pins", [])
        for field in ("action", "alternate_action")
    )
    if uses_gamepad and not firmware_supports_gamepad(info.bcd & 0xFF):
        return (
            "WARNING: this profile assigns gamepad actions, but firmware %s is "
            "keyboard-only (%s).\n"
            "         The bytes will be written, but the board cannot act on "
            "them until it is upgraded to 1.50+.\n" % (info.firmware, firmware_note(info.bcd & 0xFF))
        )
    return None
