# ipacconf

Configure an Ultimarc I-PAC 2 (2015+, USB `d209:0420`) from Linux — including
directly on a Batocera arcade cabinet, where Ultimarc's Windows-only WinIPAC
cannot go.

One file, standard library only. No pip, no libusb, no udev rules, no kernel
driver detaching. Deploying it is a copy:

```sh
scp ipacconf.py root@batocera:/userdata/system/ipac-config/
```

To have the web UI come up automatically whenever the cabinet boots, see
[`batocera/README.md`](batocera/README.md) — it installs
[`batocera/ipacconf`](batocera/ipacconf) as a Batocera service.

## Use

```sh
python3 ipacconf.py list                      # what's attached, firmware, config interface
python3 ipacconf.py dump -o before.json       # read the board  ← do this first
python3 ipacconf.py apply p.json --dry-run    # annotated byte diff, writes nothing
python3 ipacconf.py apply p.json              # write it (backs up first)
python3 ipacconf.py restore before.json       # byte-exact restore
python3 ipacconf.py serve                     # web UI on :8080
```

Reading `/dev/hidraw*` needs root. On Batocera you already are; elsewhere use
`sudo`.

**Take a dump before writing anything.** `apply` backs up automatically, but a
dump you took yourself and kept is the restore point you actually trust. The
shipped `profiles/mame-keyboard.json` is a sensible MAME-style layout, *not* a
capture of your board's factory settings.

### Web UI

`serve` prints a LAN URL. Every pin appears as a dropdown over the full code
table, so you can configure the cabinet from a laptop or phone while the
cabinet itself stays on the game. *Preview changes* runs the same diff as
`--dry-run`; *Write to board* backs up first.

Add `--fake-device fixtures/dev-board.json` to any command to work against a
saved config file instead of hardware — how the UI gets developed on a machine
with no board attached.

## Profiles

JSON, matching QtPyUltimarc's `ipac2.json` schema so profiles move between the
two tools:

```json
{
  "debounce": "standard",
  "paclink": false,
  "pins": [
    {"name": "1sw1", "action": "CTRL L", "alternate_action": "ESC"},
    {"name": "1start", "action": "1", "shift": true}
  ]
}
```

Pin names: `1up`/`1down`/`1left`/`1right` and `2up`… for the sticks, `1sw1`–
`1sw8` and `2sw1`–`2sw8` for buttons, `1start`/`1coin`/`1a`/`1b` and the `2`
equivalents. Actions are names from the code table — keys (`A`, `SPACE`,
`CTRL L`, `F1`), `GAMEPAD 1`–`32`, `HAT 0`–`3`, `ANALOG 0`–`7`, `MOUSE L`,
media keys — or `""` to unassign. `shift: true` makes that pin the shift key;
`alternate_action` is what a pin sends while shift is held.

**A profile only changes the pins it names.** Everything else on the board is
left exactly as it was.

## Safety

- Every write is **read-modify-write** on the board's live config. Bytes whose
  meaning isn't documented — and there are some — are carried across untouched
  rather than guessed at.
- `apply` backs up to `/userdata/system/ipac-backups/` (or `~/.ipac-backups`)
  before writing.
- `--dry-run` shows every changing byte with its offset, meaning, and old/new
  values.
- Pre-2015 boards (`d208:0310`) are detected and refused: different protocol,
  and 2015+ firmware bricks them.

## Gamepad mode on Batocera

This is the usual reason to reach for the tool, and it depends on firmware:

| Firmware | Gamepad |
|---|---|
| 1.22–1.33, 1.44–1.49 | **No.** Keyboard-only silicon-side; needs a firmware upgrade |
| 1.34–1.39 | Yes — keyboard and gamepad simultaneously (config on interface 3) |
| 1.50+ | Yes — multi-mode, switch with `Start1+P1SW2` held 10s |

`list` reports which you have. On 1.50+, the mode hotkeys are
`Start1+P1SW1` keyboard, `Start1+P1SW2` Dinput, `Start1+P1SW3` Xinput. If a
switch goes wrong, hold `P1SW1` while plugging in USB. Note the config
interface is unavailable in Xinput mode — configure in keyboard or Dinput.

`profiles/batocera-gamepad.template.json` is **unverified**: the joystick
direction encoding needs confirming by dumping the config before and after a
mode switch and diffing. `apply` warns if a profile uses gamepad codes the
firmware can't act on.

If the firmware is keyboard-only and you'd rather not flash, Batocera's
`keyboardToPads` (v41+) or `xarcade2jstick` (v40−) synthesize virtual gamepads
from a keyboard encoder — pair that with sensible keycodes written by this tool.

## Protocol

2015+ boards take a 256-byte config as 4-byte chunks inside HID **output**
reports (report id 3) — `HIDIOCSOUTPUT` on the hidraw node for the config
interface. Ultimarc's `wValue` of `0x0203` is `(report_type << 8) | report_id`,
and type 2 is Output, not Feature; sending it as a Feature report makes the
board STALL the transfer (`EPIPE`). Interface is 2 for firmware in `[0x40, 0x56)`, else 3. Keys are
standard USB HID usage IDs; `0x90`+ is Ultimarc's gamepad/analog/hat range.
Reading is a `0x59 0xdd 0x0f 0x00` request followed by an input report.

Worked out from [Ultimarc-linux](https://github.com/katie-snow/Ultimarc-linux)
(C) and [QtPyUltimarc](https://github.com/katie-snow/QtPyUltimarc) (Python).

If you capture the firmware flash over USB to fill in the undocumented parts
(the enter-bootloader command, the gamepad mode bytes), **read the capture
before committing it**. `tshark -i usbmon0` records every USB device on the
machine, not just the I-PAC — a keyboard attached at the time will have typed
passwords in there. Captures, and `lsusb -v` output with its device serial
numbers, are gitignored for that reason; force-add deliberately once you've
checked one.

One deviation, in `ipacconf.py`'s pin table: QtPyUltimarc lists `2sw1` and
`2sw5` with alternate-action indices that break the otherwise perfectly regular
`action+50` layout, and one of them collides with `1sw5`'s. They look like
typos, so all indices are derived from the rule. `--dry-run` will show it if a
real board ever disagrees.

## Tests

```sh
python3 -m unittest test_ipacconf.py
```

41 tests, no hardware required. The one that matters most:
`decode(encode(x)) == x` byte-for-byte, which is what makes read-modify-write
trustworthy.

## Status

Verified on this machine against a fake device: CLI, web UI, diffing, backup,
apply, restore, and the full test suite. **Not yet run against the physical
board** — `dump` on the cabinet is the next step, and the first thing that
could disprove the read path (the report-id handling on readback is the least
certain part).
