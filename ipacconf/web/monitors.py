"""One input monitor, shared by however many browsers are watching."""

from __future__ import annotations

import argparse
import json
import threading

from ..codec import decode_config
from ..device import open_board
from ..errors import DeviceError, ProtocolError
from ..inputs.monitor import open_monitor

def sse_frame(payload, name=None) -> bytes:
    """One server-sent event. Kept pure so the framing can be tested."""
    head = "event: %s\n" % name if name else ""
    return ("%sdata: %s\n\n" % (head, json.dumps(payload))).encode()


SSE_HEARTBEAT = 10.0  # seconds between keepalives on an idle stream


class MonitorHolder:
    """One input monitor, shared by however many browsers are watching.

    Reference counted, so the last tab to close is what releases an exclusive
    grab. A stream that dies with the tab is only noticed on the next
    heartbeat write, so that release can lag by up to SSE_HEARTBEAT.
    """

    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.monitor = None
        self.options = None
        self.users = 0

    def acquire(self, grab, all_devices):
        wanted = (bool(grab), bool(all_devices))
        with self.lock:
            if self.monitor is not None and self.options != wanted:
                if self.users:
                    raise DeviceError(
                        "another browser is already watching with different "
                        "options. Stop watching there first, or match its "
                        "settings."
                    )
                self._stop()
            if self.monitor is None:
                options = argparse.Namespace(**vars(self.args))
                options.grab, options.all_devices = wanted
                monitor = open_monitor(options, profile=self._profile())
                monitor.start()
                self.monitor, self.options = monitor, wanted
            self.users += 1
            return self.monitor

    def release(self):
        with self.lock:
            self.users = max(0, self.users - 1)
            if not self.users:
                self._stop()

    def refresh(self, profile):
        """Point the monitor at a config that has just been written."""
        with self.lock:
            if self.monitor is not None:
                self.monitor.profile = profile

    def _profile(self):
        try:
            with open_board(self.args) as board:
                return decode_config(board.read_config())
        except (DeviceError, ProtocolError):
            # Worth watching anyway: raw codes still say *something* arrived,
            # which separates "wrong pin" from "nothing is getting through".
            return None

    def _stop(self):
        monitor, self.monitor, self.options = self.monitor, None, None
        if monitor is not None:
            monitor.close()
