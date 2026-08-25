"""Thin wrappers over the two Linux interfaces this tool uses directly.

ioctl request numbers, built the way the kernel's macros build them, and
sysfs reads that answer with a default rather than raising. Both the hidraw
device layer and the evdev input layer need them, so neither owns them.
"""

from __future__ import annotations

import ctypes

_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction: int, type_char: str, nr: int, size: int) -> int:
    value = (direction << 30) | (size << 16) | (ord(type_char) << 8) | nr
    # fcntl.ioctl wants this as a signed int on some Python builds.
    return ctypes.c_int32(value).value


def _iowr(type_char: str, nr: int, size: int) -> int:
    return _ioc(_IOC_READ | _IOC_WRITE, type_char, nr, size)


def _iow(type_char: str, nr: int, size: int) -> int:
    return _ioc(_IOC_WRITE, type_char, nr, size)



def _read_sysfs(path, default=None):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return default
