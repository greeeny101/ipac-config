"""The pin table: where each pin's three bytes live in the data array."""

from __future__ import annotations

#
# Each pin owns three bytes in the 252 byte data array: its action, its
# alternate action (used while the shift key is held) and a shift marker.
# The layout is regular - alternate = action + 50, shift = action + 100.
#
# NOTE: QtPyUltimarc's PinMapping lists 2sw1 as (16, 56, 116) and 2sw5 as
# (32, 81, 132). Both break that rule, and 81 collides with 1sw5's alternate
# index, which would silently corrupt a second pin. They look like typos for
# 66 and 82, so we derive all three indices from the rule instead. If a real
# board ever disagrees, `apply --dry-run --diff` shows exactly which bytes
# would move before anything is written.

PIN_ACTION_INDEX = {
    # player 1 stick
    "1up": 19, "1down": 17, "1left": 21, "1right": 23,
    # player 2 stick
    "2up": 20, "2down": 18, "2left": 22, "2right": 0,
    # player 1 buttons
    "1sw1": 39, "1sw2": 37, "1sw3": 35, "1sw4": 33,
    "1sw5": 31, "1sw6": 29, "1sw7": 27, "1sw8": 25,
    # player 2 buttons
    "2sw1": 16, "2sw2": 38, "2sw3": 36, "2sw4": 34,
    "2sw5": 32, "2sw6": 28, "2sw7": 26, "2sw8": 24,
    # admin
    "1start": 47, "1coin": 45, "1a": 43, "1b": 41,
    "2start": 46, "2coin": 44, "2a": 42, "2b": 40,
}

PIN_TABLE = {
    name: (idx, idx + 50, idx + 100) for name, idx in PIN_ACTION_INDEX.items()
}

# Display order and grouping, used by the CLI and the web UI.
PIN_GROUPS = [
    ("Player 1 stick", ["1up", "1down", "1left", "1right"]),
    ("Player 1 buttons", ["1sw1", "1sw2", "1sw3", "1sw4", "1sw5", "1sw6", "1sw7", "1sw8"]),
    ("Player 2 stick", ["2up", "2down", "2left", "2right"]),
    ("Player 2 buttons", ["2sw1", "2sw2", "2sw3", "2sw4", "2sw5", "2sw6", "2sw7", "2sw8"]),
    ("Start / coin / admin", ["1start", "1coin", "1a", "1b", "2start", "2coin", "2a", "2b"]),
]

PIN_ORDER = [name for _, names in PIN_GROUPS for name in names]
