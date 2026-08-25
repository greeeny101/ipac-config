"""Config encode / decode - pure functions over a 256 byte buffer.

No I/O: everything here takes bytes and returns bytes or a profile dict, so
it can be exercised against recorded dumps without hardware.
"""

from __future__ import annotations

import re

from .codes import NONE, code_to_name, name_to_code
from .errors import ProtocolError
from .pins import PIN_ORDER, PIN_TABLE
from .protocol import (
    CONFIG_SIZE,
    DEBOUNCE,
    HEADER_WRITE,
    MACRO_FIRST_CODE,
    MACRO_LAST_CODE,
    MACRO_MAX_COUNT,
    MACRO_MAX_SIZE,
    MACRO_START,
    SHIFT_BIT,
    XINPUT_BIT,
)

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
        "xinput": bool(cfg & XINPUT_BIT),
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


def encode_config(profile: dict, base: bytes, xinput=None) -> bytearray:
    """Apply a profile on top of the board's current config.

    Read-modify-write on purpose: bytes whose meaning we do not know - and
    on this board there are some, including whatever selects game controller
    mode - survive untouched.

    XINPUT_BIT follows the same rule as debounce and paclink: the profile
    decides if it names it, the `xinput` argument overrides, and otherwise the
    board keeps what it had. Setting it sends the board to Xinput on the next
    mode change, where there is no config interface at all.
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
    want = profile.get("xinput") if xinput is None else xinput
    if want is not None:
        cfg = (cfg | XINPUT_BIT) if want else (cfg & ~XINPUT_BIT & 0xFF)
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


def as_write_command(buf: bytes, xinput=None) -> bytes:
    """Put the write header on a config buffer.

    Reads come back headed [0x00, 0x00, firmware, cfg]; writes must be
    headed 0x50 0xdd 0x0f. Byte 3 (the config bitfield) is real config and is
    left alone - a byte-exact restore includes XINPUT_BIT, unless `xinput`
    overrides it.
    """
    out = bytearray(buf[:CONFIG_SIZE])
    out[0], out[1], out[2] = HEADER_WRITE
    if xinput is not None:
        if xinput:
            out[3] |= XINPUT_BIT
        else:
            out[3] &= ~XINPUT_BIT & 0xFF
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
