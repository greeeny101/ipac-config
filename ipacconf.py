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
import re
import select
import sys
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
        if not action:
            continue
        pin = {"name": name, "action": _action_name(action, macro_names)}
        alt = data[alt_i]
        if alt:
            pin["alternate_action"] = _action_name(alt, macro_names)
        if data[shift_i] & SHIFT_BIT:
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


def _iowr(type_char: str, nr: int, size: int) -> int:
    value = (
        ((_IOC_READ | _IOC_WRITE) << 30)
        | (size << 16)
        | (ord(type_char) << 8)
        | nr
    )
    # fcntl.ioctl wants this as a signed int on some Python builds.
    return ctypes.c_int32(value).value


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
        else:
            self._buf = bytearray(default_config())
            self._flush()

    def _flush(self):
        with open(self.path, "w") as fh:
            json.dump(decode_config(bytes(self._buf)), fh, indent=2)
            fh.write("\n")

    def read_config(self) -> bytes:
        return bytes(self._buf)

    def write_config(self, buf: bytes):
        self._buf = bytearray(buf[:CONFIG_SIZE])
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
# Profiles on disk
# --------------------------------------------------------------------------


def load_profile(path) -> dict:
    with open(path) as fh:
        profile = json.load(fh)
    if not isinstance(profile, dict):
        raise ProtocolError("%s is not a profile object" % path)
    return profile


def load_raw(path) -> bytes:
    """Get the 256 raw bytes out of a dump, if it has them."""
    profile = load_profile(path)
    raw = profile.get("raw")
    if not raw:
        raise ProtocolError(
            "%s has no 'raw' field - it is an edited profile, not a dump. "
            "Use `apply` rather than `restore`." % path
        )
    buf = bytes.fromhex(raw)
    if len(buf) != CONFIG_SIZE:
        raise ProtocolError("%s holds %d bytes, expected %d" % (path, len(buf), CONFIG_SIZE))
    return buf


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
    with open(path, "w") as fh:
        json.dump(profile, fh, indent=2)
        fh.write("\n")
    return path


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
    raw = as_write_command(load_raw(args.backup))
    with open_board(args) as board:
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
        board.write_config(raw)
        print("restored %s" % args.backup)
    return 0


def cmd_serve(args) -> int:
    return serve(args)


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
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("serve", help="run the web UI", parents=[common])
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--backup-dir")
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


def _make_handler(args):
    import http.server

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

        # -- routes

        def do_GET(self):
            try:
                if self.path in ("/", "/index.html"):
                    return self._html(PAGE)
                if self.path == "/api/codes":
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
                if self.path == "/api/device":
                    with open_board(args) as board:
                        info = board.info.as_dict()
                    info["fake"] = bool(getattr(args, "fake_device", None))
                    return self._json(info)
                if self.path == "/api/config":
                    with open_board(args) as board:
                        return self._json(decode_config(board.read_config()))
                self._json({"error": "not found"}, 404)
            except (DeviceError, ProtocolError) as exc:
                self._json({"error": str(exc)}, 500)
            except Exception as exc:  # noqa: BLE001 - surface it in the UI
                self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)

        def do_POST(self):
            try:
                if self.path not in ("/api/config", "/api/config?dry_run=1"):
                    return self._json({"error": "not found"}, 404)
                payload = self._body()
                profile = payload.get("profile") or {}
                dry_run = bool(payload.get("dry_run"))

                with open_board(args) as board:
                    current = board.read_config()
                    updated = bytes(encode_config(profile, current))
                    changes = diff_config(current, updated)
                    result = {
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
                    if not dry_run and changes:
                        result["backup"] = write_backup(
                            decode_config(current),
                            backup_dir(getattr(args, "backup_dir", None)),
                        )
                        board.write_config(updated)
                        result["written"] = True
                    return self._json(result)
            except (DeviceError, ProtocolError) as exc:
                self._json({"error": str(exc)}, 500)
            except Exception as exc:  # noqa: BLE001
                self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)

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
    --err: #a11; --ok: #17692f;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #14161a; --panel: #1c1f25; --ink: #e8eaee; --muted: #96a0b0;
      --line: #2c313a; --accent: #6f9bff; --warn: #f0c168; --warn-bg: #2e2513;
      --err: #ff8a8a; --ok: #7fd396;
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

  <div class="card" id="pins"><span class="muted">loading...</span></div>
</main>
<script>
const $ = (s) => document.querySelector(s);
let CODES = null, PROFILE = null;

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json();
  if (data.error) throw new Error(data.error);
  return data;
}
function say(html, cls) {
  $('#status').innerHTML = html ? `<div class="banner ${cls||''}">${html}</div>` : '';
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

function renderPins() {
  const byName = {};
  for (const pin of (PROFILE.pins || [])) byName[pin.name] = pin;
  let html = '';
  for (const group of CODES.pin_groups) {
    html += `<h2>${esc(group.label)}</h2><table><tr>
      <th>pin</th><th>action</th><th>alternate (shifted)</th><th>is shift key</th></tr>`;
    for (const name of group.pins) {
      const pin = byName[name] || {};
      html += `<tr><td class="pin">${esc(name)}</td>
        <td><select data-pin="${name}" data-field="action">${optionsHtml(pin.action || '')}</select></td>
        <td><select data-pin="${name}" data-field="alternate_action">${optionsHtml(pin.alternate_action || '')}</select></td>
        <td><input type="checkbox" data-pin="${name}" data-field="shift" ${pin.shift ? 'checked' : ''}></td>
      </tr>`;
    }
    html += '</table>';
  }
  $('#pins').innerHTML = html;
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

function renderChanges(result) {
  if (!result.changes.length) return say('No change - the board already matches this.', 'ok');
  const rows = result.changes.map(c =>
    `[${String(c.offset).padStart(3)}] ${c.meaning.padEnd(18)} ${c.before} -> ${c.after}`).join('\\n');
  const head = result.written
    ? `<strong>Written.</strong> ${result.backup ? 'Backup: ' + esc(result.backup) : ''}`
    : `<strong>${result.changes.length} byte(s) would change.</strong>`;
  say(`${head}<pre>${esc(rows)}</pre>`, result.written ? 'ok' : '');
  if (result.warning) {
    $('#status').insertAdjacentHTML('afterbegin',
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
    renderChanges(await api('/api/config', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ profile: collect(), dry_run: dry }),
    }));
    if (!dry) loadConfig();
  } catch (err) {
    say(esc(err.message), 'err');
  }
}

$('#read').onclick = () => { say(''); loadConfig(); };
$('#preview').onclick = () => send(true);
$('#write').onclick = () => {
  if (confirm('Write this configuration to the board?')) send(false);
};
$('#download').onclick = () => {
  const blob = new Blob([JSON.stringify(collect(), null, 2)], {type: 'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'ipac2-profile.json';
  a.click();
};

(async () => {
  CODES = await api('/api/codes');
  await loadDevice();
  await loadConfig();
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
