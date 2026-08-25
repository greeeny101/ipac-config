# Running ipacconf on Batocera, and starting it at boot

The end state: the cabinet boots, and the configurator's web UI is waiting at
`http://<cabinet-ip>:8080` — so the I-PAC can be reconfigured from a phone or
laptop without leaving the game or plugging in a keyboard.

Everything lives under `/userdata`, which is the only part of Batocera that
survives a reboot (the root filesystem is a read-only squashfs). Nothing here
needs `batocera-save-overlay`.

Batocera ships Python 3.11 at `/usr/bin/python3`, and services run as root, so
the tool has what it needs: no pip, no libusb, and permission to open
`/dev/hidraw*`.

## 1. Copy the tool onto the cabinet

From your machine (default Batocera login is `root` / `linux`):

```sh
ssh root@batocera "mkdir -p /userdata/system/ipac-config"
scp -r ipacconf profiles root@batocera:/userdata/system/ipac-config/
```

Both are directories. `profiles` has to sit *beside* `ipacconf` rather than
inside it — that is where the tool looks for the shipped presets.

Substitute the cabinet's IP if the `batocera` hostname doesn't resolve — find
it in EmulationStation under **MAIN MENU → NETWORK SETTINGS**, or run `ip a`
over SSH.

Check it before automating anything:

```sh
ssh root@batocera
cd /userdata/system/ipac-config
python3 ipacconf list
python3 ipacconf dump -o /userdata/system/ipac-backups/before.json
```

`list` should name the board, its firmware and the config interface. If `dump`
works, the hard part is done.

## 2. Install the service

Batocera runs any script in `/userdata/system/services/` at boot, passing it
`start` (and `stop` on shutdown).

```sh
scp batocera/ipacconf root@batocera:/userdata/system/services/ipacconf
ssh root@batocera "chmod +x /userdata/system/services/ipacconf"
```

The filename matters: **`ipacconf`, with no `.sh` extension**. Batocera rejects
names containing dots or spaces, or starting with a digit. Verify yours is
accepted:

```sh
batocera-services list user
```

## 3. Enable it

```sh
batocera-services enable ipacconf     # start at every boot
batocera-services start  ipacconf     # and start it now, without rebooting
batocera-services status ipacconf
```

After one reboot it also appears in EmulationStation under **MAIN MENU →
SYSTEM SETTINGS → SERVICES**, where it can be toggled with a joystick.
Toggling there starts and stops it immediately.

Then browse to `http://<cabinet-ip>:8080`.

## 4. Confirm it survives a reboot

```sh
reboot
# ... once it's back ...
ssh root@batocera "batocera-services status ipacconf; tail /userdata/system/logs/ipacconf.log"
```

## Settings

Edit the variables at the top of `/userdata/system/services/ipacconf`:

| Variable | Default | Notes |
|---|---|---|
| `PORT` | `8080` | Batocera's own EmulationStation web server uses 1234, so 8080 is normally free. Check with `netstat -tuln \| grep 8080` |
| `HOST` | `0.0.0.0` | All interfaces, so the UI is reachable from your LAN. Set `127.0.0.1` to restrict it to the cabinet |
| `DIR` | `/userdata/system/ipac-config` | The directory holding `ipacconf/` and `profiles/` |

There is no authentication — this is a tool for a machine on your own network.
If that isn't your situation, bind it to `127.0.0.1` and reach it over an SSH
tunnel instead:

```sh
ssh -L 8080:localhost:8080 root@batocera
```

## Troubleshooting

**Service doesn't appear in the ES menu.** Reboot once — the list is built at
startup. Then check the name with `batocera-services list user`.

**Nothing on port 8080.** Read the log:

```sh
cat /userdata/system/logs/ipacconf.log
```

A "no Ultimarc board found" line means the service is fine and the board isn't
being seen — check `lsusb | grep -i d209`.

**Service starts but the page won't load from another machine.** Confirm
`HOST` is `0.0.0.0`, and that you're using the cabinet's IP rather than
`localhost`.

**Board found but `dump` times out.** The readback path is the least certain
part of the tool. Try the board's other hidraw nodes explicitly:

```sh
for n in /dev/hidraw*; do echo "== $n"; python3 ipacconf --device $n dump | head -3; done
```

**Batocera v42 or older.** Services work from v33 onwards, so this applies
either way — but on those versions `/userdata/system/custom.sh` is an
alternative (it was removed in v43). If you'd rather use it:

```sh
#!/bin/bash
case "$1" in
  start) /userdata/system/services/ipacconf start ;;
  stop)  /userdata/system/services/ipacconf stop  ;;
esac
```

Save with Unix (LF) line endings, or it will silently fail to run.

## Uninstalling

```sh
batocera-services stop    ipacconf
batocera-services disable ipacconf
rm /userdata/system/services/ipacconf
rm -rf /userdata/system/ipac-config
```

Backups in `/userdata/system/ipac-backups/` are left alone — keep at least one
until you're certain the board is configured the way you want it.

## A note on applying profiles at boot

You could make the service write a profile on every boot rather than serve the
UI. Don't, unless there's a specific reason: the board keeps its configuration
in flash, so it already survives reboots, reinstalls and being moved to another
machine. Writing on every boot spends flash write cycles to achieve nothing.
Configure it once, and let the board remember.

**Getting the joystick working in Dinput.** If Batocera detects the I-PAC pads
but the stick does nothing, the board's stock map has the four directions on
separate buttons and EmulationStation will not take those as a d-pad. Write a
profile with the directions on the **hat** codes — in keyboard mode — then
switch to Dinput:

```sh
# hold Start1+P1SW1 ten seconds -> keyboard (mode 1)
cd /userdata/system/ipac-config
python3 ipacconf apply profiles/gamepad.json
# hold Start1+P1SW4 ten seconds -> Dinput, mode 4
```

`profiles/gamepad.json` assigns all 32 pins and clears every alternate action,
which is what keeps the download entirely gamepad actions. Each player gets
its own block of codes — `HAT 0 …` and `GAMEPAD 0`–`10` for player 1,
`P2 HAT …` and `P2 GAMEPAD 0`–`10` for player 2. That split matters: the board
reads the code, not the pin group, to decide which controller a press belongs
to, so giving both players the same codes puts every press on controller 1 and
makes player 2's buttons mirror player 1's.

**Use `P1SW4`, not `P1SW2`.** There are five modes on 1.50+ firmware, and
`P1SW2` reaches mode 2 — the Dinput *preset*, which runs a fixed internal map
and ignores the config block entirely, so a profile checked there looks like it
was never written. `P1SW4` is mode 4, the Dinput mode that uses your config:
confirmed on hardware, the led flashes four times and the board comes up
`d209:0421`. `Start1+P1SW1`, or holding `P1SW1` while plugging in USB, always
gets you back.

Do not pass `--xinput` unless you mean it. It marks the config as an Xinput
one, and the board then comes up as an Xbox 360 pad with no config interface
at all — `apply` warns first.

`dump` does not report your config while in a preset gamepad mode, so check
your work in keyboard mode or by behaviour.

**A gamepad profile can disarm the mode hotkeys.** Start1+P1SW1-5 needs those
six pins to be doing something the board's current mode understands, and a
gamepad profile puts `GAMEPAD` actions on all six — which are inert in
keyboard mode. Written in keyboard mode, such a profile leaves no working
hotkey to reach the gamepad mode it was written for. The way back is holding
`P1SW1` while plugging in the USB cable, which ignores the config entirely.
`apply` warns before writing anything that would do this.

**Batocera needs configuring too.** The board sending correct events is only
half of it: EmulationStation maps them in
`/userdata/system/configs/emulationstation/es_input.cfg`, written by MAIN MENU
→ CONTROLLERS & BLUETOOTH SETTINGS → CONFIGURE A CONTROLLER. Both players
report the same USB ids, so they share one `deviceGUID` and one config block —
mapping either one maps both. ES menus only ever respond to player 1, so test
player 2 inside a game rather than in the menu.

**One caveat on persistence.** That only holds for a write made in keyboard mode. In Dinput
the board takes the write, acts on it, and never commits it — so the config
looks right until the next power cycle and then reverts. `apply` refuses to
write in Dinput for this reason. If you want the board in Dinput, switch to
keyboard (`Start1+P1SW1`, ten seconds), write, then switch back with
`Start1+P1SW4`; the mode is not part of the config, so the switch back leaves
your profile alone.

`apply` also reports which way it expects the mode to go. The board chooses
its mode from what it is sent, but only when the download is entirely one kind
or the other. Pins your profile does not assign, and alternate (shifted)
actions it does not clear, keep whatever the board already had — which is how
a gamepad profile ends up mixed by accident and the switch never fires.
