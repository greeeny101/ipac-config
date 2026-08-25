"""The saved-configuration library the UI browses.

Two directories are browsable: the backup directory, which we write to and
can delete from, and the shipped presets, which are read only.
"""

from __future__ import annotations

import json
import os

from .checks import _gamepad_warning
from .errors import ProtocolError
from .pins import PIN_ORDER
from .profiles import backup_dir, load_profile
from .protocol import HEADER_WRITE

#
# Two directories are browsable: the backup directory, which we write to and
# which the UI may relabel or delete from, and the profiles shipped alongside
# this script, which are read only.
# --------------------------------------------------------------------------

# Beside the package, not inside it: profiles/ is deployed as its own
# directory next to ipacconf/, both here and under /userdata on the cabinet.
PRESET_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "profiles"
)
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
    for key in ("debounce", "paclink", "xinput"):
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
