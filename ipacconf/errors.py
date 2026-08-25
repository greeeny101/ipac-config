"""The two exceptions every layer raises.

Kept in their own module because everything below the CLI needs them, and
importing them must never drag in a heavier module.
"""

from __future__ import annotations

class ProtocolError(Exception):
    pass


class DeviceError(Exception):
    pass



class ReadOnlyError(Exception):
    """A write aimed at a directory we only ever read - the shipped presets.

    Its own class so a front end can answer 403 rather than 500 without
    matching on the message text.
    """
