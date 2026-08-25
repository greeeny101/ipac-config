"""Device layer: finding boards on the bus and talking to /dev/hidrawN.

The only module that opens file descriptors to hardware. FakeBoard lives here
too so the CLI and web UI work with no board attached.

hidraw ioctls, from linux/hidraw.h:
  HIDIOCSOUTPUT(len)  = _IOWR('H', 0x0b, len)  SET_REPORT, type Output  (2)
  HIDIOCSFEATURE(len) = _IOWR('H', 0x06, len)  SET_REPORT, type Feature (3)

The board wants wValue 0x0203 - report type 2 (Output), report id 3 - which
is HIDIOCSOUTPUT. Sending the same bytes as a Feature report (0x0303) makes
the device STALL the control transfer, which arrives here as EPIPE.
"""

from __future__ import annotations

import ctypes
import errno
import glob
import json
import os
import select
import sys
import time

from .codec import decode_config
from .errors import DeviceError, ProtocolError
from .firmware import (
    config_interface_for,
    firmware_note,
    firmware_supports_gamepad,
)
from .identity import (
    KNOWN_2015_PRODUCTS,
    PRODUCT_IPAC2,
    PRODUCT_PRE2015,
    VENDOR_2015,
    VENDOR_PRE2015,
    VENDOR_XINPUT,
    board_mode,
)
from .linux import _iowr, _read_sysfs
from .profiles import default_config, load_raw
from .protocol import (
    CHUNK,
    CONFIG_SIZE,
    HEADER_READ,
    HEADER_WRITE,
    REPORT_ID,
    deframe,
    write_frames,
)

class DeviceInfo:
    def __init__(self, path, vendor, product, bcd, interface, usb_path,
                 manufacturer=None, product_name=None):
        self.path = path
        self.vendor = vendor
        self.product = product
        self.bcd = bcd
        self.interface = interface
        self.usb_path = usb_path
        # The usb string descriptors. In Xinput mode they are the only thing
        # that still says Ultimarc, so they are what identifies the board.
        self.manufacturer = manufacturer
        self.product_name = product_name

    @property
    def name(self):
        if self.vendor == VENDOR_2015:
            return KNOWN_2015_PRODUCTS.get(self.product, "unknown Ultimarc board")
        if self.vendor == VENDOR_XINPUT:
            return "I-PAC 2" if self.is_ipac2 else "Xbox 360 controller (not a board)"
        if self.vendor == VENDOR_PRE2015 and self.product == PRODUCT_PRE2015:
            return "pre-2015 I-PAC (unsupported)"
        return "unknown"

    @property
    def firmware(self):
        return "%d.%02x" % (self.bcd >> 8, self.bcd & 0xFF)

    @property
    def mode(self):
        """Which mode the board is in - it is encoded in its usb identity."""
        mode = board_mode(self.vendor, self.product,
                          self.manufacturer, self.product_name)
        if mode is None:
            return "unknown (%04x:%04x)" % (self.vendor, self.product)
        return mode

    @property
    def is_ipac2(self):
        return board_mode(self.vendor, self.product,
                          self.manufacturer, self.product_name) is not None

    @property
    def disguised(self):
        """True when the board is wearing another device's usb identity."""
        return self.is_ipac2 and self.vendor == VENDOR_XINPUT

    @property
    def firmware_summary(self):
        """What can honestly be said about the firmware version.

        The borrowed identity includes bcdDevice, so in Xinput the version on
        the wire is the Xbox pad's, not the board's - printing it as firmware
        invites someone to go looking for a firmware fault that is not there.
        What is still known is the floor: mode switching exists only on 1.50+,
        so a board that reached Xinput at all is at least that.
        """
        if self.disguised:
            return ("not reported in Xinput (the borrowed identity carries "
                    "%s); 1.50+ implied by the board having a mode to switch"
                    % self.firmware)
        return "%s  (%s)" % (self.firmware, firmware_note(self.bcd & 0xFF))

    @property
    def supports_gamepad(self):
        # In Xinput the board is a gamepad as we speak, and only 1.50+ can get
        # there, so the bcdDevice rule is both unusable and unnecessary here.
        if self.disguised:
            return True
        return firmware_supports_gamepad(self.bcd & 0xFF)

    def as_dict(self):
        return {
            "path": self.path,
            "vendor": "%04x" % self.vendor,
            "product": "%04x" % self.product,
            "name": self.name,
            "firmware": self.firmware,
            "firmware_known": not self.disguised,
            "firmware_note": self.firmware_summary,
            "supports_gamepad": self.supports_gamepad,
            "mode": self.mode,
            "interface": self.interface,
            "usb_path": self.usb_path,
            "disguised": self.disguised,
        }


def find_devices(include_unsupported=False, sys_root="/sys") -> list:
    """Find hidraw nodes belonging to Ultimarc boards."""
    found = []
    pattern = os.path.join(sys_root, "class", "hidraw", "hidraw*")
    for node in sorted(glob.glob(pattern)):
        hid_dir = os.path.realpath(os.path.join(node, "device"))
        iface_dir = os.path.dirname(hid_dir)
        usb_dir = os.path.dirname(iface_dir)

        vendor = _read_sysfs(os.path.join(usb_dir, "idVendor"))
        product = _read_sysfs(os.path.join(usb_dir, "idProduct"))
        if vendor is None or product is None:
            continue
        vendor, product = int(vendor, 16), int(product, 16)
        manufacturer = _read_sysfs(os.path.join(usb_dir, "manufacturer"))
        product_name = _read_sysfs(os.path.join(usb_dir, "product"))

        # A board in Xinput mode is only ours if its strings say so, which is
        # what keeps a genuine Xbox controller out of the list entirely - it
        # must never turn up as a candidate for a config probe.
        ours = board_mode(vendor, product, manufacturer, product_name) is not None
        if not ours and vendor not in (VENDOR_2015, VENDOR_PRE2015):
            continue
        if not include_unsupported and not ours:
            continue

        bcd_text = _read_sysfs(os.path.join(usb_dir, "bcdDevice"), "0000")
        try:
            bcd = int(bcd_text, 16)
        except ValueError:
            bcd = 0
        iface_text = _read_sysfs(os.path.join(iface_dir, "bInterfaceNumber"), "-1")
        try:
            interface = int(iface_text, 16)
        except ValueError:
            interface = -1

        found.append(
            DeviceInfo(
                path=os.path.join("/dev", os.path.basename(node)),
                vendor=vendor,
                product=product,
                bcd=bcd,
                interface=interface,
                usb_path=os.path.basename(usb_dir),
                manufacturer=manufacturer,
                product_name=product_name,
            )
        )
    return found


def find_usb_boards(sys_root="/sys") -> list:
    """Boards on the usb bus, whether or not they expose a hid node.

    find_devices() can only see a board that has a /dev/hidraw node. In Xinput
    the board is bound by xpad rather than usbhid and may expose no hid
    interface at all - and answering "no board found" for a board that is
    plainly plugged in is worse than useless. This finds it on the bus so the
    tool can name what it is and what to do about it. There is no config node,
    so `path` is None.
    """
    found = []
    pattern = os.path.join(sys_root, "bus", "usb", "devices", "*")
    for usb_dir in sorted(glob.glob(pattern)):
        if ":" in os.path.basename(usb_dir):
            continue  # an interface directory, not a device
        vendor = _read_sysfs(os.path.join(usb_dir, "idVendor"))
        product = _read_sysfs(os.path.join(usb_dir, "idProduct"))
        if vendor is None or product is None:
            continue
        try:
            vendor, product = int(vendor, 16), int(product, 16)
        except ValueError:
            continue
        manufacturer = _read_sysfs(os.path.join(usb_dir, "manufacturer"))
        product_name = _read_sysfs(os.path.join(usb_dir, "product"))
        if board_mode(vendor, product, manufacturer, product_name) is None:
            continue

        try:
            bcd = int(_read_sysfs(os.path.join(usb_dir, "bcdDevice"), "0000"), 16)
        except ValueError:
            bcd = 0
        found.append(
            DeviceInfo(
                path=None,
                vendor=vendor,
                product=product,
                bcd=bcd,
                interface=-1,
                usb_path=os.path.basename(usb_dir),
                manufacturer=manufacturer,
                product_name=product_name,
            )
        )
    return found


def no_config_node_reason(devices: list) -> str:
    """What to tell someone whose board is on the bus but has no hid node."""
    modes = ", ".join(sorted({d.mode for d in devices}))
    if all(d.disguised for d in devices):
        return (
            "the board is in %s mode and exposes no hid interface, so there "
            "is nothing to send the config protocol to. This is what Ultimarc "
            "document: the config interface is not available in Xinput.\n"
            "Hold Start1+P1SW1 for 10s to switch to keyboard mode, or hold "
            "P1SW1 while plugging in usb. Both put the board back on "
            "d209:0420, where it can be read and written." % modes
        )
    return (
        "the board is on the usb bus in %s mode but has no /dev/hidraw node. "
        "The kernel may not have bound usbhid, or another process (a VM's usb "
        "passthrough) holds the device - check `lsusb -t` for Driver=usbhid."
        % modes
    )


def select_device(explicit_path=None) -> DeviceInfo:
    """Pick the hidraw node that carries the config protocol."""
    if sys.platform != "linux":
        raise DeviceError(
            "talking to the board needs Linux (hidraw). Use --fake-device "
            "to work against a saved dump on this machine."
        )

    devices = find_devices(include_unsupported=True)
    if not devices:
        # The board may still be on the bus with no hid node - which is a
        # different problem, with a different fix, from it being absent.
        on_bus = find_usb_boards()
        if on_bus:
            raise DeviceError(no_config_node_reason(on_bus))
        raise DeviceError(
            "no Ultimarc board found - nothing on the usb bus matches one.\n"
            "  - `lsusb` shows d209:04xx?  the kernel may not have bound "
            "usbhid, or another process (a VM's USB passthrough) holds the "
            "device - check `lsusb -t` for Driver=usbhid\n"
            "  - `lsusb` shows 045e:028e?  that is an Xbox 360 pad's id, which "
            "the board wears in Xinput mode. It is identified by its usb "
            "strings, so if this is the board and it is still not found, its "
            "strings are not what we expect - report `lsusb -v -d 045e:028e | "
            "grep -i -A2 iManufacturer`\n"
            "  - nothing in lsusb?  it is a cable, port or power problem"
        )

    legacy = [d for d in devices if d.vendor == VENDOR_PRE2015]
    supported = [d for d in devices if d.is_ipac2]
    if not supported:
        if legacy:
            raise DeviceError(
                "this is a pre-2015 board (d208:0310). It speaks a different "
                "protocol (100 byte config, PS/2 scancodes) that this tool does "
                "not implement, and 2015+ firmware would brick it."
            )
        other = devices[0]
        raise DeviceError(
            "found %s (%04x:%04x), which shares the protocol but has a "
            "different pin layout. Only the I-PAC 2 is implemented."
            % (other.name, other.vendor, other.product)
        )

    if explicit_path:
        for dev in supported:
            if dev.path == explicit_path:
                return dev
        raise DeviceError("%s is not an I-PAC 2 config node" % explicit_path)

    return config_candidates(supported)[0]


def config_candidates(devices: list) -> list:
    """Order hidraw nodes by how likely they are to be the config interface.

    The firmware rule in Ultimarc-linux predates mode switching, and a board
    in Dinput mode presents four interfaces rather than three, so the rule is
    a starting guess and the rest get probed.
    """
    if not devices:
        return []
    wanted = config_interface_for(devices[0].bcd & 0xFF)
    return sorted(
        devices,
        key=lambda d: (d.interface != wanted, -d.interface),
    )


class Board:
    """A real board, reached through /dev/hidrawN."""

    MESSAGE_LENGTH = 1 + CHUNK  # report id + 4 config bytes

    # Output first, since that is what the board documents. Feature is kept as
    # a fallback so a stall does not need a second trip to the hardware.
    TRANSPORTS = (
        ("output report", _iowr("H", 0x0B, MESSAGE_LENGTH)),
        ("feature report", _iowr("H", 0x06, MESSAGE_LENGTH)),
    )

    def __init__(self, info: DeviceInfo, timeout=2.0):
        self.info = info
        self.timeout = timeout
        self.transport = None  # settles on whichever the board accepts
        try:
            self.fd = os.open(info.path, os.O_RDWR)
        except PermissionError:
            raise DeviceError(
                "permission denied opening %s - run as root (on Batocera you "
                "already are)" % info.path
            )
        except OSError as exc:
            raise DeviceError("cannot open %s: %s" % (info.path, exc))

    def close(self):
        if getattr(self, "fd", None) is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _send_feature(self, payload: bytes):
        import fcntl  # Linux only; imported late so the module loads anywhere

        buf = ctypes.create_string_buffer(bytes(payload), len(payload))

        candidates = (
            [self.transport] if self.transport else list(self.TRANSPORTS)
        )
        stalled = []
        for name, op in candidates:
            try:
                fcntl.ioctl(self.fd, op, buf, True)
            except OSError as exc:
                if exc.errno == errno.EPIPE:
                    # The device stalled the control transfer: it does not
                    # implement this report. Try the next kind, if any.
                    stalled.append(name)
                    continue
                raise DeviceError(
                    "%s while writing to %s: %s"
                    % (type(exc).__name__, self.info.path, exc)
                )
            if self.transport is None:
                self.transport = (name, op)
                if name != self.TRANSPORTS[0][0]:
                    print(
                        "note: board accepted a %s, not an %s"
                        % (name, self.TRANSPORTS[0][0]),
                        file=sys.stderr,
                    )
            return

        raise DeviceError(
            "%s stalled every request (tried: %s).\n"
            "That usually means this hidraw node is not the config interface. "
            "This board's config interface should be %d - check `ipacconf.py "
            "list`, then try the others explicitly:\n"
            "  for n in /dev/hidraw*; do echo \"== $n\"; %s --device $n dump "
            "| head -3; done"
            % (
                self.info.path,
                ", ".join(stalled),
                config_interface_for(self.info.bcd & 0xFF),
                os.path.basename(sys.argv[0]) or "ipacconf.py",
            )
        )

    def _send_block(self, buf: bytes):
        for frame in write_frames(buf):
            self._send_feature(frame)

    def read_config(self) -> bytes:
        """Ask the board for its config and read it back."""
        self._send_feature(bytes([REPORT_ID]) + bytes(HEADER_READ))

        out = bytearray()
        deadline = time.monotonic() + self.timeout
        while len(out) < CONFIG_SIZE:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([self.fd], [], [], remaining)
            if not ready:
                break
            data = os.read(self.fd, 512)
            if not data:
                continue
            # Every report carries its id, not just the first one.
            out += deframe(data)

        if len(out) < CONFIG_SIZE:
            raise DeviceError(
                "read %d of %d bytes from %s. If this is consistently short, "
                "the config interface may not be %d - try --device on the "
                "board's other hidraw nodes."
                % (len(out), CONFIG_SIZE, self.info.path, self.info.interface)
            )
        return bytes(out[:CONFIG_SIZE])

    def write_config(self, buf: bytes):
        if len(buf) != CONFIG_SIZE:
            raise ProtocolError("config must be %d bytes" % CONFIG_SIZE)
        self._send_block(buf)


class FakeBoard:
    """A board-shaped file, so the CLI and web UI work with no hardware."""

    def __init__(self, path):
        self.path = path
        self.info = DeviceInfo(
            path="fake:" + path,
            vendor=VENDOR_2015,
            product=PRODUCT_IPAC2,
            bcd=0x0044,
            interface=2,
            usb_path="fake",
        )
        if os.path.exists(path):
            self._buf = bytearray(load_raw(path))
            # Answer with the firmware the dump was taken from, so imports of
            # that dump do not warn about a mismatch that is not real.
            if self._buf[2] != HEADER_WRITE[2]:
                self.info.bcd = self._buf[2]
        else:
            self._buf = bytearray(default_config())
            self._flush()
        # A real board answers reads with its own header - 0x00 0x00 ver on
        # 1.44, 0x50 0xdd ver on 1.55 - whatever header the write carried.
        self._header = bytes(self._buf[:3])

    def _flush(self):
        with open(self.path, "w") as fh:
            json.dump(decode_config(bytes(self._buf)), fh, indent=2)
            fh.write("\n")

    def read_config(self) -> bytes:
        return bytes(self._buf)

    def write_config(self, buf: bytes):
        self._buf = bytearray(buf[:CONFIG_SIZE])
        self._buf[0:3] = self._header
        self._flush()

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def open_board(args):
    """Open the board, probing for whichever interface answers."""
    if getattr(args, "fake_device", None):
        return FakeBoard(args.fake_device)

    explicit = getattr(args, "device", None)
    if explicit:
        return Board(select_device(explicit))

    candidates = config_candidates([d for d in find_devices() if d.is_ipac2])
    if not candidates:
        select_device()  # raises with the right explanation
    if len(candidates) == 1:
        return Board(candidates[0])

    tried = []
    for info in candidates:
        board = Board(info, timeout=0.75)
        try:
            board.read_config()
        except (DeviceError, ProtocolError) as exc:
            board.close()
            tried.append("interface %d (%s)" % (info.interface, exc.__class__.__name__))
            continue
        board.timeout = 2.0
        return board

    hint = (
        "Hold Start1+P1SW1 for 10s, or hold P1SW1 while plugging in usb, to "
        "get back to keyboard mode, where the config interface is known good."
    )
    if any(info.disguised for info in candidates):
        hint = (
            "This board is in Xinput mode. It exposes a hid node there, but "
            "none of its interfaces answered - Ultimarc document the config "
            "interface as unreachable in Xinput, and this looks like that.\n"
        ) + hint
    raise DeviceError(
        "no interface answered a config read. Tried: %s.\n%s"
        % (", ".join(tried), hint)
    )
