#!/usr/bin/env python3
"""
ipacconf - configure an Ultimarc I-PAC 2 (2015+) from Linux.

Standard library only. Talks to the board through /dev/hidraw via the
HIDIOCSOUTPUT ioctl, which is the same USB transaction WinIPAC uses
(SET_REPORT, output report id 3) but needs no libusb, no udev rules and no
detaching of the kernel HID driver. That makes it a straight scp onto a
Batocera box, whose root filesystem is read-only and has no pip.

    ipacconf.py list                     # what is attached
    ipacconf.py dump -o before.json      # read the board's config
    ipacconf.py apply p.json --dry-run   # show the byte diff, write nothing
    ipacconf.py apply p.json             # write it (backs up first)
    ipacconf.py restore before.json      # byte-exact restore
    ipacconf.py saved                    # list backups and presets
    ipacconf.py monitor                  # name the pin behind each button press
    ipacconf.py serve                    # web UI on :8080

Protocol sources: katie-snow/Ultimarc-linux (C) and katie-snow/QtPyUltimarc
(Python). See README.md.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime
import errno
import glob
import json
import os
import queue
import re
import select
import struct
import sys
import threading
import time

__version__ = "0.1.0"

# --------------------------------------------------------------------------
# Device identity
# --------------------------------------------------------------------------

VENDOR_2015 = 0xD209
PRODUCT_IPAC2 = 0x0420
VENDOR_PRE2015 = 0xD208
PRODUCT_PRE2015 = 0x0310

# Multi-mode firmware (1.50+) reports the board's current mode in its product
# id: switching with Start1+P1SW2 re-enumerates it as a different device. This
# is why a keyboard-mode config and a Dinput-mode config read back identical -
# the mode is not in the config block at all.
IPAC2_MODES = {
    0x0420: "keyboard",
    0x0421: "Dinput game controller",
}

# Other 2015+ boards share this protocol but have different pin tables; we
# recognise them only to give a clear "not supported" message.
KNOWN_2015_PRODUCTS = {
    0x0420: "I-PAC 2",
    0x0421: "I-PAC 2",
    0x0430: "I-PAC 4",
    0x0440: "Mini-PAC",
    0x0450: "J-PAC",
}

# --------------------------------------------------------------------------
# Wire protocol
# --------------------------------------------------------------------------

REPORT_ID = 0x03
CONFIG_SIZE = 256  # what a read returns: 4 byte header + 252 data bytes
CHUNK = 4  # config is sent 4 bytes at a time, in 5 byte output reports

# A write is four bytes longer than a read response. Ultimarc-linux sends
# IPACSERIES_SIZE (260) - see ipac.c, which callocs 260 and passes that to
# writeIPACSeriesUSB - and the board waits for all 65 messages before
# committing to flash. Sending only 256 is accepted message by message and
# then silently discarded, which is exactly how this was found.
WRITE_SIZE = 260

HEADER_WRITE = (0x50, 0xDD, 0x0F)  # 4th byte is the config bitfield
HEADER_READ = (0x59, 0xDD, 0x0F, 0x00)

# Bit 6 of a pin's shift byte marks it as the shift key. Real boards carry
# 0x01 in the low bits of every pin's shift byte and 0x41 on the shift pin, so
# this is set and cleared as a bit rather than written as a whole byte.
SHIFT_BIT = 0x40

MACRO_START = 166  # index into the 252 byte data array
MACRO_MAX_COUNT = 30
MACRO_MAX_SIZE = 85
MACRO_FIRST_CODE = 0xE0  # macros are referenced by codes 0xe0..0xfe
MACRO_LAST_CODE = 0xFE

DEBOUNCE = {"standard": 0, "none": 1, "short": 2, "long": 3}

# Firmware version (bcdDevice) -> what the board can do. From Ultimarc-linux
# README.fw. The interface rule comes from ipacseries.c: versions in
# [0x40, 0x56) have no game controller device and carry config on interface 2.
FIRMWARE_NOTES = [
    (0x22, 0x34, "keyboard only (single mode) - no gamepad without a firmware upgrade"),
    (0x34, 0x40, "mixed mode - keyboard AND gamepad at once"),
    (0x44, 0x50, "keyboard only (single mode) - no gamepad without a firmware upgrade"),
    (0x50, 0x58, "multi-mode - keyboard/Dinput/Xinput switchable by hotkey"),
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


# --------------------------------------------------------------------------
# Pin table
# --------------------------------------------------------------------------
#
# Each pin owns three bytes in the 252 byte data array: its action, its
# alternate action (used while the shift key is held) and a shift marker.
# The layout is regular - alternate = action + 50, shift = action + 100.
#
# NOTE: QtPyUltimarc's PinMapping lists 2sw1 as (16, 56, 116) and 2sw5 as
# (32, 81, 132). Both break that rule, and 81 collides with 1sw5's alternate
# index, which would silently corrupt a second pin. They look like typos for
# 66 and 82, so we derive all three indices from the rule instead. If a real
# board ever disagrees, `apply --dry-run --diff` shows exactly which bytes
# would move before anything is written.

PIN_ACTION_INDEX = {
    # player 1 stick
    "1up": 19, "1down": 17, "1left": 21, "1right": 23,
    # player 2 stick
    "2up": 20, "2down": 18, "2left": 22, "2right": 0,
    # player 1 buttons
    "1sw1": 39, "1sw2": 37, "1sw3": 35, "1sw4": 33,
    "1sw5": 31, "1sw6": 29, "1sw7": 27, "1sw8": 25,
    # player 2 buttons
    "2sw1": 16, "2sw2": 38, "2sw3": 36, "2sw4": 34,
    "2sw5": 32, "2sw6": 28, "2sw7": 26, "2sw8": 24,
    # admin
    "1start": 47, "1coin": 45, "1a": 43, "1b": 41,
    "2start": 46, "2coin": 44, "2a": 42, "2b": 40,
}

PIN_TABLE = {
    name: (idx, idx + 50, idx + 100) for name, idx in PIN_ACTION_INDEX.items()
}

# Display order and grouping, used by the CLI and the web UI.
PIN_GROUPS = [
    ("Player 1 stick", ["1up", "1down", "1left", "1right"]),
    ("Player 1 buttons", ["1sw1", "1sw2", "1sw3", "1sw4", "1sw5", "1sw6", "1sw7", "1sw8"]),
    ("Player 2 stick", ["2up", "2down", "2left", "2right"]),
    ("Player 2 buttons", ["2sw1", "2sw2", "2sw3", "2sw4", "2sw5", "2sw6", "2sw7", "2sw8"]),
    ("Start / coin / admin", ["1start", "1coin", "1a", "1b", "2start", "2coin", "2a", "2b"]),
]

PIN_ORDER = [name for _, names in PIN_GROUPS for name in names]

# --------------------------------------------------------------------------
# Code table
# --------------------------------------------------------------------------
#
# 2015+ boards speak standard USB HID usage IDs for keys, plus Ultimarc's own
# range above 0x80 for mouse, gamepad, analog, hat and media actions.

KEY_CODES = {
    "A": 0x04, "B": 0x05, "C": 0x06, "D": 0x07, "E": 0x08, "F": 0x09,
    "G": 0x0A, "H": 0x0B, "I": 0x0C, "J": 0x0D, "K": 0x0E, "L": 0x0F,
    "M": 0x10, "N": 0x11, "O": 0x12, "P": 0x13, "Q": 0x14, "R": 0x15,
    "S": 0x16, "T": 0x17, "U": 0x18, "V": 0x19, "W": 0x1A, "X": 0x1B,
    "Y": 0x1C, "Z": 0x1D,
    "1": 0x1E, "2": 0x1F, "3": 0x20, "4": 0x21, "5": 0x22,
    "6": 0x23, "7": 0x24, "8": 0x25, "9": 0x26, "0": 0x27,
    "ENTER": 0x28, "ESC": 0x29, "BKSP": 0x2A, "TAB": 0x2B, "SPACE": 0x2C,
    "-": 0x2D, "=": 0x2E, "[": 0x2F, "]": 0x30, "\\": 0x31,
    "NON US #": 0x32, ";": 0x33, "'": 0x34, "`": 0x35,
    ",": 0x36, ".": 0x37, "/": 0x38, "CAPS": 0x39,
    "F1": 0x3A, "F2": 0x3B, "F3": 0x3C, "F4": 0x3D, "F5": 0x3E, "F6": 0x3F,
    "F7": 0x40, "F8": 0x41, "F9": 0x42, "F10": 0x43, "F11": 0x44, "F12": 0x45,
    "PRNT SCRN": 0x46, "SCROLL": 0x47, "PAUSE": 0x48, "INSERT": 0x49,
    "HOME": 0x4A, "PGUP": 0x4B, "DEL": 0x4C, "END": 0x4D, "PGDWN": 0x4E,
    "RIGHT": 0x4F, "LEFT": 0x50, "DOWN": 0x51, "UP": 0x52,
    "NUM": 0x53, "KP /": 0x54, "KP *": 0x55, "KP -": 0x56, "KP +": 0x57,
    "KP ENTER": 0x58, "KP 1": 0x59, "KP 2": 0x5A, "KP 3": 0x5B, "KP 4": 0x5C,
    "KP 5": 0x5D, "KP 6": 0x5E, "KP 7": 0x5F, "KP 8": 0x60, "KP 9": 0x61,
    "KP 0": 0x62, "KP .": 0x63, "NON US \\": 0x64, "APP": 0x65,
    "KB POWER": 0x66, "KP =": 0x67,
    "CTRL L": 0x70, "SHIFT L": 0x71, "ALT L": 0x72, "WIN L": 0x73,
    "CTRL R": 0x74, "SHIFT R": 0x75, "ALT R": 0x76, "WIN MENU": 0x77,
}

MOUSE_CODES = {
    "MOUSE L DBL CLK": 0x80, "MOUSE L": 0x81,
    "MOUSE M": 0x82, "MOUSE R": 0x83,
}

SYSTEM_CODES = {
    "POWER": 0x88, "SLEEP": 0x89, "WAKE": 0x8A,
    "VOL UP": 0x8B, "VOL DOWN": 0x8C,
    "MUTE": 0xE2, "PLAY/PAUSE": 0xE3, "NEXT": 0xE4, "PREV": 0xE5, "STOP": 0xE6,
    "EMAIL": 0xF0, "SEARCH": 0xF1, "BOOKMARKS": 0xF2, "OPEN BROWSER": 0xF3,
    "WEB BACK": 0xF4, "WEB FORWARD": 0xF5, "WEB STOP": 0xF6,
    "WEB REFRESH": 0xF7, "MEDIA PLAYER": 0xF8, "CALCULATOR": 0xFA,
    "EXPLORER": 0xFC, "WAIT 3 SEC": 0xFE,
}

GAME_CODES = {}
for _n in range(1, 33):
    GAME_CODES["GAMEPAD %d" % _n] = 0x90 + _n - 1
for _n in range(0, 8):
    GAME_CODES["ANALOG %d" % _n] = 0xB0 + _n
# Upstream lists HAT 2 as 0xDC, which is out of sequence and lands in the
# macro range; 0xBC is the obvious reading and is what we use.
for _n in range(0, 4):
    GAME_CODES["HAT %d" % _n] = 0xBA + _n
for _n, _name in enumerate(["X1", "X2", "Y1", "Y2", "Z1", "Z2"]):
    GAME_CODES["TRACKBALL %s" % _name] = 0xC0 + _n

CODE_GROUPS = [
    ("Keyboard", KEY_CODES),
    ("Game controller", GAME_CODES),
    ("Mouse", MOUSE_CODES),
    ("System / media", SYSTEM_CODES),
]

ALL_CODES = {}
for _label, _table in CODE_GROUPS:
    ALL_CODES.update(_table)

# First name wins, so "\\" decodes to "\\" rather than "NON US \\".
CODE_NAMES = {}
for _name, _value in ALL_CODES.items():
    CODE_NAMES.setdefault(_value, _name)

NONE = ""  # an unassigned action


def code_to_name(value: int):
    """Name for a byte value, or None if we do not recognise it."""
    if value == 0:
        return NONE
    return CODE_NAMES.get(value)


def name_to_code(name) -> int:
    if name is None or name == NONE:
        return 0
    key = str(name).strip()
    if key in ALL_CODES:
        return ALL_CODES[key]
    upper = key.upper()
    if upper in ALL_CODES:
        return ALL_CODES[upper]
    raise ProtocolError("unknown action %r" % name)


class ProtocolError(Exception):
    pass


class DeviceError(Exception):
    pass


# --------------------------------------------------------------------------
# Config encode / decode - pure functions over a 256 byte buffer
# --------------------------------------------------------------------------


def write_frames(buf: bytes) -> list:
    """The 5-byte messages that carry a config to the board."""
    padded = bytes(buf[:CONFIG_SIZE]).ljust(WRITE_SIZE, b"\x00")
    return [
        bytes([REPORT_ID]) + padded[pos:pos + CHUNK]
        for pos in range(0, WRITE_SIZE, CHUNK)
    ]


def deframe(chunk: bytes) -> bytes:
    """Strip report-id prefixes from one or more concatenated HID reports.

    The board answers in 5-byte reports: [0x03, b0, b1, b2, b3]. A read that
    keeps those ids inline leaves an 0x03 every five bytes, which is how this
    bug announced itself.
    """
    size = 1 + CHUNK
    out = bytearray()
    for pos in range(0, len(chunk), size):
        report = chunk[pos:pos + size]
        if report and report[0] == REPORT_ID:
            report = report[1:]
        out += report
    return bytes(out)


def decode_config(buf: bytes) -> dict:
    """Turn a raw 256 byte board config into a profile dict."""
    if len(buf) < CONFIG_SIZE:
        raise ProtocolError(
            "config is %d bytes, expected %d" % (len(buf), CONFIG_SIZE)
        )
    data = buf[4:]
    cfg = buf[3]

    macros = _decode_macros(data)
    macro_names = {m["code"]: m["name"] for m in macros}

    pins = []
    for name in PIN_ORDER:
        ai, alt_i, shift_i = PIN_TABLE[name]
        action = data[ai]
        shift = bool(data[shift_i] & SHIFT_BIT)
        if not action and not shift:
            continue  # nothing programmed here
        # A pin can be the shift key while sending nothing itself, and that is
        # worth reporting: it is a real setting, and a form that did not show
        # it would clear it on the next write.
        pin = {"name": name, "action": _action_name(action, macro_names)}
        alt = data[alt_i]
        if alt:
            pin["alternate_action"] = _action_name(alt, macro_names)
        if shift:
            pin["shift"] = True
        pins.append(pin)

    profile = {
        "schemaVersion": 2.0,
        "resourceType": "ipac2-pins",
        "deviceClass": "ipac2",
        "debounce": _debounce_name((cfg >> 3) & 0x03),
        "paclink": bool(cfg & 0x04),
        "pins": pins,
    }
    if macros:
        profile["macros"] = [
            {"name": m["name"], "action": m["action"]} for m in macros
        ]
    # Byte 2 is the firmware version in a read response, and 0x0f in anything
    # we built ourselves. Firmware 1.44 answers [0x00, 0x00, ver, cfg] and
    # 1.55 answers [0x50, 0xdd, ver, cfg], so the header prefix is no guide.
    if buf[2] != HEADER_WRITE[2]:
        profile["firmware"] = "0.%02x" % buf[2]
    profile["raw"] = buf[:CONFIG_SIZE].hex()
    return profile


def _action_name(value: int, macro_names: dict):
    name = code_to_name(value)
    if name is not None:
        return name
    if value in macro_names:
        return macro_names[value]
    return "0x%02x" % value  # unknown: round-trips as a literal


def _debounce_name(value: int) -> str:
    for name, val in DEBOUNCE.items():
        if val == value:
            return name
    return "standard"


def _decode_macros(data: bytes) -> list:
    """Macros live at the tail of the data array.

    Each one starts with a control code in 0xe0..0xfe; the bytes after it,
    up to the next control code or a zero, are the keys it plays back.
    """
    macros = []
    i = MACRO_START
    while i < len(data):
        code = data[i]
        if MACRO_FIRST_CODE <= code <= MACRO_LAST_CODE:
            actions = []
            j = i + 1
            while j < len(data):
                nxt = data[j]
                if nxt == 0 or MACRO_FIRST_CODE <= nxt <= MACRO_LAST_CODE:
                    break
                name = code_to_name(nxt)
                actions.append(name if name is not None else "0x%02x" % nxt)
                j += 1
            macros.append(
                {
                    "name": "macro %d" % (len(macros) + 1),
                    "code": code,
                    "action": actions,
                }
            )
            i = j
        else:
            i += 1
    return macros


def encode_config(profile: dict, base: bytes) -> bytearray:
    """Apply a profile on top of the board's current config.

    Read-modify-write on purpose: bytes whose meaning we do not know - and
    on this board there are some, including whatever selects game controller
    mode - survive untouched.
    """
    buf = bytearray(base[:CONFIG_SIZE])
    if len(buf) != CONFIG_SIZE:
        raise ProtocolError("base config must be %d bytes" % CONFIG_SIZE)

    buf[0], buf[1], buf[2] = HEADER_WRITE

    cfg = buf[3]
    if "debounce" in profile:
        value = profile["debounce"]
        if value not in DEBOUNCE:
            raise ProtocolError("unknown debounce %r" % value)
        cfg = (cfg & ~0x18) | (DEBOUNCE[value] << 3)
    if "paclink" in profile:
        cfg = (cfg | 0x04) if profile["paclink"] else (cfg & ~0x04)
    buf[3] = cfg

    data = memoryview(buf)[4:]

    macro_codes = {}
    if "macros" in profile:
        macro_codes = _encode_macros(profile["macros"], data)
    else:
        for macro in _decode_macros(bytes(buf[4:])):
            macro_codes[macro["name"]] = macro["code"]

    named = {}
    for pin in profile.get("pins", []):
        name = pin.get("name")
        if name not in PIN_TABLE:
            raise ProtocolError("unknown pin %r" % name)
        named[name] = pin

    for name, (ai, alt_i, shift_i) in PIN_TABLE.items():
        pin = named.get(name)
        if pin is None:
            continue  # not mentioned: leave the board's current value alone
        # Only fields the profile actually names are touched - naming a pin to
        # change its alternate must not silently clear its action.
        if "action" in pin:
            data[ai] = _resolve(pin.get("action"), macro_codes)
        if "alternate_action" in pin:
            data[alt_i] = _resolve(pin.get("alternate_action"), macro_codes)
        if "shift" in pin:
            if pin["shift"]:
                data[shift_i] |= SHIFT_BIT
            else:
                data[shift_i] &= ~SHIFT_BIT & 0xFF
    return buf


def _resolve(action, macro_codes: dict) -> int:
    if action is None or action == NONE:
        return 0
    if action in macro_codes:
        return macro_codes[action]
    text = str(action).strip()
    if re.fullmatch(r"0x[0-9a-fA-F]{2}", text):
        return int(text, 16)
    return name_to_code(text)


def _encode_macros(macros: list, data: memoryview) -> dict:
    if len(macros) > MACRO_MAX_COUNT:
        raise ProtocolError(
            "%d macros defined, the board holds %d" % (len(macros), MACRO_MAX_COUNT)
        )
    payload = []
    codes = {}
    for i, macro in enumerate(macros):
        actions = macro.get("action") or []
        if not actions:
            continue
        code = MACRO_FIRST_CODE + i
        codes[macro.get("name", "macro %d" % (i + 1))] = code
        payload.append(code)
        for action in actions:
            payload.append(_resolve(action, {}))
    if len(payload) > MACRO_MAX_SIZE:
        raise ProtocolError(
            "macros total %d bytes, the board holds %d" % (len(payload), MACRO_MAX_SIZE)
        )
    room = len(data) - MACRO_START
    for i in range(room):
        data[MACRO_START + i] = payload[i] if i < len(payload) else 0
    return codes


def as_write_command(buf: bytes) -> bytes:
    """Put the write header on a config buffer.

    Reads come back headed [0x00, 0x00, firmware, cfg]; writes must be
    headed 0x50 0xdd 0x0f. Byte 3 (the config bitfield) is real config and is
    left alone.
    """
    out = bytearray(buf[:CONFIG_SIZE])
    out[0], out[1], out[2] = HEADER_WRITE
    return bytes(out)


def diff_config(before: bytes, after: bytes) -> list:
    """Byte level diff, annotated with what each offset controls.

    The first three bytes are command framing rather than configuration -
    they always differ between what was read and what will be written, so
    reporting them would be noise on every single apply.
    """
    out = []
    for i in range(3, min(len(before), len(after))):
        if before[i] != after[i]:
            out.append(
                {
                    "offset": i,
                    "meaning": describe_offset(i),
                    "before": before[i],
                    "after": after[i],
                }
            )
    return out


def describe_offset(offset: int) -> str:
    if offset < 4:
        return ["header type", "header 0xdd", "header 0x0f", "config bits"][offset]
    idx = offset - 4
    for name, (ai, alt_i, shift_i) in PIN_TABLE.items():
        if idx == ai:
            return "%s action" % name
        if idx == alt_i:
            return "%s alternate" % name
        if idx == shift_i:
            return "%s shift" % name
    if idx >= MACRO_START:
        return "macro area"
    return "unknown"


# --------------------------------------------------------------------------
# Device layer
# --------------------------------------------------------------------------

# hidraw ioctls, from linux/hidraw.h:
#   HIDIOCSOUTPUT(len)  = _IOWR('H', 0x0b, len)  SET_REPORT, type Output  (2)
#   HIDIOCSFEATURE(len) = _IOWR('H', 0x06, len)  SET_REPORT, type Feature (3)
#
# The board wants wValue 0x0203 - report type 2 (Output), report id 3 - which
# is HIDIOCSOUTPUT. Sending the same bytes as a Feature report (0x0303) makes
# the device STALL the control transfer, which arrives here as EPIPE.
_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction: int, type_char: str, nr: int, size: int) -> int:
    value = (direction << 30) | (size << 16) | (ord(type_char) << 8) | nr
    # fcntl.ioctl wants this as a signed int on some Python builds.
    return ctypes.c_int32(value).value


def _iowr(type_char: str, nr: int, size: int) -> int:
    return _ioc(_IOC_READ | _IOC_WRITE, type_char, nr, size)


def _iow(type_char: str, nr: int, size: int) -> int:
    return _ioc(_IOC_WRITE, type_char, nr, size)


class DeviceInfo:
    def __init__(self, path, vendor, product, bcd, interface, usb_path):
        self.path = path
        self.vendor = vendor
        self.product = product
        self.bcd = bcd
        self.interface = interface
        self.usb_path = usb_path

    @property
    def name(self):
        if self.vendor == VENDOR_2015:
            return KNOWN_2015_PRODUCTS.get(self.product, "unknown Ultimarc board")
        if self.vendor == VENDOR_PRE2015 and self.product == PRODUCT_PRE2015:
            return "pre-2015 I-PAC (unsupported)"
        return "unknown"

    @property
    def firmware(self):
        return "%d.%02x" % (self.bcd >> 8, self.bcd & 0xFF)

    @property
    def mode(self):
        """Which mode the board is in - it is encoded in the product id."""
        if self.vendor != VENDOR_2015:
            return "unknown"
        return IPAC2_MODES.get(self.product, "unknown (product %04x)" % self.product)

    @property
    def is_ipac2(self):
        return self.vendor == VENDOR_2015 and self.product in IPAC2_MODES

    def as_dict(self):
        return {
            "path": self.path,
            "vendor": "%04x" % self.vendor,
            "product": "%04x" % self.product,
            "name": self.name,
            "firmware": self.firmware,
            "firmware_note": firmware_note(self.bcd & 0xFF),
            "supports_gamepad": firmware_supports_gamepad(self.bcd & 0xFF),
            "mode": self.mode,
            "interface": self.interface,
            "usb_path": self.usb_path,
        }


def _read_sysfs(path, default=None):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return default


def find_devices(include_unsupported=False) -> list:
    """Find hidraw nodes belonging to Ultimarc boards."""
    found = []
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        hid_dir = os.path.realpath(os.path.join(node, "device"))
        iface_dir = os.path.dirname(hid_dir)
        usb_dir = os.path.dirname(iface_dir)

        vendor = _read_sysfs(os.path.join(usb_dir, "idVendor"))
        product = _read_sysfs(os.path.join(usb_dir, "idProduct"))
        if vendor is None or product is None:
            continue
        vendor, product = int(vendor, 16), int(product, 16)
        if vendor not in (VENDOR_2015, VENDOR_PRE2015):
            continue
        if not include_unsupported and not (
            vendor == VENDOR_2015 and product in IPAC2_MODES
        ):
            continue

        bcd_text = _read_sysfs(os.path.join(usb_dir, "bcdDevice"), "0000")
        try:
            bcd = int(bcd_text, 16)
        except ValueError:
            bcd = 0
        iface_text = _read_sysfs(os.path.join(iface_dir, "bInterfaceNumber"), "-1")
        try:
            interface = int(iface_text, 16)
        except ValueError:
            interface = -1

        found.append(
            DeviceInfo(
                path=os.path.join("/dev", os.path.basename(node)),
                vendor=vendor,
                product=product,
                bcd=bcd,
                interface=interface,
                usb_path=os.path.basename(usb_dir),
            )
        )
    return found


def select_device(explicit_path=None) -> DeviceInfo:
    """Pick the hidraw node that carries the config protocol."""
    if sys.platform != "linux":
        raise DeviceError(
            "talking to the board needs Linux (hidraw). Use --fake-device "
            "to work against a saved dump on this machine."
        )

    devices = find_devices(include_unsupported=True)
    if not devices:
        raise DeviceError(
            "no Ultimarc board found - no /dev/hidraw node belongs to one.\n"
            "  - `lsusb | grep -i d20` shows it?  the kernel may not have bound "
            "usbhid, or another process (a VM's USB passthrough) holds the "
            "device - check `lsusb -t` for Driver=usbhid\n"
            "  - nothing in lsusb?  it is a cable, port or power problem"
        )

    legacy = [d for d in devices if d.vendor == VENDOR_PRE2015]
    supported = [d for d in devices if d.is_ipac2]
    if not supported:
        if legacy:
            raise DeviceError(
                "this is a pre-2015 board (d208:0310). It speaks a different "
                "protocol (100 byte config, PS/2 scancodes) that this tool does "
                "not implement, and 2015+ firmware would brick it."
            )
        other = devices[0]
        raise DeviceError(
            "found %s (%04x:%04x), which shares the protocol but has a "
            "different pin layout. Only the I-PAC 2 is implemented."
            % (other.name, other.vendor, other.product)
        )

    if explicit_path:
        for dev in supported:
            if dev.path == explicit_path:
                return dev
        raise DeviceError("%s is not an I-PAC 2 config node" % explicit_path)

    return config_candidates(supported)[0]


def config_candidates(devices: list) -> list:
    """Order hidraw nodes by how likely they are to be the config interface.

    The firmware rule in Ultimarc-linux predates mode switching, and a board
    in Dinput mode presents four interfaces rather than three, so the rule is
    a starting guess and the rest get probed.
    """
    if not devices:
        return []
    wanted = config_interface_for(devices[0].bcd & 0xFF)
    return sorted(
        devices,
        key=lambda d: (d.interface != wanted, -d.interface),
    )


class Board:
    """A real board, reached through /dev/hidrawN."""

    MESSAGE_LENGTH = 1 + CHUNK  # report id + 4 config bytes

    # Output first, since that is what the board documents. Feature is kept as
    # a fallback so a stall does not need a second trip to the hardware.
    TRANSPORTS = (
        ("output report", _iowr("H", 0x0B, MESSAGE_LENGTH)),
        ("feature report", _iowr("H", 0x06, MESSAGE_LENGTH)),
    )

    def __init__(self, info: DeviceInfo, timeout=2.0):
        self.info = info
        self.timeout = timeout
        self.transport = None  # settles on whichever the board accepts
        try:
            self.fd = os.open(info.path, os.O_RDWR)
        except PermissionError:
            raise DeviceError(
                "permission denied opening %s - run as root (on Batocera you "
                "already are)" % info.path
            )
        except OSError as exc:
            raise DeviceError("cannot open %s: %s" % (info.path, exc))

    def close(self):
        if getattr(self, "fd", None) is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _send_feature(self, payload: bytes):
        import fcntl  # Linux only; imported late so the module loads anywhere

        buf = ctypes.create_string_buffer(bytes(payload), len(payload))

        candidates = (
            [self.transport] if self.transport else list(self.TRANSPORTS)
        )
        stalled = []
        for name, op in candidates:
            try:
                fcntl.ioctl(self.fd, op, buf, True)
            except OSError as exc:
                if exc.errno == errno.EPIPE:
                    # The device stalled the control transfer: it does not
                    # implement this report. Try the next kind, if any.
                    stalled.append(name)
                    continue
                raise DeviceError(
                    "%s while writing to %s: %s"
                    % (type(exc).__name__, self.info.path, exc)
                )
            if self.transport is None:
                self.transport = (name, op)
                if name != self.TRANSPORTS[0][0]:
                    print(
                        "note: board accepted a %s, not an %s"
                        % (name, self.TRANSPORTS[0][0]),
                        file=sys.stderr,
                    )
            return

        raise DeviceError(
            "%s stalled every request (tried: %s).\n"
            "That usually means this hidraw node is not the config interface. "
            "This board's config interface should be %d - check `ipacconf.py "
            "list`, then try the others explicitly:\n"
            "  for n in /dev/hidraw*; do echo \"== $n\"; %s --device $n dump "
            "| head -3; done"
            % (
                self.info.path,
                ", ".join(stalled),
                config_interface_for(self.info.bcd & 0xFF),
                os.path.basename(sys.argv[0]) or "ipacconf.py",
            )
        )

    def _send_block(self, buf: bytes):
        for frame in write_frames(buf):
            self._send_feature(frame)

    def read_config(self) -> bytes:
        """Ask the board for its config and read it back."""
        self._send_feature(bytes([REPORT_ID]) + bytes(HEADER_READ))

        out = bytearray()
        deadline = time.monotonic() + self.timeout
        while len(out) < CONFIG_SIZE:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([self.fd], [], [], remaining)
            if not ready:
                break
            data = os.read(self.fd, 512)
            if not data:
                continue
            # Every report carries its id, not just the first one.
            out += deframe(data)

        if len(out) < CONFIG_SIZE:
            raise DeviceError(
                "read %d of %d bytes from %s. If this is consistently short, "
                "the config interface may not be %d - try --device on the "
                "board's other hidraw nodes."
                % (len(out), CONFIG_SIZE, self.info.path, self.info.interface)
            )
        return bytes(out[:CONFIG_SIZE])

    def write_config(self, buf: bytes):
        if len(buf) != CONFIG_SIZE:
            raise ProtocolError("config must be %d bytes" % CONFIG_SIZE)
        self._send_block(buf)


class FakeBoard:
    """A board-shaped file, so the CLI and web UI work with no hardware."""

    def __init__(self, path):
        self.path = path
        self.info = DeviceInfo(
            path="fake:" + path,
            vendor=VENDOR_2015,
            product=PRODUCT_IPAC2,
            bcd=0x0044,
            interface=2,
            usb_path="fake",
        )
        if os.path.exists(path):
            self._buf = bytearray(load_raw(path))
            # Answer with the firmware the dump was taken from, so imports of
            # that dump do not warn about a mismatch that is not real.
            if self._buf[2] != HEADER_WRITE[2]:
                self.info.bcd = self._buf[2]
        else:
            self._buf = bytearray(default_config())
            self._flush()
        # A real board answers reads with its own header - 0x00 0x00 ver on
        # 1.44, 0x50 0xdd ver on 1.55 - whatever header the write carried.
        self._header = bytes(self._buf[:3])

    def _flush(self):
        with open(self.path, "w") as fh:
            json.dump(decode_config(bytes(self._buf)), fh, indent=2)
            fh.write("\n")

    def read_config(self) -> bytes:
        return bytes(self._buf)

    def write_config(self, buf: bytes):
        self._buf = bytearray(buf[:CONFIG_SIZE])
        self._buf[0:3] = self._header
        self._flush()

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def open_board(args):
    """Open the board, probing for whichever interface answers."""
    if getattr(args, "fake_device", None):
        return FakeBoard(args.fake_device)

    explicit = getattr(args, "device", None)
    if explicit:
        return Board(select_device(explicit))

    candidates = config_candidates([d for d in find_devices() if d.is_ipac2])
    if not candidates:
        select_device()  # raises with the right explanation
    if len(candidates) == 1:
        return Board(candidates[0])

    tried = []
    for info in candidates:
        board = Board(info, timeout=0.75)
        try:
            board.read_config()
        except (DeviceError, ProtocolError) as exc:
            board.close()
            tried.append("interface %d (%s)" % (info.interface, exc.__class__.__name__))
            continue
        board.timeout = 2.0
        return board

    raise DeviceError(
        "no interface answered a config read. Tried: %s.\n"
        "If the board is in Xinput mode the config interface is not exposed - "
        "hold P1SW1 while plugging in USB to force it back to keyboard mode."
        % ", ".join(tried)
    )


# --------------------------------------------------------------------------
# Input monitor
# --------------------------------------------------------------------------
#
# Reading the config tells you what each pin is *supposed* to send. It cannot
# tell you which physical button is wired to which pin - and that is exactly
# what has gone wrong when an action turns up on the wrong control.
#
# The board is a keyboard (or, in Dinput mode, two gamepads), so every press
# raises a Linux input event. Reverse-mapping that event through the config we
# just read names the pin. Pressing a button on the panel and pressing one
# while EmulationStation is asking for it are the same event, so a single
# monitor answers both directions of the question.
#
# We read /dev/input/event* directly rather than through python-evdev: same
# stdlib-only constraint as the rest of the tool.

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
BOARD_TO_LINUX = {}
for _keycode, _value in LINUX_TO_BOARD.items():
    BOARD_TO_LINUX.setdefault(_value, _keycode)


def parse_input_events(blob: bytes) -> list:
    """Split one read() from an event node into (sec, usec, type, code, value).

    A partial trailing record is dropped. The kernel only ever hands out whole
    events, so this is belt and braces.
    """
    size = INPUT_EVENT_SIZE
    return [
        struct.unpack(INPUT_EVENT_FORMAT, blob[start : start + size])
        for start in range(0, len(blob) - size + 1, size)
    ]


def event_action(etype: int, code: int):
    """(kind, board byte) for an evdev event; the byte is None if unmapped."""
    if etype == EV_KEY:
        if code < BTN_MOUSE:
            return "key", LINUX_TO_BOARD.get(code)
        if BTN_MOUSE <= code < BTN_MOUSE + 3:
            # BTN_LEFT/RIGHT/MIDDLE against MOUSE L/R/M.
            return "mouse", (MOUSE_CODES["MOUSE L"], MOUSE_CODES["MOUSE R"],
                             MOUSE_CODES["MOUSE M"])[code - BTN_MOUSE]
        if BTN_JOYSTICK <= code < BTN_LAST:
            # 0x120..0x13f is exactly 32 codes, against GAMEPAD 1..32. This
            # assumes hid-input numbered the board's buttons from BTN_TRIGGER,
            # which is what it does for a device that presents as a joystick.
            # Every event carries its raw code, so if a real board in Dinput
            # mode disagrees the offset is visible rather than silent.
            return "gamepad", GAME_CODES["GAMEPAD %d" % (code - BTN_JOYSTICK + 1)]
        return "button", None
    if etype == EV_ABS:
        if ABS_HAT0X <= code < ABS_HAT0X + 4:
            return "hat", GAME_CODES["HAT %d" % (code - ABS_HAT0X)]
        if code < 8:
            return "analog", GAME_CODES["ANALOG %d" % code]
        return "axis", None
    if etype == EV_REL:
        if code < 2:
            return "trackball", GAME_CODES["TRACKBALL %s" % ("X1", "Y1")[code - REL_X]]
        return "relative", None
    return "other", None


def pins_for_action(profile, name, player=None) -> list:
    """Every pin whose action or alternate action is `name`.

    More than one pin can carry the same code, in which case they are all
    returned - an ambiguous answer still narrows the search, and saying so is
    better than picking one at random.
    """
    if not profile or not name:
        return []
    hits = []
    for pin in profile.get("pins") or []:
        for field in ("action", "alternate_action"):
            if pin.get(field) != name:
                continue
            hits.append({"pin": pin.get("name"), "field": field})
            break  # a pin whose alternate repeats its action is still one pin
    if player and len(hits) > 1:
        # In Dinput mode both players' buttons share the GAMEPAD 1..32 code
        # space, so the code alone cannot say who pressed it. Which event node
        # it arrived on can.
        narrowed = [h for h in hits if str(h["pin"] or "").startswith(str(player))]
        if narrowed:
            return narrowed
    return hits


class InputDevice:
    """One /dev/input/eventN node belonging to a board we care about."""

    def __init__(self, path, name, vendor, product, interface, joystick):
        self.path = path
        self.name = name
        self.vendor = vendor
        self.product = product
        self.interface = interface
        self.joystick = joystick
        self.player = None  # filled in for joystick nodes, in interface order

    @property
    def node(self):
        return os.path.basename(self.path)

    def as_dict(self):
        return {
            "path": self.path,
            "node": self.node,
            "name": self.name,
            "interface": self.interface,
            "player": self.player,
        }


def _ancestor_with(path: str, filename: str, limit: int = 8):
    """Walk up from `path` for a directory holding `filename`."""
    current = path
    for _ in range(limit):
        parent = os.path.dirname(current)
        if not parent or parent == current or parent == "/":
            return None
        current = parent
        if os.path.exists(os.path.join(current, filename)):
            return current
    return None


def find_input_devices(all_devices=False, sys_root="/sys") -> list:
    """Event nodes for the board, or for everything if all_devices."""
    found = []
    pattern = os.path.join(sys_root, "class", "input", "event*")
    for node in sorted(glob.glob(pattern), key=lambda p: _node_index(p)):
        dev_dir = os.path.realpath(os.path.join(node, "device"))
        vendor = _read_sysfs(os.path.join(dev_dir, "id", "vendor"))
        product = _read_sysfs(os.path.join(dev_dir, "id", "product"))
        if vendor is None or product is None:
            continue
        try:
            vendor, product = int(vendor, 16), int(product, 16)
        except ValueError:
            continue
        ours = vendor == VENDOR_2015 and product in IPAC2_MODES
        if not ours and not all_devices:
            continue

        iface_dir = _ancestor_with(dev_dir, "bInterfaceNumber")
        interface = -1
        if iface_dir:
            try:
                interface = int(_read_sysfs(
                    os.path.join(iface_dir, "bInterfaceNumber"), "-1"), 16)
            except ValueError:
                interface = -1

        # A node with absolute axes is a stick or pad rather than the
        # keyboard, which is what tells the two Dinput players apart.
        abs_caps = _read_sysfs(os.path.join(dev_dir, "capabilities", "abs"), "0")
        joystick = any(int(word, 16) for word in (abs_caps or "0").split() if word)

        found.append(
            InputDevice(
                path=os.path.join("/dev", "input", os.path.basename(node)),
                name=_read_sysfs(os.path.join(dev_dir, "name"), "unknown device"),
                vendor=vendor,
                product=product,
                interface=interface,
                joystick=joystick,
            )
        )

    pads = sorted([d for d in found if d.joystick and d.vendor == VENDOR_2015],
                  key=lambda d: (d.interface, d.path))
    for index, dev in enumerate(pads):
        dev.player = index + 1
    return found


def _node_index(path: str) -> int:
    match = re.search(r"(\d+)$", path)
    return int(match.group(1)) if match else 0


EVENT_BUFFER = 200


class EventStream:
    """Ring buffer plus subscriber fan-out. No I/O, so tests can drive it."""

    def __init__(self, size=EVENT_BUFFER):
        self.size = size
        self._lock = threading.Lock()
        self._events = []
        self._seq = 0
        self._subscribers = set()

    def publish(self, event: dict) -> dict:
        with self._lock:
            self._seq += 1
            event = dict(event, seq=self._seq)
            self._events.append(event)
            if len(self._events) > self.size:
                del self._events[: -self.size]
            subscribers = list(self._subscribers)
        for sub in subscribers:
            try:
                sub.put_nowait(event)
            except queue.Full:
                pass  # a stalled reader loses events; the board never waits
        return event

    def since(self, seq: int) -> list:
        with self._lock:
            return [e for e in self._events if e["seq"] > seq]

    @property
    def latest(self) -> int:
        with self._lock:
            return self._seq

    def subscribe(self, maxsize=256) -> queue.Queue:
        sub = queue.Queue(maxsize)
        with self._lock:
            self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub) -> None:
        with self._lock:
            self._subscribers.discard(sub)


class BaseMonitor:
    """Shared translation and lifecycle. Subclasses provide the events."""

    def __init__(self, devices, stream=None, profile=None):
        self.devices = list(devices)
        self.stream = stream or EventStream()
        self.profile = profile
        self.error = None
        self._rest = {}  # (node, axis) -> the value that counts as "not held"
        self._held = {}  # (node, axis) -> whether it is away from rest
        self._muted = set()  # (node, type) already reported as unreadable
        self._thread = None
        self._stop = threading.Event()

    # -- lifecycle

    def start(self):
        self._open()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def close(self):
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    def _open(self):
        pass

    def _close(self):
        pass

    def _run(self):
        raise NotImplementedError("a monitor subclass provides the events")

    # -- translation

    def translate(self, device, etype, code, value):
        """One evdev event as a payload dict, or None if not worth reporting."""
        if etype == EV_SYN:
            return None
        if etype == EV_KEY and value == 2:
            return None  # autorepeat, which would flood a held button

        kind, board_code = event_action(etype, code)

        muted = False
        if kind == "other":
            # An event type we have no reading of - in practice the EV_MSC scan
            # code the kernel raises alongside every single key event, so
            # reporting each one doubles the log and buries the presses. Say so
            # once per node and type, then drop the rest. This lives on the
            # monitor, so it is once per watching session rather than per
            # subscriber: a browser joining a stream already running sees no
            # such line.
            key = (device.node, etype)
            if key in self._muted:
                return None
            self._muted.add(key)
            muted = True

        if etype == EV_ABS:
            # Axes have no press/release; the value they sit at when nothing is
            # touched counts as released. Taking the first value seen as that
            # resting point works for sticks centred at 0 and at 128 alike.
            key = (device.node, code)
            rest = self._rest.setdefault(key, value)
            held = value != rest
            if held == self._held.get(key, False):
                return None  # jitter, or the axis settling back
            self._held[key] = held
        else:
            held = value != 0

        name = code_to_name(board_code) if board_code is not None else None
        player = device.player if kind in ("gamepad", "hat", "analog") else None
        return {
            "ts": time.time(),
            "device": device.path,
            "node": device.node,
            "source": device.name,
            "player": player,
            "kind": kind,
            "raw": code,
            "type": etype,
            "value": value,
            "held": held,
            "name": name,
            "code": board_code,
            "muted": muted,
            "pins": pins_for_action(self.profile, name, player),
        }

    def _emit(self, device, etype, code, value):
        event = self.translate(device, etype, code, value)
        if event is not None:
            self.stream.publish(event)


class InputMonitor(BaseMonitor):
    """Reads the board's evdev nodes in a background thread."""

    POLL = 0.25  # how often the loop notices it has been asked to stop
    BATCH = 64  # events per read()

    def __init__(self, devices, grab=False, stream=None, profile=None):
        super().__init__(devices, stream=stream, profile=profile)
        self.grab = grab
        self._fds = {}

    def _open(self):
        import fcntl  # Linux only; imported late so the module loads anywhere

        if not self.devices:
            raise DeviceError(
                "no input devices to watch - the board is attached (the "
                "config read works) but no /dev/input/event node belongs to "
                "it. Check `ls /dev/input/by-id | grep -i ultimarc`."
            )
        for dev in self.devices:
            try:
                fd = os.open(dev.path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError as exc:
                self._close()
                raise DeviceError("cannot open %s: %s" % (dev.path, exc))
            self._fds[fd] = dev
            if not self.grab:
                continue
            try:
                fcntl.ioctl(fd, _iow("E", EVIOCGRAB_NR, 4), 1)
            except OSError as exc:
                self._close()
                raise DeviceError(
                    "cannot take exclusive control of %s: %s. Something else "
                    "already holds it - stop the other reader, or watch "
                    "without exclusive capture." % (dev.path, exc)
                )

    def _close(self):
        for fd in list(self._fds):
            # Closing the fd is what releases any grab, so there is nothing
            # else to undo here.
            try:
                os.close(fd)
            except OSError:
                pass
            del self._fds[fd]

    def _run(self):
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select(list(self._fds), [], [], self.POLL)
            except (OSError, ValueError):
                return  # the fds went away under us, which means close()
            for fd in ready:
                device = self._fds.get(fd)
                if device is None:
                    continue
                try:
                    blob = os.read(fd, INPUT_EVENT_SIZE * self.BATCH)
                except OSError as exc:
                    if exc.errno in (errno.EAGAIN, errno.EINTR):
                        continue
                    self.error = "%s: %s" % (device.path, exc)
                    return
                for _sec, _usec, etype, code, value in parse_input_events(blob):
                    self._emit(device, etype, code, value)


class FakeInputMonitor(BaseMonitor):
    """Replays a JSONL script, so the UI can be built without a cabinet.

    Each line is one event. Either name a board action, which is turned back
    into the keycode the kernel would have reported so the whole translation
    path is exercised:

        {"after": 0.4, "action": "5", "value": 1}

    or give the raw evdev numbers directly:

        {"after": 0.1, "type": 3, "code": 16, "value": -1}
    """

    def __init__(self, path, stream=None, profile=None, loop=True):
        self.path = path
        self.loop = loop
        super().__init__([_fake_device(path)], stream=stream, profile=profile)
        self.script = self._load()

    def _load(self):
        steps = []
        with open(self.path) as handle:
            for number, line in enumerate(handle, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    step = json.loads(line)
                except ValueError as exc:
                    raise ProtocolError(
                        "%s line %d: %s" % (self.path, number, exc)
                    )
                if "action" in step:
                    code = BOARD_TO_LINUX.get(name_to_code(step["action"]))
                    if code is None:
                        raise ProtocolError(
                            "%s line %d: %r is not something a keyboard can "
                            "send" % (self.path, number, step["action"])
                        )
                    step.setdefault("type", EV_KEY)
                    step["code"] = code
                steps.append(step)
        if not steps:
            raise ProtocolError("%s has no events in it" % self.path)
        return steps

    def _run(self):
        while not self._stop.is_set():
            for step in self.script:
                if self._stop.wait(float(step.get("after", 0.5))):
                    return
                self._emit(
                    self.devices[0],
                    int(step.get("type", EV_KEY)),
                    int(step["code"]),
                    int(step.get("value", 1)),
                )
            if not self.loop:
                return


def _fake_device(path) -> InputDevice:
    device = InputDevice(
        path=path, name="scripted input (%s)" % os.path.basename(path),
        vendor=VENDOR_2015, product=PRODUCT_IPAC2, interface=0, joystick=False,
    )
    device.player = 1
    return device


def open_monitor(args, profile=None, stream=None):
    """The monitor the CLI and the web UI both want."""
    fake = getattr(args, "fake_input", None)
    if fake:
        return FakeInputMonitor(fake, stream=stream, profile=profile)
    if sys.platform != "linux":
        raise DeviceError(
            "watching the panel needs Linux (/dev/input). Use --fake-input "
            "with a script to work on this machine."
        )
    devices = find_input_devices(all_devices=getattr(args, "all_devices", False))
    return InputMonitor(
        devices, grab=getattr(args, "grab", False), stream=stream, profile=profile
    )


def sse_frame(payload, name=None) -> bytes:
    """One server-sent event. Kept pure so the framing can be tested."""
    head = "event: %s\n" % name if name else ""
    return ("%sdata: %s\n\n" % (head, json.dumps(payload))).encode()


# --------------------------------------------------------------------------
# Profiles on disk
# --------------------------------------------------------------------------


def load_profile(path) -> dict:
    with open(path) as fh:
        profile = json.load(fh)
    if not isinstance(profile, dict):
        raise ProtocolError("%s is not a profile object" % path)
    return profile


def raw_from_profile(profile: dict, origin: str) -> bytes:
    """Get the 256 raw bytes out of a dump, if it has them."""
    raw = profile.get("raw")
    if not raw:
        raise ProtocolError(
            "%s has no 'raw' field - it is an edited profile, not a dump. "
            "Use `apply` rather than `restore`." % origin
        )
    try:
        buf = bytes.fromhex(raw)
    except (TypeError, ValueError):
        raise ProtocolError("%s has a 'raw' field that is not hex" % origin)
    if len(buf) != CONFIG_SIZE:
        raise ProtocolError(
            "%s holds %d bytes, expected %d" % (origin, len(buf), CONFIG_SIZE)
        )
    return buf


def load_raw(path) -> bytes:
    return raw_from_profile(load_profile(path), path)


def default_config() -> bytes:
    """A plausible MAME-style keyboard config, for the fake device."""
    buf = bytearray(CONFIG_SIZE)
    buf[0], buf[1], buf[2] = HEADER_WRITE
    return bytes(encode_config(MAME_KEYBOARD, bytes(buf)))


MAME_KEYBOARD = {
    "debounce": "standard",
    "paclink": False,
    "pins": [
        {"name": "1up", "action": "UP"},
        {"name": "1down", "action": "DOWN"},
        {"name": "1left", "action": "LEFT"},
        {"name": "1right", "action": "RIGHT"},
        {"name": "1sw1", "action": "CTRL L"},
        {"name": "1sw2", "action": "ALT L"},
        {"name": "1sw3", "action": "SPACE"},
        {"name": "1sw4", "action": "SHIFT L"},
        {"name": "1sw5", "action": "Z"},
        {"name": "1sw6", "action": "X"},
        {"name": "1sw7", "action": "C"},
        {"name": "1sw8", "action": "V"},
        {"name": "2up", "action": "R"},
        {"name": "2down", "action": "F"},
        {"name": "2left", "action": "D"},
        {"name": "2right", "action": "G"},
        {"name": "2sw1", "action": "A"},
        {"name": "2sw2", "action": "S"},
        {"name": "2sw3", "action": "Q"},
        {"name": "2sw4", "action": "W"},
        {"name": "2sw5", "action": "I"},
        {"name": "2sw6", "action": "K"},
        {"name": "2sw7", "action": "J"},
        {"name": "2sw8", "action": "L"},
        {"name": "1start", "action": "1", "shift": True, "alternate_action": ""},
        {"name": "2start", "action": "2"},
        {"name": "1coin", "action": "5", "alternate_action": "ESC"},
        {"name": "2coin", "action": "6"},
        {"name": "1a", "action": "3"},
        {"name": "1b", "action": "4"},
        {"name": "2a", "action": "7"},
        {"name": "2b", "action": "8"},
    ],
}


def backup_dir(explicit=None) -> str:
    if explicit:
        return explicit
    if os.path.isdir("/userdata/system"):
        return "/userdata/system/ipac-backups"
    return os.path.join(os.path.expanduser("~"), ".ipac-backups")


def write_backup(profile: dict, directory: str) -> str:
    os.makedirs(directory, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(directory, "ipac2-%s.json" % stamp)
    # Restoring a backup takes a backup, and the two land in the same second.
    # Without this the second one overwrites the file being restored from.
    suffix = 2
    while os.path.exists(path):
        path = os.path.join(directory, "ipac2-%s-%d.json" % (stamp, suffix))
        suffix += 1
    with open(path, "w") as fh:
        json.dump(profile, fh, indent=2)
        fh.write("\n")
    return path


# --------------------------------------------------------------------------
# Saved configurations
#
# Two directories are browsable: the backup directory, which we write to and
# which the UI may relabel or delete from, and the profiles shipped alongside
# this script, which are read only.
# --------------------------------------------------------------------------

PRESET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles")
LABEL_MAX = 60
SAVED_LIMIT = 200


def saved_dirs(args=None) -> list:
    """The directories the UI browses, newest-first source order."""
    candidates = [
        {
            "source": "backups",
            "path": backup_dir(getattr(args, "backup_dir", None)),
            "writable": True,
        },
        {"source": "presets", "path": PRESET_DIR, "writable": False},
    ]
    return [d for d in candidates if os.path.isdir(d["path"])]


def profile_firmware(profile: dict) -> str:
    """Which firmware a saved config came off.

    Dumps taken before firmware detection was fixed carry no 'firmware' key,
    but their raw bytes still hold it in byte 2 - read it back out rather than
    reporting those files as coming from nowhere.
    """
    firmware = profile.get("firmware")
    if firmware:
        return str(firmware)
    try:
        buf = bytes.fromhex(profile.get("raw") or "")
    except (TypeError, ValueError):
        return ""
    if len(buf) < 4 or buf[2] == HEADER_WRITE[2]:
        return ""
    return "0.%02x" % buf[2]


def _saved_entry(directory: dict, name: str) -> dict:
    path = os.path.join(directory["path"], name)
    entry = {
        "id": "%s/%s" % (directory["source"], name),
        "source": directory["source"],
        "name": name,
        "writable": directory["writable"],
        "mtime": 0.0,
        "size": 0,
    }
    try:
        stat = os.stat(path)
        entry["mtime"] = stat.st_mtime
        entry["size"] = stat.st_size
        profile = load_profile(path)
    except (OSError, ValueError, ProtocolError) as exc:
        # One unreadable file must not take the whole listing down with it.
        entry["error"] = str(exc)
        return entry
    entry["label"] = str(profile.get("label") or "")
    entry["firmware"] = profile_firmware(profile)
    entry["pins"] = len(profile.get("pins") or [])
    entry["macros"] = len(profile.get("macros") or [])
    entry["has_raw"] = bool(profile.get("raw"))
    return entry


def list_saved(dirs, limit=SAVED_LIMIT) -> list:
    """Every .json in the given directories, newest first."""
    entries = []
    for directory in dirs:
        try:
            names = sorted(os.listdir(directory["path"]))
        except OSError:
            continue
        for name in names:
            if name.startswith(".") or not name.endswith(".json"):
                continue
            if not os.path.isfile(os.path.join(directory["path"], name)):
                continue
            entries.append(_saved_entry(directory, name))
    entries.sort(key=lambda e: (e["mtime"], e["name"]), reverse=True)
    return entries[:limit]


def resolve_saved(dirs, ident) -> tuple:
    """Turn a "source/name" id from a request into a path we are willing to open.

    This is the security boundary. `serve` binds 0.0.0.0 by default, so `ident`
    is untrusted input off the LAN; nothing else in the server builds a path
    out of request data.
    """
    source, _, name = str(ident).partition("/")
    if not name:
        raise ProtocolError("bad saved id %r - expected source/name.json" % ident)
    if "/" in name or "\\" in name or name.startswith("."):
        raise ProtocolError("bad saved name %r" % name)
    if not name.endswith(".json"):
        raise ProtocolError("%r is not a .json file" % name)
    for directory in dirs:
        if directory["source"] != source:
            continue
        root = os.path.realpath(directory["path"])
        path = os.path.realpath(os.path.join(root, name))
        # Catches a symlink pointing out of the directory, which the name
        # checks above cannot see.
        if not path.startswith(root + os.sep):
            raise ProtocolError("%s is outside the %s directory" % (name, source))
        if not os.path.isfile(path):
            raise ProtocolError("no such saved config: %s" % ident)
        return directory, path
    raise ProtocolError("unknown source %r" % source)


def set_label(path: str, label) -> dict:
    """Name a saved config. The label lives in the file, so a copy keeps it."""
    profile = load_profile(path)
    label = " ".join(str(label or "").split())[:LABEL_MAX]
    if label:
        profile["label"] = label
    else:
        profile.pop("label", None)
    stat = os.stat(path)
    with open(path, "w") as fh:
        json.dump(profile, fh, indent=2)
        fh.write("\n")
    # Naming a backup is not the same as saving it again: keep the timestamp,
    # so relabelling does not shuffle it to the top of the list.
    os.utime(path, (stat.st_atime, stat.st_mtime))
    return profile


def import_notes(profile: dict, info=None) -> list:
    """What the user should know before writing this file to the board."""
    notes = []
    firmware = profile_firmware(profile)
    if firmware and info is not None and firmware != info.firmware:
        notes.append(
            "Saved from firmware %s, but the board is running %s. The bytes "
            "will still be written - check the diff before trusting them."
            % (firmware, info.firmware)
        )
    if not profile.get("raw"):
        notes.append(
            "No raw bytes in this file, so it can be loaded into the form but "
            "not restored byte for byte."
        )
    named = len(profile.get("pins") or [])
    if named < len(PIN_ORDER):
        notes.append(
            "Names %d of the %d pins - the rest keep whatever the board "
            "already has." % (named, len(PIN_ORDER))
        )
    if profile.get("macros"):
        notes.append(
            "Holds %d macro(s), which the pin form cannot show. Use "
            "'Restore exactly' to keep them." % len(profile["macros"])
        )
    warning = _gamepad_warning(profile, info) if info is not None else None
    if warning:
        notes.append(" ".join(warning.split()))
    return notes


def merge_profile(base: dict, incoming: dict) -> dict:
    """Overlay one profile's pins onto another, matching `apply` semantics.

    A profile may name a single pin; pins it does not name keep what they
    already had, exactly as the CLI's read-modify-write does.
    """
    merged = dict(base)
    by_name = {}
    order = []
    for pin in base.get("pins") or []:
        by_name[pin["name"]] = dict(pin)
        order.append(pin["name"])
    for pin in incoming.get("pins") or []:
        name = pin.get("name")
        if not name:
            continue
        if name not in by_name:
            by_name[name] = {"name": name}
            order.append(name)
        by_name[name].update({k: v for k, v in pin.items() if k != "name"})
    merged["pins"] = [by_name[name] for name in order]
    for key in ("debounce", "paclink"):
        if key in incoming:
            merged[key] = incoming[key]
    # The incoming file's raw bytes describe the incoming file, not the
    # merge, and its firmware is the board it came off. Neither survives.
    merged.pop("raw", None)
    merged.pop("firmware", None)
    merged.pop("label", None)
    return merged


PIN_FIELDS = ("action", "alternate_action", "shift")


def profile_changes(before: dict, after: dict) -> list:
    """Which pin fields differ, so the UI can highlight what an import touched."""
    def index(profile):
        return {p["name"]: p for p in (profile.get("pins") or []) if p.get("name")}

    old, new = index(before), index(after)
    out = []
    for name in new:
        for field in PIN_FIELDS:
            was = old.get(name, {}).get(field, "")
            now = new[name].get(field, "")
            if field == "shift":
                was, now = bool(was), bool(now)
            if was != now:
                out.append(
                    {"pin": name, "field": field, "before": was, "after": now}
                )
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def cmd_list(args) -> int:
    if getattr(args, "fake_device", None):
        board = FakeBoard(args.fake_device)
        print("fake device backed by %s" % args.fake_device)
        _print_device(board.info)
        return 0

    if sys.platform != "linux":
        print("Device access needs Linux. On this machine use --fake-device.", file=sys.stderr)
        return 2

    devices = find_devices(include_unsupported=True)
    if not devices:
        print("No Ultimarc board found.")
        print("Check `lsusb | grep -i d20`; reading /dev/hidraw* needs root.")
        return 1

    for dev in devices:
        _print_device(dev)
        print()

    try:
        chosen = select_device()
    except DeviceError as exc:
        print("Config node: %s" % exc, file=sys.stderr)
        return 1
    print("Config node: %s (interface %d)" % (chosen.path, chosen.interface))
    return 0


def _print_device(dev: DeviceInfo):
    print("%s  %04x:%04x  %s" % (dev.path, dev.vendor, dev.product, dev.name))
    print("  mode       %s" % dev.mode)
    print("  firmware   %s  (%s)" % (dev.firmware, firmware_note(dev.bcd & 0xFF)))
    print("  interface  %d" % dev.interface)
    print("  gamepad    %s" % ("yes" if firmware_supports_gamepad(dev.bcd & 0xFF) else "no"))


def cmd_dump(args) -> int:
    with open_board(args) as board:
        raw = board.read_config()
    profile = decode_config(raw)

    if args.raw:
        with open(args.raw, "wb") as fh:
            fh.write(raw)
        print("wrote %d raw bytes to %s" % (len(raw), args.raw), file=sys.stderr)

    text = json.dumps(profile, indent=2)
    if args.output:
        with open(args.output, "w") as fh:
            fh.write(text + "\n")
        print("wrote %s" % args.output, file=sys.stderr)
    else:
        print(text)
    return 0


def cmd_apply(args) -> int:
    profile = load_profile(args.profile)
    with open_board(args) as board:
        current = board.read_config()
        updated = bytes(encode_config(profile, current))
        changes = diff_config(current, updated)

        warning = _gamepad_warning(profile, board.info)
        if warning:
            print(warning, file=sys.stderr)

        if not changes:
            print("no change - the board already matches %s" % args.profile)
            return 0

        print("%d byte%s would change:" % (len(changes), "" if len(changes) == 1 else "s"))
        for change in changes:
            print(
                "  [%3d] %-18s %s -> %s"
                % (
                    change["offset"],
                    change["meaning"],
                    _fmt_byte(change["before"]),
                    _fmt_byte(change["after"]),
                )
            )

        if args.dry_run:
            print("\ndry run - nothing written")
            return 0

        if not args.no_backup:
            path = write_backup(decode_config(current), backup_dir(args.backup_dir))
            print("backed up current config to %s" % path)

        board.write_config(updated)
        print(
            "wrote %d bytes in %d messages to %s"
            % (WRITE_SIZE, WRITE_SIZE // CHUNK, board.info.path)
        )

    return 0


def _fmt_byte(value: int) -> str:
    name = code_to_name(value)
    if name == NONE:
        return "0x00 (none)"
    if name:
        return "0x%02x (%s)" % (value, name)
    return "0x%02x" % value


def _gamepad_warning(profile: dict, info: DeviceInfo):
    uses_gamepad = any(
        str(pin.get(field, "")).upper().startswith(("GAMEPAD", "HAT", "ANALOG"))
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


def cmd_restore(args) -> int:
    profile = load_profile(args.backup)
    raw = as_write_command(raw_from_profile(profile, args.backup))
    with open_board(args) as board:
        for note in import_notes(profile, board.info):
            print("note: %s" % note, file=sys.stderr)
        current = board.read_config()
        changes = diff_config(current, raw)
        if not changes:
            print("no change - the board already matches %s" % args.backup)
            return 0
        print("restoring %d byte%s" % (len(changes), "" if len(changes) == 1 else "s"))
        if args.dry_run:
            for change in changes:
                print("  [%3d] %-18s %s -> %s" % (
                    change["offset"], change["meaning"],
                    _fmt_byte(change["before"]), _fmt_byte(change["after"])))
            print("\ndry run - nothing written")
            return 0
        if not args.no_backup:
            path = write_backup(decode_config(current), backup_dir(args.backup_dir))
            print("backed up current config to %s" % path)
        board.write_config(raw)
        print("restored %s" % args.backup)
    return 0


def cmd_saved(args) -> int:
    """List what the web UI's file browser lists, for use over SSH."""
    dirs = saved_dirs(args)
    if not dirs:
        print("no saved configurations yet", file=sys.stderr)
        return 0
    for directory in dirs:
        print("%s  %s" % (directory["source"], directory["path"]))
    print()
    for entry in list_saved(dirs):
        when = datetime.datetime.fromtimestamp(entry["mtime"]).strftime(
            "%Y-%m-%d %H:%M"
        )
        if entry.get("error"):
            print("  %-8s %-34s unreadable: %s"
                  % (entry["source"], entry["name"], entry["error"]))
            continue
        bits = ["%2d pin%s" % (entry["pins"], "" if entry["pins"] == 1 else "s")]
        if entry["macros"]:
            bits.append("%d macros" % entry["macros"])
        if entry["firmware"]:
            bits.append("fw %s" % entry["firmware"])
        bits.append("restorable" if entry["has_raw"] else "form only")
        print("  %-8s %-34s %s  %s" % (entry["source"], entry["name"], when,
                                       ", ".join(bits)))
        if entry["label"]:
            print("  %-8s %s" % ("", entry["label"]))
    return 0


def read_profile_quietly(args):
    """The board's current config, or None with a note on stderr.

    The monitor is still useful without it - it just reports raw codes rather
    than naming pins - so a board that will not answer is not fatal here.
    """
    try:
        with open_board(args) as board:
            return decode_config(board.read_config())
    except (DeviceError, ProtocolError) as exc:
        print(
            "cannot read the board's config, so presses will not be matched "
            "to pins: %s" % exc,
            file=sys.stderr,
        )
        return None


def monitor_line(event: dict) -> str:
    """One press as a line of terminal output."""
    when = datetime.datetime.fromtimestamp(event["ts"]).strftime("%H:%M:%S")
    what = event["name"] or "%s %d" % (event["kind"], event["raw"])
    if event["code"] is not None:
        what += " (0x%02x)" % event["code"]
    if event["pins"]:
        where = " ".join(
            pin["pin"] if pin["field"] == "action" else "%s (shifted)" % pin["pin"]
            for pin in event["pins"]
        )
        if len(event["pins"]) > 1:
            where += "  <- several pins carry this code"
    elif event["name"]:
        where = "-- no pin carries this code"
    elif event.get("muted"):
        where = "-- not an action the board can store; hiding the rest of these"
    else:
        where = "-- not an action the board can store"
    return "%s  %-4s %-22s %-30s %s" % (
        when, "down" if event["held"] else "up", what, where, event["node"]
    )


def cmd_monitor(args) -> int:
    profile = read_profile_quietly(args)
    try:
        monitor = open_monitor(args, profile=profile)
        monitor.start()
    except (DeviceError, ProtocolError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    for device in monitor.devices:
        print("watching %s  %s%s" % (
            device.path, device.name,
            " (player %d)" % device.player if device.player else "",
        ))
    if getattr(args, "grab", False):
        print("exclusive capture is on - presses will NOT reach Batocera")
    print("press a control on the panel; ctrl-c to stop")
    print()

    stream = monitor.stream.subscribe()
    try:
        while True:
            try:
                print(monitor_line(stream.get(timeout=0.5)))
            except queue.Empty:
                if monitor.error:
                    print("error: %s" % monitor.error, file=sys.stderr)
                    return 1
    except KeyboardInterrupt:
        print()
    finally:
        monitor.stream.unsubscribe(stream)
        monitor.close()
    return 0


def cmd_serve(args) -> int:
    return serve(args)


def _add_input_args(parser):
    """Options for the input monitor, shared by `monitor` and `serve`."""
    parser.add_argument(
        "--fake-input",
        metavar="FILE",
        help="replay a JSONL script instead of reading /dev/input "
             "(for development off the cabinet)",
    )
    parser.add_argument(
        "--all-devices",
        action="store_true",
        help="watch every input device, not just the board - use this to "
             "prove a press came from some other controller",
    )
    parser.add_argument(
        "--grab",
        action="store_true",
        help="take exclusive control, so presses do not also reach "
             "EmulationStation while you test them",
    )


def _add_device_args(parser, suppress=False):
    """Device selection options.

    On subparsers these default to SUPPRESS so that omitting them does not
    overwrite a value already given before the subcommand.
    """
    extra = {"default": argparse.SUPPRESS} if suppress else {}
    parser.add_argument(
        "--device", help="hidraw node to use instead of auto-detection", **extra
    )
    parser.add_argument(
        "--fake-device",
        metavar="FILE",
        help="work against a saved dump instead of hardware (for development)",
        **extra
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ipacconf",
        description="Configure an Ultimarc I-PAC 2 (2015+) from Linux.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    _add_device_args(parser)

    # The same two options are accepted after the subcommand as well, since
    # `serve --fake-device x` is the order everyone reaches for first.
    common = argparse.ArgumentParser(add_help=False)
    _add_device_args(common, suppress=True)

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="show attached Ultimarc boards", parents=[common])
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("dump", help="read the board's config", parents=[common])
    p.add_argument("-o", "--output", help="write JSON here instead of stdout")
    p.add_argument("--raw", help="also write the raw 256 bytes here")
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("apply", help="write a profile to the board", parents=[common])
    p.add_argument("profile")
    p.add_argument("--dry-run", action="store_true", help="show the diff, write nothing")
    p.add_argument("--no-backup", action="store_true")
    p.add_argument("--backup-dir")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("restore", help="write a dump back byte for byte", parents=[common])
    p.add_argument("backup")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    p.add_argument("--backup-dir")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("saved", help="list saved configs and presets", parents=[common])
    p.add_argument("--backup-dir")
    p.set_defaults(func=cmd_saved)

    p = sub.add_parser(
        "monitor",
        help="name the pin behind each button press",
        parents=[common],
        description="Watch the board's input events and say which pin each "
                    "press came from. This is how you find an action that "
                    "has been assigned to the wrong pin.",
    )
    _add_input_args(p)
    p.set_defaults(func=cmd_monitor)

    p = sub.add_parser("serve", help="run the web UI", parents=[common])
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--backup-dir")
    _add_input_args(p)
    p.set_defaults(func=cmd_serve)

    return parser


# --------------------------------------------------------------------------
# Web UI
# --------------------------------------------------------------------------


def serve(args) -> int:
    import http.server
    import socketserver

    handler = _make_handler(args)

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with Server((args.host, args.port), handler) as httpd:
        where = args.host if args.host != "0.0.0.0" else _lan_address()
        print("ipacconf %s" % __version__)
        print("open http://%s:%d/" % (where, args.port))
        if getattr(args, "fake_device", None):
            print("(fake device: %s)" % args.fake_device)
        if getattr(args, "fake_input", None):
            print("(fake input: %s)" % args.fake_input)
        print("ctrl-c to stop")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print()
    return 0


def _lan_address() -> str:
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))  # no packets are sent
        return sock.getsockname()[0]
    except OSError:
        return "localhost"
    finally:
        sock.close()


SSE_HEARTBEAT = 10.0  # seconds between keepalives on an idle stream


class MonitorHolder:
    """One input monitor, shared by however many browsers are watching.

    Reference counted, so the last tab to close is what releases an exclusive
    grab. A stream that dies with the tab is only noticed on the next
    heartbeat write, so that release can lag by up to SSE_HEARTBEAT.
    """

    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.monitor = None
        self.options = None
        self.users = 0

    def acquire(self, grab, all_devices):
        wanted = (bool(grab), bool(all_devices))
        with self.lock:
            if self.monitor is not None and self.options != wanted:
                if self.users:
                    raise DeviceError(
                        "another browser is already watching with different "
                        "options. Stop watching there first, or match its "
                        "settings."
                    )
                self._stop()
            if self.monitor is None:
                options = argparse.Namespace(**vars(self.args))
                options.grab, options.all_devices = wanted
                monitor = open_monitor(options, profile=self._profile())
                monitor.start()
                self.monitor, self.options = monitor, wanted
            self.users += 1
            return self.monitor

    def release(self):
        with self.lock:
            self.users = max(0, self.users - 1)
            if not self.users:
                self._stop()

    def refresh(self, profile):
        """Point the monitor at a config that has just been written."""
        with self.lock:
            if self.monitor is not None:
                self.monitor.profile = profile

    def _profile(self):
        try:
            with open_board(self.args) as board:
                return decode_config(board.read_config())
        except (DeviceError, ProtocolError):
            # Worth watching anyway: raw codes still say *something* arrived,
            # which separates "wrong pin" from "nothing is getting through".
            return None

    def _stop(self):
        monitor, self.monitor, self.options = self.monitor, None, None
        if monitor is not None:
            monitor.close()


def _make_handler(args):
    import http.server
    import urllib.parse

    monitors = MonitorHolder(args)

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "ipacconf/" + __version__

        def log_message(self, fmt, *fmt_args):
            sys.stderr.write("  %s\n" % (fmt % fmt_args))

        # -- helpers

        def _json(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, text):
            body = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length))

        def _route(self):
            """The request path, unescaped, with the query string dropped."""
            return urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)

        def _saved(self, ident):
            """Resolve an id from the URL. All path safety lives in here."""
            return resolve_saved(saved_dirs(args), ident)

        def _device_info(self):
            """The board, or None - the file browser works without hardware."""
            try:
                with open_board(args) as board:
                    return board.info
            except (DeviceError, ProtocolError):
                return None

        def _send_file(self, path, name):
            with open(path, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header(
                "Content-Disposition", 'attachment; filename="%s"' % name
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # -- live input

        def _flag(self, params, name):
            value = (params.get(name) or ["0"])[0]
            return value.lower() not in ("0", "", "false", "no")

        def _sse(self, payload, name=None):
            self.wfile.write(sse_frame(payload, name))
            self.wfile.flush()

        def _input_devices(self):
            """What could be watched, and what already is.

            Called before opening a stream: EventSource cannot read an error
            response body, so anything that would refuse the stream has to be
            findable up front.
            """
            fake = getattr(args, "fake_input", None)
            devices = (
                [_fake_device(fake)] if fake else find_input_devices(all_devices=True)
            )
            note = None
            if sys.platform != "linux" and not fake:
                note = ("watching the panel needs Linux (/dev/input). Restart "
                        "with --fake-input to try this out here.")
            elif not any(d.vendor == VENDOR_2015 for d in devices):
                note = ("no /dev/input node belongs to the board. If `list` "
                        "finds it, the kernel may not have bound a keyboard "
                        "driver to it.")
            return {
                "devices": [
                    dict(d.as_dict(),
                         ours=d.vendor == VENDOR_2015 and d.product in IPAC2_MODES)
                    for d in devices
                ],
                "fake": bool(fake),
                "note": note,
                "running": monitors.monitor is not None,
                "watchers": monitors.users,
                "options": (
                    {"grab": monitors.options[0], "all": monitors.options[1]}
                    if monitors.options else None
                ),
            }

        def _input_stream(self):
            """A long-lived text/event-stream of presses.

            The options ride in the query string because EventSource can only
            GET - which also makes an exclusive grab last exactly as long as
            the connection that asked for it.
            """
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            grab, every = self._flag(params, "grab"), self._flag(params, "all")
            try:
                monitor = monitors.acquire(grab, every)
            except (DeviceError, ProtocolError) as exc:
                return self._json({"error": str(exc)}, 503)

            events = monitor.stream.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self._sse(
                    {
                        "devices": [d.as_dict() for d in monitor.devices],
                        "grab": grab,
                        "all": every,
                        "matching": monitor.profile is not None,
                        "fake": bool(getattr(args, "fake_input", None)),
                    },
                    "watching",
                )
                while True:
                    try:
                        self._sse(events.get(timeout=SSE_HEARTBEAT))
                    except queue.Empty:
                        if monitor.error:
                            return self._sse({"error": monitor.error}, "fault")
                        # Also how a closed tab is noticed: this write fails.
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                monitor.stream.unsubscribe(events)
                monitors.release()

        def _changes_result(self, board, current, updated, profile):
            """The response shape shared by apply and restore."""
            changes = diff_config(current, updated)
            return {
                "changes": [
                    {
                        "offset": c["offset"],
                        "meaning": c["meaning"],
                        "before": _fmt_byte(c["before"]),
                        "after": _fmt_byte(c["after"]),
                    }
                    for c in changes
                ],
                "warning": _gamepad_warning(profile, board.info),
                "written": False,
            }

        # -- routes

        def do_GET(self):
            try:
                path = self._route()
                if path in ("/", "/index.html"):
                    return self._html(PAGE)
                if path == "/api/saved":
                    return self._json({"saved": list_saved(saved_dirs(args))})
                if path.startswith("/api/saved/"):
                    ident = path[len("/api/saved/"):]
                    if ident.endswith("/download"):
                        directory, full = self._saved(ident[: -len("/download")])
                        return self._send_file(full, os.path.basename(full))
                    directory, full = self._saved(ident)
                    profile = load_profile(full)
                    return self._json(
                        {
                            "id": ident,
                            "writable": directory["writable"],
                            "profile": profile,
                            "notes": import_notes(profile, self._device_info()),
                        }
                    )
                if path == "/api/codes":
                    return self._json(
                        {
                            "groups": [
                                {"label": label, "codes": sorted(table)}
                                for label, table in CODE_GROUPS
                            ],
                            "pin_groups": [
                                {"label": label, "pins": pins}
                                for label, pins in PIN_GROUPS
                            ],
                        }
                    )
                if path == "/api/device":
                    with open_board(args) as board:
                        info = board.info.as_dict()
                    info["fake"] = bool(getattr(args, "fake_device", None))
                    return self._json(info)
                if path == "/api/config":
                    with open_board(args) as board:
                        return self._json(decode_config(board.read_config()))
                if path == "/api/input/devices":
                    return self._json(self._input_devices())
                if path == "/api/input/stream":
                    return self._input_stream()
                if path == "/api/input":
                    params = urllib.parse.parse_qs(
                        urllib.parse.urlsplit(self.path).query)
                    since = int((params.get("since") or ["0"])[0])
                    monitor = monitors.monitor
                    if monitor is None:
                        return self._json({"running": False, "events": []})
                    return self._json(
                        {"running": True, "events": monitor.stream.since(since)}
                    )
                self._json({"error": "not found"}, 404)
            except (DeviceError, ProtocolError) as exc:
                self._json({"error": str(exc)}, 500)
            except Exception as exc:  # noqa: BLE001 - surface it in the UI
                self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)

        def do_POST(self):
            try:
                path = self._route()
                payload = self._body()
                if path == "/api/config":
                    return self._apply(payload)
                if path == "/api/restore":
                    return self._restore(payload)
                if path.startswith("/api/saved/") and path.endswith("/label"):
                    ident = path[len("/api/saved/"): -len("/label")]
                    directory, full = self._saved(ident)
                    if not directory["writable"]:
                        return self._json(
                            {"error": "%s files are read only" % directory["source"]},
                            403,
                        )
                    set_label(full, payload.get("label"))
                    return self._json({"saved": list_saved(saved_dirs(args))})
                if path == "/api/import":
                    return self._import(payload)
                return self._json({"error": "not found"}, 404)
            except (DeviceError, ProtocolError) as exc:
                self._json({"error": str(exc)}, 500)
            except Exception as exc:  # noqa: BLE001
                self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)

        def do_DELETE(self):
            try:
                path = self._route()
                if not path.startswith("/api/saved/"):
                    return self._json({"error": "not found"}, 404)
                directory, full = self._saved(path[len("/api/saved/"):])
                if not directory["writable"]:
                    return self._json(
                        {"error": "%s files are read only" % directory["source"]}, 403
                    )
                os.remove(full)
                return self._json(
                    {"deleted": os.path.basename(full),
                     "saved": list_saved(saved_dirs(args))}
                )
            except (DeviceError, ProtocolError) as exc:
                self._json({"error": str(exc)}, 500)
            except Exception as exc:  # noqa: BLE001
                self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)

        # -- board writes

        def _apply(self, payload):
            profile = payload.get("profile") or {}
            dry_run = bool(payload.get("dry_run"))
            with open_board(args) as board:
                current = board.read_config()
                updated = bytes(encode_config(profile, current))
                result = self._changes_result(board, current, updated, profile)
                if not dry_run and result["changes"]:
                    result["backup"] = self._backup(current)
                    board.write_config(updated)
                    result["written"] = True
                    # Anyone watching the panel should be matched against what
                    # the board holds now, not what it held when they started.
                    monitors.refresh(decode_config(updated))
                return self._json(result)

        def _incoming(self, payload):
            """The profile a request wants to use: a saved file, or an upload."""
            source = payload.get("source")
            if source:
                _, full = self._saved(source)
                return load_profile(full), os.path.basename(full)
            profile = payload.get("profile")
            if not isinstance(profile, dict):
                raise ProtocolError("expected a profile object or a source id")
            return profile, str(payload.get("name") or "the imported file")

        def _import(self, payload):
            """Merge a saved profile into the form's current state."""
            incoming, origin = self._incoming(payload)
            base = payload.get("base") or {}
            merged = merge_profile(base, incoming)
            return self._json(
                {
                    "source": origin,
                    "profile": merged,
                    "changed": profile_changes(base, merged),
                    "notes": import_notes(incoming, self._device_info()),
                }
            )

        def _restore(self, payload):
            """Byte-exact: write a dump's 256 bytes back, macros and all."""
            dry_run = bool(payload.get("dry_run"))
            profile, origin = self._incoming(payload)
            updated = as_write_command(raw_from_profile(profile, origin))

            with open_board(args) as board:
                current = board.read_config()
                result = self._changes_result(board, current, updated, profile)
                result["source"] = origin
                result["notes"] = import_notes(profile, board.info)
                if not dry_run and result["changes"]:
                    result["backup"] = self._backup(current)
                    board.write_config(updated)
                    result["written"] = True
                    monitors.refresh(decode_config(updated))
                return self._json(result)

        def _backup(self, current):
            return write_backup(
                decode_config(current), backup_dir(getattr(args, "backup_dir", None))
            )

    return Handler


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>I-PAC 2 configurator</title>
<style>
  :root {
    --bg: #f6f7f9; --panel: #fff; --ink: #16181d; --muted: #626b7a;
    --line: #d8dde5; --accent: #2f6df6; --warn: #8a5a00; --warn-bg: #fff5e0;
    --err: #a11; --ok: #17692f; --live: #b3005c; --live-bg: #ffe8f1;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #14161a; --panel: #1c1f25; --ink: #e8eaee; --muted: #96a0b0;
      --line: #2c313a; --accent: #6f9bff; --warn: #f0c168; --warn-bg: #2e2513;
      --err: #ff8a8a; --ok: #7fd396; --live: #ff85b8; --live-bg: #3a1327;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 1.5rem; background: var(--bg); color: var(--ink);
    font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  header { max-width: 60rem; margin: 0 auto 1.25rem; }
  h1 { font-size: 1.3rem; margin: 0 0 .3rem; }
  main { max-width: 60rem; margin: 0 auto; }
  .card {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 10px; padding: 1rem 1.15rem; margin-bottom: 1rem;
  }
  .muted { color: var(--muted); }
  .row { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; }
  button {
    font: inherit; padding: .45rem .9rem; border-radius: 7px;
    border: 1px solid var(--line); background: var(--panel); color: var(--ink);
    cursor: pointer;
  }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button:disabled { opacity: .5; cursor: default; }
  h2 { font-size: .95rem; text-transform: uppercase; letter-spacing: .04em;
       color: var(--muted); margin: 1.25rem 0 .5rem; }
  table { width: 100%; border-collapse: collapse; }
  td, th { padding: .3rem .4rem; text-align: left; border-bottom: 1px solid var(--line); }
  th { font-size: .8rem; color: var(--muted); font-weight: 600; }
  td.pin { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; width: 6rem; }
  select { font: inherit; width: 100%; max-width: 14rem; padding: .25rem;
           background: var(--panel); color: var(--ink);
           border: 1px solid var(--line); border-radius: 5px; }
  .banner { padding: .6rem .8rem; border-radius: 7px; margin-bottom: .75rem; }
  .banner.warn { background: var(--warn-bg); color: var(--warn); }
  .banner.err { background: var(--panel); color: var(--err); border: 1px solid var(--err); }
  .banner.ok { color: var(--ok); }
  pre { overflow-x: auto; font-size: .82rem; margin: .5rem 0 0;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  dl { display: grid; grid-template-columns: max-content 1fr; gap: .15rem .8rem; margin: 0; }
  dt { color: var(--muted); }
  button.small { padding: .25rem .55rem; font-size: .82rem; }
  button.danger { color: var(--err); }
  .badge { font-size: .72rem; text-transform: uppercase; letter-spacing: .04em;
           border: 1px solid var(--line); border-radius: 4px;
           padding: .05rem .35rem; color: var(--muted); }
  .name { font-weight: 600; }
  .when { font-size: .82rem; color: var(--muted); white-space: nowrap; }
  .changed { outline: 2px solid var(--accent); outline-offset: 1px; }
  .live { outline: 2px solid var(--live); outline-offset: 1px; }
  tr.live > td { background: var(--live-bg); }
  tr.live > td.pin { color: var(--live); font-weight: 700; }
  tr.fading > td { transition: background .5s ease-out; }
  #inputLog { max-height: 13rem; overflow-y: auto; margin: .6rem 0 0; }
  #inputLog div { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
                  font-size: .82rem; padding: .1rem 0;
                  border-bottom: 1px solid var(--line); white-space: pre; }
  #inputLog div.miss { color: var(--muted); }
  #inputLog div:first-child { color: var(--live); }
  label.check { display: inline-flex; align-items: center; gap: .3rem; }
  ul.notes { margin: .4rem 0 0; padding-left: 1.1rem; }
  ul.notes li { margin: .15rem 0; }
  input[type=file] { font: inherit; max-width: 100%; }
</style>
</head>
<body>
<header>
  <h1>I-PAC 2 configurator</h1>
  <p class="muted">Reads and writes the board's flash over USB. Every write is
  preceded by an automatic backup.</p>
</header>
<main>
  <div class="card" id="device"><span class="muted">looking for the board...</span></div>

  <div class="card">
    <div class="row">
      <button id="read">Read from board</button>
      <button id="preview">Preview changes</button>
      <button id="write" class="primary">Write to board</button>
      <button id="download">Download JSON</button>
      <button id="clear">Reset all pins</button>
    </div>
    <div id="status"></div>
  </div>

  <div class="card">
    <h2 style="margin-top:0">Changing mode</h2>
    <p class="muted">The board's mode is not stored in its configuration - it is
    a property of how the board presents itself over USB, and it is changed by
    holding buttons on the panel, not from here. Hold for a full 10 seconds:</p>
    <table>
      <tr><th>hold</th><th>gives</th><th>notes</th></tr>
      <tr><td class="pin">Start1 + P1SW1</td><td>keyboard</td>
          <td class="muted">sends keycodes; this tool can configure it</td></tr>
      <tr><td class="pin">Start1 + P1SW2</td><td>Dinput</td>
          <td class="muted">two game controllers; this tool can still configure it</td></tr>
      <tr><td class="pin">Start1 + P1SW3</td><td>Xinput</td>
          <td class="muted">two Xbox 360 pads - <strong>this tool cannot reach the
          board in this mode</strong></td></tr>
    </table>
    <p class="muted">Start1 must be the shift key for these to work. If a switch
    goes wrong, hold P1SW1 while plugging in the USB cable to force keyboard
    mode.</p>
  </div>

  <div class="card">
    <h2 style="margin-top:0">Saved configurations</h2>
    <p class="muted"><strong>Load into form</strong> fills the dropdowns below so
    you can review and edit before writing - pins the file does not name keep
    what the board already has. <strong>Restore exactly</strong> writes all 256
    bytes straight back, including macros the form cannot show. Either way the
    board's current config is backed up first.</p>
    <div id="saved"><span class="muted">looking for saved files...</span></div>
    <div id="savedStatus"></div>
    <div class="row" style="margin-top:.75rem">
      <label class="muted" for="upload">From this device:</label>
      <input type="file" id="upload" accept="application/json,.json">
    </div>
    <div id="uploadRow"></div>
  </div>

  <div class="card">
    <h2 style="margin-top:0">Live input</h2>
    <p class="muted">Press a control on the cabinet and the pin it came from
    lights up in the table below. Use it to find an action sitting on the wrong
    pin: if pressing P1 button 1 lights up <span class="pin">1sw3</span>, that
    is where the button is wired.</p>
    <div class="row">
      <button id="watch" class="primary">Start watching</button>
      <label class="check muted"><input type="checkbox" id="grab">
        exclusive - stop presses reaching Batocera</label>
      <label class="check muted"><input type="checkbox" id="allDevices">
        watch every input device</label>
    </div>
    <div id="inputStatus"></div>
    <div id="inputLog"></div>
  </div>

  <div class="card" id="pins"><span class="muted">loading...</span></div>
</main>
<script>
const $ = (s) => document.querySelector(s);
let CODES = null, PROFILE = null, SAVED = [], UPLOAD = null;

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json();
  if (data.error) throw new Error(data.error);
  return data;
}
function post(path, body) {
  return api(path, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
}
function banner(sel, html, cls) {
  $(sel).innerHTML = html ? `<div class="banner ${cls||''}">${html}</div>` : '';
}
function say(html, cls) { banner('#status', html, cls); }
function saySaved(html, cls) { banner('#savedStatus', html, cls); }
function notesHtml(notes) {
  if (!notes || !notes.length) return '';
  return `<ul class="notes">${notes.map(n => `<li>${esc(n)}</li>`).join('')}</ul>`;
}
function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

function optionsHtml(selected) {
  let html = `<option value=""${selected ? '' : ' selected'}>- none -</option>`;
  for (const group of CODES.groups) {
    html += `<optgroup label="${esc(group.label)}">`;
    for (const code of group.codes) {
      const sel = code === selected ? ' selected' : '';
      html += `<option value="${esc(code)}"${sel}>${esc(code)}</option>`;
    }
    html += '</optgroup>';
  }
  if (selected && !CODES.groups.some(g => g.codes.includes(selected))) {
    html += `<option value="${esc(selected)}" selected>${esc(selected)} (raw)</option>`;
  }
  return html;
}

function renderPins(changed) {
  const byName = {};
  for (const pin of (PROFILE.pins || [])) byName[pin.name] = pin;
  const marked = new Set((changed || []).map(c => `${c.pin}.${c.field}`));
  let html = '';
  for (const group of CODES.pin_groups) {
    html += `<h2>${esc(group.label)}</h2><table><tr>
      <th>pin</th><th>action</th><th>alternate (shifted)</th><th>is shift key</th></tr>`;
    for (const name of group.pins) {
      const pin = byName[name] || {};
      const mark = (field) => marked.has(`${name}.${field}`) ? ' class="changed"' : '';
      html += `<tr><td class="pin">${esc(name)}</td>
        <td><select${mark('action')} data-pin="${name}" data-field="action">${optionsHtml(pin.action || '')}</select></td>
        <td><select${mark('alternate_action')} data-pin="${name}" data-field="alternate_action">${optionsHtml(pin.alternate_action || '')}</select></td>
        <td><input${mark('shift')} type="checkbox" data-pin="${name}" data-field="shift" ${pin.shift ? 'checked' : ''}></td>
      </tr>`;
    }
    html += '</table>';
  }
  $('#pins').innerHTML = html;
  repaintLive();  // a re-render must not drop what is being held down
}

function collect() {
  const pins = {};
  for (const el of document.querySelectorAll('#pins [data-pin]')) {
    const name = el.dataset.pin;
    pins[name] = pins[name] || { name };
    pins[name][el.dataset.field] = el.type === 'checkbox' ? el.checked : el.value;
  }
  return {
    schemaVersion: 2.0, resourceType: 'ipac2-pins', deviceClass: 'ipac2',
    debounce: PROFILE.debounce || 'standard',
    paclink: !!PROFILE.paclink,
    pins: Object.values(pins),
  };
}

function renderChanges(result, target) {
  target = target || '#status';
  const what = result.source ? ` ${esc(result.source)}` : '';
  if (!result.changes.length) {
    return banner(target, `No change - the board already matches${what || ' this'}.`
      + notesHtml(result.notes), 'ok');
  }
  const rows = result.changes.map(c =>
    `[${String(c.offset).padStart(3)}] ${c.meaning.padEnd(18)} ${c.before} -> ${c.after}`).join('\\n');
  const head = result.written
    ? `<strong>Written${what}.</strong> ${result.backup ? 'Backup: ' + esc(result.backup) : ''}`
    : `<strong>${result.changes.length} byte(s) would change.</strong>`;
  banner(target, `${head}${notesHtml(result.notes)}<pre>${esc(rows)}</pre>`,
         result.written ? 'ok' : '');
  if (result.warning) {
    $(target).insertAdjacentHTML('afterbegin',
      `<div class="banner warn">${esc(result.warning)}</div>`);
  }
}

async function loadDevice() {
  try {
    const d = await api('/api/device');
    $('#device').innerHTML = `<dl>
      <dt>board</dt><dd>${esc(d.name)} (${esc(d.vendor)}:${esc(d.product)})${d.fake ? ' <em>fake</em>' : ''}</dd>
      <dt>mode</dt><dd><strong>${esc(d.mode)}</strong></dd>
      <dt>firmware</dt><dd>${esc(d.firmware)} - ${esc(d.firmware_note)}</dd>
      <dt>gamepad</dt><dd>${d.supports_gamepad ? 'supported' : 'not in this firmware'}</dd>
      <dt>node</dt><dd>${esc(d.path)} (interface ${d.interface})</dd></dl>`;
  } catch (err) {
    $('#device').innerHTML = `<div class="banner err">${esc(err.message)}</div>`;
  }
}

async function loadConfig() {
  try {
    PROFILE = await api('/api/config');
    renderPins();
  } catch (err) {
    $('#pins').innerHTML = `<div class="banner err">${esc(err.message)}</div>`;
  }
}

async function send(dry) {
  say('working...');
  try {
    renderChanges(await post('/api/config', { profile: collect(), dry_run: dry }));
    if (!dry) { loadConfig(); loadSaved(); }
  } catch (err) {
    say(esc(err.message), 'err');
  }
}

// -- live input
//
// The board is a keyboard, so a press raises an input event on the cabinet.
// The server reads /dev/input, names the board action behind the event and
// resolves it to pins against the config it last read; all that is left here
// is lighting up the right cell. A tap is far too short to see, so a
// highlight is held for a minimum time regardless of when the release lands.

let WATCH = null;
const HELD = new Map();
const LIVE_MIN_MS = 450;
const LIVE_MAX_MS = 6000;  // a release we never saw must not light a row forever

function paintLive(pin, field, on) {
  const cell = document.querySelector(
    `#pins [data-pin="${pin}"][data-field="${field}"]`);
  if (!cell) return;
  cell.classList.toggle('live', on);
  const row = cell.closest('tr');
  if (row) row.classList.toggle('live', on || !!row.querySelector('.live'));
}

function repaintLive() {
  for (const held of HELD.values()) paintLive(held.pin, held.field, true);
}

function holdPin(p) {
  const key = `${p.pin}.${p.field}`;
  const existing = HELD.get(key);
  if (existing) clearTimeout(existing.timer);
  HELD.set(key, {
    pin: p.pin, field: p.field, since: Date.now(),
    timer: setTimeout(() => releasePin(p, true), LIVE_MAX_MS),
  });
  paintLive(p.pin, p.field, true);
}

function releasePin(p, now) {
  const key = `${p.pin}.${p.field}`;
  const held = HELD.get(key);
  if (!held) return;
  clearTimeout(held.timer);
  const left = now ? 0 : Math.max(0, LIVE_MIN_MS - (Date.now() - held.since));
  if (left) {
    held.timer = setTimeout(() => releasePin(p, true), left);
    return;
  }
  HELD.delete(key);
  paintLive(p.pin, p.field, false);
}

function clearLive() {
  for (const held of HELD.values()) {
    clearTimeout(held.timer);
    paintLive(held.pin, held.field, false);
  }
  HELD.clear();
}

function describePins(e) {
  if (e.pins && e.pins.length) {
    const names = e.pins.map(
      p => p.field === 'action' ? p.pin : `${p.pin} (shifted)`).join(', ');
    return [names + (e.pins.length > 1 ? '  <- shared by several pins' : ''), false];
  }
  if (e.name) return ['no pin carries this code', true];
  if (e.muted) return ['not an action the board can store; hiding the rest of these', true];
  return ['not an action the board can store', true];
}

function logInput(e) {
  const [where, missed] = describePins(e);
  const hex = e.code === null || e.code === undefined
    ? '' : ` (0x${e.code.toString(16).padStart(2, '0')})`;
  const what = (e.name || `${e.kind} ${e.raw}`) + hex;
  const line = document.createElement('div');
  if (missed) line.className = 'miss';
  line.textContent = [
    new Date(e.ts * 1000).toLocaleTimeString(),
    e.held ? 'down' : 'up  ',
    what.padEnd(20),
    where.padEnd(30),
    e.node,
  ].join('  ');
  const log = $('#inputLog');
  log.insertBefore(line, log.firstChild);
  while (log.children.length > 40) log.removeChild(log.lastChild);
}

function onInput(e) {
  logInput(e);
  for (const pin of e.pins || []) {
    if (e.held) holdPin(pin); else releasePin(pin, false);
  }
}

function watchingText(info) {
  const bits = [];
  if (info.fake) bits.push('<strong>scripted input</strong> - no real hardware');
  const names = (info.devices || []).map(d => esc(
    d.node + (d.player ? ` (player ${d.player})` : ''))).join(', ');
  bits.push(names ? `watching ${names}` : 'watching nothing');
  if (info.grab) bits.push('<strong>presses are not reaching Batocera</strong>');
  if (info.matching === false) {
    bits.push('the board\\'s config could not be read, so presses cannot be '
              + 'matched to pins - codes only');
  }
  return bits.join(' &middot; ');
}

function inputSay(html, cls) { banner('#inputStatus', html, cls); }

// The board is a keyboard, so with this page open on the cabinet its presses
// also arrive here as ordinary keystrokes: arrows and space scroll the page,
// space and Enter fire whatever button has focus, and arrows land on a focused
// pin dropdown and silently change what it says. The evdev grab only covers
// the nodes the monitor matched, so it is not an answer on its own. Swallow
// the lot for as long as the stream is open - there is nothing on this page to
// type into - but leave ctrl/cmd/alt combos alone so reload and close still
// work.
function swallowKeys(ev) {
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
  ev.preventDefault();
}

function watchKeys(on) {
  for (const type of ['keydown', 'keypress']) {
    if (on) window.addEventListener(type, swallowKeys, true);
    else window.removeEventListener(type, swallowKeys, true);
  }
}

async function preflight() {
  const info = await api('/api/input/devices');
  if (!info.fake && !info.devices.some(d => d.ours) && !$('#allDevices').checked) {
    throw new Error(info.note || 'no input device belongs to the board. '
      + 'If it is plugged in and `list` finds it, try "watch every input '
      + 'device" to see what the panel is actually talking to.');
  }
  const wanted = { grab: $('#grab').checked, all: $('#allDevices').checked };
  if (info.running && info.options
      && (info.options.grab !== wanted.grab || info.options.all !== wanted.all)) {
    throw new Error('another browser is already watching with different '
      + `options (exclusive ${info.options.grab ? 'on' : 'off'}, `
      + `all devices ${info.options.all ? 'on' : 'off'}). Match those, or stop `
      + 'watching there first.');
  }
  return info;
}

async function startWatching() {
  inputSay('starting...');
  try {
    await preflight();
  } catch (err) {
    return inputSay(esc(err.message), 'err');
  }
  const params = new URLSearchParams();
  if ($('#grab').checked) params.set('grab', '1');
  if ($('#allDevices').checked) params.set('all', '1');

  let opened = false;
  const source = new EventSource('/api/input/stream?' + params);
  WATCH = source;
  source.addEventListener('watching', (msg) => {
    opened = true;
    inputSay(watchingText(JSON.parse(msg.data)), 'ok');
  });
  source.addEventListener('fault', (msg) => {
    inputSay(esc(JSON.parse(msg.data).error), 'err');
    stopWatching();
  });
  watchKeys(true);
  if (document.activeElement) document.activeElement.blur();
  source.onmessage = (msg) => onInput(JSON.parse(msg.data));
  source.onerror = () => {
    // EventSource retries by itself unless the response was never usable,
    // which is how a refused stream arrives here.
    if (source.readyState === EventSource.CLOSED || !opened) {
      inputSay('the server refused the stream - check its log', 'err');
      stopWatching();
    } else {
      inputSay('connection lost, reconnecting...', 'warn');
    }
  };
  $('#watch').textContent = 'Stop watching';
}

function stopWatching() {
  if (WATCH) { WATCH.close(); WATCH = null; }
  watchKeys(false);
  clearLive();
  $('#watch').textContent = 'Start watching';
}

function toggleWatching() {
  if (WATCH) { stopWatching(); inputSay(''); } else { startWatching(); }
}

// -- saved configurations

function savedUrl(id, suffix) {
  const [source, ...rest] = String(id).split('/');
  return `/api/saved/${encodeURIComponent(source)}/`
    + `${encodeURIComponent(rest.join('/'))}${suffix || ''}`;
}
function holdsText(e) {
  if (e.error) return 'unreadable: ' + e.error;
  const bits = [`${e.pins} pins`];
  if (e.macros) bits.push(`${e.macros} macros`);
  if (e.firmware) bits.push(`fw ${e.firmware}`);
  return bits.join(', ');
}

function renderSaved() {
  if (!SAVED.length) {
    $('#saved').innerHTML = '<p class="muted">Nothing saved yet. Every write to '
      + 'the board leaves a backup here.</p>';
    return;
  }
  let html = '<table><tr><th>file</th><th>saved</th><th>holds</th><th></th></tr>';
  for (const e of SAVED) {
    const title = e.label
      ? `<span class="name">${esc(e.label)}</span><div class="muted">${esc(e.name)}</div>`
      : `<span class="name">${esc(e.name)}</span>`;
    let buttons = '';
    if (!e.error) {
      buttons += `<button class="small" data-act="load" data-id="${esc(e.id)}">Load into form</button>`;
      if (e.has_raw) {
        buttons += `<button class="small" data-act="restore" data-id="${esc(e.id)}">Restore exactly</button>`;
        buttons += `<button class="small" data-act="compare" data-id="${esc(e.id)}">Compare to board</button>`;
      }
    }
    buttons += `<button class="small" data-act="download" data-id="${esc(e.id)}">Download</button>`;
    if (e.writable) {
      // An unreadable file cannot be relabelled - only removed.
      if (!e.error) {
        buttons += `<button class="small" data-act="label" data-id="${esc(e.id)}">Label</button>`;
      }
      buttons += `<button class="small danger" data-act="delete" data-id="${esc(e.id)}">Delete</button>`;
    }
    html += `<tr><td>${title} <span class="badge">${esc(e.source)}</span></td>
      <td class="when">${esc(e.mtime ? new Date(e.mtime * 1000).toLocaleString() : '')}</td>
      <td class="muted">${esc(holdsText(e))}</td>
      <td><div class="row">${buttons}</div></td></tr>`;
  }
  $('#saved').innerHTML = html + '</table>';
}

async function loadSaved() {
  try {
    SAVED = (await api('/api/saved')).saved;
    renderSaved();
  } catch (err) {
    $('#saved').innerHTML = `<div class="banner err">${esc(err.message)}</div>`;
  }
}

// Merge a saved profile into the form. The server does the merging so that
// "pins it does not name keep what they have" means the same thing here as it
// does for `apply` on the command line.
async function importInto(source) {
  saySaved('working...');
  try {
    const res = await post('/api/import', Object.assign({ base: collect() }, source));
    PROFILE = res.profile;
    renderPins(res.changed);
    const n = res.changed.length;
    saySaved(`<strong>Loaded ${esc(res.source)} into the form.</strong> `
      + (n ? `${n} field(s) highlighted below. ` : 'It matches the form already. ')
      + 'Nothing reaches the board until you press <em>Write to board</em>.'
      + notesHtml(res.notes), n ? '' : 'ok');
  } catch (err) {
    saySaved(esc(err.message), 'err');
  }
}

async function restoreExactly(source, name, dry) {
  if (!dry && !confirm(`Restore ${name} exactly?\\n\\n`
      + 'This writes all 256 bytes back to the board - every pin, plus any '
      + 'macros - replacing what is on it now. The current config is backed '
      + 'up first.')) return;
  saySaved('working...');
  try {
    renderChanges(await post('/api/restore',
      Object.assign({ dry_run: dry }, source)), '#savedStatus');
    if (!dry) { loadConfig(); loadSaved(); }
  } catch (err) {
    saySaved(esc(err.message), 'err');
  }
}

async function relabel(id, current) {
  const label = prompt('Name this saved config (blank to clear):', current || '');
  if (label === null) return;
  try {
    SAVED = (await post(savedUrl(id, '/label'), { label })).saved;
    renderSaved();
  } catch (err) {
    saySaved(esc(err.message), 'err');
  }
}

async function removeSaved(id, name) {
  if (!confirm(`Delete ${name} from the cabinet? This cannot be undone.`)) return;
  try {
    const res = await api(savedUrl(id), { method: 'DELETE' });
    SAVED = res.saved;
    renderSaved();
    saySaved(`Deleted ${esc(res.deleted)}.`, 'ok');
  } catch (err) {
    saySaved(esc(err.message), 'err');
  }
}

function renderUpload() {
  if (!UPLOAD) { $('#uploadRow').innerHTML = ''; return; }
  const raw = !!UPLOAD.profile.raw;
  $('#uploadRow').innerHTML = `<div class="row" style="margin-top:.5rem">
    <span class="name">${esc(UPLOAD.name)}</span>
    <button class="small" id="upLoad">Load into form</button>
    ${raw
      ? '<button class="small" id="upRestore">Restore exactly</button>'
        + '<button class="small" id="upCompare">Compare to board</button>'
      : '<span class="muted">no raw bytes in this file - form only</span>'}
  </div>`;
  const src = { profile: UPLOAD.profile, name: UPLOAD.name };
  $('#upLoad').onclick = () => importInto(src);
  if (raw) {
    $('#upRestore').onclick = () => restoreExactly(src, UPLOAD.name, false);
    $('#upCompare').onclick = () => restoreExactly(src, UPLOAD.name, true);
  }
}

$('#saved').addEventListener('click', (ev) => {
  const btn = ev.target.closest('button[data-act]');
  if (!btn) return;
  const id = btn.dataset.id;
  const entry = SAVED.find(e => e.id === id) || { name: id };
  switch (btn.dataset.act) {
    case 'load': return importInto({ source: id });
    case 'restore': return restoreExactly({ source: id }, entry.name, false);
    case 'compare': return restoreExactly({ source: id }, entry.name, true);
    case 'download': window.location = savedUrl(id, '/download'); return;
    case 'label': return relabel(id, entry.label);
    case 'delete': return removeSaved(id, entry.name);
  }
});

$('#upload').onchange = async (ev) => {
  const file = ev.target.files && ev.target.files[0];
  UPLOAD = null;
  if (!file) return renderUpload();
  try {
    const profile = JSON.parse(await file.text());
    if (!profile || typeof profile !== 'object' || Array.isArray(profile)) {
      throw new Error('not a profile object');
    }
    UPLOAD = { name: file.name, profile };
    saySaved('');
  } catch (err) {
    saySaved(`Could not read ${esc(file.name)} as a profile: ${esc(err.message)}`, 'err');
  }
  renderUpload();
};

$('#read').onclick = () => { say(''); loadConfig(); };
$('#preview').onclick = () => send(true);
$('#write').onclick = () => {
  if (confirm('Write this configuration to the board?')) send(false);
};
$('#clear').onclick = () => {
  // A blank board is how you tell two pins carrying the same code apart: clear
  // everything, put one action back, and whatever arrives came from that pin.
  // The shift-key checkboxes are left alone on purpose - clearing Start1's
  // would take the hold-to-switch-mode combos with it.
  const fields = document.querySelectorAll('#pins select[data-pin]');
  if (!fields.length) return say('the pin table has not loaded yet.', 'err');
  if (!confirm('Set every pin to none and write that to the board?\\n\\n'
      + 'The panel will do nothing until you fill pins back in. Shift-key '
      + 'flags are kept, and the board is backed up first.')) return;
  for (const el of fields) el.value = '';
  send(false);
};
$('#download').onclick = () => {
  const blob = new Blob([JSON.stringify(collect(), null, 2)], {type: 'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'ipac2-profile.json';
  a.click();
};

$('#watch').onclick = toggleWatching;
for (const id of ['#grab', '#allDevices']) {
  // Both options are properties of the connection, so changing one while
  // watching means opening a new stream.
  $(id).onchange = () => { if (WATCH) { stopWatching(); startWatching(); } };
}
window.addEventListener('beforeunload', stopWatching);

(async () => {
  CODES = await api('/api/codes');
  await loadDevice();
  await loadConfig();
  await loadSaved();
})();
</script>
</body>
</html>
"""


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (DeviceError, ProtocolError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
