# ipacconf — full reference

*This is the long-form reference: usage, protocol findings and hardware notes,
kept in full. It was the project's original README. For a short introduction,
start at the [main README](../README.md).*

Configure an Ultimarc I-PAC 2 (2015+, USB `d209:0420`) from Linux — including
directly on a Batocera arcade cabinet, where Ultimarc's Windows-only WinIPAC
cannot go.

Standard library only. No pip, no libusb, no udev rules, no kernel driver
detaching. Deploying it is a copy:

```sh
scp -r ipacconf profiles root@batocera:/userdata/system/ipac-config/
```

`ipacconf` is a directory — Python runs one by executing the `__main__.py`
inside it, so `python3 ipacconf serve` is all the cabinet needs.

To have the web UI come up automatically whenever the cabinet boots, see
[`batocera/README.md`](../batocera/README.md) — it installs
[`batocera/ipacconf`](../batocera/ipacconf) as a Batocera service.

## Use

```sh
python3 ipacconf list                      # what's attached, firmware, config interface
python3 ipacconf dump -o before.json       # read the board  ← do this first
python3 ipacconf apply p.json --dry-run    # annotated byte diff, writes nothing
python3 ipacconf apply p.json              # write it (backs up first)
python3 ipacconf restore before.json       # byte-exact restore (backs up first)
python3 ipacconf saved                     # list backups and shipped presets
python3 ipacconf monitor                   # name the pin behind each button press
python3 ipacconf serve                     # web UI on :8080
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
python3 ipacconf monitor
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
python3 ipacconf serve --fake-device fixtures/ipac2-1.55-keyboard.json \
                          --fake-input fixtures/input-keyboard-mode.jsonl
```

Each player has its own block of codes, so a code identifies the player as
well as the control. The monitor decodes a press against the block of the pad
node it arrived on, and prints the node and player on every line. Before that
was true it named every player 2 press with a player 1 code and pointed at a
player 1 pin, so a press on one panel came back as a pin on the other.

Every line also carries its raw evdev code, so if the board ever disagrees
with the table it is visible rather than silent.

Until it is settled, the way to read a muddled panel is to stop guessing and
isolate: the UI's *Reset all pins* button sets every action to none and writes
that, after which you put one action back at a time. With only one pin able to
produce anything, whatever arrives came from it. Shift-key flags are left
alone, so the hold-to-switch-mode combos keep working, and the board is backed
up first like any other write.

### Saved configurations

The UI's *Saved configurations* card lists two places: the backup directory,
which fills up on its own because every write backs up first, and the
`profiles/` shipped beside the package. A file from the browsing phone or
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

`python3 ipacconf saved` prints the same listing over SSH.

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
`CTRL L`, `F1`), `GAMEPAD 0`–`10` and `HAT 0 UP`/`DOWN`/`LEFT`/`RIGHT` for
player 1, `P2 GAMEPAD 0`–`10` and `P2 HAT UP`/… for player 2, `MOUSE L`, media
keys — or `""` to unassign. `shift: true` makes that pin the shift key;
`alternate_action` is what a pin sends while shift is held.

`description` is an optional label. The board never sees it — the web UI shows
it under the pin name, which is how `GAMEPAD 0` reads as *GP1 South (A /
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
| 1.50+ | Yes — multi-mode, switch with `Start1+P1SW4` held 10s |

`list` reports which you have.

**There are five modes on 1.50+, and only three of them use your config.**
Ultimarc's preset gamepad modes run a fixed internal map and ignore the config
block entirely, which is not obvious from the hotkey layout:

| Hold with Start1, 10s | Mode | Uses your config? | Observed on 1.55 |
|---|---|---|---|
| `P1SW1` | 1 — keyboard | **yes** | `d209:0420` ✅ |
| `P1SW2` | 2 — Dinput preset | no — fixed internal map | not recorded |
| `P1SW3` | 3 — Xinput preset | no — fixed internal map | not recorded |
| `P1SW4` | 4 — Dinput user set | **yes** | `d209:0421` ✅ |
| `P1SW5` | 5 — Xinput user set | **yes** | not recorded |

Documented source: the Multi-Mode tab on [Ultimarc's I-PAC 2
page](https://www.ultimarc.com/control-interfaces/i-pacs/i-pac2/).

**`P1SW4` is the gamepad mode to use**, and it does what Ultimarc document —
led flashes four times, board comes up `d209:0421`.

**The mode a hotkey reaches depends on the config the board is holding.** An
earlier run of `P1SW4` on the same board produced `045e:028e` — Xinput — and
this document recorded that as the documentation being wrong. It was not. What
differed was the config. See "the reconfigure bit" below for the likely reason.

If a switch appears to do nothing at all, check the shift pin — see "A gamepad
profile can disarm the mode hotkeys" below.

**Write in keyboard mode.** Dinput accepts a write, acts on it immediately,
and never commits it to flash, so the board reverts on the next power cycle
with no error anywhere. Xinput cannot be written at all — it exposes no hid
interface, so the question of whether the write would stick never arises. `apply` and `restore` refuse to write in
any mode but keyboard; `--force` overrides. Switch to keyboard, write, then
switch back — the mode is not in the config block, so switching back does not
disturb what you wrote.

**You may not need a gamepad profile at all.** On 1.50+ firmware, mode 2
makes the board present as two game controllers with a fixed internal
pin-to-button map, no config involved. Switch to mode 2, and Batocera sees
controllers. You need a profile — and mode 4 — only when you want to choose
which button each pin sends.

### The board picks its own mode from what you send it

Ultimarc document three rules, and they are the reason a correct write can
look like it did nothing:

- a **keyboard-only** download, board in mode 4 → board switches to mode 1
- a **gamepad-only** download, board in mode 1 → board switches to mode 4
- a gamepad-only download **carrying an Xbox HOME key**, board in mode 1 →
  mode 5

Anything mixed leaves the mode alone. This is easier to hit than it sounds,
because *the download is the whole 256-byte block, not the profile*. Pins a
profile does not assign keep whatever the board already had, and alternate
(shifted) actions count too. Applying `profiles/batocera-gamepad.template.json`
to a factory board leaves fifteen keycodes in place — the eight stick pins and
seven alternate actions — so the download is mixed and the board stays in
keyboard mode. `TestGamepadTemplateIsMixed` pins that down.

The switch does fire — but with the Xinput bit set it goes to Xinput, not
Dinput. See "Force Board Reconfiguration" below. In practice: write in
keyboard mode, then reach for `Start1+P1SW4`.

`apply` says which way it expects the mode to go, and says so explicitly when
the answer is "nowhere, because this download is mixed".

### A gamepad profile can disarm the mode hotkeys

Confirmed on hardware. Mode switching is Start1 — as the I-PAC shift control —
held with `P1SW1`..`P1SW5`. Those are six ordinary pins, and a gamepad profile
reassigns all six:

| pin | after the gamepad template | in keyboard mode |
|---|---|---|
| `1start` (shift key) | `GAMEPAD 9` | inert |
| `1sw1`..`1sw5` (mode selectors) | `GAMEPAD 1`..`5` | inert |

Gamepad actions do nothing while the board is in keyboard mode, so the board
ends up with no working way to reach the gamepad mode the profile was written
for. The shift *bit* survives — `encode_config` preserves it — but the pin
sends an action that mode 1 has no use for.

The way out is the one route that ignores the config entirely: **hold `P1SW1`
while plugging in the USB cable**. Ultimarc document it as working
"irrespective of the current board mode or input configuration". Writing a
keyboard profile back puts keycodes on all six pins and re-arms the hotkeys.

`apply` warns before writing any config that would do this, and names the
pins. It does not refuse — the backdoor makes it recoverable, and someone
configuring a panel that never leaves gamepad mode may well want it.

### Making Dinput actually work on Batocera

Switching to Dinput on its own is often not enough: Batocera sees the pads but
the joystick does nothing, which is a recurring complaint on the Batocera
forums. The board's stock map puts the stick on four separate buttons, and
EmulationStation will not accept those as a d-pad.

Writing a profile fixes it — put the four directions on the **hat** codes and
the buttons on the button codes:

```sh
# 1. keyboard mode - the only mode that commits to flash
#    hold Start1+P1SW1 for ten seconds
ipacconf apply profiles/gamepad.json
# 2. hold Start1+P1SW4 for ten seconds -> Dinput, mode 4 (user set)
#    NOT P1SW2, which is mode 2 and ignores what you just wrote
```

Batocera then detects both pads and the sticks map as up/down/left/right in
the ES controller wizard. Confirmed on hardware, both players.

**`profiles/gamepad.json` is the one to use.** It assigns all 32 pins, clears
every alternate action, and gives each player its own block:

| | player 1 | player 2 |
|---|---|---|
| stick | `HAT 0 UP`/`DOWN`/`LEFT`/`RIGHT` | `P2 HAT UP`/… |
| buttons | `GAMEPAD 0`–`10` | `P2 GAMEPAD 0`–`10` |

`profiles/batocera-gamepad.template.json` is the older partial one; it predates
everything below and is kept only because it leaves unassigned pins alone,
which is the safer thing if you want to change a few buttons and nothing else.

Use `monitor` to find which physical control is on which pin before editing
either — the pin names are the board's, not the panel's.

Three things `gamepad.json` does on purpose:

- **No HOME key.** A gamepad-only config carrying an Xbox HOME key sends the
  board to Xinput mode 5 rather than Dinput mode 4, and Ultimarc recommend
  Dinput unless an application needs Xinput. Add one if you want Xinput.
- **`Start1` stays the I-PAC shift control**, even though it also sends a
  gamepad button. It costs nothing and mode switching needs a shift control.
- **Each player gets its own block of codes.** Giving both players the same
  codes is not harmless: the board reads the code to decide which controller a
  press belongs to, so identical codes put every press — player 2's included —
  on controller 1, and player 2's buttons mirror player 1's.

It does disarm the mode hotkeys while the board is in keyboard mode — `Start1`
and `P1SW1`–`P1SW5` all carry gamepad actions, which are inert there. That is
unavoidable in a gamepad-only map; `apply` warns, and holding `P1SW1` while
plugging in USB always gets you back.

Note you cannot check your work with `dump` while in Dinput; it does not report
the live config. Dump in keyboard mode, or judge by behaviour.

If the firmware is keyboard-only and you'd rather not flash, Batocera's
`keyboardToPads` (v41+) or `xarcade2jstick` (v40−) synthesize virtual gamepads
from a keyboard encoder — pair that with sensible keycodes written by this tool.

## Protocol

Reads return 256 bytes; writes send 260 (`IPACSERIES_SIZE`), the last four
being a read header `59 dd 0f 00` — captured from WinIPAC, not padding. A
short write is silently discarded. A full write is
committed to flash **only in keyboard mode**; in Dinput it reaches RAM and
goes no further.
2015+ boards take the config as 4-byte chunks inside HID **output**
reports (report id 3) — `HIDIOCSOUTPUT` on the hidraw node for the config
interface. Ultimarc's `wValue` of `0x0203` is `(report_type << 8) | report_id`,
and type 2 is Output, not Feature; sending it as a Feature report makes the
board STALL the transfer (`EPIPE`). Interface is 2 for firmware in `[0x40, 0x56)`, else 3. Keys are
standard USB HID usage IDs; `0x8e`+ is Ultimarc's gamepad/analog/hat range.
Reading is a `0x59 0xdd 0x0f 0x00` request followed by an input report.

Worked out from [Ultimarc-linux](https://github.com/katie-snow/Ultimarc-linux)
(C) and [QtPyUltimarc](https://github.com/katie-snow/QtPyUltimarc) (Python).

### Modes live in the product id, not the config

Multi-mode firmware (1.50+) reports the board's current mode as a **different
USB product id**, and switching re-enumerates it:

| Product | Mode | Interfaces |
|---|---|---|
| `d209:0420` | keyboard (mode 1) | 3 |
| `d209:0421` | Dinput game controller (mode 2 or 4) | 4 |
| `045e:028e` | Xinput game controller (mode 3 or 5) | — |

**The product id names the device class, not which of the five modes the board
is in.** Both Dinput modes present as `0421`, so a `0421` board may be running
your config (mode 4) or the fixed preset map (mode 2), and nothing in the
descriptor says which.

**That is what the "stale Dinput read" was.** An earlier version of this
document recorded that dumping in Dinput returns what looks like a stock
internal map — directions and the P2 side on codes nothing had written —
regardless of what is in flash, and called the read untrustworthy. The simpler
explanation is that those dumps were taken in **mode 2**, where that map is
what the board is genuinely running, because mode 2 ignores the config block
by design. The board was not serving a stale map; it was in a mode we did not
know existed.

This is not settled on hardware. Re-dumping in **mode 4** (`Start1+P1SW4`) is
the test: if the dump matches what was written, the theory holds and there is
no mystery left. If mode 4 also returns the preset map, then there really is
something undocumented and the old note should come back.

**Not an interface-selection problem.** Checked on hardware: with the board in
Dinput, exactly one of seven hidraw nodes answers a config read at all. There
is no other node holding a different config, so `open_board` picking the wrong
interface is ruled out either way.

`dump` warns when it is reading in a mode where the answer may not be the live
config, and says that a preset mode's map is unrelated to what was last
written.

`fixtures/ipac2-1.55-dinput.json` is byte-identical to the keyboard fixture
beside it, which is what the earlier "the two modes are byte-identical" claim
rested on. It is almost certainly a keyboard capture that was mislabelled — the
hotkey needs a full ten-second hold — and it records no product id, so there is
no way to tell from the file. `dump` now writes `capturedIn` and
`capturedProduct` for exactly this reason; both fixtures predate that and
should be recaptured before being trusted.

Switching is done by holding buttons — see the five-mode table above, ten
seconds each.

### Force Board Reconfiguration

WinIPAC has a *File → Force Board Reconfiguration* command, and Ultimarc's
documented recipe for a custom Xinput map is *change settings → save → Force
Board Reconfiguration*, with no separate "program the board" step. WinIPAC V2
advertises itself as reading and writing the board "on the fly", so the config
is likely already there and reconfiguration is the step that makes the board
re-evaluate it and re-enumerate.

**Captured, on a 1.55 board.** It is not a separate message. Every burst
WinIPAC sent was a write (`0x50`) or a read (`0x59`) — the same two headers
this tool already speaks. What it does instead is **set bit 1 of the config
bitfield**, and that bit means *"this config is an Xinput one"*.

The capture holds two full 260-byte downloads: an ordinary write after a pin
change, then *Force Board Reconfiguration*. They differ in exactly one byte:

| | byte 3 |
|---|---|
| ordinary write | `0x00` |
| Force Board Reconfiguration | `0x02` |

All 256 config bytes were identical.

Bit 1 is what Ultimarc-linux calls `accelerometer_uio` and QtPyUltimarc calls
`accelerometer` — a field belonging to the Ultimate I/O, the only board in the
family that has one. An I-PAC 2 does not.

#### How it was pinned down, and two wrong turns

1. **"It applies the download."** The obvious reading of the capture, and
   wrong. Setting it did not make the board act on a gamepad-only download,
   and it **persisted in flash** rather than being consumed — so it is a
   stored setting, not a command. The captured session never re-enumerated
   either: we had recorded the menu item, not its effect.
2. **"Ultimarc's automatic mode switch is broken."** Also wrong. A
   gamepad-only config written with the bit *clear* stayed in keyboard mode,
   and that looked like the documented auto-switch failing.
3. **What actually happens.** Writing a gamepad-only config with the bit
   **set**, from keyboard mode, takes the board to **Xinput** — confirmed with
   Batocera watching, which reported a *Microsoft Xbox controller* connecting.
   That is `045e:028e`, the identity the board wears in Xinput. Holding
   `Start1+P1SW4` then moved it to Dinput (`d209:0421`).

Which is exactly what Ultimarc document, once the menu item is read for what
it is *used for* rather than what it is *called*. Their only recipe involving
it builds a custom **Xinput** map:

> Save the file as an Xinput configuration. Click "File, Force Board
> Reconfiguration". The board should switch to Xinput mode using the custom
> configuration.

#### How this tool exposes it

`XINPUT_BIT`, as `--xinput` / `--no-xinput` on `apply` and `restore` (with
`--reconfigure` kept as an alias for the old name), a checkbox in the web UI,
and an `"xinput"` field in dumped profiles.

**It is preserved, not cleared.** It is ordinary config, so a write keeps
whatever the board had unless the profile or a flag says otherwise — the same
rule as `debounce` and `paclink`. An earlier version cleared it on every write,
which would have silently taken an Xinput board back to Dinput.

⚠️ **A write that sets it is the last write this tool can make.** Xinput
exposes no hid interface. `apply` warns before writing one, and names the ways
back: `Start1+P1SW4` for Dinput, `Start1+P1SW1` for keyboard, or hold `P1SW1`
while plugging in USB.

### The trailing four bytes are a read header

The same capture settles a second question. A download is 260 bytes, and this
tool sent the last four as zero padding. **WinIPAC sends `59 dd 0f 00`** — a
read request appended to the download.

Ultimarc-linux does the same on its **JPAC** path, writing `0x59 0xdd 0x0f`
into `barray[256]`..`[258]`
([ipac.c:629](https://github.com/katie-snow/Ultimarc-linux/blob/master/src/libs/ipac.c#L629)),
and zero-pads on the I-PAC 2 path — which is where this tool's zeros came from.
WinIPAC is the reference implementation, so `WRITE_TAIL` now follows WinIPAC.
A write is byte-for-byte what WinIPAC sends.

#### Capturing it

`analyse_capture.py` does the needle-finding. Run it on a tshark dump of the
capture and it reports every config burst, classifies each by its header byte,
and lists anything that is neither `0x50` nor `0x59`.

**Capture with `usbmon` on the Linux host, not inside the VM.** USB
passthrough hands the guest's URBs to the host's controller, so the host sees
every transfer WinIPAC makes — and usbmon taps below the guest, which means
nothing about the VM's USB emulation gets in the way.

**Capture one bus, not all of them.** `usbmon0` is every bus on the machine,
which is how a capture ends up holding the keystrokes typed into the host while
it ran. Find the board's bus and record only that:

```sh
lsusb | grep -iE "d209|045e:028e"      # -> "Bus 001 Device 010: ID d209:0420"
sudo modprobe usbmon
sudo tshark -i usbmon1 -w capture.pcapng     # usbmon<BUS>, so bus 001 -> usbmon1
```

Make the recording diffable — the same action twice, in opposite directions,
one file:

1. Start the capture. Note the board's device number from `lsusb`.
2. In WinIPAC, note the current mode. Wait ~3 seconds between every step, so
   the bursts separate cleanly.
3. *File → Force Board Reconfiguration* to get to Dinput.
4. Wait, then *Force Board Reconfiguration* again to get back to keyboard.
5. Stop the capture.

Then:

```sh
tshark -r capture.pcapng -T fields -E header=y -E separator=, \
  -e frame.number -e frame.time_relative \
  -e usb.bus_id -e usb.device_address \
  -e usb.urb_id -e usb.urb_type -e usb.endpoint_address \
  -e usb.bmRequestType -e usb.setup.bRequest -e usb.setup.wValue \
  -e usb.setup.wIndex -e usb.setup.wLength \
  -e usb.capdata -e usb.data_fragment -e usbhid.data \
  > capture.csv
python3 analyse_capture.py capture.csv
```

`usbhid.data` and `usb.endpoint_address` are not optional. **The board's
answers do not appear in any `usb.*` payload field** — they come back as
interrupt IN reports on endpoint `0x84`, which Wireshark hands to the HID
dissector. Leave them out and you see only what the host sent, never what the
board said. They are worth more than the writes: a WinIPAC session carries one
read per explicit refresh *plus one per download* (a download ends with a read
header, and the board answers it), so consecutive reads reassemble into the
board's config before and after every write and diff down to the single byte a
pin change moved.

**One invalid field name aborts the whole extraction** — tshark writes
`Some fields aren't valid`, exits, and leaves an empty file that looks exactly
like a failed capture. Field names vary by version (`usb.control.Data` exists
in some builds and not others; `usb.irp_id` is USBPcap-only). If it complains,
drop the field it names and re-run. `analyse_capture.py` needs only
`usb.capdata`; everything else sharpens the report.

Two usbmon-specific things the analyser handles, worth knowing about anyway:

- **Every transfer is logged twice**, once submitted and once completed.
  Messages are deduplicated by URB id, so a 65 message download does not read
  as 130.
- **The board changes usb device address when its mode changes**, because it
  re-enumerates. So a capture of a switch spans two or more addresses — pass
  all of them to `--address`, or leave it off. The analyser reports where the
  address changed, and that boundary is the highest-signal thing in the file:
  whatever went out immediately before it caused the switch, whatever its
  header byte.

Three outcomes, and all of them are answers:

- **An unknown header appears.** That is the command. With both directions in
  one capture, a byte that differs between them is the mode selector and one
  that does not is part of the command.
- **Nothing but `0x50` and `0x59`, but the address changed.** Force Board
  Reconfiguration is a plain config download, and the mode change is the
  firmware reacting to its content — this tool is missing nothing, and the
  question closes.
- **The address never changed.** No mode switch happened while recording, so
  the capture missed it. The analyser says so rather than letting it read as
  the second outcome.

`--gap` tunes how much silence ends a burst (default 1 second).

`*.pcap` and `*.pcapng` are gitignored. Read a capture before force-adding one
as a fixture — see the note in `.gitignore`.

#### Nothing on usbmonN

Run `python3 analyse_capture.py capture.csv` on what you did get: it reports
how many packets the file holds, which buses and device addresses appear, and
what payload sizes. That separates the two failures — *the capture recorded
nothing* from *the capture recorded the wrong bus*. In order of likelihood:

**The bus is not 1.** `usbmon<N>` takes the **bus** number, not the device
number, and not `1` by default. Check it with the board attached to the VM,
since attaching can move it:

```sh
lsusb | grep -iE "d209|045e:028e"     # "Bus 003 Device 010" -> usbmon3
```

**The interface does not exist.** usbmon needs its module loaded and debugfs
mounted, or tshark has nothing to open:

```sh
tshark -D | grep usbmon               # nothing listed? then:
sudo modprobe usbmon
sudo mount -t debugfs none /sys/kernel/debug     # if it is not already
ls /sys/kernel/debug/usb/usbmon/
```

**The VM has the whole USB controller, not just the board.** If the controller
was passed through with VFIO-PCI, the host kernel is not driving it and usbmon
can never see that traffic — there is no host-side USB stack for it to tap.
The tell is decisive: with the VM running and the board attached, the board is
**absent from the host's `lsusb`**. The fix is to pass the *device* through
instead (virt-manager's "USB Host Device", QEMU's `usb-host`), which routes it
via usbfs where usbmon can see it. Per-device passthrough leaves the board
visible in the host's `lsusb` the whole time.

**Nothing happened during the capture.** WinIPAC talks to the board on
explicit actions, so a capture that only spans idle time is genuinely empty.
Smoke-test it live — this should print lines as you click around in WinIPAC:

```sh
sudo tshark -i usbmon0 -c 20
```

**Fallback: capture every bus.** `usbmon0` sidesteps the bus question
entirely, at the cost of recording every device on the machine — read the
`.gitignore` note before keeping such a file. Find the board's address in the
analyser's output and narrow with `--address`, remembering that a mode switch
moves the board to a new one, so pass all of them.

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

Two deviations from QtPyUltimarc, both deliberate.

**The pin table.** QtPyUltimarc lists `2sw1` and `2sw5` with alternate-action
indices that break the otherwise perfectly regular `action+50` layout, and one
of them collides with `1sw5`'s. They look like typos, so all indices are
derived from the rule. `--dry-run` will show it if a real board ever disagrees.

**The game-controller code map is nothing like QtPyUltimarc's.** That table
puts 32 contiguous buttons at `0x90`, analog at `0xB0` and hats at `0xBA`.
Measured on a 1.55 board, pin by pin, it is two blocks of 25 — one per
controller:

| | player 1 | player 2 |
|---|---|---|
| 11 buttons | `0x8e`–`0x98` | `0xa7`–`0xb1` |
| 4 hat directions | `0x99`–`0x9c` | `0xb2`–`0xb5` |
| 10 axes | `0x9d`–`0xa6` | `0xb6`–`0xbf` |

Four things in there contradict upstream, and each cost real time:

- **Buttons start at `0x8e`, not `0x90`.** With a board read by WinIPAC, `0x8e`
  is *P1 Button 1 (A)* and `0x92` is *P1 Button 5 (LR)* — four apart on both
  scales. Every `GAMEPAD n` this tool wrote was two buttons out.
- **There are eleven per player, not 32.** A twelfth is the first hat
  direction, which is why a stick built on `GAMEPAD 12`–`15` half worked.
- **The hat is at `0x99`, not `0xBA`.** `HAT 0 UP`/`DOWN`/`LEFT`/`RIGHT` are
  `0x99`–`0x9c` in that order, measured by reading the evdev axis and value
  each raised. Upstream's `HAT 0`–`3` are not registered at all: two things
  with that name is how a stick ends up on codes that do nothing.
- **The code picks the controller, not the pin group.** `0x9d` is not a
  button, a hat direction, or past the end of anything — it is player 1's
  first *axis* code, which is why assigning it to a direction moved `ABS_X`.

Buttons are numbered from **zero**, matching Batocera, SDL and evdev rather
than WinIPAC's 1-based display, since a cabinet is configured against the
former.

⚠️ **Both changes alter what an existing profile means.** `GAMEPAD 1` was
`0x90`, then `0x8e`, and is now `0x8f`; nothing in a file records which
convention it was written under, so hand-written profiles need checking by
eye. Dumps and backups are unaffected — they carry `raw` bytes and `restore`
is byte-exact. Only the names moved.

The ten axis codes at the top of each block are the one part still inferred:
25 minus 11 minus 4, with `0x9d` moving `ABS_X` as the only direct evidence.
Nothing has needed them.

## Layout

```
ipacconf/
├── __main__.py    python3 ipacconf ...
├── version.py     the version number
├── errors.py      ProtocolError, DeviceError, ReadOnlyError
├── identity.py    which usb ids are an I-PAC 2, and what mode each means
├── firmware.py    what a given firmware can do, keyed by bcdDevice
├── protocol.py    report sizes, headers, config bits, HID framing
├── linux.py       ioctl request numbers and sysfs reads
├── pins.py        where each pin's three bytes live in the data array
├── codes.py       action codes and the names we give them
├── codec.py       encode/decode a 256 byte config - pure, no I/O
├── device.py      finding boards, and /dev/hidrawN - plus FakeBoard
├── checks.py      the warnings raised before a write
├── profiles.py    loading profiles, the built-in default, backups
├── library.py     the saved-configuration library the UI browses
├── cli.py         one function per subcommand, plus the parser
├── inputs/        keymap → events → devices → monitor
└── web/           monitors → service → handler → server
    └── static/    index.html · app.css · app.js
```

Each module may import the ones above it and none of the ones below. Modules
import their siblings directly (`from .codec import ...`) and never from the
package itself, which is a one-way facade re-exporting the public names so
`import ipacconf` still reaches all of them.

The web layer is split so the UI can be replaced without touching anything
else. `web/service.py` holds every operation the UI performs and knows nothing
about HTTP — no `http.server` import, no request objects. `web/handler.py` is
the only module that touches a request, and `web/static/` is plain
HTML/CSS/JS. A different front end reuses the service and replaces the other
two.

## Tests

```sh
python3 -m unittest test_ipacconf.py
```

324 tests, no hardware required. Seven groups matter most:
`decode(encode(x)) == x` byte-for-byte, which is what makes read-modify-write
trustworthy; `TestRealBoardDump`, which checks the pin table against a capture
from an actual board — every pin decoding to its factory MAME default is
strong evidence the indices are right; `TestResolveSaved`, which is the
web UI's file-access boundary and refuses `..`, absolute paths, non-JSON names
and symlinks pointing out of the directory; `TestKeycodeTable`, which is
what the input monitor stands on — it asserts no two keys reverse to the same
action, since a collision there would name a pin that is not the one being
pressed; and `TestAnalyseCapture`, which runs the capture analyser against a
synthetic tshark dump, so the "every message has a header" mistake cannot come
back and bury the one message a capture exists to find; and
`TestShippedGamepadProfile`, which holds the working cabinet's map in place —
each direction on the code that means it, opposite directions adjacent so they
share an axis, and no code shared between the two players; and
`TestCollectChecks`, which keeps the pre-write warnings in one ordered list,
since the CLI and the web UI running their own sets is how `restore` came to
be the one route that could disarm the mode hotkeys silently.

## Next

Nothing here blocks a working cabinet. Both players run as Dinput game
controllers with hat sticks and eleven buttons each, mapped in Batocera.

**Software mode switching.** Still unsolved, and now the only interesting gap.
Setting the Xinput bit moves the board to Xinput on the next download; nothing
found so far moves it to *Dinput* from software, so `Start1+P1SW4` is the way
in. Ultimarc say the supported method is loading a config file from WinIPAC,
so the capture worth taking is WinIPAC writing its default **Dinput** IPC file
with `usbmon` running. `analyse_capture.py` will point at the burst before the
address changed.

**The rest of the hotkey → product id table.** `P1SW1` (`d209:0420`) and
`P1SW4` (`d209:0421`) are recorded. `P1SW2`, `P1SW3` and `P1SW5` are not, and
filling them in would let `IPAC2_MODES` name the exact mode rather than the
device class — a `0421` board could be in mode 2 or mode 4 and the descriptor
does not say which.

**The ten axis codes per block.** `0x9d`–`0xa6` and `0xb6`–`0xbf` are inferred
from the block arithmetic, with `0x9d` moving `ABS_X` as the only measurement.
Nothing has needed them; a trackball or spinner would.

**Firmware 1.57** adds a pin that can be assigned as a mode-change button,
which would be writable from here. It is beta, the upgrade path is
Windows-only through WinIPAC, and it buys a physical button rather than
software switching — see the bootloader notes above.

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
- **A dump taken in a preset gamepad mode does not report your config**,
  because those modes run a fixed internal map and ignore the config block.
  Dump in keyboard mode, or judge by behaviour.
- **The stick has to be on the hat codes.** Four separate buttons are not a
  d-pad as far as EmulationStation is concerned, and opposite directions must
  be adjacent codes so they land on the same axis — split them across two and
  the hat never centres, which reads as a sluggish, sticky stick rather than a
  plainly wrong one.
- **Each player needs its own block of codes.** The board reads the code, not
  the pin group, to decide which controller a press belongs to.

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
