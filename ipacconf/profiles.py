"""Profiles on disk: loading, the built-in default, and backups."""

from __future__ import annotations

import datetime
import json
import os

from .codec import encode_config
from .errors import ProtocolError
from .protocol import CONFIG_SIZE, HEADER_WRITE

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
