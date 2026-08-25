#!/usr/bin/env python3
"""
analyse_capture - find the I-PAC's config messages in a USB capture.

The point of a capture is one specific unknown: the message WinIPAC sends for
*File -> Force Board Reconfiguration*. Everything else in the protocol is
already implemented, so this reads a capture, picks out the board's config
traffic, and reports anything that is not a write (header 0x50) or a read
(header 0x59). That short list is the answer.

It does not parse pcap. The input is tshark's field output, which is the same
whether the capture came from Linux usbmon or Windows USBPcap:

    tshark -r capture.pcapng -T fields -E header=y -E separator=, \\
      -e frame.number -e frame.time_relative \\
      -e usb.bus_id -e usb.device_address \\
      -e usb.urb_id -e usb.irp_id -e usb.urb_type \\
      -e usb.bmRequestType -e usb.setup.bRequest -e usb.setup.wValue \\
      -e usb.setup.wIndex -e usb.setup.wLength \\
      -e usb.capdata -e usb.data_fragment -e usb.control.Data \\
      > capture.csv
    python3 analyse_capture.py capture.csv

Column order does not matter; the header line is what is read. Missing columns
are tolerated, so a shorter -e list still works.

Two things matter when capturing on a Linux host while WinIPAC drives the
board from a VM. Both are handled here, but they are worth knowing:

  * usbmon logs each transfer twice - once as the URB is submitted and once as
    it completes. The submission of a control OUT carries the data and the
    completion does not, so messages are deduplicated by URB id.

  * The board re-enumerates when its mode changes, which gives it a NEW usb
    device address. A capture of a mode switch therefore spans two or more
    addresses, and --address must be given all of them (or left off). The
    address change is itself the most useful marker in the file: whatever was
    sent immediately before it is what caused the switch.

See README.md, "Force Board Reconfiguration". Captures are gitignored: read
one before committing it.
"""

from __future__ import annotations

import argparse
import collections
import csv
import sys

REPORT_ID = 0x03
CHUNK = 4
MESSAGE = 1 + CHUNK  # report id + four config bytes

# The two headers we already understand. A third one is what we are looking for.
KNOWN_HEADERS = {
    0x50: "write (config download)",
    0x59: "read (config request)",
}

# Columns holding a hex payload, in the order they are preferred. tshark puts
# the bytes in a different field depending on capture source and version, so
# ask for several and take the first that has anything.
PAYLOAD_FIELDS = (
    "usb.capdata",
    "usb.data_fragment",
    "usb.control.Data",
    "usb.setup.data",
    "data.data",
)

# The board's ANSWERS come back as interrupt IN reports on endpoint 0x84, and
# Wireshark hands those to the HID dissector - so they land in usbhid.data and
# in none of the fields above. Missing them costs a lot: three reads in a
# WinIPAC session reassemble into the board's config before and after every
# write, which is what makes a capture diffable down to the single byte a pin
# change moved.
RESPONSE_FIELDS = ("usbhid.data", "usb.capdata", "usb.data_fragment")

# What ipacconf puts in bytes 256-259 of a download, copied from WinIPAC.
EXPECTED_TAIL = bytes([0x59, 0xDD, 0x0F, 0x00])
CONFIG_IN_ENDPOINT = "0x84"
CONFIG_SIZE = 256

# usbmon calls it urb_id, USBPcap calls it irp_id. Either one identifies the
# submit/complete pair that a single transfer produces, so it is what lets one
# transfer be counted once.
URB_ID_FIELDS = ("usb.urb_id", "usb.irp_id")


def parse_hex(text: str) -> bytes:
    """tshark writes payloads as 03:50:dd:0f:00, sometimes space separated."""
    cleaned = text.strip().replace(":", "").replace(" ", "").replace(".", "")
    if not cleaned:
        return b""
    if len(cleaned) % 2:
        return b""
    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        return b""


def as_int(text: str):
    """tshark emits numbers as decimal, 0x-prefixed hex, or empty."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return int(text, 0)
    except ValueError:
        return None


class Message:
    """One five-byte config message, with where it came from."""

    __slots__ = ("frame", "time", "bus", "address", "payload", "windex", "urb")

    def __init__(self, frame, time, address, payload, windex, bus=None, urb=None):
        self.frame = frame
        self.time = time
        self.bus = bus
        self.address = address
        self.payload = payload
        self.windex = windex
        self.urb = urb

    @property
    def header(self) -> int:
        return self.payload[1]

    @property
    def body(self) -> bytes:
        return self.payload[1:]

    def hex(self) -> str:
        return " ".join("%02x" % b for b in self.payload)


def read_messages(path: str, addresses=None) -> list:
    """Every five-byte report-id-3 message in the capture, in order.

    Only the shape is matched, not the device: a capture with just the board
    on it has nothing else to confuse, and a busy one is narrowed with
    --address. Deduplicated by URB id, because usbmon logs every transfer
    twice and would otherwise double every message.
    """
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise SystemExit("no rows in %s - was -E header=y passed to tshark?" % path)

    present = [f for f in PAYLOAD_FIELDS if f in rows[0]]
    if not present:
        raise SystemExit(
            "none of the payload columns are present (%s).\n"
            "Found: %s\n"
            "Re-run tshark with at least -e usb.capdata."
            % (", ".join(PAYLOAD_FIELDS), ", ".join(sorted(rows[0])))
        )

    urb_fields = [f for f in URB_ID_FIELDS if f in rows[0]]
    previous_key = None

    out = []
    for row in rows:
        payload = b""
        for field in present:
            payload = parse_hex(row.get(field) or "")
            if payload:
                break
        if len(payload) != MESSAGE or payload[0] != REPORT_ID:
            continue
        addr = as_int(row.get("usb.device_address", ""))
        if addresses and addr not in addresses:
            continue

        # One transfer, one message. usbmon reports the submission and the
        # completion of the same URB, and a capture that carries data on both
        # would double every burst.
        #
        # Only *adjacent* repeats are dropped. A urb id is a kernel pointer,
        # and the kernel recycles them: in a real 65 message download the same
        # id comes back dozens of messages later carrying the same bytes,
        # which an id-and-payload set would silently swallow. Confirmed on a
        # real capture - six legitimate `03 00 00 00 00` messages went missing
        # that way, turning two full downloads into 63 and 61 messages.
        urb = ""
        for field in urb_fields:
            urb = (row.get(field) or "").strip()
            if urb:
                break
        key = (urb, payload) if urb else None
        if key is not None and key == previous_key:
            continue
        previous_key = key

        out.append(
            Message(
                frame=as_int(row.get("frame.number", "")),
                time=(row.get("frame.time_relative") or "").strip(),
                address=addr,
                payload=payload,
                windex=as_int(row.get("usb.setup.wIndex", "")),
                bus=as_int(row.get("usb.bus_id", "")),
                urb=urb or None,
            )
        )
    return out


def describe_capture(path: str) -> str:
    """What the file actually holds, for when none of it is ours.

    "No config messages" has two very different causes - a capture that
    recorded the wrong bus and holds nothing, versus one that holds plenty
    but not from the board. Telling them apart is the difference between
    re-capturing and re-filtering.
    """
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return "The file has no rows at all: the capture recorded nothing."

    present = [f for f in PAYLOAD_FIELDS if f in rows[0]]
    sizes = collections.Counter()
    addresses = collections.Counter()
    buses = collections.Counter()
    with_payload = 0
    for row in rows:
        addr = row.get("usb.device_address", "").strip()
        if addr:
            addresses[addr] += 1
        bus = row.get("usb.bus_id", "").strip()
        if bus:
            buses[bus] += 1
        for field in present:
            payload = parse_hex(row.get(field) or "")
            if payload:
                with_payload += 1
                sizes[len(payload)] += 1
                break

    out = ["%d packet(s) in the capture, %d carrying data." % (len(rows), with_payload)]
    if buses:
        out.append("  usb buses:     %s" % ", ".join(
            "%s (%d)" % (b, n) for b, n in buses.most_common(8)))
    if addresses:
        out.append("  device addrs:  %s" % ", ".join(
            "%s (%d)" % (a, n) for a, n in addresses.most_common(12)))
    if sizes:
        out.append("  payload sizes: %s" % ", ".join(
            "%d bytes x%d" % (size, n) for size, n in sorted(sizes.items())[:10]))
    if with_payload == 0:
        out.append(
            "\nNothing in this file carries a payload, so the capture caught no "
            "traffic at all.\nThat is a capture problem, not a filtering one - "
            "see README.md, 'Nothing on usbmonN'."
        )
    elif 5 not in sizes:
        out.append(
            "\nThere is traffic here, but none of it is a five byte message. "
            "The board's config\nprotocol is always five bytes (report id 0x03 "
            "plus four), so this capture saw\nother devices but not the board - "
            "most likely the wrong bus."
        )
    return "\n".join(out)


def read_responses(path: str) -> list:
    """The 256 byte configs the board sent back, in order.

    A WinIPAC session holds more of these than you would expect: one per
    explicit read, plus one per download, because a download ends with a read
    header and the board answers it. Diffing consecutive answers shows exactly
    what each write changed, which no amount of staring at the writes will.
    """
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return []

    present = [f for f in RESPONSE_FIELDS if f in rows[0]]
    has_endpoint = "usb.endpoint_address" in rows[0]

    buf = bytearray()
    for row in rows:
        if has_endpoint:
            endpoint = (row.get("usb.endpoint_address") or "").strip()
            if endpoint.lower() != CONFIG_IN_ENDPOINT:
                continue
        payload = b""
        for field in present:
            payload = parse_hex(row.get(field) or "")
            if payload:
                break
        if len(payload) != MESSAGE or payload[0] != REPORT_ID:
            continue
        buf += payload[1:]

    return [bytes(buf[i:i + CONFIG_SIZE])
            for i in range(0, len(buf) - CONFIG_SIZE + 1, CONFIG_SIZE)]


def diff_responses(configs: list) -> list:
    """(index, offset, before, after) for every byte that moved between reads."""
    out = []
    for i, (before, after) in enumerate(zip(configs, configs[1:])):
        for off in range(min(len(before), len(after))):
            if before[off] != after[off]:
                out.append((i, off, before[off], after[off]))
    return out


def group_blocks(messages: list, gap=1.0) -> list:
    """Split the message stream into bursts.

    A config download is 65 messages back to back; a read request is one on
    its own. Anything else is what we are here for, and seeing it as its own
    burst is most of the identification.
    """
    blocks = []
    current = []
    last = None
    for msg in messages:
        try:
            when = float(msg.time)
        except (TypeError, ValueError):
            when = None
        if current and last is not None and when is not None and when - last > gap:
            blocks.append(current)
            current = []
        current.append(msg)
        if when is not None:
            last = when
    if current:
        blocks.append(current)
    return blocks


def describe(header: int) -> str:
    return KNOWN_HEADERS.get(header, "UNKNOWN - candidate")


def reassemble(block: list) -> bytes:
    """The config bytes a burst carried, headers included.

    Each message contributes its four payload bytes, so a 65 message download
    reassembles to the 260 bytes ipacconf sends: 256 of config and four more
    whose purpose is an open question - see README.md.
    """
    out = bytearray()
    for msg in block:
        out += msg.body
    return bytes(out)


def report(messages: list, verbose=False, gap=1.0, path=None) -> int:
    if not messages:
        print(
            "No five-byte report-id-3 messages found.\n"
            "Either the capture holds no config traffic, or the payload landed "
            "in a column that was not requested. Try adding -e usb.data_fragment "
            "and -e usb.control.Data to the tshark line, and check the capture "
            "covers the moment WinIPAC talked to the board."
        )
        return 1

    blocks = group_blocks(messages, gap)

    # Only the FIRST message of a burst carries a header byte. The other 64 of
    # a download carry config, whose second byte is whatever the config says -
    # reading those as headers turns one candidate into twenty.
    print("%d config message(s) in %d burst(s)\n" % (len(messages), len(blocks)))

    print("  #   frames          addr  msgs  bytes  header  meaning")
    print("  --  --------------  ----  ----  -----  ------  -------")
    for i, block in enumerate(blocks, 1):
        header = block[0].header
        note = describe(header)
        if header == 0x50 and len(block) != 65:
            note += " - TRUNCATED, expected 65 messages"
        elif header not in KNOWN_HEADERS and len(block) > 1:
            note += " (multi-message)"
        addrs = sorted({m.address for m in block if m.address is not None})
        shown = ",".join(str(a) for a in addrs) or "?"
        print("  %2d  %-14s  %-4s  %4d  %5d    0x%02x  %s"
              % (i, "%s-%s" % (block[0].frame, block[-1].frame), shown,
                 len(block), len(block) * CHUNK, header, note))

    # A mode change re-enumerates the board, which gives it a new usb device
    # address. That boundary is the most reliable marker in the whole file:
    # whatever went out just before it is what caused the switch.
    addressed = [b for b in blocks
                 if any(m.address is not None for m in b)]
    changes = []
    for prev, nxt in zip(addressed, addressed[1:]):
        before = next(m.address for m in prev if m.address is not None)
        after = next(m.address for m in nxt if m.address is not None)
        if before != after:
            changes.append((blocks.index(prev) + 1, before,
                            blocks.index(nxt) + 1, after))
    if changes:
        print("\nRE-ENUMERATION - the board changed usb address, so its mode "
              "changed:")
        for prev_i, before, next_i, after in changes:
            print("  between burst %d (address %d) and burst %d (address %d)"
                  % (prev_i, before, next_i, after))
        print("  The last thing sent before each of those is the candidate, "
              "whatever its header.")
    elif len({m.address for m in messages if m.address is not None}) <= 1:
        print("\nOne usb address throughout: the board never re-enumerated, so "
              "no mode change\nhappened in this capture. If the point was to "
              "record a mode switch, the capture\nmissed it - check the mode "
              "actually changed while recording.")

    # The trailing four bytes of a download are an open question: Ultimarc-linux
    # zero-pads them for the I-PAC 2 and writes 0x59 0xdd 0x0f for the JPAC.
    # A real WinIPAC download settles it, so say what this one carried.
    for i, block in enumerate(blocks, 1):
        if block[0].header == 0x50 and len(block) == 65:
            data = reassemble(block)
            tail = data[256:260]
            print("\nburst %d is a full download. Its trailing four bytes are "
                  "%s" % (i, " ".join("%02x" % b for b in tail)))
            if tail == EXPECTED_TAIL:
                print("  a read header - the board is being asked to re-read "
                      "its config, and\n  answers it. This is what ipacconf "
                      "sends (WRITE_TAIL).")
            elif tail == b"\x00\x00\x00\x00":
                print("  zero padding. ipacconf sends a read header (59 dd 0f "
                      "00) here, copied from\n  WinIPAC - so this capture "
                      "disagrees and is worth a second look.")
            else:
                print("  neither zero padding nor the read header ipacconf "
                      "sends. Unexpected;\n  worth recording.")

    responses = read_responses(path) if path else []
    if responses:
        print("\n%d config(s) read back from the board." % len(responses))
        changes = diff_responses(responses)
        if changes:
            print("What changed between them - this is what each write did:")
            for i, off, before, after in changes:
                print("  read %d -> %d   offset %3d:  0x%02x -> 0x%02x"
                      % (i + 1, i + 2, off, before, after))
        elif len(responses) > 1:
            print("They are all identical: nothing the board reports changed.")
        print("Decode one with:  ipacconf.py restore, or decode_config() on "
              "the bytes.")

    candidates = [b for b in blocks if b[0].header not in KNOWN_HEADERS]
    if candidates:
        heads = sorted({b[0].header for b in candidates})
        print("\n%s\nCANDIDATES: %s\n%s"
              % ("=" * 64,
                 ", ".join("0x%02x" % h for h in heads),
                 "=" * 64))
        for i, block in enumerate(blocks, 1):
            if block[0].header in KNOWN_HEADERS:
                continue
            print("\nburst %d - %d message(s), header 0x%02x:"
                  % (i, len(block), block[0].header))
            for msg in block[:20]:
                print("  frame %-8s t=%-10s wIndex=%s  %s"
                      % (msg.frame, msg.time or "?",
                         "?" if msg.windex is None else msg.windex, msg.hex()))
            if len(block) > 20:
                print("  ... %d more" % (len(block) - 20))
            if len(block) > 1:
                data = reassemble(block)
                print("  reassembled (%d bytes): %s"
                      % (len(data), data[:32].hex()))
        print(
            "\nThat is the message to implement. Capture the same action twice "
            "and diff:\na byte that differs between switching to Dinput and "
            "switching to keyboard is\nthe mode selector; one that does not is "
            "part of the command."
        )
    else:
        print(
            "\nNo unknown header - every burst is a write (0x50) or a read "
            "(0x59). That means\nForce Board Reconfiguration is not a separate "
            "message: it is a plain config\ndownload, and the mode change is "
            "the firmware reacting to its content.\n"
            "A real result, and the one that says this tool is missing nothing."
        )

    if verbose:
        print("\nfull message list:")
        for msg in messages:
            print("  frame %-8s t=%-10s %s" % (msg.frame, msg.time or "?", msg.hex()))

    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Find the I-PAC's config messages in a tshark field dump.",
        epilog="See the module docstring for the tshark command line.",
    )
    parser.add_argument("capture", help="csv from tshark -T fields -E header=y")
    parser.add_argument(
        "--address", default=None,
        help="comma separated usb.device_address values to keep. A mode switch "
             "re-enumerates the board onto a NEW address, so pass every one it "
             "used, or leave this off entirely",
    )
    parser.add_argument(
        "--gap", type=float, default=1.0,
        help="seconds of silence that ends a burst (default 1.0)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="list every message",
    )
    args = parser.parse_args(argv)
    addresses = None
    if args.address:
        addresses = {int(part, 0) for part in args.address.split(",") if part.strip()}
    messages = read_messages(args.capture, addresses)
    if not messages:
        # Two very different failures wear the same "no messages" face, so
        # say which one this is rather than listing both.
        print("No config messages found.\n")
        print(describe_capture(args.capture))
        if addresses:
            print("\n--address %s was in force. A mode switch moves the board "
                  "to a NEW address,\nso try without it."
                  % ",".join(str(a) for a in sorted(addresses)))
        return 1
    return report(messages, args.verbose, args.gap, args.capture)


if __name__ == "__main__":
    sys.exit(main())
