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

**You probably do not need a gamepad profile at all.** On 1.50+ firmware,
switching to Dinput makes the board present as two game controllers on its
own; the pin-to-button mapping is internal and the config block is untouched.
Switch mode, and Batocera sees controllers.

`profiles/batocera-gamepad.template.json` remains only for the case where you
want *custom* per-pin gamepad button assignments. It is unverified, and
applying it is not part of the normal path.

If the firmware is keyboard-only and you'd rather not flash, Batocera's
`keyboardToPads` (v41+) or `xarcade2jstick` (v40−) synthesize virtual gamepads
from a keyboard encoder — pair that with sensible keycodes written by this tool.

## Protocol

Reads return 256 bytes; writes send 260 (`IPACSERIES_SIZE`), the last four
being zero padding — a short write is silently discarded.
2015+ boards take the config as 4-byte chunks inside HID **output**
reports (report id 3) — `HIDIOCSOUTPUT` on the hidraw node for the config
interface. Ultimarc's `wValue` of `0x0203` is `(report_type << 8) | report_id`,
and type 2 is Output, not Feature; sending it as a Feature report makes the
board STALL the transfer (`EPIPE`). Interface is 2 for firmware in `[0x40, 0x56)`, else 3. Keys are
standard USB HID usage IDs; `0x90`+ is Ultimarc's gamepad/analog/hat range.
Reading is a `0x59 0xdd 0x0f 0x00` request followed by an input report.

Worked out from [Ultimarc-linux](https://github.com/katie-snow/Ultimarc-linux)
(C) and [QtPyUltimarc](https://github.com/katie-snow/QtPyUltimarc) (Python).

### Modes live in the product id, not the config

Multi-mode firmware (1.50+) reports the board's current mode as a **different
USB product id**, and switching with `Start1+P1SW2` re-enumerates it:

| Product | Mode | Interfaces |
|---|---|---|
| `d209:0420` | keyboard | 3 |
| `d209:0421` | Dinput game controller | 4 |

Confirmed by dumping a real board in both modes: the two configs are
**byte-identical**, keycodes and all. In Dinput the board maps pins to gamepad
buttons internally and simply ignores the key assignments.

This is why a keyboard-mode dump and a Dinput-mode dump are **byte-identical**
— the mode is not in the 256-byte config at all. `list` reports it from the
descriptor. Xinput presumably has its own id; unverified.

Because Dinput adds a fourth interface, the `bcdDevice` rule from
Ultimarc-linux (which predates mode switching) is treated as a first guess
only: the tool probes each interface with a config read and uses whichever
answers.

### The bootloader

Putting a board into firmware-upgrade mode makes it re-enumerate as
**`d209:0750` "Ultimarc UHID Firmware Update"** — Ultimarc's own bootloader,
sharing the vendor id, not the stock Microchip HID bootloader (`04d8:003c`).
So `mphidflash` and friends are out, and flashing from Linux would mean
reimplementing this bootloader's protocol from a USB capture of WinIPAC doing
it. The `.ufw` files are ASCII hex records that look ready to replay — 171 of
them: `ff 38` start, `ff 39` data (a block index plus 66 payload bytes), `ff
3b` end — but the command that *enters* bootloader mode is undocumented and
has to come off the wire.

Noted here because it is not written down anywhere else.

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

82 tests, no hardware required. Two groups matter most:
`decode(encode(x)) == x` byte-for-byte, which is what makes read-modify-write
trustworthy; and `TestRealBoardDump`, which checks the pin table against a
capture from an actual board — every pin decoding to its factory MAME default
is strong evidence the indices are right.

## Status

Reading from a real board works. The config it returns decodes to exactly the
factory MAME layout, re-encodes byte-for-byte identically, and a single-field
change moves exactly one byte — so the pin table, the code table, the
transport and the read-modify-write model are all confirmed against hardware.

Writing is confirmed too: a one-byte change applied to a real board, and a
re-dump showed it had stuck. Read, write, diff, backup and restore have all
now run against hardware.

Five bugs the hardware found, all fixed:

1. The config goes out as an **output** report, not a feature report.
   Ultimarc's `wValue` 0x0203 is type 2 (Output); reading it as Feature made
   the board STALL every write (`EPIPE`).
2. **Every** read carries a report id, not just the first — leaving one 0x03
   embedded every five bytes and truncating the config.
3. Shift is **bit 6** of a pin's shift byte, not the whole byte. Real boards
   carry 0x01 there normally and 0x41 on the shift pin, so writing 0x00 would
   have cleared something the board cares about.
4. Naming a pin in a profile without giving it an `action` silently **wiped**
   that action. Only fields a profile actually names are written now.
5. A write is **260 bytes, not 256** — four longer than a read response, so 65
   messages rather than 64. The board accepts a short write message by message
   and then discards it, committing nothing: no error, no change.
