"""Turning raw input events into named board actions, and back into pins."""

from __future__ import annotations

import struct

from ..codes import (
    GAMEPAD_FIRST_CODE,
    GAME_CODES,
    MOUSE_CODES,
    PLAYER_BLOCK,
    is_game_code,
)
from .keymap import (
    ABS_HAT0X,
    BTN_JOYSTICK,
    BTN_LAST,
    BTN_MOUSE,
    EV_ABS,
    EV_KEY,
    EV_REL,
    INPUT_EVENT_FORMAT,
    INPUT_EVENT_SIZE,
    LINUX_TO_BOARD,
    REL_X,
)

def parse_input_events(blob: bytes) -> list:
    """Split one read() from an event node into (sec, usec, type, code, value).

    A partial trailing record is dropped. The kernel only ever hands out whole
    events, so this is belt and braces.
    """
    size = INPUT_EVENT_SIZE
    return [
        struct.unpack(INPUT_EVENT_FORMAT, blob[start : start + size])
        for start in range(0, len(blob) - size + 1, size)
    ]


def player_block_first(player=None) -> int:
    """The first code of a player's block. Player 1 unless told otherwise."""
    if not player or player < 1:
        return GAMEPAD_FIRST_CODE
    return GAMEPAD_FIRST_CODE + PLAYER_BLOCK * (player - 1)


def event_action(etype: int, code: int, player=None):
    """(kind, board byte) for an evdev event; the byte is None if unmapped.

    `player` matters for anything in a controller block. Each player is a
    separate device numbering its own buttons from zero, so button 1 on
    player 2's node is player 2's block plus one - not player 1's. Without it
    the monitor named every player 2 press with a player 1 code and pointed at
    a player 1 pin, which is how a press on one panel came back as a pin on
    the other.
    """
    if etype == EV_KEY:
        if code < BTN_MOUSE:
            return "key", LINUX_TO_BOARD.get(code)
        if BTN_MOUSE <= code < BTN_MOUSE + 3:
            # BTN_LEFT/RIGHT/MIDDLE against MOUSE L/R/M.
            return "mouse", (MOUSE_CODES["MOUSE L"], MOUSE_CODES["MOUSE R"],
                             MOUSE_CODES["MOUSE M"])[code - BTN_MOUSE]
        if BTN_JOYSTICK <= code < BTN_LAST:
            # BTN_JOYSTICK is button 0, and GAMEPAD 0 is the board's first
            # button, so the offset is zero. It used to be +1, left over from
            # when the code names were 1-based; after the names were renumbered
            # the monitor kept adding one and named every button one too high,
            # and then pointed at whichever pin carried THAT code. Pressing
            # start reported the pin next to it.
            #
            # This assumes hid-input numbered the board's buttons from
            # BTN_TRIGGER, which is what it does for a device presenting as a
            # joystick. Every line carries its raw evdev code, so a board that
            # disagrees shows the offset rather than hiding it.
            index = code - BTN_JOYSTICK
            board = player_block_first(player) + index
            return "gamepad", board if is_game_code(board) else None
        return "button", None
    if etype == EV_ABS:
        # No board byte. An axis event says which axis the HOST moved, and the
        # board byte that caused it cannot be recovered from that - several
        # different board codes drive the same axis, in opposite directions.
        #
        # This used to answer HAT n / ANALOG n by mapping the evdev axis number
        # through the board's code table, which is a category error: it made
        # the monitor print a board code the config did not contain, then say
        # "no pin carries this code" about a pin that plainly did. It sent an
        # afternoon chasing a table that was never involved.
        if ABS_HAT0X <= code < ABS_HAT0X + 4:
            return "hat", None
        return "axis", None
    if etype == EV_REL:
        if code < 2:
            return "trackball", GAME_CODES["TRACKBALL %s" % ("X1", "Y1")[code - REL_X]]
        return "relative", None
    return "other", None


def pins_for_action(profile, name, player=None) -> list:
    """Every pin whose action or alternate action is `name`.

    More than one pin can carry the same code, in which case they are all
    returned - an ambiguous answer still narrows the search, and saying so is
    better than picking one at random.
    """
    if not profile or not name:
        return []
    hits = []
    for pin in profile.get("pins") or []:
        for field in ("action", "alternate_action"):
            if pin.get(field) != name:
                continue
            hits.append({"pin": pin.get("name"), "field": field})
            break  # a pin whose alternate repeats its action is still one pin
    if player and len(hits) > 1:
        # In Dinput mode both players' buttons share the GAMEPAD 1..32 code
        # space, so the code alone cannot say who pressed it. Which event node
        # it arrived on can.
        narrowed = [h for h in hits if str(h["pin"] or "").startswith(str(player))]
        if narrowed:
            return narrowed
    return hits
