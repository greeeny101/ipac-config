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
python3 ipacconf.py restore before.json       # byte-exact restore (backs up first)
python3 ipacconf.py saved                     # list backups and shipped presets
python3 ipacconf.py monitor                   # name the pin behind each button press
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
`--dry-run`; *Write to board* backs up first. *Reset all pins* blanks every
action and writes that, which is how you take a panel apart to work out what
is wired where — see [below](#which-button-is-on-which-pin).

Add `--fake-device fixtures/dev-board.json` to any command to work against a
saved config file instead of hardware — how the UI gets developed on a machine
with no board attached.

### Which button is on which pin

Reading the config tells you what each pin is *supposed* to send. It cannot
tell you which physical button is wired to which pin — and that is exactly
what has gone wrong when an action turns up on the wrong control.

`monitor`, and the **Live input** card in the web UI, close that loop: press a
control on the cabinet and it names the pin the press came from.

```sh
python3 ipacconf.py monitor
```
```
watching /dev/input/event7  Ultimarc Ultimarc IPAC 2
press a control on the panel; ctrl-c to stop

12:04:31  down  CTRL L (0x70)   1sw1                       event7
12:04:31  up    CTRL L (0x70)   1sw1                       event7
12:04:34  down  5 (0x22)        1sw1 (shifted), 1coin      event7
```

If pressing P1 button 1 names `1sw3`, that is where the button is wired, and
the action you wanted on it is sitting on the wrong pin. In the web UI the
same press lights up the row instead.

Both directions of the question have the same answer. The arcade buttons *are*
the controller: in keyboard mode a press emits a keycode, in Dinput mode a
gamepad button, and either way the board raises a Linux input event that gets
reverse-mapped through the config. So pressing a control while EmulationStation
is asking you to "press a button for A" names its pin just the same.

Two options, both off by default and both available as toggles in the UI:

| | |
|---|---|
| `--grab` | Take exclusive control, so presses **stop reaching Batocera** while you test them. Without it, testing P1 buttons also navigates EmulationStation and can launch a game. The kernel releases the grab when the monitor stops. |
| `--all-devices` | Watch every input device rather than just the board — worth a try when nothing shows up, since it proves whether the press is coming from some other controller entirely. |

Reading `/dev/input/*` needs root, same as `/dev/hidraw*`.

Lines are reported even when nothing matches. That is not a failure — it is the
useful part. `no pin carries this code` means the board is sending something
its stored config does not account for, which separates a mis-assigned pin from
a config that was never written.

The exception is an event type there is no reading of at all — chiefly the
`EV_MSC` scan code the kernel raises alongside *every* key event. One of those
per press would bury the presses, so the first is reported and the rest are
dropped with `hiding the rest of these`.

In the web UI, keystrokes are swallowed for as long as the stream is open.
The board is a keyboard, so with the page open on the cabinet its own presses
would otherwise scroll it, fire whatever button has focus, and land on a
focused pin dropdown and change what it says.

Off the cabinet, `--fake-input` replays a script through the same translation
and matching path:

```sh
python3 ipacconf.py serve --fake-device fixtures/ipac2-1.55-keyboard.json \
                          --fake-input fixtures/input-keyboard-mode.jsonl
```

**One caveat, still to be settled against hardware.** In Dinput mode both
players' buttons live in the same `GAMEPAD 1..32` code space, so the code alone
cannot say who pressed. The event node it arrived on decides, on the assumption
that the two gamepad interfaces come up in player order — which is unverified.
Every line carries its raw evdev code, so if the board disagrees it is visible
rather than silent.

Until it is settled, the way to read a muddled panel is to stop guessing and
isolate: the UI's *Reset all pins* button sets every action to none and writes
that, after which you put one action back at a time. With only one pin able to
produce anything, whatever arrives came from it. Shift-key flags are left
alone, so the hold-to-switch-mode combos keep working, and the board is backed
up first like any other write.

### Saved configurations

The UI's *Saved configurations* card lists two places: the backup directory,
which fills up on its own because every write backs up first, and the
`profiles/` shipped next to the script. A file from the browsing phone or
laptop can be dropped in with the file picker.

Each saved file offers two different things, and the distinction matters:

- **Load into form** fills the dropdowns and highlights what it changed.
  Nothing reaches the board until *Write to board*, so it can be reviewed and
  edited first. Pins the file does not name keep what the board already has,
  exactly as `apply` behaves.
- **Restore exactly** writes all 256 bytes straight back — every pin, plus
  macros and the bytes whose meaning nobody has documented. This is the one to
  use when putting a board back the way it was, because a dump holds things the
  pin form cannot show, and loading it into the form would quietly drop them.
  Only offered for dumps, which are the files with a `raw` field.

*Compare to board* is the same diff without writing. Backups can also be
labelled — the name is stored inside the file, so a copy keeps it — downloaded
to the device browsing, or deleted. Presets are read only. Restoring backs up
what was on the board first, so a restore is itself undoable.

`python3 ipacconf.py saved` prints the same listing over SSH.

## Profiles

JSON, matching QtPyUltimarc's `ipac2.json` schema so profiles move between the
two tools:

```json
{
  "debounce": "standard",
  "paclink": false,
  "pins": [
    {"name": "1sw1", "action": "CTRL L", "alternate_action": "ESC"},
    {"name": "1start", "action": "1", "shift": true},
    {"name": "1sw2", "action": "GAMEPAD 2", "description": "GP1 East (B / Circle)"}
  ]
}
```

Pin names: `1up`/`1down`/`1left`/`1right` and `2up`… for the sticks, `1sw1`–
`1sw8` and `2sw1`–`2sw8` for buttons, `1start`/`1coin`/`1a`/`1b` and the `2`
equivalents. Actions are names from the code table — keys (`A`, `SPACE`,
`CTRL L`, `F1`), `GAMEPAD 1`–`32`, `HAT 0`–`3`, `ANALOG 0`–`7`, `MOUSE L`,
media keys — or `""` to unassign. `shift: true` makes that pin the shift key;
`alternate_action` is what a pin sends while shift is held.

`description` is an optional label. The board never sees it — the web UI shows
it under the pin name, which is how `GAMEPAD 1` reads as *GP1 South (A /
Cross)* in the Batocera preset without the code table having to carry
Batocera's vocabulary. Descriptions survive loading a profile into the form and
downloading it again.

**A profile only changes the pins it names.** Everything else on the board is
left exactly as it was — and within a named pin, only the fields it actually
lists. A pin with a `description` but no `action` is therefore a pure label: it
keeps whatever the board already had. That is how the gamepad preset annotates
the sticks without claiming to know their encoding.

## Safety

- Every write is **read-modify-write** on the board's live config. Bytes whose
  meaning isn't documented — and there are some — are carried across untouched
  rather than guessed at.
- `apply` and `restore` both back up to `/userdata/system/ipac-backups/` (or
  `~/.ipac-backups`) before writing. `--no-backup` opts out.
- The web UI only ever opens files inside those two directories. Ids from the
  browser are checked against the real path before anything is read, deleted or
  written — `serve` binds `0.0.0.0`, so the page is reachable by anything on
  the LAN.
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
switch goes wrong, hold `P1SW1` while plugging in USB.

**Write in keyboard mode.** Dinput accepts a write, acts on it immediately,
and never commits it to flash, so the board reverts on the next power cycle
with no error anywhere. Xinput cannot be written at all — it exposes no hid
interface, so the question of whether the write would stick never arises. `apply` and `restore` refuse to write in
any mode but keyboard; `--force` overrides. Switch to keyboard, write, then
switch back — the mode is not in the config block, so switching back does not
disturb what you wrote.

**You probably do not need a gamepad profile at all.** On 1.50+ firmware,
switching to Dinput makes the board present as two game controllers on its
own; the pin-to-button mapping is internal and the config block is untouched.
Switch mode, and Batocera sees controllers.

### Making Dinput actually work on Batocera

Switching to Dinput on its own is often not enough: Batocera sees the pads but
the joystick does nothing, which is a recurring complaint on the Batocera
forums. The board's stock map puts the stick on four separate buttons, and
EmulationStation will not accept those as a d-pad.

Writing a profile fixes it. Put the four directions on `HAT 1` and the buttons
on `GAMEPAD 1`..`n`, then:

```sh
# 1. keyboard mode - the only mode that commits to flash
#    hold Start1+P1SW1 for ten seconds
ipacconf.py apply profiles/your-gamepad.json
# 2. hold Start1+P1SW2 for ten seconds -> Dinput
```

Batocera then detects the I-PAC pads and the joystick maps as up/down/left/
right in the ES controller wizard. Confirmed on hardware.

`profiles/batocera-gamepad.template.json` is the starting point. Use `monitor`
to find which physical control is on which pin before editing it — the pin
names are the board's, not the panel's.

Note you cannot check your work with `dump` while in Dinput; it does not report
the live config. Dump in keyboard mode, or judge by behaviour.

If the firmware is keyboard-only and you'd rather not flash, Batocera's
`keyboardToPads` (v41+) or `xarcade2jstick` (v40−) synthesize virtual gamepads
from a keyboard encoder — pair that with sensible keycodes written by this tool.

## Protocol

Reads return 256 bytes; writes send 260 (`IPACSERIES_SIZE`), the last four
being zero padding — a short write is silently discarded. A full write is
committed to flash **only in keyboard mode**; in Dinput it reaches RAM and
goes no further.
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

**A read returns different bytes in each mode, and the Dinput read is the
one to distrust.** Dumping in Dinput returns what looks like a stock internal
map — joystick directions as GAMEPAD 10-13, the P2 side on ANALOG 1-5 —
regardless of what is actually in flash. Switch back to keyboard and the real
config is there, untouched.

**The board's behaviour follows flash, not that read.** Confirmed on hardware:
a profile written in keyboard mode is what the board acts on once switched to
Dinput. So per-pin gamepad assignments *do* work — you just cannot verify them
by dumping in Dinput.

**Not an interface-selection problem.** Checked on hardware: with the board in
Dinput, exactly one of seven hidraw nodes answers a config read at all, and it
is the one that returns the stale map. There is no other node holding the live
config, so `open_board` picking the wrong interface is ruled out.

What the Dinput read returns is the config as it was *before* the last
keyboard-mode write — not a generic factory default. Why the board serves that
while acting on something else is unknown; the mechanism is undocumented in
both open-source implementations, and settling it means capturing WinIPAC over
USB (see the bootloader notes below for the same problem).

Practically it does not matter: dump in keyboard mode, judge Dinput by
behaviour. `dump` warns when it is reading in a mode where the answer is not
live.

`fixtures/ipac2-1.55-dinput.json` is byte-identical to the keyboard fixture
beside it, which is what the earlier "the two modes are byte-identical" claim
rested on. It is almost certainly a keyboard capture that was mislabelled — the
hotkey needs a full ten-second hold — and it records no product id, so there is
no way to tell from the file. `dump` now writes `capturedIn` and
`capturedProduct` for exactly this reason; both fixtures predate that and
should be recaptured before being trusted.

Switching is done by holding buttons — `Start1+P1SW1` keyboard, `+P1SW2`
Dinput, `+P1SW3` Xinput, ten seconds each. WinIPAC can also switch in software
(*File → Force Board Reconfiguration*), so a command for it exists, but it is
not in either open-source implementation and we have not captured it. Since
the mode is not in the config block, it must be a separate message — likely
another `0x5x` header beside `0x50` (write) and `0x59` (read).

**Xinput hides the board behind an Xbox pad's identity.** Confirmed on
hardware: in Xinput the board does not enumerate as Ultimarc at all — it
reports `045e:028e`, a wired Microsoft Xbox 360 Controller, which is what gets
the `xpad` driver to bind to it. Every Ultimarc id disappears from `lsusb`.
That is why the tool used to say "no Ultimarc board found" here: discovery
filtered on the vendor id and the board no longer had one.

It is now found by its **string descriptors**, which it keeps while wearing
the borrowed ids — it still reports `Ultimarc` / `I-PAC 2`, visible in
`/dev/input/by-id` as `usb-Ultimarc_I-PAC_2-*`. This matters for safety as
much as for detection: `045e:028e` is the genuine Microsoft pad's id, shared
with thousands of clones, so it can never identify a board on its own. A
device answering to it is only ever treated as a board when the strings agree,
and one that does not match is dropped from discovery entirely rather than
becoming a candidate for a config probe. `list` labels the board `identity
borrowed from an Xbox 360 pad` so the Microsoft ids do not read as the tool
having found the wrong device.

Whether the **config interface** is reachable in Xinput is a separate question
from whether the board is *found*, and the answer appears to be no: on
hardware the board in Xinput produces no `/dev/hidraw` node at all, matching
what Ultimarc document. `xpad` binds it, `usbhid` does not, so there is
nothing to send the config protocol to — no amount of probing reaches it.

That makes finding the board on the **usb bus** the point, rather than a
consolation prize. Discovery scans `/sys/bus/usb/devices` as well as the
hidraw nodes, so `list` reports the board, its mode and its firmware with
`(no hid node)` where the config node would be, and then says why there is no
config node and how to get one back. The three cases are kept distinct,
because they have three different fixes:

| What is true | What the tool says |
|---|---|
| Board in Xinput, no hid node | It is in Xinput; the config interface does not exist there; switch with `Start1+P1SW1` |
| Board on the bus, hid node missing in a mode that should have one | Nothing bound `usbhid` — check `lsusb -t` |
| Nothing matching on the bus | Cable, port or power |

If the board is ever seen exposing a hid node in Xinput, nothing needs
changing to take advantage of it: it would be discovered and probed like any
other interface, and only the message above would become wrong.

**The borrowed identity includes `bcdDevice`.** Confirmed on hardware: in
Xinput the board reports version 1.00, which is the Xbox pad's, not its own —
so the firmware version and the `bcdDevice`-based gamepad rule are both
meaningless in this mode. `list` says so rather than printing 1.00 as the
board's firmware, which reads as an unrecognised-firmware fault that is not
there. What survives is the floor: mode switching exists only on 1.50+, so a
board that reached Xinput at all is at least that, and it is self-evidently a
gamepad while it is one.

Two joystick nodes appear in Xinput (`if00` and `if01` — one per player), and
no keyboard node. The input monitor follows the board there, so panel-watching
works in Xinput; note that `xpad` numbers buttons from `BTN_SOUTH` rather than
`BTN_TRIGGER`, so gamepad button *names* in the monitor are offset in this
mode. Every event carries its raw code, so the offset is visible rather than
silent — mapping it properly needs a capture of the board's Xinput button
order, which we do not have.

**Switching modes does not destroy the config.** Confirmed on hardware:
write a custom map in keyboard mode, switch to Dinput (the dump changes to the
board's internal map), switch back — the custom map is still there, byte for
byte. Flash survives the round trip; only what a read *reports* changes.

**Dinput accepts writes it will not keep.** Confirmed on hardware: in Dinput
the board takes all 65 messages, applies them to RAM and acts on them straight
away — the panel behaves exactly as the new profile says — and then drops the
flash commit. No stall, no error, no short read. Unplug the board and the old
config is back. The same write in keyboard mode persists across a cold power
cycle. This is invisible to any check that only reads the config back, because
a read returns what is in RAM; the only test that distinguishes the two is
*unplug the board and dump again*.

The mode itself is not in the 256-byte config — `list` reports it from the
descriptor. Keyboard is `d209:0420`, Dinput `d209:0421`, and Xinput
`045e:028e`; because Xinput changes the *vendor* too, the mode table is keyed
on the vendor/product pair rather than on the product id alone.

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

163 tests, no hardware required. Four groups matter most:
`decode(encode(x)) == x` byte-for-byte, which is what makes read-modify-write
trustworthy; `TestRealBoardDump`, which checks the pin table against a capture
from an actual board — every pin decoding to its factory MAME default is
strong evidence the indices are right; `TestResolveSaved`, which is the
web UI's file-access boundary and refuses `..`, absolute paths, non-JSON names
and symlinks pointing out of the directory; and `TestKeycodeTable`, which is
what the input monitor stands on — it asserts no two keys reverse to the same
action, since a collision there would name a pin that is not the one being
pressed.

## Next

**Mode switching from the CLI and UI.** The command is not public and is not in
either open-source implementation. Get it by capturing WinIPAC doing *File →
Force Board Reconfiguration* — switch to Dinput and back to keyboard in one
recording, so both directions can be diffed against each other. Look for a
`SET_REPORT` whose header byte is neither `0x50` (write) nor `0x59` (read).
Gate any Xinput switch behind a confirmation until a config read is confirmed
to work in Xinput: if it does not, that is the one switch the tool cannot
undo.

## Status

Reading from a real board works. The config it returns decodes to exactly the
factory MAME layout, re-encodes byte-for-byte identically, and a single-field
change moves exactly one byte — so the pin table, the code table, the
transport and the read-modify-write model are all confirmed against hardware.

Writing is confirmed too, and now confirmed *persistent*: a one-byte change
applied to a real board in keyboard mode survived unplugging the board and
plugging it back in. Read, write, diff, backup and restore have all now run
against hardware.

The earlier "a re-dump showed it had stuck" was not enough evidence — a re-dump
reads RAM, and the board had been in Dinput, where writes never reach flash.
Verify a write by **power-cycling the board**, not by reading it back.

Two hardware findings that changed the model, not just the code:

- **Dinput never commits a write to flash.** It applies to RAM, acts on it
  immediately, and reverts on the next power cycle, silently. Write in
  keyboard mode.
- **A dump taken in Dinput does not report the live config**, but the board
  still acts on it. Writing `HAT 1` on the four directions in keyboard mode is
  what makes Batocera accept the stick as a d-pad in Dinput — the fix for a
  well-known "Batocera sees the I-PAC pads but the joystick does nothing"
  complaint. Verify by behaviour, or by dumping in keyboard mode.

Six bugs the hardware found, all fixed:

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
6. **Dinput mode accepts writes and never flashes them.** This one hid behind
   (5): sending 260 bytes made the config take effect, which read as "fixed",
   but taking effect is a RAM write. The flash commit was still failing, and
   nothing distinguishes the two until the board is power-cycled. Write in
   keyboard mode.

The input monitor has **not** been run against the board yet. Everything it
does off-hardware works — the keycode table round-trips, a scripted panel walk
resolves each press to the right pin against a real board dump, and the web UI
lights the matching row — but the two things only the cabinet can settle are
whether the board's event node is found where `find_input_devices` looks for
it, and the Dinput player question above. `monitor` on the cabinet is the
one-command check for the first.
