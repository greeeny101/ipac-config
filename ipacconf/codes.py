"""The action code table: byte values and the names we give them.

Also the range test that decides whether a byte is a game-controller action,
which several callers need and which must not be a name-prefix match.
"""

from __future__ import annotations

from .errors import ProtocolError

#
# 2015+ boards speak standard USB HID usage IDs for keys, plus Ultimarc's own
# range above 0x80 for mouse, gamepad, analog, hat and media actions.

KEY_CODES = {
    "A": 0x04, "B": 0x05, "C": 0x06, "D": 0x07, "E": 0x08, "F": 0x09,
    "G": 0x0A, "H": 0x0B, "I": 0x0C, "J": 0x0D, "K": 0x0E, "L": 0x0F,
    "M": 0x10, "N": 0x11, "O": 0x12, "P": 0x13, "Q": 0x14, "R": 0x15,
    "S": 0x16, "T": 0x17, "U": 0x18, "V": 0x19, "W": 0x1A, "X": 0x1B,
    "Y": 0x1C, "Z": 0x1D,
    "1": 0x1E, "2": 0x1F, "3": 0x20, "4": 0x21, "5": 0x22,
    "6": 0x23, "7": 0x24, "8": 0x25, "9": 0x26, "0": 0x27,
    "ENTER": 0x28, "ESC": 0x29, "BKSP": 0x2A, "TAB": 0x2B, "SPACE": 0x2C,
    "-": 0x2D, "=": 0x2E, "[": 0x2F, "]": 0x30, "\\": 0x31,
    "NON US #": 0x32, ";": 0x33, "'": 0x34, "`": 0x35,
    ",": 0x36, ".": 0x37, "/": 0x38, "CAPS": 0x39,
    "F1": 0x3A, "F2": 0x3B, "F3": 0x3C, "F4": 0x3D, "F5": 0x3E, "F6": 0x3F,
    "F7": 0x40, "F8": 0x41, "F9": 0x42, "F10": 0x43, "F11": 0x44, "F12": 0x45,
    "PRNT SCRN": 0x46, "SCROLL": 0x47, "PAUSE": 0x48, "INSERT": 0x49,
    "HOME": 0x4A, "PGUP": 0x4B, "DEL": 0x4C, "END": 0x4D, "PGDWN": 0x4E,
    "RIGHT": 0x4F, "LEFT": 0x50, "DOWN": 0x51, "UP": 0x52,
    "NUM": 0x53, "KP /": 0x54, "KP *": 0x55, "KP -": 0x56, "KP +": 0x57,
    "KP ENTER": 0x58, "KP 1": 0x59, "KP 2": 0x5A, "KP 3": 0x5B, "KP 4": 0x5C,
    "KP 5": 0x5D, "KP 6": 0x5E, "KP 7": 0x5F, "KP 8": 0x60, "KP 9": 0x61,
    "KP 0": 0x62, "KP .": 0x63, "NON US \\": 0x64, "APP": 0x65,
    "KB POWER": 0x66, "KP =": 0x67,
    "CTRL L": 0x70, "SHIFT L": 0x71, "ALT L": 0x72, "WIN L": 0x73,
    "CTRL R": 0x74, "SHIFT R": 0x75, "ALT R": 0x76, "WIN MENU": 0x77,
}

MOUSE_CODES = {
    "MOUSE L DBL CLK": 0x80, "MOUSE L": 0x81,
    "MOUSE M": 0x82, "MOUSE R": 0x83,
}

SYSTEM_CODES = {
    "POWER": 0x88, "SLEEP": 0x89, "WAKE": 0x8A,
    "VOL UP": 0x8B, "VOL DOWN": 0x8C,
    "MUTE": 0xE2, "PLAY/PAUSE": 0xE3, "NEXT": 0xE4, "PREV": 0xE5, "STOP": 0xE6,
    "EMAIL": 0xF0, "SEARCH": 0xF1, "BOOKMARKS": 0xF2, "OPEN BROWSER": 0xF3,
    "WEB BACK": 0xF4, "WEB FORWARD": 0xF5, "WEB STOP": 0xF6,
    "WEB REFRESH": 0xF7, "MEDIA PLAYER": 0xF8, "CALCULATOR": 0xFA,
    "EXPLORER": 0xFC, "WAIT 3 SEC": 0xFE,
}

# Gamepad buttons start at 0x8e, not 0x90.
#
# QtPyUltimarc's IPACSeriesMapping starts them at 0x90, and this table copied
# that. Confirmed wrong against hardware: with a board read by WinIPAC, 0x8e is
# "P1 Button 1 (A)" and 0x92 is "P1 Button 5 (LR)" - two points four apart on
# both scales, so the origin is 0x8e and upstream is off by two. LR is Left
# Rear, which is what Ultimarc's own multi-mode table calls button 5 in Xinput,
# so that is a third agreement.
#
# The correction matters because the label is a promise: a profile asking for
# GAMEPAD 1 used to send 0x90, which the board and the host both call button 3.
#
# Only the bottom of the range is confirmed. Buttons are numbered up to where
# ANALOG 0 begins at 0xB0, which makes 34 of them; nobody has checked whether
# the board really has 33 and 34 or whether those two codes mean something
# else, so they are named for consistency rather than from evidence.
# Buttons are numbered from ZERO, because that is how the host numbers them.
# Batocera, SDL and evdev all call the first button 0; WinIPAC calls it "P1
# Button 1". The tool a cabinet is actually configured against wins, so
# GAMEPAD 0 is 0x8e and GAMEPAD 10 is the last confirmed button.
#
# This changed. Profiles written when the names were 1-based mean one code
# lower now - "GAMEPAD 1" was 0x8e and is 0x8f. There is no way to tell the
# two conventions apart from the file, so hand-written profiles need checking
# by eye. Dumps and backups are unaffected: they carry raw bytes and `restore`
# is byte-exact.
GAMEPAD_FIRST_CODE = 0x8E
GAMEPAD_COUNT = 34

# ...but only the first eleven are known to BE buttons.
#
# Confirmed on hardware, pin by pin: 0x8e..0x98 are buttons 1..11, each one
# arriving as EV_KEY. Above that the names stop describing what happens -
# 0x9a, 0x9b and 0x9c produce hat events (EV_ABS 0x10/0x11) and 0x9d produces
# EV_ABS 0, an ordinary axis. A stick assigned "GAMEPAD 16" moved an axis
# instead of pressing button 16.
#
# That is not a naming accident, it is how the board works: a gamepad has a
# dozen-odd buttons and then a d-pad and axes, and the code space is laid out
# the same way. QtPyUltimarc's table implies 32 contiguous buttons followed by
# analog at 0xB0 and hats at 0xBA, and that is wrong for this board by a wide
# margin - the hats turn up around 0x9a, not 0xBA.
#
# The range ends at 0x98. 0x99..0x9c are the d-pad - see DPAD_FIRST_CODE - and
# above those the names are placeholders for codes rather than promises about
# them. apply says so before writing one.
GAMEPAD_BUTTONS_CONFIRMED = 11

# The four codes immediately above the buttons, 0x99..0x9c, are the d-pad, and
# they are in the order you would guess. MEASURED on a panel, one direction at
# a time, reading the evdev axis and value the host raised:
#
#     0x99  ->  ABS_HAT0Y -1   =  UP
#     0x9a  ->  ABS_HAT0Y +1   =  DOWN
#     0x9b  ->  ABS_HAT0X -1   =  LEFT
#     0x9c  ->  ABS_HAT0X +1   =  RIGHT
#
# (Linux convention: HAT0Y runs -1 up to +1 down, HAT0X -1 left to +1 right.)
#
# 0x9d is not a d-pad code; assigned to a direction it does nothing useful.
#
# Getting the ORDER wrong does not simply mirror the stick, it puts opposite
# directions on different axes: up and down each moving a different one. The
# hat then never returns to a clean centre, diagonals are impossible, and the
# stick feels sluggish and sticky rather than plainly wrong. That symptom is
# what this table was worked out from.
#
# Two earlier attempts to derive the pairing were wrong, both because the
# axis readings came from a monitor that was mislabelling axis events. This
# table is from the fixed monitor and the panel, not from a theory.
#
# QtPyUltimarc puts hats at 0xBA..0xBD and analog at 0xB0..0xB7. Neither is
# anywhere near this.
DPAD_FIRST_CODE = 0x99
DPAD_COUNT = 4

# The code says which CONTROLLER, not just which control.
#
# Confirmed on hardware. A profile that gave both players the same codes put
# every press - player 2's included - on player 1's event node, and player 2's
# buttons mirrored player 1's. Giving player 2 its own codes put it back on its
# own node. So two pins carrying one code are the same button on the same
# controller, whichever pin group they sit in.
#
# Player 2's block starts at 0xa7: codes 0xa8..0xab came back as player 2's
# buttons 1..4, four consistent points, so button 0 is 0xa7. That makes each
# player's block 25 codes:
#
#     player 1                     player 2
#     0x8e..0x98  11 buttons       0xa7..0xb1   measured
#     0x99..0x9c   4 hat           0xb2..0xb5   measured
#     0x9d..0xa6  10 axes          0xb6..0xbf   inferred - never pressed
#
# Player 2's hat was predicted from the block arithmetic and then confirmed:
# 0xb2..0xb5 fired ABS_HAT0Y -1, +1 and ABS_HAT0X -1, +1 - the same axis
# pairing and the same up/down/left/right order as player 1's.
#
# And that finally explains 0x9d, which cost an evening: it is not past the
# end of the buttons and it is not a d-pad direction. It is player 1's first
# AXIS code, which is why assigning it to a stick direction moved ABS_X and
# did nothing useful.
#
# The axis rows are arithmetic, not measurement - 25 minus 11 minus 4 is 10,
# and 0x9d moving ABS_X is the only direct evidence for what lives there.
PLAYER_BLOCK = 0x19  # 25 codes per controller
P2_FIRST_CODE = GAMEPAD_FIRST_CODE + PLAYER_BLOCK

DPAD_DIRECTIONS = ("UP", "DOWN", "LEFT", "RIGHT")  # in code order, measured

# Named for the hat the host reports them on: the four drive ABS_HAT0X and
# ABS_HAT0Y, which is hat 0. "HAT 0 UP" says which hat and which way; "DPAD 1"
# said neither, and the order being wrong is the failure that actually happens.
DPAD_NAME = "HAT 0 %s"

def _build_game_codes() -> dict:
    """Every game-controller action name, in the order registration matters.

    A function rather than a run of module-level loops so its counters stay
    out of the module namespace - the same reason _build_linux_to_board is
    one. Registration order is not cosmetic: the first name a byte gets is
    the one it decodes back to, so the comments below are the spec.
    """
    codes = {}
    # Registered before GAMEPAD so these win when a byte is decoded back to a
    # name: 0x99 is the d-pad, whatever an older profile called it. The named
    # form wins over the numbered one, because "DPAD UP" is checkable by
    # reading it and "DPAD 1" is not - and the order being wrong is the
    # failure that actually happens.
    # Player 2's block, registered first so it wins when a byte is decoded:
    # those codes are player 2 controls, not high-numbered player 1 buttons.
    for n in range(0, GAMEPAD_BUTTONS_CONFIRMED):
        codes["P2 GAMEPAD %d" % n] = P2_FIRST_CODE + n
    for n, direction in enumerate(DPAD_DIRECTIONS):
        codes["P2 HAT %s" % direction] = (
            P2_FIRST_CODE + GAMEPAD_BUTTONS_CONFIRMED + n)

    for n, direction in enumerate(DPAD_DIRECTIONS):
        codes[DPAD_NAME % direction] = DPAD_FIRST_CODE + n
    # Kept so profiles written before the codes were measured still apply.
    for n, direction in enumerate(DPAD_DIRECTIONS):
        codes["DPAD %s" % direction] = DPAD_FIRST_CODE + n
    for n in range(1, DPAD_COUNT + 1):
        codes["DPAD %d" % n] = DPAD_FIRST_CODE + n - 1
    for n in range(0, GAMEPAD_COUNT):
        codes["GAMEPAD %d" % n] = GAMEPAD_FIRST_CODE + n
    for n in range(0, 8):
        codes["ANALOG %d" % n] = 0xB0 + n  # unverified; see PLAYER_BLOCK
    # QtPyUltimarc puts hats at 0xBA..0xBD. Measured, this board's hat is at
    # 0x99..0x9c - see DPAD_FIRST_CODE - so those names are not registered:
    # they were never verified, they contradict what the hardware does, and
    # having two things called "HAT n" is how a stick ends up on codes that do
    # nothing. 0xBA..0xBD still round-trip, as literal 0xNN.
    for n, axis in enumerate(["X1", "X2", "Y1", "Y2", "Z1", "Z2"]):
        codes["TRACKBALL %s" % axis] = 0xC0 + n
    return codes


def invert_first_wins(mapping: dict) -> dict:
    """Flip a mapping, the first key to claim a value keeping it.

    Both directions of the name/byte tables are built this way, and both
    depend on "first wins" rather than "last wins": CODE_NAMES so "\\\\"
    decodes to "\\\\" rather than "NON US \\\\", and BOARD_TO_LINUX so a board
    code reverses to the keycode most likely to have produced it.
    """
    flipped = {}
    for key, value in mapping.items():
        flipped.setdefault(value, key)
    return flipped


GAME_CODES = _build_game_codes()

CODE_GROUPS = [
    ("Keyboard", KEY_CODES),
    ("Game controller", GAME_CODES),
    ("Mouse", MOUSE_CODES),
    ("System / media", SYSTEM_CODES),
]

ALL_CODES = {
    name: code for _, table in CODE_GROUPS for name, code in table.items()
}

CODE_NAMES = invert_first_wins(ALL_CODES)

NONE = ""  # an unassigned action


def code_to_name(value: int):
    """Name for a byte value, or None if we do not recognise it."""
    if value == 0:
        return NONE
    return CODE_NAMES.get(value)


def name_to_code(name) -> int:
    if name is None or name == NONE:
        return 0
    key = str(name).strip()
    if key in ALL_CODES:
        return ALL_CODES[key]
    upper = key.upper()
    if upper in ALL_CODES:
        return ALL_CODES[upper]
    raise ProtocolError("unknown action %r" % name)


def _fmt_byte(value: int) -> str:
    name = code_to_name(value)
    if name == NONE:
        return "0x00 (none)"
    if name:
        return "0x%02x (%s)" % (value, name)
    return "0x%02x" % value

GAMEPAD_PREFIXES = ("GAMEPAD", "DPAD", "HAT", "ANALOG")

# Whether a byte is a game-controller action, tested by RANGE rather than by
# the name it happens to have. Both players' blocks and their axes live in
# 0x8e..0xbf; keycodes are below and macros above.
#
# This used to match on the first word of the name, which broke the moment
# player 2's codes were called "P2 GAMEPAD n": "P2" is not in the prefix list,
# so a perfectly good two-player gamepad profile read as mixed and would have
# been reported as unable to switch the board's mode.
GAME_CODE_FIRST = GAMEPAD_FIRST_CODE
GAME_CODE_LAST = P2_FIRST_CODE + PLAYER_BLOCK - 1


def is_game_code(code: int) -> bool:
    return GAME_CODE_FIRST <= code <= GAME_CODE_LAST


def control_span(first: int):
    """(buttons, hat, axes) code ranges for one player's block."""
    buttons = range(first, first + GAMEPAD_BUTTONS_CONFIRMED)
    hat = range(buttons.stop, buttons.stop + DPAD_COUNT)
    axes = range(hat.stop, first + PLAYER_BLOCK)
    return buttons, hat, axes
