"""Every operation the web UI performs, with no HTTP in sight.

Each method takes plain data and returns a plain dict, or raises. That is the
whole contract: `handler` is one adapter over it, and a Django view would be
another. Nothing here may import `http.server` or touch a request object.

Errors carry their own meaning rather than a status code:

    ProtocolError / DeviceError  the board, or the request, said no
    ReadOnlyError                a write was aimed at the shipped presets
"""

from __future__ import annotations

import os
import sys

from ..checks import (
    _gamepad_warning,
    hotkey_warning,
    mode_switch_note,
    xinput_warning,
)
from ..codec import as_write_command, decode_config, diff_config, encode_config
from ..codes import CODE_GROUPS, _fmt_byte
from ..device import open_board
from ..errors import DeviceError, ProtocolError, ReadOnlyError
from ..firmware import flash_write_blocked
from ..inputs.devices import find_input_devices
from ..inputs.monitor import _fake_device
from ..library import (
    import_notes,
    list_saved,
    merge_profile,
    profile_changes,
    resolve_saved,
    saved_dirs,
    set_label,
)
from ..pins import PIN_GROUPS
from ..profiles import backup_dir, load_profile, raw_from_profile, write_backup
from .monitors import MonitorHolder


class Service:
    """The UI's operations against one set of device options."""

    def __init__(self, args):
        self.args = args
        self.monitors = MonitorHolder(args)

    # -- helpers

    def _saved(self, ident):
        """Resolve an id from the URL. All path safety lives in here."""
        return resolve_saved(self._dirs(), ident)

    def _dirs(self):
        return saved_dirs(self.args)

    def _device_info(self):
        """The board, or None - the file browser works without hardware."""
        try:
            with open_board(self.args) as board:
                return board.info
        except (DeviceError, ProtocolError):
            return None

    def _writable(self, ident):
        directory, full = self._saved(ident)
        if not directory["writable"]:
            raise ReadOnlyError("%s files are read only" % directory["source"])
        return directory, full

    # -- reads

    def saved_list(self) -> dict:
        return {"saved": list_saved(self._dirs())}

    def saved_get(self, ident) -> dict:
        directory, full = self._saved(ident)
        profile = load_profile(full)
        return {
            "id": ident,
            "writable": directory["writable"],
            "profile": profile,
            "notes": import_notes(profile, self._device_info()),
        }

    def saved_file(self, ident) -> str:
        """The path to download, having checked the id is one we serve."""
        return self._saved(ident)[1]

    def codes(self) -> dict:
        return {
            "groups": [
                {"label": label, "codes": sorted(table)}
                for label, table in CODE_GROUPS
            ],
            "pin_groups": [
                {"label": label, "pins": pins} for label, pins in PIN_GROUPS
            ],
        }

    def device(self) -> dict:
        with open_board(self.args) as board:
            info = board.info.as_dict()
        info["fake"] = bool(getattr(self.args, "fake_device", None))
        return info

    def config(self) -> dict:
        with open_board(self.args) as board:
            return decode_config(board.read_config())

    def input_devices(self) -> dict:
        """What could be watched, and what already is.

        Called before opening a stream: EventSource cannot read an error
        response body, so anything that would refuse the stream has to be
        findable up front.
        """
        fake = getattr(self.args, "fake_input", None)
        devices = (
            [_fake_device(fake)] if fake else find_input_devices(all_devices=True)
        )
        note = None
        if sys.platform != "linux" and not fake:
            note = ("watching the panel needs Linux (/dev/input). Restart "
                    "with --fake-input to try this out here.")
        elif not any(d.ours for d in devices):
            note = ("no /dev/input node belongs to the board. If `list` "
                    "finds it, the kernel may not have bound a keyboard "
                    "driver to it.")
        return {
            "devices": [d.as_dict() for d in devices],
            "fake": bool(fake),
            "note": note,
            "running": self.monitors.monitor is not None,
            "watchers": self.monitors.users,
            "options": (
                {"grab": self.monitors.options[0],
                 "all": self.monitors.options[1]}
                if self.monitors.options else None
            ),
        }

    def input_events(self, since: int) -> dict:
        monitor = self.monitors.monitor
        if monitor is None:
            return {"running": False, "events": []}
        return {"running": True, "events": monitor.stream.since(since)}

    # -- writes to the library

    def saved_label(self, ident, label) -> dict:
        _, full = self._writable(ident)
        set_label(full, label)
        return self.saved_list()

    def saved_delete(self, ident) -> dict:
        _, full = self._writable(ident)
        os.remove(full)
        result = {"deleted": os.path.basename(full)}
        result.update(self.saved_list())
        return result

    def import_(self, payload) -> dict:
        """Merge a saved profile into the form's current state."""
        incoming, origin = self._incoming(payload)
        base = payload.get("base") or {}
        merged = merge_profile(base, incoming)
        return {
            "source": origin,
            "profile": merged,
            "changed": profile_changes(base, merged),
            "notes": import_notes(incoming, self._device_info()),
        }

    # -- writes to the board

    def apply(self, payload) -> dict:
        profile = payload.get("profile") or {}
        with open_board(self.args) as board:
            current = board.read_config()
            updated = bytes(encode_config(
                profile, current, payload.get("xinput")))
            return self._write(board, current, updated, profile, payload)

    def restore(self, payload) -> dict:
        """Byte-exact: write a dump's 256 bytes back, macros and all."""
        profile, origin = self._incoming(payload)
        updated = as_write_command(raw_from_profile(profile, origin),
                                   payload.get("xinput"))
        with open_board(self.args) as board:
            current = board.read_config()
            result = self._write(board, current, updated, profile, payload)
            result["source"] = origin
            result["notes"] = import_notes(profile, board.info)
            return result

    # -- shared by apply and restore

    def _incoming(self, payload):
        """The profile a request wants to use: a saved file, or an upload."""
        source = payload.get("source")
        if source:
            full = self.saved_file(source)
            return load_profile(full), os.path.basename(full)
        profile = payload.get("profile")
        if not isinstance(profile, dict):
            raise ProtocolError("expected a profile object or a source id")
        return profile, str(payload.get("name") or "the imported file")

    def _write(self, board, current, updated, profile, payload) -> dict:
        result = {
            "changes": [
                {
                    "offset": c["offset"],
                    "meaning": c["meaning"],
                    "before": _fmt_byte(c["before"]),
                    "after": _fmt_byte(c["after"]),
                }
                for c in diff_config(current, updated)
            ],
            "warning": _gamepad_warning(profile, board.info),
            "written": False,
        }
        blocked = flash_write_blocked(board.info)
        if blocked:
            result["flash_warning"] = blocked
        result["mode_note"] = mode_switch_note(updated, board.info)
        result["hotkey_warning"] = hotkey_warning(updated)
        result["xinput_warning"] = xinput_warning(updated, board.info)

        if payload.get("dry_run") or not result["changes"]:
            return result
        if blocked and not payload.get("force"):
            raise ProtocolError(blocked)
        result["backup"] = write_backup(
            decode_config(current),
            backup_dir(getattr(self.args, "backup_dir", None)),
        )
        board.write_config(updated)
        result["written"] = True
        # Anyone watching the panel should be matched against what the board
        # holds now, not what it held when they started.
        self.monitors.refresh(decode_config(updated))
        return result
