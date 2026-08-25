"""Wire protocol: report sizes, headers, config bits and HID framing.

Pure constants plus the two functions that turn a config buffer into HID
reports and back. Nothing here opens a device.
"""

from __future__ import annotations


REPORT_ID = 0x03
CONFIG_SIZE = 256  # what a read returns: 4 byte header + 252 data bytes
CHUNK = 4  # config is sent 4 bytes at a time, in 5 byte output reports

# A write is four bytes longer than a read response. Ultimarc-linux sends
# IPACSERIES_SIZE (260) - see ipac.c, which callocs 260 and passes that to
# writeIPACSeriesUSB - and the board waits for all 65 messages before
# committing to flash. Sending only 256 is accepted message by message and
# then silently discarded, which is exactly how this was found.
WRITE_SIZE = 260

HEADER_WRITE = (0x50, 0xDD, 0x0F)  # 4th byte is the config bitfield
HEADER_READ = (0x59, 0xDD, 0x0F, 0x00)

# The four bytes past the 256 byte config. Captured from WinIPAC on a 1.55
# board: its downloads end with a read header, not zero padding. Ultimarc-linux
# does the same on its JPAC path (ipac.c writes 0x59 0xdd 0x0f into barray[256]
# ..[258]) and zero-pads for the I-PAC 2, which is where this tool got its
# zeros from. WinIPAC is the reference implementation, so follow WinIPAC.
WRITE_TAIL = bytes(HEADER_READ)

# Bit 1 of the config bitfield: "this config is an Xinput one".
#
# Ultimarc-linux calls it accelerometer_uio and QtPyUltimarc calls it
# accelerometer - a field belonging to the Ultimate I/O, the only board in the
# family that has one. An I-PAC 2 does not, and on an I-PAC 2 it selects
# Xinput. Worked out in three steps, each of which corrected the last:
#
#   1. Captured from WinIPAC: an ordinary write sends byte 3 clear, and
#      *File -> Force Board Reconfiguration* sends the same 260 bytes with bit
#      1 set. Read at the time as "apply this download now".
#   2. That was wrong. Setting it did not make a 1.55 board act on a
#      gamepad-only download, and the bit persisted in flash rather than being
#      consumed - so it is a stored setting, not a command.
#   3. Confirmed on hardware: writing a gamepad-only config with the bit set,
#      from keyboard mode, takes the board to XINPUT. Batocera, watching at
#      the time, reported a "Microsoft Xbox controller" connecting - which is
#      045e:028e, the identity the board wears in Xinput. Holding
#      Start1+P1SW4 then moved it to Dinput (d209:0421), and the same hotkey
#      with the bit clear had given Dinput directly.
#
# Which is exactly what Ultimarc document, once the menu item is read as what
# it is used for rather than what it is called: their only recipe involving
# Force Board Reconfiguration is the one for building a custom Xinput map -
# "Save the file as an Xinput configuration. Click File, Force Board
# Reconfiguration. The board should switch to Xinput mode using the custom
# configuration."
#
# It is ordinary config, so it is preserved across a read-modify-write like
# debounce and paclink. It is also the one config bit that can make the board
# unreachable - Xinput exposes no hid interface - so a write that would set it
# says so first.
XINPUT_BIT = 0x02

# Bit 6 of a pin's shift byte marks it as the shift key. Real boards carry
# 0x01 in the low bits of every pin's shift byte and 0x41 on the shift pin, so
# this is set and cleared as a bit rather than written as a whole byte.
SHIFT_BIT = 0x40

MACRO_START = 166  # index into the 252 byte data array
MACRO_MAX_COUNT = 30
MACRO_MAX_SIZE = 85
MACRO_FIRST_CODE = 0xE0  # macros are referenced by codes 0xe0..0xfe
MACRO_LAST_CODE = 0xFE

DEBOUNCE = {"standard": 0, "none": 1, "short": 2, "long": 3}

def write_frames(buf: bytes) -> list:
    """The 5-byte messages that carry a config to the board."""
    padded = bytes(buf[:CONFIG_SIZE]).ljust(CONFIG_SIZE, b"\x00") + WRITE_TAIL
    return [
        bytes([REPORT_ID]) + padded[pos:pos + CHUNK]
        for pos in range(0, WRITE_SIZE, CHUNK)
    ]


def deframe(chunk: bytes) -> bytes:
    """Strip report-id prefixes from one or more concatenated HID reports.

    The board answers in 5-byte reports: [0x03, b0, b1, b2, b3]. A read that
    keeps those ids inline leaves an 0x03 every five bytes, which is how this
    bug announced itself.
    """
    size = 1 + CHUNK
    out = bytearray()
    for pos in range(0, len(chunk), size):
        report = chunk[pos:pos + size]
        if report and report[0] == REPORT_ID:
            report = report[1:]
        out += report
    return bytes(out)
