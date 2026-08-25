"""The monitors themselves: a ring buffer, a real reader, and a fake one."""

from __future__ import annotations

import errno
import json
import os
import queue
import select
import sys
import threading
import time

from ..codes import code_to_name, name_to_code
from ..errors import DeviceError, ProtocolError
from ..identity import PRODUCT_IPAC2, VENDOR_2015
from ..linux import _iow
from .devices import InputDevice, find_input_devices
from .events import event_action, parse_input_events, pins_for_action
from .keymap import (
    BOARD_TO_LINUX,
    EVIOCGRAB_NR,
    EV_ABS,
    EV_KEY,
    EV_SYN,
    INPUT_EVENT_SIZE,
)

EVENT_BUFFER = 200


class EventStream:
    """Ring buffer plus subscriber fan-out. No I/O, so tests can drive it."""

    def __init__(self, size=EVENT_BUFFER):
        self.size = size
        self._lock = threading.Lock()
        self._events = []
        self._seq = 0
        self._subscribers = set()

    def publish(self, event: dict) -> dict:
        with self._lock:
            self._seq += 1
            event = dict(event, seq=self._seq)
            self._events.append(event)
            if len(self._events) > self.size:
                del self._events[: -self.size]
            subscribers = list(self._subscribers)
        for sub in subscribers:
            try:
                sub.put_nowait(event)
            except queue.Full:
                pass  # a stalled reader loses events; the board never waits
        return event

    def since(self, seq: int) -> list:
        with self._lock:
            return [e for e in self._events if e["seq"] > seq]

    @property
    def latest(self) -> int:
        with self._lock:
            return self._seq

    def subscribe(self, maxsize=256) -> queue.Queue:
        sub = queue.Queue(maxsize)
        with self._lock:
            self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub) -> None:
        with self._lock:
            self._subscribers.discard(sub)


class BaseMonitor:
    """Shared translation and lifecycle. Subclasses provide the events."""

    def __init__(self, devices, stream=None, profile=None):
        self.devices = list(devices)
        self.stream = stream or EventStream()
        self.profile = profile
        self.error = None
        self._last = {}  # (node, axis) -> the last value it reported
        # Player attribution is only meaningful when the board actually
        # presents two pads. With one, every event carries player 1 and a code
        # both players share gets narrowed to the player 1 pin - a guess
        # dressed as a fact, and one that names the wrong pin every time the
        # other player presses something.
        self._can_tell_players_apart = len(
            [d for d in self.devices if getattr(d, "player", None)]) > 1
        self._muted = set()  # (node, type) already reported as unreadable
        self._thread = None
        self._stop = threading.Event()

    # -- lifecycle

    def start(self):
        self._open()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def close(self):
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    def _open(self):
        pass

    def _close(self):
        pass

    def _run(self):
        raise NotImplementedError("a monitor subclass provides the events")

    # -- translation

    def translate(self, device, etype, code, value):
        """One evdev event as a payload dict, or None if not worth reporting."""
        if etype == EV_SYN:
            return None
        if etype == EV_KEY and value == 2:
            return None  # autorepeat, which would flood a held button

        kind, board_code = event_action(
            etype, code, getattr(device, "player", None))

        muted = False
        if kind == "other":
            # An event type we have no reading of - in practice the EV_MSC scan
            # code the kernel raises alongside every single key event, so
            # reporting each one doubles the log and buries the presses. Say so
            # once per node and type, then drop the rest. This lives on the
            # monitor, so it is once per watching session rather than per
            # subscriber: a browser joining a stream already running sees no
            # such line.
            key = (device.node, etype)
            if key in self._muted:
                return None
            self._muted.add(key)
            muted = True

        if etype == EV_ABS:
            # An axis has no press and no release, so held stays None and the
            # value is what gets reported.
            #
            # This used to guess a resting point - "the first value seen counts
            # as not held". An evdev axis only emits when it CHANGES, so the
            # first event is always a press, which made the press define rest,
            # get swallowed as a non-event, and the release then read as a
            # press. Every axis line was the wrong edge, and every first press
            # was missing. Confirmed on hardware while mapping a stick.
            key = (device.node, code)
            if self._last.get(key) == value:
                return None  # nothing actually changed
            self._last[key] = value
            held = None
        else:
            held = value != 0

        name = code_to_name(board_code) if board_code is not None else None
        player = device.player if kind in ("gamepad", "hat", "analog") else None
        narrow_by = player if self._can_tell_players_apart else None
        return {
            "ts": time.time(),
            "device": device.path,
            "node": device.node,
            "source": device.name,
            "player": player,
            "kind": kind,
            "raw": code,
            "type": etype,
            "value": value,
            "held": held,
            "name": name,
            "code": board_code,
            "muted": muted,
            "pins": pins_for_action(self.profile, name, narrow_by),
        }

    def _emit(self, device, etype, code, value):
        event = self.translate(device, etype, code, value)
        if event is not None:
            self.stream.publish(event)


class InputMonitor(BaseMonitor):
    """Reads the board's evdev nodes in a background thread."""

    POLL = 0.25  # how often the loop notices it has been asked to stop
    BATCH = 64  # events per read()

    def __init__(self, devices, grab=False, stream=None, profile=None):
        super().__init__(devices, stream=stream, profile=profile)
        self.grab = grab
        self._fds = {}

    def _open(self):
        import fcntl  # Linux only; imported late so the module loads anywhere

        if not self.devices:
            raise DeviceError(
                "no input devices to watch - the board is attached (the "
                "config read works) but no /dev/input/event node belongs to "
                "it. Check `ls /dev/input/by-id | grep -i ultimarc`."
            )
        for dev in self.devices:
            try:
                fd = os.open(dev.path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError as exc:
                self._close()
                raise DeviceError("cannot open %s: %s" % (dev.path, exc))
            self._fds[fd] = dev
            if not self.grab:
                continue
            try:
                fcntl.ioctl(fd, _iow("E", EVIOCGRAB_NR, 4), 1)
            except OSError as exc:
                self._close()
                raise DeviceError(
                    "cannot take exclusive control of %s: %s. Something else "
                    "already holds it - stop the other reader, or watch "
                    "without exclusive capture." % (dev.path, exc)
                )

    def _close(self):
        for fd in list(self._fds):
            # Closing the fd is what releases any grab, so there is nothing
            # else to undo here.
            try:
                os.close(fd)
            except OSError:
                pass
            del self._fds[fd]

    def _run(self):
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select(list(self._fds), [], [], self.POLL)
            except (OSError, ValueError):
                return  # the fds went away under us, which means close()
            for fd in ready:
                device = self._fds.get(fd)
                if device is None:
                    continue
                try:
                    blob = os.read(fd, INPUT_EVENT_SIZE * self.BATCH)
                except OSError as exc:
                    if exc.errno in (errno.EAGAIN, errno.EINTR):
                        continue
                    self.error = "%s: %s" % (device.path, exc)
                    return
                for _sec, _usec, etype, code, value in parse_input_events(blob):
                    self._emit(device, etype, code, value)


class FakeInputMonitor(BaseMonitor):
    """Replays a JSONL script, so the UI can be built without a cabinet.

    Each line is one event. Either name a board action, which is turned back
    into the keycode the kernel would have reported so the whole translation
    path is exercised:

        {"after": 0.4, "action": "5", "value": 1}

    or give the raw evdev numbers directly:

        {"after": 0.1, "type": 3, "code": 16, "value": -1}
    """

    def __init__(self, path, stream=None, profile=None, loop=True):
        self.path = path
        self.loop = loop
        super().__init__([_fake_device(path)], stream=stream, profile=profile)
        self.script = self._load()

    def _load(self):
        steps = []
        with open(self.path) as handle:
            for number, line in enumerate(handle, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    step = json.loads(line)
                except ValueError as exc:
                    raise ProtocolError(
                        "%s line %d: %s" % (self.path, number, exc)
                    )
                if "action" in step:
                    code = BOARD_TO_LINUX.get(name_to_code(step["action"]))
                    if code is None:
                        raise ProtocolError(
                            "%s line %d: %r is not something a keyboard can "
                            "send" % (self.path, number, step["action"])
                        )
                    step.setdefault("type", EV_KEY)
                    step["code"] = code
                steps.append(step)
        if not steps:
            raise ProtocolError("%s has no events in it" % self.path)
        return steps

    def _run(self):
        while not self._stop.is_set():
            for step in self.script:
                if self._stop.wait(float(step.get("after", 0.5))):
                    return
                self._emit(
                    self.devices[0],
                    int(step.get("type", EV_KEY)),
                    int(step["code"]),
                    int(step.get("value", 1)),
                )
            if not self.loop:
                return


def _fake_device(path) -> InputDevice:
    device = InputDevice(
        path=path, name="scripted input (%s)" % os.path.basename(path),
        vendor=VENDOR_2015, product=PRODUCT_IPAC2, interface=0, joystick=False,
    )
    device.player = 1
    return device


def open_monitor(args, profile=None, stream=None):
    """The monitor the CLI and the web UI both want."""
    fake = getattr(args, "fake_input", None)
    if fake:
        return FakeInputMonitor(fake, stream=stream, profile=profile)
    if sys.platform != "linux":
        raise DeviceError(
            "watching the panel needs Linux (/dev/input). Use --fake-input "
            "with a script to work on this machine."
        )
    devices = find_input_devices(all_devices=getattr(args, "all_devices", False))
    return InputMonitor(
        devices, grab=getattr(args, "grab", False), stream=stream, profile=profile
    )
