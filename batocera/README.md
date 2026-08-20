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
scp ipacconf.py root@batocera:/userdata/system/ipac-config/
scp -r profiles root@batocera:/userdata/system/ipac-config/
```

Substitute the cabinet's IP if the `batocera` hostname doesn't resolve — find
it in EmulationStation under **MAIN MENU → NETWORK SETTINGS**, or run `ip a`
over SSH.

Check it before automating anything:

```sh
ssh root@batocera
cd /userdata/system/ipac-config
python3 ipacconf.py list
python3 ipacconf.py dump -o /userdata/system/ipac-backups/before.json
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
| `DIR` | `/userdata/system/ipac-config` | Where `ipacconf.py` lives |

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
for n in /dev/hidraw*; do echo "== $n"; python3 ipacconf.py --device $n dump | head -3; done
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
