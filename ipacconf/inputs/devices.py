"""Finding the /dev/input/eventN nodes that belong to a board."""

from __future__ import annotations

import glob
import os
import re

from ..identity import board_mode
from ..linux import _read_sysfs

class InputDevice:
    """One /dev/input/eventN node belonging to a board we care about."""

    def __init__(self, path, name, vendor, product, interface, joystick,
                 manufacturer=None, product_name=None):
        self.path = path
        self.name = name
        self.vendor = vendor
        self.product = product
        self.interface = interface
        self.joystick = joystick
        self.manufacturer = manufacturer
        self.product_name = product_name
        self.player = None  # filled in for joystick nodes, in interface order

    @property
    def node(self):
        return os.path.basename(self.path)

    @property
    def ours(self):
        """Whether this node belongs to the board.

        In Xinput mode the kernel binds xpad and names the node after an Xbox
        pad, so the evdev name is no help; the usb strings behind it are.
        """
        return board_mode(self.vendor, self.product,
                          self.manufacturer, self.product_name) is not None

    def as_dict(self):
        return {
            "path": self.path,
            "node": self.node,
            "name": self.name,
            "interface": self.interface,
            "player": self.player,
            "ours": self.ours,
        }


def _ancestor_with(path: str, filename: str, limit: int = 8):
    """Walk up from `path` for a directory holding `filename`."""
    current = path
    for _ in range(limit):
        parent = os.path.dirname(current)
        if not parent or parent == current or parent == "/":
            return None
        current = parent
        if os.path.exists(os.path.join(current, filename)):
            return current
    return None


def find_input_devices(all_devices=False, sys_root="/sys") -> list:
    """Event nodes for the board, or for everything if all_devices."""
    found = []
    pattern = os.path.join(sys_root, "class", "input", "event*")
    for node in sorted(glob.glob(pattern), key=lambda p: _node_index(p)):
        dev_dir = os.path.realpath(os.path.join(node, "device"))
        vendor = _read_sysfs(os.path.join(dev_dir, "id", "vendor"))
        product = _read_sysfs(os.path.join(dev_dir, "id", "product"))
        if vendor is None or product is None:
            continue
        try:
            vendor, product = int(vendor, 16), int(product, 16)
        except ValueError:
            continue
        usb_dir = _ancestor_with(dev_dir, "idVendor")
        manufacturer = product_name = None
        if usb_dir:
            manufacturer = _read_sysfs(os.path.join(usb_dir, "manufacturer"))
            product_name = _read_sysfs(os.path.join(usb_dir, "product"))

        ours = board_mode(vendor, product, manufacturer, product_name) is not None
        if not ours and not all_devices:
            continue

        iface_dir = _ancestor_with(dev_dir, "bInterfaceNumber")
        interface = -1
        if iface_dir:
            try:
                interface = int(_read_sysfs(
                    os.path.join(iface_dir, "bInterfaceNumber"), "-1"), 16)
            except ValueError:
                interface = -1

        # A node with absolute axes is a stick or pad rather than the
        # keyboard, which is what tells the two Dinput players apart.
        abs_caps = _read_sysfs(os.path.join(dev_dir, "capabilities", "abs"), "0")
        joystick = any(int(word, 16) for word in (abs_caps or "0").split() if word)

        found.append(
            InputDevice(
                path=os.path.join("/dev", "input", os.path.basename(node)),
                name=_read_sysfs(os.path.join(dev_dir, "name"), "unknown device"),
                vendor=vendor,
                product=product,
                interface=interface,
                joystick=joystick,
                manufacturer=manufacturer,
                product_name=product_name,
            )
        )

    pads = sorted([d for d in found if d.joystick and d.ours],
                  key=lambda d: (d.interface, d.path))
    for index, dev in enumerate(pads):
        dev.player = index + 1
    return found


def _node_index(path: str) -> int:
    match = re.search(r"(\d+)$", path)
    return int(match.group(1)) if match else 0
