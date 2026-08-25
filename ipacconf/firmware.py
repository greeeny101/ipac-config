"""Firmware capability rules, keyed by bcdDevice.

What a given firmware can do, which interface carries its config, and when a
write to flash has to be refused.
"""

from __future__ import annotations

from .identity import MODE_KEYBOARD, VENDOR_2015, VENDOR_XINPUT


# Firmware version (bcdDevice) -> what the board can do. From Ultimarc-linux
# README.fw. The interface rule comes from ipacseries.c: versions in
# [0x40, 0x56) have no game controller device and carry config on interface 2.
FIRMWARE_NOTES = [
    (0x22, 0x34, "keyboard only (single mode) - no gamepad without a firmware upgrade"),
    (0x34, 0x40, "mixed mode - keyboard AND gamepad at once"),
    (0x44, 0x50, "keyboard only (single mode) - no gamepad without a firmware upgrade"),
    (0x50, 0x57, "multi-mode - keyboard/Dinput/Xinput switchable by hotkey"),
    (0x57, 0x58, "multi-mode (beta) - a pin can be assigned as a mode change "
                 "button, so switching need not be a ten second hotkey hold"),
]


def firmware_supports_gamepad(bcd: int) -> bool:
    """True if this firmware can act as a game controller at all."""
    return 0x34 <= bcd < 0x40 or bcd >= 0x50


def firmware_note(bcd: int) -> str:
    for low, high, note in FIRMWARE_NOTES:
        if low <= bcd < high:
            return note
    return "unrecognised firmware version"


def config_interface_for(bcd: int) -> int:
    """Which USB interface carries the config protocol.

    Mixed-mode firmware (1.34-1.39) exposes an extra game controller
    interface, which pushes config to interface 3. Everything else uses 2.
    """
    return 2 if 0x40 <= bcd < 0x56 else 3


def flash_write_blocked(info):
    """Why a write to this board would not survive a power cycle, or None.

    Confirmed on hardware: in Dinput the board takes all 65 messages, applies
    them to RAM and acts on them straight away, then drops the commit. No
    stall, no error, no short read - the config simply reverts on the next
    power cycle. Only keyboard mode writes flash.
    """
    # A pre-2015 board speaks a different protocol and is refused long before
    # a write; this has nothing to say about it. Of the boards it does cover,
    # only a mode positively known to be keyboard is safe - Dinput, Xinput and
    # any product id no version of this tool has seen all warn.
    if info.vendor not in (VENDOR_2015, VENDOR_XINPUT):
        return None
    if info.mode == MODE_KEYBOARD:
        return None
    if info.vendor == VENDOR_XINPUT:
        # Not separately confirmed on hardware: Xinput is assumed to behave
        # like Dinput because it is the same non-keyboard case in the same
        # firmware, and the safe assumption is the one that warns. If a write
        # here does survive a power cycle, this is the note to delete.
        return (
            "the board is in %s mode. Dinput is confirmed to apply a write "
            "and then drop the flash commit, and Xinput is assumed to do the "
            "same - so treat what you write here as lasting only until the "
            "next power cycle.\n"
            "Hold Start1+P1SW1 for 10s to switch to keyboard mode, then "
            "write. The mode is not in the config block, so switching back "
            "afterwards will not disturb what you wrote - but which hotkey "
            "gets you to a *user set* Xinput mode is not settled: mode 3 "
            "(Start1+P1SW3) is documented as the Xinput preset, which "
            "ignores your config, and mode 4 has been seen landing in Xinput "
            "too. Switch, then check with `list`." % info.mode
        )
    return (
        "the board is in %s mode, where a write is applied but never "
        "committed to flash - it takes effect immediately and then reverts on "
        "the next power cycle.\n"
        "Hold Start1+P1SW1 for 10s to switch to keyboard mode, write, then "
        "switch back with Start1+P1SW4 - mode 4, Dinput user set, which is "
        "the Dinput mode that acts on your config. Confirmed on hardware: "
        "the led flashes four times and the board comes up d209:0421. "
        "Start1+P1SW2 is mode 2, the Dinput *preset*, which runs a fixed "
        "internal map and ignores what you wrote. The mode is not in the "
        "config block, so switching back will not disturb it." % info.mode
    )
