# ipacconf

**Configure an Ultimarc I-PAC 2 arcade encoder from Linux — including directly on
a Batocera cabinet.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![Standard library only](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](#requirements)
[![Tests](https://img.shields.io/badge/tests-324%20passing-brightgreen.svg)](#development)

The I-PAC 2 is the board that turns arcade buttons and joysticks into something a
PC understands. Ultimarc's own configuration tool, WinIPAC, is Windows-only —
which is awkward, because the machine the board is actually plugged into is very
often a Linux arcade cabinet running Batocera.

`ipacconf` does the same job from Linux: read the board's configuration, change
what each pin sends, write it back, and put it all the way back again if it goes
wrong. It ships with a web UI, so the cabinet can be reconfigured from a phone or
a laptop while the cabinet itself stays on the game.

## Features

- **Read, diff and write** the board's 256-byte configuration, with a byte-level
  dry run before anything is committed.
- **Automatic backups** before every write, and a byte-exact restore.
- **Web UI** (`serve`) reachable over the LAN — every pin as a dropdown, plus
  live input and a browsable library of saved configurations.
- **Live input monitor** (`monitor`) that names the pin behind each button press,
  which is how you find out what is actually wired where.
- **Gamepad mode on Batocera** — profiles that make both players show up as real
  game controllers with working joysticks, not four loose buttons.
- **JSON profiles** compatible with QtPyUltimarc's `ipac2.json` schema.
- **No installation.** Standard library only: no pip, no libusb, no udev rules,
  no kernel driver detaching. Deploying it is a copy.

## Requirements

- An Ultimarc I-PAC 2, 2015 or later (USB `d209:0420`). Pre-2015 boards
  (`d208:0310`) speak a different protocol and are detected and refused.
- Linux, and Python 3 (Batocera ships 3.11).
- Root, or `sudo` — reading `/dev/hidraw*` and `/dev/input/*` needs it.

## Quick start

```sh
git clone https://github.com/greeeny101/ipac-config.git
cd ipac-config

sudo python3 ipacconf list                  # what's attached, firmware, mode
sudo python3 ipacconf dump -o before.json   # read the board  ← do this first
sudo python3 ipacconf serve                 # web UI on :8080
```

`ipacconf` is a directory, and Python runs a directory by executing the
`__main__.py` inside it — so there is nothing to install or build.

> [!IMPORTANT]
> **Take a dump before writing anything.** `apply` backs up automatically, but a
> dump you took yourself and kept is the restore point you actually trust.

### On a Batocera cabinet

```sh
scp -r ipacconf profiles root@batocera:/userdata/system/ipac-config/
```

To have the web UI come up automatically at every boot, see
[`batocera/README.md`](batocera/README.md), which installs it as a Batocera
service.

## Commands

| Command | What it does |
|---|---|
| `list` | What's attached — firmware, mode, config interface |
| `dump -o before.json` | Read the board's configuration to a file |
| `apply p.json --dry-run` | Annotated byte diff; writes nothing |
| `apply p.json` | Write a profile (backs up first) |
| `restore before.json` | Byte-exact restore (backs up first) |
| `saved` | List backups and shipped presets |
| `monitor` | Name the pin behind each button press |
| `serve` | Web UI on :8080 |

Add `--fake-device fixtures/dev-board.json` to any command to work against a
saved file instead of hardware — which is how the UI gets developed on a machine
with no board attached.

## Profiles

Profiles are JSON, and only change the pins they name:

```json
{
  "pins": [
    {"name": "1sw1", "action": "CTRL L", "alternate_action": "ESC"},
    {"name": "1start", "action": "1", "shift": true},
    {"name": "1sw2", "action": "GAMEPAD 2", "description": "GP1 East (B / Circle)"}
  ]
}
```

The ones shipped in [`profiles/`](profiles/):

- **`mame-keyboard.json`** — a sensible MAME-style keyboard layout.
- **`gamepad.json`** — both players as Dinput controllers with hat sticks and
  eleven buttons each. This is the one to use for Batocera.
- **`batocera-gamepad.template.json`** — an older partial map, kept because it
  leaves unassigned pins alone.

## Safety

Configuring an arcade board is one of those jobs where a bad write is genuinely
annoying to undo, so the tool is built to be reversible:

- Every write is **read-modify-write** on the live config. Undocumented bytes are
  carried across untouched rather than guessed at.
- `apply` and `restore` **back up first**, to `/userdata/system/ipac-backups/`
  (or `~/.ipac-backups`). A restore is itself undoable.
- `--dry-run` shows every changing byte with its offset, meaning and values.
- Writes are refused outside keyboard mode, because that is the only mode that
  commits to flash — Dinput accepts a write, acts on it, and silently loses it on
  the next power cycle.
- The web UI only opens files inside the backup and profile directories, and
  checks ids from the browser against the real path first.

## Documentation

- **[Full reference](docs/reference.md)** — every command in detail, the profile
  schema, the five firmware modes, the USB protocol as it was reverse-engineered,
  and the hardware findings behind each design decision. This was the project's
  original README and nothing has been dropped from it.
- **[Running on Batocera](batocera/README.md)** — copying the tool onto a
  cabinet and starting the web UI at boot.

## Development

```sh
python3 -m unittest test_ipacconf.py
```

324 tests, no hardware required. The suite carries the parts that hardware
confirmed: `decode(encode(x)) == x` byte-for-byte, the pin table checked against
a real board's factory dump, the web UI's file-access boundary, and the shipped
gamepad profile's exact code assignments.

Issues and pull requests are welcome — particularly hardware reports. Several of
the findings in the reference came from watching an actual board disagree with
the documentation, and the gaps that remain (the rest of the hotkey-to-mode
table, software mode switching, the inferred axis codes) all need a board in
front of someone to close.

## Acknowledgements

This tool would not exist without the people who worked out the protocol first:

- **[Ultimarc-linux](https://github.com/katie-snow/Ultimarc-linux)** and
  **[QtPyUltimarc](https://github.com/katie-snow/QtPyUltimarc)** by
  [Katie Snow](https://github.com/katie-snow) — the C and Python implementations
  that this one's understanding of the wire format, the pin table and the code
  table was built from. The profile schema is deliberately QtPyUltimarc's, so
  profiles move between the two tools.
- **[Ultimarc](https://www.ultimarc.com/control-interfaces/i-pacs/i-pac2/)** for
  the board itself and for documenting the multi-mode behaviour, without which
  the mode-switching hotkeys would still be a mystery.
- **[Batocera](https://batocera.org/)**, the reason this needed to run somewhere
  WinIPAC cannot.

Not affiliated with or endorsed by Ultimarc.

## License

[MIT](LICENSE) © greeeny101
