"""The command line: one function per subcommand, plus the parser."""

from __future__ import annotations

import argparse
import datetime
import json
import queue
import sys

from .checks import collect, find
from .codec import (
    as_write_command,
    decode_config,
    diff_config,
    encode_config,
)
from .codes import _fmt_byte
from .device import (
    DeviceInfo,
    FakeBoard,
    find_devices,
    find_usb_boards,
    no_config_node_reason,
    open_board,
    select_device,
)
from .errors import DeviceError, ProtocolError
from .firmware import flash_write_blocked
from .inputs.monitor import open_monitor
from .library import import_notes, list_saved, saved_dirs
from .profiles import backup_dir, load_profile, raw_from_profile, write_backup
from .protocol import CHUNK, WRITE_SIZE
from .version import __version__
from .web.server import serve



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
        on_bus = find_usb_boards()
        if on_bus:
            for dev in on_bus:
                _print_device(dev)
                print()
            print("Config node: %s" % no_config_node_reason(on_bus),
                  file=sys.stderr)
            return 1
        print("No Ultimarc board found.")
        print("Check `lsusb` for d209:04xx (keyboard/Dinput) or 045e:028e "
              "(Xinput); reading /dev/hidraw* needs root.")
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
    print("%s  %04x:%04x  %s"
          % (dev.path or "(no hid node)", dev.vendor, dev.product, dev.name))
    if dev.disguised:
        # Worth saying out loud: lsusb calls this a Microsoft pad, and without
        # this line the ids above look like the tool has found the wrong device.
        print("  identity   borrowed from an Xbox 360 pad; recognised by its "
              "usb strings (%s / %s)"
              % (dev.manufacturer or "?", dev.product_name or "?"))
    print("  mode       %s" % dev.mode)
    print("  firmware   %s" % dev.firmware_summary)
    if dev.interface >= 0:
        print("  interface  %d" % dev.interface)
    print("  gamepad    %s" % ("yes" if dev.supports_gamepad else "no"))


def cmd_dump(args) -> int:
    with open_board(args) as board:
        raw = board.read_config()
        info = board.info
    profile = decode_config(raw)
    # What a read returns depends on the mode the board is in, so a dump that
    # does not say which mode it came from cannot be trusted later. A
    # mislabelled Dinput capture is exactly how this repo ended up asserting
    # that the two modes are byte-identical.
    profile["capturedIn"] = info.mode
    profile["capturedProduct"] = "%04x" % info.product
    if flash_write_blocked(info):
        print(
            "WARNING: dumped in %s mode, where a read does not report the live "
            "config.\n         Switch to keyboard mode (Start1+P1SW1, ten "
            "seconds) for a dump you can trust. If this is a preset gamepad "
            "mode (2 or 3) the map you are seeing is the board's fixed "
            "internal one, which is unrelated to what was last written."
            % info.mode,
            file=sys.stderr,
        )

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


def _print_checks(found, skip=()) -> None:
    """Say what the checks found, loudest first. The web UI shows this list."""
    for item in found:
        if item["key"] in skip:
            continue
        prefix = "WARNING: " if item["level"] == "warning" else "note: "
        print("%s%s\n" % (prefix, item["text"]), file=sys.stderr)


def cmd_apply(args) -> int:
    profile = load_profile(args.profile)
    with open_board(args) as board:
        current = board.read_config()
        updated = bytes(encode_config(profile, current, args.xinput))
        changes = diff_config(current, updated)

        found = collect(updated, board.info, profile)
        # A blocked write is raised as an error below rather than warned
        # about - except on a dry run, where there is no write to block.
        _print_checks(found, skip=() if args.dry_run else ("flash",))
        blocked = find(found, "flash")

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

        if blocked and not args.force:
            print(
                "error: %s\n       Pass --force to write anyway." % blocked,
                file=sys.stderr,
            )
            return 2

        if not args.no_backup:
            path = write_backup(decode_config(current), backup_dir(args.backup_dir))
            print("backed up current config to %s" % path)

        board.write_config(updated)
        print(
            "wrote %d bytes in %d messages to %s"
            % (WRITE_SIZE, WRITE_SIZE // CHUNK, board.info.path)
        )

    return 0


def cmd_restore(args) -> int:
    profile = load_profile(args.backup)
    raw = as_write_command(raw_from_profile(profile, args.backup), args.xinput)
    with open_board(args) as board:
        for note in import_notes(profile, board.info):
            print("note: %s" % note, file=sys.stderr)
        current = board.read_config()
        changes = diff_config(current, raw)

        # The same checks the web UI runs on a restore. They used to be
        # skipped here, so `restore` was the one route that could disarm the
        # mode hotkeys without saying so.
        found = collect(raw, board.info, profile)
        _print_checks(found, skip=() if args.dry_run else ("flash",))
        blocked = find(found, "flash")

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
        if blocked and not args.force:
            print(
                "error: %s\n       Pass --force to write anyway." % blocked,
                file=sys.stderr,
            )
            return 2
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
    # The name is what the board code WOULD be if the host's numbering and the
    # board's line up. They do for buttons 1-8, confirmed on hardware; they do
    # not for everything, and when they disagree the pin named below is the
    # wrong one. Always print the raw evdev code so the inference is checkable
    # rather than invisible.
    if event["kind"] in ("hat", "axis"):
        # Name the axis the host moved, not a board code - there is no board
        # code to name, and inventing one is what made this confusing.
        what = "%s %s" % (
            event["kind"], AXIS_NAMES.get(event["raw"], "0x%x" % event["raw"]))
    else:
        what = event["name"] or "%s %d" % (event["kind"], event["raw"])
    if event["code"] is not None:
        what += " (0x%02x)" % event["code"]
    if event.get("raw") is not None:
        what += " [evdev %s/0x%x]" % (event.get("type", "?"), event["raw"])
    if event["pins"]:
        where = " ".join(
            pin["pin"] if pin["field"] == "action" else "%s (shifted)" % pin["pin"]
            for pin in event["pins"]
        )
        if len(event["pins"]) > 1:
            where += "  <- several pins carry this code"
    elif event["kind"] in ("hat", "axis"):
        where = "-- an axis; which pin moved it is not recoverable from the event"
    elif event["name"]:
        where = "-- no pin carries this code"
    elif event.get("muted"):
        where = "-- not an action the board can store; hiding the rest of these"
    else:
        where = "-- not an action the board can store"
    if event["held"] is None:
        # An axis: there is no edge to name, so say where it went.
        edge = "=%s" % event.get("value")
    else:
        edge = "down" if event["held"] else "up"
    # The node is what says which controller a press came from, and that is
    # the whole answer when a code is on both players' pins. Spell it out.
    node = event["node"]
    if event.get("player"):
        node += " (player %d)" % event["player"]
    return "%s  %-6s %-30s %-30s %s" % (when, edge, what, where, node)


AXIS_NAMES = {0x00: "X", 0x01: "Y", 0x02: "Z",
              0x10: "HAT0X", 0x11: "HAT0Y", 0x12: "HAT1X", 0x13: "HAT1Y"}


def cmd_monitor(args) -> int:
    profile = read_profile_quietly(args)
    try:
        monitor = open_monitor(args, profile=profile)
        monitor.start()
    except (DeviceError, ProtocolError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    pads = [d for d in monitor.devices if d.player]
    for device in monitor.devices:
        print("watching %s  %s%s" % (
            device.path, device.name,
            " (player %d)" % device.player if device.player else "",
        ))
    if len(pads) < 2:
        print(
            "note: %d game controller node%s. Both players' controls share one "
            "code space,\n      so with fewer than two pads a code cannot be "
            "attributed to a player -\n      lines will name every pin that "
            "carries the code, not one of them."
            % (len(pads), "" if len(pads) == 1 else "s")
        )
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
    p.add_argument(
        "--force",
        action="store_true",
        help="write even in a mode where the board will not commit to flash",
    )
    xinput = p.add_mutually_exclusive_group()
    xinput.add_argument(
        "--xinput", "--reconfigure",
        dest="xinput",
        action="store_true",
        default=None,
        help="mark the config as an Xinput one (config bit 1), so the board "
             "comes up as an Xbox 360 pad. This is the bit WinIPAC's File -> "
             "Force Board Reconfiguration sets. NOTE Xinput has no config "
             "interface, so this tool cannot reach the board afterwards",
    )
    xinput.add_argument(
        "--no-xinput",
        dest="xinput",
        action="store_false",
        help="clear the Xinput bit, so the board's gamepad mode is Dinput. "
             "Without either flag the board keeps whatever it had",
    )
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("restore", help="write a dump back byte for byte", parents=[common])
    p.add_argument("backup")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    p.add_argument("--backup-dir")
    p.add_argument(
        "--force",
        action="store_true",
        help="write even in a mode where the board will not commit to flash",
    )
    xinput = p.add_mutually_exclusive_group()
    xinput.add_argument(
        "--xinput", "--reconfigure",
        dest="xinput",
        action="store_true",
        default=None,
        help="mark the config as an Xinput one (config bit 1), so the board "
             "comes up as an Xbox 360 pad. This is the bit WinIPAC's File -> "
             "Force Board Reconfiguration sets. NOTE Xinput has no config "
             "interface, so this tool cannot reach the board afterwards",
    )
    xinput.add_argument(
        "--no-xinput",
        dest="xinput",
        action="store_false",
        help="clear the Xinput bit, so the board's gamepad mode is Dinput. "
             "Without either flag the board keeps whatever it had",
    )
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


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (DeviceError, ProtocolError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
