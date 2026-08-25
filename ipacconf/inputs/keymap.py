"""Linux evdev constants and the keycode translation tables.

The board speaks USB HID usage ids; Linux reports its own keycodes. These
tables move between the two in both directions.
"""

from __future__ import annotations

import struct

from ..codes import CODE_NAMES, SYSTEM_CODES, invert_first_wins

EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02
EV_ABS = 0x03

BTN_MOUSE = 0x110  # BTN_LEFT, then BTN_RIGHT and BTN_MIDDLE
BTN_JOYSTICK = 0x120  # BTN_TRIGGER; joystick buttons run upwards from here
BTN_LAST = 0x140  # one past BTN_THUMBR - 0x120..0x13f is exactly 32 buttons

REL_X = 0x00
ABS_HAT0X = 0x10

# struct input_event, from linux/input.h: a struct timeval (two longs) then
# __u16 type, __u16 code, __s32 value. That is 24 bytes on 64-bit and 16 on
# 32-bit, so derive it rather than hardcoding - Batocera also ships for ARM.
INPUT_EVENT_FORMAT = "@llHHi"
INPUT_EVENT_SIZE = struct.calcsize(INPUT_EVENT_FORMAT)

# EVIOCGRAB = _IOW('E', 0x90, int): take exclusive control of a device, so
# presses stop reaching EmulationStation while you are testing them. The
# kernel drops the grab when the fd closes, so there is no cleanup path to get
# wrong - stopping the monitor is enough.
EVIOCGRAB_NR = 0x90

# The kernel's own HID-usage -> Linux-keycode table, verbatim from
# drivers/hid/usbhid/usbkbd.c (usb_kbd_keycode), indexed by HID usage ID.
# Inverting it gives the direction we need. Taking it from the kernel rather
# than writing one out by hand means it agrees with whatever the kernel did to
# produce the event we are trying to reverse.
USB_KBD_KEYCODE = [
      0,   0,   0,   0,  30,  48,  46,  32,  18,  33,  34,  35,  23,  36,  37,  38,
     50,  49,  24,  25,  16,  19,  31,  20,  22,  47,  17,  45,  21,  44,   2,   3,
      4,   5,   6,   7,   8,   9,  10,  11,  28,   1,  14,  15,  57,  12,  13,  26,
     27,  43,  43,  39,  40,  41,  51,  52,  53,  58,  59,  60,  61,  62,  63,  64,
     65,  66,  67,  68,  87,  88,  99,  70, 119, 110, 102, 104, 111, 107, 109, 106,
    105, 108, 103,  69,  98,  55,  74,  78,  96,  79,  80,  81,  75,  76,  77,  71,
     72,  73,  82,  83,  86, 127, 116, 117, 183, 184, 185, 186, 187, 188, 189, 190,
    191, 192, 193, 194, 134, 138, 130, 132, 128, 129, 131, 137, 133, 135, 136, 113,
    115, 114,   0,   0,   0, 121,   0,  89,  93, 124,  92,  94,  95,   0,   0,   0,
    122, 123,  90,  91,  85,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,
      0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,
      0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,
      0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,
      0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,
     29,  42,  56, 125,  97,  54, 100, 126, 164, 166, 165, 163, 161, 115, 114, 113,
    150, 158, 159, 128, 136, 177, 178, 176, 142, 152, 173, 140,   0,   0,   0,   0,
]

# Ultimarc keeps the modifiers at 0x70-0x77 rather than HID's own 0xe0-0xe7,
# so a press of the pin holding "CTRL L" (0x70) arrives as HID usage 0xe0.
# The two runs are in the same order, so the fixup is a straight offset.
HID_MODIFIER_FIRST = 0xE0
BOARD_MODIFIER_FIRST = 0x70

# Above 0x67 the board stops speaking HID usage IDs and uses its own
# numbering - modifiers at 0x70, mouse at 0x80, system at 0x88, gamepad at
# 0x90. The kernel table still has real HID usages up there (F21-F24, the
# international and language keys), and taking them at face value would report
# a press of F21 as "CTRL L" and a Katakana key as "POWER". So the straight
# usage-is-the-byte reading only holds below here.
BOARD_PRIVATE_FIRST = 0x68

# Media keys are not HID keyboard usages at all - they arrive on the board's
# consumer-control interface, and Ultimarc numbers them its own way. Only the
# ones the board can actually be programmed with are worth listing.
LINUX_TO_BOARD_MEDIA = {
    113: SYSTEM_CODES["MUTE"],
    115: SYSTEM_CODES["VOL UP"],
    114: SYSTEM_CODES["VOL DOWN"],
    164: SYSTEM_CODES["PLAY/PAUSE"],
    163: SYSTEM_CODES["NEXT"],
    165: SYSTEM_CODES["PREV"],
    166: SYSTEM_CODES["STOP"],
    116: SYSTEM_CODES["POWER"],
    142: SYSTEM_CODES["SLEEP"],
    143: SYSTEM_CODES["WAKE"],
    155: SYSTEM_CODES["EMAIL"],
    217: SYSTEM_CODES["SEARCH"],
    156: SYSTEM_CODES["BOOKMARKS"],
    150: SYSTEM_CODES["OPEN BROWSER"],
    158: SYSTEM_CODES["WEB BACK"],
    159: SYSTEM_CODES["WEB FORWARD"],
    128: SYSTEM_CODES["WEB STOP"],
    173: SYSTEM_CODES["WEB REFRESH"],
    140: SYSTEM_CODES["CALCULATOR"],
    226: SYSTEM_CODES["MEDIA PLAYER"],
    144: SYSTEM_CODES["EXPLORER"],
}


def _build_linux_to_board() -> dict:
    """Linux keycode -> the byte the board would be programmed with."""
    table = {}
    for usage, keycode in enumerate(USB_KBD_KEYCODE):
        if not keycode:
            continue
        if HID_MODIFIER_FIRST <= usage < HID_MODIFIER_FIRST + 8:
            table.setdefault(
                keycode, usage - HID_MODIFIER_FIRST + BOARD_MODIFIER_FIRST
            )
        elif usage < BOARD_PRIVATE_FIRST:
            # Earliest usage wins: 0x31 and 0x32 both give KEY_BACKSLASH, and
            # 0x31 decodes back to "\" rather than "NON US #".
            table.setdefault(keycode, usage)
    # The media keys are not HID keyboard usages at all, so the kernel table
    # has nothing useful to say about them.
    table.update(LINUX_TO_BOARD_MEDIA)
    # A byte the board cannot hold is worse than no answer - it would name a
    # pin that cannot be carrying it.
    return {k: v for k, v in table.items() if v in CODE_NAMES}


LINUX_TO_BOARD = _build_linux_to_board()
# Earliest keycode wins, same rule as the table above was built with.
BOARD_TO_LINUX = invert_first_wins(LINUX_TO_BOARD)
