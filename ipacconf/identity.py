"""Device identity: which usb ids are an I-PAC 2, and what mode each means.

A board's usb vendor/product pair is the only thing visible before anything
is opened, so mode detection lives here rather than in the device layer.
"""

from __future__ import annotations


VENDOR_2015 = 0xD209
PRODUCT_IPAC2 = 0x0420
VENDOR_PRE2015 = 0xD208
PRODUCT_PRE2015 = 0x0310

# In Xinput mode the board stops advertising itself as Ultimarc at all: it
# borrows a wired Xbox 360 pad's vendor and product ids, which is the only way
# the xpad driver will bind to it. Confirmed on hardware - the board appears as
# 045e:028e and every Ultimarc id disappears from lsusb.
VENDOR_XINPUT = 0x045E
PRODUCT_XINPUT = 0x028E

# Multi-mode firmware (1.50+) reports the board's current mode in its usb ids:
# switching re-enumerates it as a different device, which is why the mode is
# not in the config block at all. Xinput changes the vendor too, so these are
# keyed on the pair rather than on the product id alone.
#
# The usb identity names the device *class*, not which of the five modes the
# board is in - see MODE_HOTKEYS. Both Dinput modes present as 0421 and both
# Xinput modes as 045e:028e, as far as we can tell, so a d209:0421 board may
# be running its own config (mode 4) or the fixed preset map (mode 2) and the
# descriptor cannot say which.
IPAC2_MODES = {
    (VENDOR_2015, 0x0420): "keyboard",
    (VENDOR_2015, 0x0421): "Dinput game controller",
    (VENDOR_XINPUT, PRODUCT_XINPUT): "Xinput game controller",
}

MODE_KEYBOARD = "keyboard"

# Ultimarc's five modes, and the button held with Start1 for ten seconds to
# reach each. The distinction that matters is preset vs user set: the preset
# gamepad modes (2 and 3) run a fixed internal map and ignore the config block
# entirely, so a profile written by this tool has no effect in them. Only
# modes 1, 4 and 5 act on what we write.
#
# This is the reason a dump taken in "Dinput" looks like a stock map that has
# nothing to do with what was last written: it was almost certainly taken in
# mode 2, where the stock map is what the board is genuinely running.
#
# Source: the Multi-Mode tab on Ultimarc's I-PAC 2 product page - with one
# entry confirmed on hardware. The fourth field is whether the mode acts on
# the config; the fifth is what a board was actually observed to enumerate as,
# or None where nobody has looked yet.
MODE_HOTKEYS = [
    (1, "P1SW1", "keyboard", True, "d209:0420"),
    (2, "P1SW2", "Dinput preset", False, None),
    (3, "P1SW3", "Xinput preset", False, None),
    (4, "P1SW4", "Dinput user set", True, "d209:0421"),
    (5, "P1SW5", "Xinput user set", True, None),
]

# Mode 4 is Dinput, as Ultimarc document. Confirmed on a 1.55 board: the led
# flashes four times and it comes up d209:0421.
#
# An earlier run of the SAME hotkey on the SAME board produced 045e:028e -
# Xinput - and that was recorded here as a contradiction. It was not. What
# differed was the config the board was holding, so the mode a hotkey reaches
# is not a property of the hotkey alone.
#
# Two things changed between the two runs, and only one of them can be the
# cause:
#
#   1. The config went from a mostly-keyboard map to a gamepad-only one.
#   2. Byte 3 went from 0x02 to 0x00 - RECONFIGURE_BIT, left set in flash by
#      WinIPAC's Force Board Reconfiguration.
#
# (2) is the better fit, because Ultimarc's *only* documented use of Force
# Board Reconfiguration is their recipe for building a custom Xinput map:
# "Save the file as an Xinput configuration. Click File, Force Board
# Reconfiguration. The board should switch to Xinput mode using the custom
# configuration." That reads as the bit meaning "this config is an Xinput
# one", not "apply this now".
#
# Untested: the two changed together, so this is a hypothesis with a confound,
# which is exactly the shape of the last inference drawn about this bit that
# turned out to be wrong. The isolating test is in README.md.
MODE_HOTKEY_CONFLICTS = {}


def looks_ultimarc(manufacturer, product_name) -> bool:
    """True if the USB strings name a board that is hiding behind other ids.

    045e:028e is the genuine Microsoft Xbox 360 Controller id, shared with
    thousands of clones, so it can never identify a board on its own. What
    does is that the board keeps its own string descriptors while wearing it:
    confirmed on hardware, it still reports Ultimarc / I-PAC 2 in Xinput mode,
    where a real pad reports Microsoft / Controller. Nothing is sent to a
    045e:028e device unless these strings match.
    """
    text = ("%s %s" % (manufacturer or "", product_name or "")).lower()
    return "ultimarc" in text or "i-pac" in text or "ipac" in text


def board_mode(vendor, product, manufacturer=None, product_name=None):
    """Which mode this usb identity means, or None if it is not our board."""
    mode = IPAC2_MODES.get((vendor, product))
    if mode is None:
        return None
    if vendor == VENDOR_XINPUT and not looks_ultimarc(manufacturer, product_name):
        return None  # somebody's actual Xbox controller
    return mode

# Other 2015+ boards share this protocol but have different pin tables; we
# recognise them only to give a clear "not supported" message.
KNOWN_2015_PRODUCTS = {
    0x0420: "I-PAC 2",
    0x0421: "I-PAC 2",
    0x0430: "I-PAC 4",
    0x0440: "Mini-PAC",
    0x0450: "J-PAC",
}
