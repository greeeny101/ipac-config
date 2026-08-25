#!/usr/bin/env python3
"""Tests for ipacconf. Stdlib only, no hardware needed.

    python3 -m unittest -v test_ipacconf.py
"""

import json
import os
import shutil
import struct
import tempfile
import types
import unittest

import ipacconf as ic


class TestPinTable(unittest.TestCase):
    def test_thirty_two_pins(self):
        self.assertEqual(len(ic.PIN_TABLE), 32)
        self.assertEqual(sorted(ic.PIN_TABLE), sorted(ic.PIN_ORDER))

    def test_no_index_is_shared(self):
        """A collision here would silently corrupt a second pin on write."""
        seen = {}
        for name, indices in ic.PIN_TABLE.items():
            for kind, idx in zip(("action", "alternate", "shift"), indices):
                self.assertNotIn(
                    idx, seen, "index %d used by %s and %s" % (idx, seen.get(idx), name)
                )
                seen[idx] = "%s %s" % (name, kind)

    def test_indices_stay_inside_the_data_array(self):
        for name, indices in ic.PIN_TABLE.items():
            for idx in indices:
                self.assertLess(idx, ic.CONFIG_SIZE - 4, name)

    def test_pins_do_not_reach_into_the_macro_area(self):
        for name, indices in ic.PIN_TABLE.items():
            for idx in indices:
                self.assertLess(idx, ic.MACRO_START, name)


class TestCodeTable(unittest.TestCase):
    def test_names_round_trip(self):
        for name, value in ic.ALL_CODES.items():
            self.assertEqual(ic.name_to_code(name), value)

    def test_lookup_is_case_insensitive(self):
        self.assertEqual(ic.name_to_code("space"), ic.ALL_CODES["SPACE"])

    def test_unknown_name_is_rejected(self):
        with self.assertRaises(ic.ProtocolError):
            ic.name_to_code("NOT A KEY")

    def test_zero_is_unassigned(self):
        self.assertEqual(ic.code_to_name(0), ic.NONE)
        self.assertEqual(ic.name_to_code(""), 0)

    def test_gamepad_codes_start_at_zero_at_0x8e(self):
        """0x8e is button 0, matching Batocera, SDL and evdev.

        The origin came from WinIPAC, which calls 0x8e "P1 Button 1" and 0x92
        "P1 Button 5" - four apart on both scales. The zero base came from
        Batocera, which is what a cabinet is configured against.
        """
        self.assertEqual(ic.ALL_CODES["GAMEPAD 0"], 0x8E)
        self.assertEqual(ic.ALL_CODES["GAMEPAD 4"], 0x92)
        self.assertEqual(ic.ALL_CODES["ANALOG 0"], 0xB0)

    def test_the_last_button_is_ten(self):
        top = ic.GAMEPAD_FIRST_CODE + ic.GAMEPAD_BUTTONS_CONFIRMED - 1
        self.assertEqual(top, 0x98)
        self.assertEqual(ic.code_to_name(top), "GAMEPAD 10")

    def test_upstreams_unverified_hats_are_not_registered(self):
        """0xBA..0xBD are QtPyUltimarc's. The real hat is at 0x99..0x9c.

        Two things called "HAT n" is how a stick ends up on codes that do
        nothing, so only the measured ones carry a name.
        """
        for code in range(0xBA, 0xBE):
            with self.subTest(code=code):
                self.assertIsNone(ic.code_to_name(code))

    def test_no_gamepad_code_collides_with_a_named_control(self):
        """The old numbering left 0x8e and 0x8f nameless; nothing else moved."""
        for name in ("POWER", "SLEEP", "WAKE", "VOL UP", "VOL DOWN"):
            self.assertLess(ic.ALL_CODES[name], ic.GAMEPAD_FIRST_CODE)


class TestEncodeDecode(unittest.TestCase):
    def setUp(self):
        self.raw = ic.default_config()

    def test_default_config_is_the_right_size(self):
        self.assertEqual(len(self.raw), ic.CONFIG_SIZE)

    def test_header_is_written(self):
        self.assertEqual(tuple(self.raw[:3]), ic.HEADER_WRITE)

    def test_decode_then_encode_is_byte_identical(self):
        """The property that makes read-modify-write safe."""
        profile = ic.decode_config(self.raw)
        again = ic.encode_config(profile, self.raw)
        self.assertEqual(bytes(again), self.raw)

    def test_round_trip_through_json(self):
        profile = json.loads(json.dumps(ic.decode_config(self.raw)))
        again = ic.encode_config(profile, self.raw)
        self.assertEqual(bytes(again), self.raw)

    def test_decoded_pins_match_what_was_encoded(self):
        profile = ic.decode_config(self.raw)
        actions = {pin["name"]: pin["action"] for pin in profile["pins"]}
        self.assertEqual(actions["1up"], "UP")
        self.assertEqual(actions["1sw1"], "CTRL L")
        self.assertEqual(actions["2sw5"], "I")
        self.assertEqual(actions["1start"], "1")

    def test_shift_flag_survives(self):
        profile = ic.decode_config(self.raw)
        start = next(p for p in profile["pins"] if p["name"] == "1start")
        self.assertTrue(start.get("shift"))

    def test_a_shift_key_that_sends_nothing_is_still_reported(self):
        """Otherwise a form filled from this would clear the flag on write."""
        base = bytearray(self.raw)
        base[4 + ic.PIN_TABLE["1start"][0]] = 0  # shift key, no action of its own
        profile = ic.decode_config(bytes(base))
        start = next(p for p in profile["pins"] if p["name"] == "1start")
        self.assertEqual(start["action"], ic.NONE)
        self.assertTrue(start["shift"])
        again = ic.encode_config(profile, bytes(base))
        self.assertEqual(bytes(again), bytes(base))

    def test_a_pin_with_neither_action_nor_shift_is_left_out(self):
        base = bytearray(self.raw)
        for index in ic.PIN_TABLE["1up"]:
            base[4 + index] = 0
        names = [p["name"] for p in ic.decode_config(bytes(base))["pins"]]
        self.assertNotIn("1up", names)

    def test_alternate_action_survives(self):
        profile = ic.decode_config(self.raw)
        coin = next(p for p in profile["pins"] if p["name"] == "1coin")
        self.assertEqual(coin["alternate_action"], "ESC")

    def test_unmentioned_pins_keep_their_current_value(self):
        updated = ic.encode_config({"pins": [{"name": "1up", "action": "W"}]}, self.raw)
        ai = ic.PIN_TABLE["1down"][0]
        self.assertEqual(updated[4 + ai], self.raw[4 + ai])

    def test_unknown_bytes_are_preserved(self):
        base = bytearray(self.raw)
        base[200] = 0x5A  # something we have no name for
        updated = ic.encode_config({"pins": [{"name": "1up", "action": "W"}]}, bytes(base))
        self.assertEqual(updated[200], 0x5A)

    def test_unknown_action_byte_round_trips_as_hex(self):
        base = bytearray(self.raw)
        base[4 + ic.PIN_TABLE["1up"][0]] = 0x7B  # not in any table
        profile = ic.decode_config(bytes(base))
        pin = next(p for p in profile["pins"] if p["name"] == "1up")
        self.assertEqual(pin["action"], "0x7b")
        again = ic.encode_config(profile, bytes(base))
        self.assertEqual(bytes(again), bytes(base))

    def test_clearing_an_action(self):
        updated = ic.encode_config({"pins": [{"name": "1sw8", "action": ""}]}, self.raw)
        self.assertEqual(updated[4 + ic.PIN_TABLE["1sw8"][0]], 0)

    def test_naming_a_pin_without_an_action_leaves_the_action_alone(self):
        """Setting only an alternate must not wipe the pin's main action."""
        before = self.raw[4 + ic.PIN_TABLE["1sw1"][0]]
        updated = ic.encode_config(
            {"pins": [{"name": "1sw1", "alternate_action": "F1"}]}, self.raw
        )
        self.assertEqual(updated[4 + ic.PIN_TABLE["1sw1"][0]], before)

    def test_naming_a_pin_without_shift_leaves_the_shift_byte_alone(self):
        base = bytearray(self.raw)
        index = 4 + ic.PIN_TABLE["1sw1"][2]
        base[index] = 0x41
        updated = ic.encode_config(
            {"pins": [{"name": "1sw1", "action": "A"}]}, bytes(base)
        )
        self.assertEqual(updated[index], 0x41)

    def test_unknown_pin_is_rejected(self):
        with self.assertRaises(ic.ProtocolError):
            ic.encode_config({"pins": [{"name": "3sw1", "action": "A"}]}, self.raw)

    def test_short_config_is_rejected(self):
        with self.assertRaises(ic.ProtocolError):
            ic.decode_config(b"\x00" * 12)


class TestConfigBits(unittest.TestCase):
    def test_debounce_round_trips(self):
        for name in ic.DEBOUNCE:
            raw = ic.encode_config({"debounce": name}, ic.default_config())
            self.assertEqual(ic.decode_config(bytes(raw))["debounce"], name)

    def test_paclink_round_trips(self):
        for value in (True, False):
            raw = ic.encode_config({"paclink": value}, ic.default_config())
            self.assertIs(ic.decode_config(bytes(raw))["paclink"], value)

    def test_debounce_leaves_other_bits_alone(self):
        base = bytearray(ic.default_config())
        base[3] = 0xFF
        raw = ic.encode_config({"debounce": "none"}, bytes(base))
        self.assertEqual(raw[3] & ~0x18, 0xFF & ~0x18)

    def test_unknown_debounce_is_rejected(self):
        with self.assertRaises(ic.ProtocolError):
            ic.encode_config({"debounce": "quick"}, ic.default_config())


class TestXinputBit(unittest.TestCase):
    """Bit 1 of the config bitfield: "this config is an Xinput one".

    Confirmed on hardware after two wrong readings. Writing a gamepad-only
    config with the bit set, from keyboard mode, took a 1.55 board to Xinput -
    Batocera reported a Microsoft Xbox controller connecting, which is
    045e:028e. The same config with the bit clear gave Dinput.

    It is ordinary config, so it is preserved across a read-modify-write like
    debounce and paclink. The earlier code cleared it unless asked, which
    would have silently taken an Xinput board back to Dinput on any write.
    """

    def test_it_is_bit_one(self):
        self.assertEqual(ic.XINPUT_BIT, 0x02)

    def test_it_is_preserved_when_nobody_says_otherwise(self):
        base = bytearray(ic.default_config())
        base[3] |= ic.XINPUT_BIT
        raw = ic.encode_config({}, bytes(base))
        self.assertEqual(raw[3] & ic.XINPUT_BIT, ic.XINPUT_BIT)

    def test_a_clear_bit_stays_clear(self):
        raw = ic.encode_config({}, ic.default_config())
        self.assertEqual(raw[3] & ic.XINPUT_BIT, 0)

    def test_the_argument_sets_it(self):
        raw = ic.encode_config({}, ic.default_config(), xinput=True)
        self.assertEqual(raw[3] & ic.XINPUT_BIT, ic.XINPUT_BIT)

    def test_the_argument_clears_it(self):
        base = bytearray(ic.default_config())
        base[3] |= ic.XINPUT_BIT
        raw = ic.encode_config({}, bytes(base), xinput=False)
        self.assertEqual(raw[3] & ic.XINPUT_BIT, 0)

    def test_a_profile_can_carry_it(self):
        raw = ic.encode_config({"xinput": True}, ic.default_config())
        self.assertEqual(raw[3] & ic.XINPUT_BIT, ic.XINPUT_BIT)

    def test_the_argument_beats_the_profile(self):
        raw = ic.encode_config({"xinput": True}, ic.default_config(), xinput=False)
        self.assertEqual(raw[3] & ic.XINPUT_BIT, 0)

    def test_it_round_trips_through_a_profile(self):
        raw = bytes(ic.encode_config({}, ic.default_config(), xinput=True))
        profile = ic.decode_config(raw)
        self.assertTrue(profile["xinput"])
        self.assertEqual(bytes(ic.encode_config(profile, raw))[3], raw[3])

    def test_restore_is_byte_exact_by_default(self):
        """A backup taken in Xinput restores to Xinput."""
        base = bytearray(ic.default_config())
        base[3] |= ic.XINPUT_BIT
        self.assertEqual(
            ic.as_write_command(bytes(base))[3] & ic.XINPUT_BIT, ic.XINPUT_BIT)
        self.assertEqual(
            ic.as_write_command(bytes(base), xinput=False)[3] & ic.XINPUT_BIT, 0)

    def test_it_does_not_disturb_debounce_or_paclink(self):
        profile = {"debounce": "long", "paclink": True}
        plain = ic.encode_config(profile, ic.default_config())
        armed = ic.encode_config(profile, ic.default_config(), xinput=True)
        self.assertEqual(plain[3] | ic.XINPUT_BIT, armed[3])
        decoded = ic.decode_config(bytes(armed))
        self.assertEqual(decoded["debounce"], "long")
        self.assertTrue(decoded["paclink"])

    def test_it_is_the_only_difference_in_the_whole_block(self):
        base = ic.default_config()
        plain = bytes(ic.encode_config({}, base, xinput=False))
        armed = bytes(ic.encode_config({}, base, xinput=True))
        differ = [i for i in range(ic.CONFIG_SIZE) if plain[i] != armed[i]]
        self.assertEqual(differ, [3])


class TestXinputWarning(unittest.TestCase):
    """Setting the bit is the last write this tool can make to the board."""

    @staticmethod
    def _info(vendor=None, product=None):
        return ic.DeviceInfo("/dev/hidraw0", vendor or ic.VENDOR_2015,
                             product or ic.PRODUCT_IPAC2, 0x0055, 2, "1-1")

    def _raw(self, xinput):
        return bytes(ic.encode_config({}, ic.default_config(), xinput=xinput))

    def test_setting_it_warns(self):
        warning = ic.xinput_warning(self._raw(True), self._info())
        self.assertIsNotNone(warning)
        self.assertIn("045e:028e", warning)

    def test_the_warning_says_how_to_come_back(self):
        warning = ic.xinput_warning(self._raw(True), self._info())
        self.assertIn("Start1+P1SW4", warning)
        self.assertIn("--no-xinput", warning)

    def test_a_clear_bit_on_a_normal_board_says_nothing(self):
        self.assertIsNone(ic.xinput_warning(self._raw(False), self._info()))

    def test_clearing_it_on_an_xinput_board_is_worth_saying(self):
        info = self._info(ic.VENDOR_XINPUT, ic.PRODUCT_XINPUT)
        warning = ic.xinput_warning(self._raw(False), info)
        self.assertIn("leave Xinput", warning)


class TestMacros(unittest.TestCase):
    def test_macros_round_trip(self):
        profile = {"macros": [{"name": "exit", "action": ["CTRL L", "ESC"]}],
                   "pins": [{"name": "1a", "action": "exit"}]}
        raw = ic.encode_config(profile, ic.default_config())
        decoded = ic.decode_config(bytes(raw))
        self.assertEqual(decoded["macros"][0]["action"], ["CTRL L", "ESC"])
        pin = next(p for p in decoded["pins"] if p["name"] == "1a")
        self.assertEqual(pin["action"], "macro 1")

    def test_too_many_macros_is_rejected(self):
        macros = [{"name": "m%d" % i, "action": ["A"]} for i in range(ic.MACRO_MAX_COUNT + 1)]
        with self.assertRaises(ic.ProtocolError):
            ic.encode_config({"macros": macros}, ic.default_config())

    def test_oversized_macros_are_rejected(self):
        macros = [{"name": "big", "action": ["A"] * (ic.MACRO_MAX_SIZE + 1)}]
        with self.assertRaises(ic.ProtocolError):
            ic.encode_config({"macros": macros}, ic.default_config())


class TestDiff(unittest.TestCase):
    def test_diff_reports_offset_and_meaning(self):
        before = ic.default_config()
        after = ic.encode_config({"pins": [{"name": "1up", "action": "W"}]}, before)
        changes = ic.diff_config(before, bytes(after))
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["meaning"], "1up action")
        self.assertEqual(changes[0]["after"], ic.ALL_CODES["W"])

    def test_identical_configs_have_no_diff(self):
        raw = ic.default_config()
        self.assertEqual(ic.diff_config(raw, raw), [])

    def test_offsets_are_described(self):
        self.assertEqual(ic.describe_offset(3), "config bits")
        self.assertEqual(ic.describe_offset(4 + ic.PIN_TABLE["1up"][2]), "1up shift")
        self.assertEqual(ic.describe_offset(4 + ic.MACRO_START), "macro area")


class TestFirmwareRules(unittest.TestCase):
    def test_interface_matches_ultimarc_linux_rule(self):
        self.assertEqual(ic.config_interface_for(0x44), 2)  # the board we have
        self.assertEqual(ic.config_interface_for(0x55), 2)  # multi-mode
        self.assertEqual(ic.config_interface_for(0x39), 3)  # mixed mode
        self.assertEqual(ic.config_interface_for(0x56), 3)

    def test_gamepad_support_by_firmware(self):
        self.assertFalse(ic.firmware_supports_gamepad(0x44))  # ours today
        self.assertFalse(ic.firmware_supports_gamepad(0x33))
        self.assertTrue(ic.firmware_supports_gamepad(0x36))  # mixed mode
        self.assertTrue(ic.firmware_supports_gamepad(0x55))  # multi-mode

    def test_firmware_note_is_never_empty(self):
        for bcd in range(0x20, 0x60):
            self.assertTrue(ic.firmware_note(bcd))


class TestWriteFrames(unittest.TestCase):
    """A short write is accepted message by message and then discarded."""

    def setUp(self):
        self.frames = ic.write_frames(ic.default_config())

    def test_sixty_five_messages_not_sixty_four(self):
        self.assertEqual(ic.WRITE_SIZE, 260)
        self.assertEqual(len(self.frames), 65)

    def test_every_frame_is_a_five_byte_report(self):
        for i, frame in enumerate(self.frames):
            with self.subTest(frame=i):
                self.assertEqual(len(frame), 1 + ic.CHUNK)
                self.assertEqual(frame[0], ic.REPORT_ID)

    def test_first_frame_carries_the_write_header(self):
        self.assertEqual(tuple(self.frames[0][1:4]), ic.HEADER_WRITE)

    def test_the_tail_is_a_read_header_not_padding(self):
        """Captured from WinIPAC: its downloads end 59 dd 0f 00."""
        self.assertEqual(self.frames[-1][1:], bytes(ic.HEADER_READ))
        self.assertEqual(ic.WRITE_TAIL, bytes(ic.HEADER_READ))

    def test_the_tail_does_not_eat_config(self):
        """All 256 config bytes still arrive; the tail is past them."""
        sent = ic.deframe(b"".join(self.frames))
        self.assertEqual(sent[:ic.CONFIG_SIZE], ic.default_config())
        self.assertEqual(sent[ic.CONFIG_SIZE:], ic.WRITE_TAIL)

    def test_the_config_survives_the_framing(self):
        config = ic.default_config()
        sent = ic.deframe(b"".join(self.frames))
        self.assertEqual(sent[:ic.CONFIG_SIZE], config)
        self.assertEqual(len(sent), ic.WRITE_SIZE)


class TestDeframe(unittest.TestCase):
    """The board answers in 5-byte reports, each prefixed with its id."""

    @staticmethod
    def _frame(payload):
        out = bytearray()
        for pos in range(0, len(payload), ic.CHUNK):
            out += bytes([ic.REPORT_ID]) + payload[pos:pos + ic.CHUNK]
        return bytes(out)

    def test_round_trip(self):
        payload = bytes(range(0, 64))
        self.assertEqual(ic.deframe(self._frame(payload)), payload)

    def test_single_report(self):
        self.assertEqual(ic.deframe(bytes([ic.REPORT_ID, 1, 2, 3, 4])), b"\x01\x02\x03\x04")

    def test_report_without_an_id_is_left_alone(self):
        self.assertEqual(ic.deframe(b"\x50\xdd\x0f\x00"), b"\x50\xdd\x0f\x00")

    def test_no_stray_ids_survive(self):
        payload = ic.default_config()
        recovered = ic.deframe(self._frame(payload))
        self.assertEqual(len(recovered), len(payload))
        self.assertEqual(recovered, payload)


class TestRealBoardDump(unittest.TestCase):
    """Checks against a real board's config, read over USB. Firmware 1.44.

    This is the regression net for the pin and code tables: every pin
    decoding to its factory MAME default is hard to achieve by accident.
    """

    @classmethod
    def setUpClass(cls):
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, "fixtures", "before-1.44.json")
        if not os.path.exists(path):
            raise unittest.SkipTest("no board dump present")
        cls.raw = bytes.fromhex(ic.load_profile(path)["raw"])
        cls.data = cls.raw[4:]

    def test_the_dump_is_a_full_config(self):
        self.assertEqual(len(self.raw), ic.CONFIG_SIZE)

    def test_no_report_ids_left_in_the_data(self):
        """The framing bug left an 0x03 every fifth byte."""
        stride = 1 + ic.CHUNK
        embedded = [i for i in range(4, len(self.raw), stride) if self.raw[i] == ic.REPORT_ID]
        self.assertEqual(embedded, [])

    def test_response_header_carries_the_firmware(self):
        self.assertEqual(self.raw[2], 0x44)  # firmware 1.44
        self.assertEqual(ic.decode_config(self.raw)["firmware"], "0.44")

    def test_every_pin_is_assigned(self):
        profile = ic.decode_config(self.raw)
        self.assertEqual(len(profile["pins"]), 32)

    def test_read_modify_write_is_a_no_op_on_real_data(self):
        """The property the whole safety model rests on."""
        profile = ic.decode_config(self.raw)
        again = bytes(ic.encode_config(profile, self.raw))
        self.assertEqual(again[3:], self.raw[3:])
        self.assertEqual(ic.diff_config(self.raw, again), [])

    def test_changing_one_pin_moves_exactly_one_byte(self):
        updated = ic.encode_config(
            {"pins": [{"name": "1b", "alternate_action": "F1"}]}, self.raw
        )
        changes = ic.diff_config(self.raw, bytes(updated))
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["meaning"], "1b alternate")

    def test_pin_table_matches_the_factory_mame_layout(self):
        """If an index were wrong, these would decode as garbage."""
        expected = {
            "1up": "UP", "1down": "DOWN", "1left": "LEFT", "1right": "RIGHT",
            "1sw1": "CTRL L", "1sw2": "ALT L", "1sw3": "SPACE", "1sw4": "SHIFT L",
            "1sw5": "Z", "1sw6": "X", "1sw7": "C", "1sw8": "V",
            "2up": "R", "2down": "F", "2left": "D", "2right": "G",
            "2sw1": "A", "2sw2": "S", "2sw3": "Q", "2sw4": "W",
            "2sw5": "I", "2sw6": "K", "2sw7": "J", "2sw8": "L",
            "1start": "1", "2start": "2", "1coin": "5", "2coin": "6",
        }
        for pin, action in expected.items():
            with self.subTest(pin=pin):
                index = ic.PIN_TABLE[pin][0]
                self.assertEqual(ic.code_to_name(self.data[index]), action)

    def test_the_two_disputed_indices_decode_sensibly(self):
        """2sw1 and 2sw5, where we deviate from QtPyUltimarc's table."""
        self.assertEqual(ic.code_to_name(self.data[ic.PIN_TABLE["2sw1"][0]]), "A")
        self.assertEqual(ic.code_to_name(self.data[ic.PIN_TABLE["2sw5"][0]]), "I")

    def test_start1_is_the_shift_key(self):
        shift_index = ic.PIN_TABLE["1start"][2]
        self.assertEqual(self.data[shift_index], 0x41)
        self.assertTrue(self.data[shift_index] & ic.SHIFT_BIT)

    def test_other_pins_carry_0x01_in_their_shift_byte(self):
        """Which is why shift must be a bit operation, not a byte write."""
        for pin in ("1sw1", "2sw8", "1coin"):
            with self.subTest(pin=pin):
                value = self.data[ic.PIN_TABLE[pin][2]]
                self.assertEqual(value, 0x01)
                self.assertFalse(value & ic.SHIFT_BIT)

    def test_alternate_actions_are_the_documented_defaults(self):
        self.assertEqual(ic.code_to_name(self.data[ic.PIN_TABLE["2start"][1]]), "ESC")
        self.assertEqual(ic.code_to_name(self.data[ic.PIN_TABLE["1right"][1]]), "TAB")


class TestShiftBit(unittest.TestCase):
    def test_clearing_shift_preserves_the_other_bits(self):
        base = bytearray(ic.default_config())
        index = 4 + ic.PIN_TABLE["1sw1"][2]
        base[index] = 0x41
        updated = ic.encode_config({"pins": [{"name": "1sw1", "action": "A", "shift": False}]},
                                   bytes(base))
        self.assertEqual(updated[index], 0x01)

    def test_setting_shift_preserves_the_other_bits(self):
        base = bytearray(ic.default_config())
        index = 4 + ic.PIN_TABLE["1sw1"][2]
        base[index] = 0x01
        updated = ic.encode_config({"pins": [{"name": "1sw1", "action": "A", "shift": True}]},
                                   bytes(base))
        self.assertEqual(updated[index], 0x41)

    def test_shift_survives_a_round_trip(self):
        base = bytearray(ic.default_config())
        base[4 + ic.PIN_TABLE["1start"][2]] = 0x41
        profile = ic.decode_config(bytes(base))
        pin = next(p for p in profile["pins"] if p["name"] == "1start")
        self.assertTrue(pin["shift"])


class TestWriteHeader(unittest.TestCase):
    def test_a_read_response_gets_the_write_header(self):
        response = bytes([0x00, 0x00, 0x44, 0x00]) + b"\x01" * (ic.CONFIG_SIZE - 4)
        self.assertEqual(tuple(ic.as_write_command(response)[:3]), ic.HEADER_WRITE)

    def test_the_config_bitfield_is_not_touched(self):
        response = bytes([0x00, 0x00, 0x44, 0x18]) + b"\x01" * (ic.CONFIG_SIZE - 4)
        self.assertEqual(ic.as_write_command(response)[3], 0x18)

    def test_diff_ignores_the_command_header(self):
        before = bytes([0x00, 0x00, 0x44, 0x00]) + b"\x01" * (ic.CONFIG_SIZE - 4)
        after = ic.as_write_command(before)
        self.assertEqual(ic.diff_config(before, after), [])


class TestIoctlNumbers(unittest.TestCase):
    """The board STALLs anything but an output report, so this matters."""

    @staticmethod
    def _iowr(nr, size):
        # _IOWR('H', nr, size), computed independently of ipacconf
        value = (3 << 30) | (size << 16) | (0x48 << 8) | nr
        return value - (1 << 32) if value >= (1 << 31) else value

    def test_output_report_is_tried_first(self):
        name, op = ic.Board.TRANSPORTS[0]
        self.assertEqual(name, "output report")
        self.assertEqual(op, self._iowr(0x0B, 5))  # HIDIOCSOUTPUT(5)

    def test_feature_report_is_the_fallback(self):
        name, op = ic.Board.TRANSPORTS[1]
        self.assertEqual(name, "feature report")
        self.assertEqual(op, self._iowr(0x06, 5))  # HIDIOCSFEATURE(5)

    def test_message_length_is_report_id_plus_chunk(self):
        self.assertEqual(ic.Board.MESSAGE_LENGTH, 5)
        self.assertEqual(ic.CHUNK, 4)

    def test_ioctl_fits_in_a_signed_int(self):
        for _, op in ic.Board.TRANSPORTS:
            self.assertGreaterEqual(op, -(1 << 31))
            self.assertLess(op, 1 << 31)


class TestPostFlashDumps(unittest.TestCase):
    """Firmware 1.55, dumped in both modes from a real board."""

    @classmethod
    def setUpClass(cls):
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
        try:
            cls.kb = bytes.fromhex(
                ic.load_profile(os.path.join(here, "ipac2-1.55-keyboard.json"))["raw"])
            cls.di = bytes.fromhex(
                ic.load_profile(os.path.join(here, "ipac2-1.55-dinput.json"))["raw"])
        except (OSError, ic.ProtocolError):
            raise unittest.SkipTest("post-flash dumps not present")

    def test_mode_is_not_stored_in_the_config(self):
        """Keyboard and Dinput dumps are byte-identical."""
        self.assertEqual(ic.diff_config(self.kb, self.di), [])
        self.assertEqual(self.kb, self.di)

    def test_firmware_is_reported_despite_a_different_header_prefix(self):
        """1.44 answers 00 00 ver cfg; 1.55 answers 50 dd ver cfg."""
        self.assertEqual(self.kb[2], 0x55)
        self.assertEqual(ic.decode_config(self.kb)["firmware"], "0.55")

    def test_the_flash_preserved_the_key_mapping(self):
        actions = {p["name"]: p["action"] for p in ic.decode_config(self.kb)["pins"]}
        self.assertEqual(actions["1sw1"], "CTRL L")
        self.assertEqual(actions["2sw5"], "I")
        self.assertEqual(len(actions), 32)

    def test_shift_survived_the_flash(self):
        pins = ic.decode_config(self.kb)["pins"]
        self.assertTrue(next(p for p in pins if p["name"] == "1start").get("shift"))

    def test_a_config_we_built_has_no_firmware_field(self):
        self.assertIsNone(ic.decode_config(ic.default_config()).get("firmware"))


class TestModeDetection(unittest.TestCase):
    """Multi-mode firmware reports the mode in the product id."""

    @staticmethod
    def _info(product, interface=2, bcd=0x0055):
        return ic.DeviceInfo("/dev/hidraw0", ic.VENDOR_2015, product, bcd, interface, "1-1")

    def test_keyboard_mode(self):
        info = self._info(0x0420)
        self.assertEqual(info.mode, "keyboard")
        self.assertTrue(info.is_ipac2)

    def test_dinput_mode(self):
        info = self._info(0x0421)
        self.assertEqual(info.mode, "Dinput game controller")
        self.assertTrue(info.is_ipac2)

    def test_an_unknown_mode_is_named_not_swallowed(self):
        info = self._info(0x0422)
        self.assertIn("0422", info.mode)
        self.assertFalse(info.is_ipac2)

    def test_another_board_is_not_an_ipac2(self):
        self.assertFalse(self._info(0x0430).is_ipac2)  # I-PAC 4

    def test_mode_is_reported_to_the_web_ui(self):
        self.assertEqual(self._info(0x0421).as_dict()["mode"], "Dinput game controller")


class TestFlashWriteBlocked(unittest.TestCase):
    """Only keyboard mode commits a write to flash - confirmed on hardware.

    In Dinput the board takes all 65 messages, acts on them immediately and
    then drops the commit, so the config reverts on the next power cycle with
    nothing anywhere reporting a failure.
    """

    @staticmethod
    def _info(product):
        return ic.DeviceInfo("/dev/hidraw0", ic.VENDOR_2015, product, 0x0055, 2, "1-1")

    def test_keyboard_mode_commits(self):
        self.assertIsNone(ic.flash_write_blocked(self._info(ic.PRODUCT_IPAC2)))

    def test_dinput_does_not_commit(self):
        reason = ic.flash_write_blocked(self._info(0x0421))
        self.assertIsNotNone(reason)
        self.assertIn("Dinput", reason)

    def test_the_reason_says_how_to_get_out_of_it(self):
        reason = ic.flash_write_blocked(self._info(0x0421))
        self.assertIn("Start1+P1SW1", reason)

    def test_an_unknown_mode_is_treated_as_unsafe(self):
        self.assertIsNotNone(ic.flash_write_blocked(self._info(0x0422)))

    def test_a_pre_2015_board_is_not_second_guessed(self):
        info = ic.DeviceInfo("/dev/hidraw0", ic.VENDOR_PRE2015,
                             ic.PRODUCT_PRE2015, 0x0100, 2, "1-1")
        self.assertIsNone(ic.flash_write_blocked(info))


class TestAnalyseCapture(unittest.TestCase):
    """The capture analyser, against a synthetic tshark dump.

    The bug this locks out: only the first message of a burst carries a
    header byte. Reading byte 1 of all 65 messages of a download as a header
    reported sixteen candidate commands, burying the one real one.
    """

    @classmethod
    def setUpClass(cls):
        here = os.path.dirname(os.path.abspath(__file__))
        if not os.path.exists(os.path.join(here, "analyse_capture.py")):
            raise unittest.SkipTest("analyse_capture.py not present")
        import analyse_capture
        cls.ac = analyse_capture

    def _csv(self, bursts, address=7, usbmon=False):
        """bursts: list of (list-of-payloads). Three seconds between them.

        `address` may be a list, one entry per burst, to model the board
        re-enumerating. `usbmon` emits each URB twice, submit and complete,
        which is what a Linux host capture actually looks like.
        """
        import csv as _csv
        rows, n, t, urb = [], 0, 0.0, 0x1000
        for index, burst in enumerate(bursts):
            addr = address[index] if isinstance(address, list) else address
            t += 3.0
            for payload in burst:
                urb += 8
                for _ in range(2 if usbmon else 1):
                    n += 1
                    t += 0.002
                    rows.append({
                        "frame.number": n,
                        "frame.time_relative": "%.6f" % t,
                        "usb.device_address": str(addr),
                        "usb.urb_id": "0x%x" % urb,
                        "usb.setup.wIndex": "2",
                        "usb.capdata": ":".join("%02x" % b for b in payload),
                    })
        fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="")
        self.addCleanup(os.unlink, fh.name)
        writer = _csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
        fh.close()
        return fh.name

    def _raw_csv(self, rows):
        """Write exactly these rows, for testing the capture diagnostics."""
        import csv as _csv
        fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="")
        self.addCleanup(os.unlink, fh.name)
        writer = _csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
        fh.close()
        return fh.name

    @staticmethod
    def _download(tail=b"\x00\x00\x00\x00"):
        buf = bytearray(ic.CONFIG_SIZE)
        buf[0], buf[1], buf[2] = ic.HEADER_WRITE
        padded = bytes(buf) + tail
        return [bytes([3]) + padded[p:p + 4] for p in range(0, 260, 4)]

    def test_a_download_is_one_burst_with_one_header(self):
        path = self._csv([self._download()])
        blocks = self.ac.group_blocks(self.ac.read_messages(path))
        self.assertEqual(len(blocks), 1)
        self.assertEqual(len(blocks[0]), 65)
        self.assertEqual(blocks[0][0].header, 0x50)

    def test_a_download_does_not_produce_candidates(self):
        """Byte 1 of a config message is data, not a header."""
        path = self._csv([self._download()])
        blocks = self.ac.group_blocks(self.ac.read_messages(path))
        unknown = [b for b in blocks if b[0].header not in self.ac.KNOWN_HEADERS]
        self.assertEqual(unknown, [])

    def test_an_unknown_single_message_is_a_candidate(self):
        path = self._csv([
            [bytes([3, 0x59, 0xdd, 0x0f, 0x00])],
            self._download(),
            [bytes([3, 0x5B, 0xdd, 0x04, 0x00])],
        ])
        blocks = self.ac.group_blocks(self.ac.read_messages(path))
        self.assertEqual([b[0].header for b in blocks], [0x59, 0x50, 0x5B])
        unknown = [b for b in blocks if b[0].header not in self.ac.KNOWN_HEADERS]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0][0].payload, bytes([3, 0x5B, 0xdd, 0x04, 0x00]))

    def test_the_trailing_four_bytes_are_recovered(self):
        """The open question about bytes 256-259 of a download."""
        path = self._csv([self._download(tail=bytes([0x59, 0xdd, 0x0F, 0x00]))])
        block = self.ac.group_blocks(self.ac.read_messages(path))[0]
        data = self.ac.reassemble(block)
        self.assertEqual(len(data), 260)
        self.assertEqual(data[256:260], bytes([0x59, 0xdd, 0x0F, 0x00]))

    def test_non_config_traffic_is_ignored(self):
        """Anything that is not a five byte report-id-3 message is not ours."""
        path = self._csv([[bytes([0x01, 0x00, 0x00]), bytes([3, 0x59, 0xdd, 0x0F, 0x00])]])
        messages = self.ac.read_messages(path)
        self.assertEqual(len(messages), 1)

    def test_an_address_filter_excludes_other_devices(self):
        path = self._csv([[bytes([3, 0x59, 0xdd, 0x0F, 0x00])]])
        self.assertEqual(len(self.ac.read_messages(path, {7})), 1)
        self.assertEqual(len(self.ac.read_messages(path, {9})), 0)

    def test_usbmon_submit_and_complete_count_once(self):
        """usbmon logs every transfer twice; 65 messages must not become 130."""
        path = self._csv([self._download()], usbmon=True)
        messages = self.ac.read_messages(path)
        self.assertEqual(len(messages), 65)
        blocks = self.ac.group_blocks(messages)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0][0].header, 0x50)

    def test_the_trailing_bytes_survive_deduplication(self):
        path = self._csv([self._download(tail=bytes([0x59, 0xdd, 0x0F, 0x00]))],
                         usbmon=True)
        block = self.ac.group_blocks(self.ac.read_messages(path))[0]
        self.assertEqual(self.ac.reassemble(block)[256:260],
                         bytes([0x59, 0xdd, 0x0F, 0x00]))

    def test_a_mode_switch_shows_as_a_new_device_address(self):
        """The board re-enumerates, so a capture of a switch spans addresses."""
        path = self._csv(
            [[bytes([3, 0x5B, 0xdd, 0x04, 0x00])],
             [bytes([3, 0x59, 0xdd, 0x0F, 0x00])]],
            address=[12, 14], usbmon=True,
        )
        messages = self.ac.read_messages(path)
        self.assertEqual([m.address for m in messages], [12, 14])

    def test_the_boards_answers_are_recovered(self):
        """Responses arrive as HID data on 0x84, not in any usb.* payload field.

        Missing them costs the most useful thing in a capture: consecutive
        reads diff down to the exact byte a write moved.
        """
        rows = []
        for index, fill in enumerate((0x11, 0x22)):
            config = bytes([0x50, 0xDD, 0x55, 0x00]) + bytes([fill]) * 252
            for pos in range(0, 256, 4):
                rows.append({
                    "frame.number": str(len(rows) + 1),
                    "usb.endpoint_address": "0x84",
                    "usbhid.data": ":".join(
                        "%02x" % b for b in bytes([3]) + config[pos:pos + 4]),
                })
        path = self._raw_csv(rows)
        configs = self.ac.read_responses(path)
        self.assertEqual(len(configs), 2)
        self.assertEqual(len(configs[0]), 256)
        self.assertEqual(configs[0][:4], bytes([0x50, 0xDD, 0x55, 0x00]))
        self.assertEqual(configs[0][4], 0x11)
        self.assertEqual(configs[1][4], 0x22)

    def test_traffic_on_other_endpoints_is_not_a_response(self):
        rows = [{
            "frame.number": "1",
            "usb.endpoint_address": "0x02",
            "usbhid.data": "03:50:dd:55:00",
        }]
        self.assertEqual(self.ac.read_responses(self._raw_csv(rows)), [])

    def test_consecutive_reads_diff_to_the_changed_byte(self):
        a = bytes(256)
        b = bytearray(a)
        b[44] = 0x92
        changes = self.ac.diff_responses([a, bytes(b)])
        self.assertEqual(changes, [(0, 44, 0x00, 0x92)])

    def test_identical_reads_diff_to_nothing(self):
        a = bytes(256)
        self.assertEqual(self.ac.diff_responses([a, a]), [])

    def test_an_empty_capture_says_so(self):
        """Nothing recorded is a capture fault, not a filtering one."""
        path = self._raw_csv([{"frame.number": "1", "usb.bus_id": "1",
                               "usb.device_address": "4", "usb.capdata": ""}])
        text = self.ac.describe_capture(path)
        self.assertIn("caught no traffic at all", text)

    def test_traffic_from_other_devices_says_so(self):
        """Payloads present but none five bytes means the wrong bus."""
        path = self._raw_csv([
            {"frame.number": "1", "usb.bus_id": "1",
             "usb.device_address": "4", "usb.capdata": "01:02:03:04:05:06:07:08"},
            {"frame.number": "2", "usb.bus_id": "2",
             "usb.device_address": "9", "usb.capdata": "aa:bb"},
        ])
        text = self.ac.describe_capture(path)
        self.assertIn("none of it is a five byte message", text)
        self.assertNotIn("caught no traffic at all", text)

    def test_the_description_lists_buses_and_addresses(self):
        """So a wrong-bus capture can be turned into a right-bus one."""
        path = self._raw_csv([
            {"frame.number": "1", "usb.bus_id": "3",
             "usb.device_address": "11", "usb.capdata": "03:59:dd:0f:00"},
        ])
        text = self.ac.describe_capture(path)
        self.assertIn("usb buses:     3", text)
        self.assertIn("11", text)

    def test_filtering_on_one_address_loses_the_other_half(self):
        """Why --address takes a list: a single value hides the switch."""
        path = self._csv(
            [[bytes([3, 0x5B, 0xdd, 0x04, 0x00])],
             [bytes([3, 0x59, 0xdd, 0x0F, 0x00])]],
            address=[12, 14],
        )
        self.assertEqual(len(self.ac.read_messages(path, {12})), 1)
        self.assertEqual(len(self.ac.read_messages(path, {12, 14})), 2)


class TestModeHotkeys(unittest.TestCase):
    """Five modes, and only three of them act on the config.

    Getting this wrong is not cosmetic: Start1+P1SW2 reaches mode 2, the
    Dinput *preset*, which runs a fixed internal map. A profile written and
    then checked in mode 2 looks like the write was ignored, because as far
    as that mode is concerned it was.
    """

    def test_there_are_five(self):
        self.assertEqual([m[0] for m in ic.MODE_HOTKEYS], [1, 2, 3, 4, 5])

    def test_the_hotkey_matches_the_mode_number(self):
        for number, button, _, _, _ in ic.MODE_HOTKEYS:
            self.assertEqual(button, "P1SW%d" % number)

    def test_the_preset_modes_do_not_use_the_config(self):
        presets = {n for n, _, _, uses, _ in ic.MODE_HOTKEYS if not uses}
        self.assertEqual(presets, {2, 3})

    def test_the_user_set_modes_do(self):
        user_set = {n for n, _, _, uses, _ in ic.MODE_HOTKEYS if uses}
        self.assertEqual(user_set, {1, 4, 5})

    def test_observed_ids_are_recorded_where_we_have_them(self):
        seen = {n: pid for n, _, _, _, pid in ic.MODE_HOTKEYS if pid}
        self.assertEqual(seen, {1: "d209:0420", 4: "d209:0421"})

    def test_mode_4_is_dinput(self):
        """Confirmed on a 1.55 board: led flashes four times, comes up 0421.

        An earlier run of the same hotkey gave 045e:028e and was recorded as a
        contradiction. It was not one - the board was holding a different
        config. Nothing is marked contested any more.
        """
        number, _, name, uses_config, product = ic.MODE_HOTKEYS[3]
        self.assertEqual(number, 4)
        self.assertEqual(name, "Dinput user set")
        self.assertTrue(uses_config)
        self.assertEqual(product, "d209:0421")
        self.assertEqual(ic.MODE_HOTKEY_CONFLICTS, {})

    def test_the_observed_id_matches_what_the_mode_table_decodes(self):
        """The product id recorded for a mode must decode to that mode."""
        for _, _, name, _, product in ic.MODE_HOTKEYS:
            if not product:
                continue
            vendor, prod = (int(part, 16) for part in product.split(":"))
            mode = ic.board_mode(vendor, prod, "Ultimarc", "I-PAC 2")
            with self.subTest(product=product):
                self.assertEqual(name.split()[0].lower(), mode.split()[0].lower())


class TestConfigKind(unittest.TestCase):
    """What the board will make of a download.

    Multi-mode firmware picks its mode from the content of what it is sent,
    so this decides whether an apply moves the board or leaves it where it
    is. The mixed case is the one that bites.
    """

    @staticmethod
    def _raw(actions):
        """A config with exactly these pins assigned and nothing else."""
        buf = bytearray(ic.CONFIG_SIZE)
        buf[0], buf[1], buf[2] = ic.HEADER_WRITE
        for name, action in actions.items():
            index = ic.PIN_TABLE[name][0]
            buf[4 + index] = ic.name_to_code(action)
        return bytes(buf)

    def test_keyboard_only(self):
        self.assertEqual(ic.config_kind(self._raw({"1sw1": "A", "1sw2": "B"})), "keyboard")

    def test_gamepad_only(self):
        raw = self._raw({"1sw1": "GAMEPAD 1", "1up": "HAT 0 UP", "2sw1": "ANALOG 1"})
        self.assertEqual(ic.config_kind(raw), "gamepad")

    def test_one_keycode_makes_it_mixed(self):
        raw = self._raw({"1sw1": "GAMEPAD 1", "1up": "UP"})  # UP is the keycode
        self.assertEqual(ic.config_kind(raw), "mixed")

    def test_an_alternate_action_counts(self):
        """The shifted code is part of the download, so it decides too."""
        buf = bytearray(self._raw({"1sw1": "GAMEPAD 1"}))
        buf[4 + ic.PIN_TABLE["1sw1"][1]] = ic.name_to_code("5")
        self.assertEqual(ic.config_kind(bytes(buf)), "mixed")

    def test_an_empty_config_has_no_opinion(self):
        self.assertEqual(ic.config_kind(bytes(ic.CONFIG_SIZE)), "mixed")


class TestUnconfirmedCodeWarning(unittest.TestCase):
    """Gamepad codes above the confirmed button range are not buttons.

    Confirmed on hardware: 0x8e..0x98 arrive as EV_KEY, but 0x9a-0x9c produce
    hat events and 0x9d an axis. A pin named "GAMEPAD 16" moved an axis. The
    names above the range are placeholders, and a write says so.
    """

    @staticmethod
    def _raw(actions):
        buf = bytearray(ic.CONFIG_SIZE)
        buf[0], buf[1], buf[2] = ic.HEADER_WRITE
        for name, action in actions.items():
            buf[4 + ic.PIN_TABLE[name][0]] = ic.name_to_code(action)
        return bytes(buf)

    def test_the_confirmed_range_is_quiet(self):
        top = "GAMEPAD %d" % (ic.GAMEPAD_BUTTONS_CONFIRMED - 1)   # zero based
        raw = self._raw({"1sw1": "GAMEPAD 0", "1sw2": top})
        self.assertIsNone(ic.unconfirmed_code_warning(raw))

    def test_a_code_above_the_block_is_flagged(self):
        raw = self._raw({"1right": "GAMEPAD 15"})   # 0x9d, tried and useless
        warning = ic.unconfirmed_code_warning(raw)
        self.assertIn("1right", warning)
        self.assertIn("0x9d", warning)

    def test_the_last_button_is_not_flagged(self):
        top = "GAMEPAD %d" % (ic.GAMEPAD_BUTTONS_CONFIRMED - 1)
        self.assertIsNone(ic.unconfirmed_code_warning(self._raw({"1sw1": top})))

    def test_the_hat_is_not_flagged(self):
        """It is identified, not unmapped - buttons below it, unknown above."""
        for direction in ic.DPAD_DIRECTIONS:
            with self.subTest(direction=direction):
                self.assertIsNone(ic.unconfirmed_code_warning(
                    self._raw({"1up": ic.DPAD_NAME % direction})))

    def test_the_first_code_past_the_dpad_is_flagged(self):
        """0x9d - observed moving an axis when it was asked for a button."""
        buf = bytearray(self._raw({}))
        buf[4 + ic.PIN_TABLE["1sw1"][0]] = ic.DPAD_FIRST_CODE + ic.DPAD_COUNT
        self.assertIsNotNone(ic.unconfirmed_code_warning(bytes(buf)))

    def test_keycodes_and_analog_are_not_flagged(self):
        """It is only about the gamepad range."""
        raw = self._raw({"1sw1": "CTRL L", "1up": "HAT 0 UP", "1down": "ANALOG 0"})
        self.assertIsNone(ic.unconfirmed_code_warning(raw))

    def test_alternate_actions_count_too(self):
        buf = bytearray(self._raw({}))
        buf[4 + ic.PIN_TABLE["1sw1"][1]] = ic.name_to_code("GAMEPAD 19")
        self.assertIn("1sw1 alt", ic.unconfirmed_code_warning(bytes(buf)))


class TestPlayerAttribution(unittest.TestCase):
    """A code both players carry can only be attributed with two pad nodes.

    Both players use the same GAMEPAD codes - in Dinput each is a separate
    controller, so they do not collide on the host. But if the board presents
    only ONE pad node, every event arrives as player 1, and narrowing to the
    player 1 pin turns a guess into a confident wrong answer: pressing player
    2's start reported "1start".
    """

    PROFILE = {"pins": [{"name": "1start", "action": "GAMEPAD 7"},
                        {"name": "2start", "action": "GAMEPAD 7"}]}

    @staticmethod
    def _device(node, player):
        dev = ic.InputDevice(path="/dev/input/" + node, name="I-PAC 2",
                             vendor=ic.VENDOR_2015, product=0x0421,
                             interface=2, joystick=True)
        dev.player = player
        return dev

    def _monitor(self, devices):
        return ic.BaseMonitor(devices, profile=self.PROFILE)

    def test_two_pads_can_be_told_apart(self):
        mon = self._monitor([self._device("event2", 1), self._device("event6", 2)])
        self.assertTrue(mon._can_tell_players_apart)

    def test_one_pad_cannot(self):
        mon = self._monitor([self._device("event2", 1)])
        self.assertFalse(mon._can_tell_players_apart)

    def test_a_player_two_press_decodes_to_a_player_two_code(self):
        """Each player numbers its own buttons from zero, so the block matters.

        Without this the monitor named every player 2 press with a player 1
        code and pointed at a player 1 pin - a press on one panel reported as
        a pin on the other.
        """
        profile = {"pins": [{"name": "1sw2", "action": "GAMEPAD 1"},
                            {"name": "2sw2", "action": "P2 GAMEPAD 1"}]}
        mon = ic.BaseMonitor(
            [self._device("event2", 1), self._device("event6", 2)], profile=profile)
        one = mon.translate(mon.devices[0], ic.EV_KEY, ic.BTN_JOYSTICK + 1, 1)
        two = mon.translate(mon.devices[1], ic.EV_KEY, ic.BTN_JOYSTICK + 1, 1)
        self.assertEqual(one["name"], "GAMEPAD 1")
        self.assertEqual([p["pin"] for p in one["pins"]], ["1sw2"])
        self.assertEqual(two["name"], "P2 GAMEPAD 1")
        self.assertEqual([p["pin"] for p in two["pins"]], ["2sw2"])

    def test_the_block_offset_is_one_per_player(self):
        self.assertEqual(ic.player_block_first(1), ic.GAMEPAD_FIRST_CODE)
        self.assertEqual(ic.player_block_first(2), ic.P2_FIRST_CODE)
        self.assertEqual(ic.player_block_first(None), ic.GAMEPAD_FIRST_CODE)

    def test_with_one_pad_a_press_names_both(self):
        """Ambiguous, and saying so beats naming the wrong one."""
        mon = self._monitor([self._device("event2", 1)])
        event = mon.translate(mon.devices[0], ic.EV_KEY, ic.BTN_JOYSTICK + 7, 1)
        self.assertEqual(sorted(p["pin"] for p in event["pins"]),
                         ["1start", "2start"])

    def test_the_ambiguous_line_flags_itself(self):
        mon = self._monitor([self._device("event2", 1)])
        event = mon.translate(mon.devices[0], ic.EV_KEY, ic.BTN_JOYSTICK + 7, 1)
        self.assertIn("several pins carry this code", ic.monitor_line(event))


class TestDpadCodes(unittest.TestCase):
    """0x99..0x9c are the d-pad, not buttons 12..15.

    Established one code at a time on a 1.55 board: 0x9a and 0x9b move
    ABS_HAT0Y, 0x9c and 0x99 move ABS_HAT0X, and a stick on all four navigates
    EmulationStation. 0x9d is past the block and moves an ordinary axis, which
    is how the end was found - a "right" that did nothing.
    """

    def test_the_block_sits_directly_above_the_buttons(self):
        last_button = ic.GAMEPAD_FIRST_CODE + ic.GAMEPAD_BUTTONS_CONFIRMED - 1
        self.assertEqual(ic.DPAD_FIRST_CODE, last_button + 1)

    def test_the_block_is_0x99_to_0x9c(self):
        self.assertEqual(ic.DPAD_COUNT, 4)
        self.assertEqual(ic.ALL_CODES["DPAD 1"], 0x99)
        self.assertEqual(ic.ALL_CODES["DPAD 4"], 0x9C)

    def test_the_directions_are_in_code_order(self):
        """Measured on a panel: 0x99 up, 0x9a down, 0x9b left, 0x9c right."""
        self.assertEqual(ic.ALL_CODES["HAT 0 UP"], 0x99)
        self.assertEqual(ic.ALL_CODES["HAT 0 DOWN"], 0x9A)
        self.assertEqual(ic.ALL_CODES["HAT 0 LEFT"], 0x9B)
        self.assertEqual(ic.ALL_CODES["HAT 0 RIGHT"], 0x9C)

    def test_they_are_named_for_the_hat_the_host_reports(self):
        """ABS_HAT0X and ABS_HAT0Y - hat 0. The name says which and which way."""
        self.assertEqual(ic.DPAD_NAME % "UP", "HAT 0 UP")

    def test_opposites_are_adjacent_so_they_share_an_axis(self):
        """The failure this guards against is not a mirrored stick.

        Split up/down across two axes and the hat never centres cleanly:
        diagonals become impossible and the stick reads as sluggish and
        sticky. Up/down must be one pair of codes and left/right the other.
        """
        self.assertEqual(ic.ALL_CODES["HAT 0 DOWN"] - ic.ALL_CODES["HAT 0 UP"], 1)
        self.assertEqual(ic.ALL_CODES["HAT 0 RIGHT"] - ic.ALL_CODES["HAT 0 LEFT"], 1)

    def test_the_named_form_wins_when_decoding(self):
        """"HAT 0 UP" can be checked by reading it; "DPAD 1" cannot."""
        self.assertEqual(ic.code_to_name(0x99), "HAT 0 UP")

    def test_the_older_names_still_resolve(self):
        """Profiles written before the codes were measured still apply."""
        for n, direction in enumerate(ic.DPAD_DIRECTIONS, start=1):
            with self.subTest(n=n):
                self.assertEqual(ic.name_to_code("DPAD %d" % n),
                                 ic.name_to_code(ic.DPAD_NAME % direction))
                self.assertEqual(ic.name_to_code("DPAD %s" % direction),
                                 ic.name_to_code(ic.DPAD_NAME % direction))

    def test_the_hat_wins_when_a_byte_is_decoded(self):
        """An old profile calling 0x99 "GAMEPAD 12" reads back as the hat."""
        for n, direction in enumerate(ic.DPAD_DIRECTIONS, start=1):
            with self.subTest(n=n):
                self.assertEqual(ic.code_to_name(0x98 + n),
                                 ic.DPAD_NAME % direction)

    def test_a_gamepad_name_can_still_land_on_the_hat(self):
        """The block is contiguous with the buttons, so it has GAMEPAD names
        too. They resolve, but decode back as the hat, which is the point."""
        self.assertEqual(ic.name_to_code("GAMEPAD 11"), ic.ALL_CODES["HAT 0 UP"])
        self.assertEqual(ic.code_to_name(ic.name_to_code("GAMEPAD 11")), "HAT 0 UP")

    def test_a_dpad_action_counts_as_a_gamepad_action(self):
        """Otherwise a d-pad profile reads as mixed and the mode never switches."""
        self.assertIn("DPAD", ic.GAMEPAD_PREFIXES)

    def test_the_first_unmapped_code_is_past_the_block(self):
        self.assertEqual(ic.code_to_name(0x9D), "GAMEPAD 15")


class TestShippedGamepadProfile(unittest.TestCase):
    """profiles/gamepad.json must stay gamepad-ONLY, or it stops working.

    The whole point of it is that Ultimarc's firmware switches the board to
    Dinput mode 4 by itself when the download is entirely gamepad actions. One
    stray keycode - a pin left unassigned so it keeps the board's old value, an
    alternate action not cleared - makes the download mixed and the switch
    never fires, which looks exactly like the write being ignored.
    """

    @classmethod
    def setUpClass(cls):
        here = os.path.dirname(os.path.abspath(__file__))
        profile = os.path.join(here, "profiles", "gamepad.json")
        fixture = os.path.join(here, "fixtures", "ipac2-1.55-keyboard.json")
        for path in (profile, fixture):
            if not os.path.exists(path):
                raise unittest.SkipTest("missing %s" % path)
        cls.profile = ic.load_profile(profile)
        cls.base = bytes.fromhex(ic.load_profile(fixture)["raw"])
        cls.raw = bytes(ic.encode_config(cls.profile, cls.base))

    def test_every_pin_is_assigned(self):
        named = {pin["name"] for pin in self.profile["pins"]}
        self.assertEqual(named, set(ic.PIN_ORDER))

    def test_it_is_gamepad_only_over_a_factory_board(self):
        self.assertEqual(ic.config_kind(self.raw), "gamepad")

    def test_every_alternate_action_is_cleared(self):
        data = self.raw[4:]
        leftover = [n for n in ic.PIN_ORDER if data[ic.PIN_TABLE[n][1]]]
        self.assertEqual(leftover, [])

    def test_the_sticks_are_hats(self):
        """Four buttons are not a d-pad; each player's hat block is."""
        data = self.raw[4:]
        for player, first in ((1, ic.GAMEPAD_FIRST_CODE), (2, ic.P2_FIRST_CODE)):
            hat = ic.control_span(first)[1]
            for direction in ("up", "down", "left", "right"):
                name = "%d%s" % (player, direction)
                with self.subTest(pin=name):
                    self.assertIn(data[ic.PIN_TABLE[name][0]], hat)

    def test_each_direction_gets_a_different_dpad_code(self):
        """Two directions on one code would collapse them into one."""
        data = self.raw[4:]
        for player in (1, 2):
            codes = {data[ic.PIN_TABLE["%d%s" % (player, d)][0]]
                     for d in ("up", "down", "left", "right")}
            with self.subTest(player=player):
                self.assertEqual(len(codes), 4)

    def test_each_direction_gets_the_code_that_means_it(self):
        """Measured for player 1; player 2 follows the block by symmetry."""
        data = self.raw[4:]
        for player, prefix in ((1, "HAT 0 %s"), (2, "P2 HAT %s")):
            for direction in ("up", "down", "left", "right"):
                pin = "%d%s" % (player, direction)
                with self.subTest(pin=pin):
                    self.assertEqual(
                        ic.code_to_name(data[ic.PIN_TABLE[pin][0]]),
                        prefix % direction.upper())

    def test_opposite_directions_are_on_the_same_axis(self):
        """up/down on one axis, left/right on the other - or the stick sticks."""
        data = self.raw[4:]
        for player in (1, 2):
            for a, b in (("up", "down"), ("left", "right")):
                one = data[ic.PIN_TABLE["%d%s" % (player, a)][0]]
                two = data[ic.PIN_TABLE["%d%s" % (player, b)][0]]
                with self.subTest(player=player, pair=(a, b)):
                    self.assertEqual(abs(one - two), 1)

    def test_no_direction_uses_0x9d(self):
        """It is not a stick direction; assigned to one it does nothing."""
        data = self.raw[4:]
        for player in (1, 2):
            for direction in ("up", "down", "left", "right"):
                pin = "%d%s" % (player, direction)
                with self.subTest(pin=pin):
                    self.assertNotEqual(data[ic.PIN_TABLE[pin][0]], 0x9D)

    def test_the_players_share_no_code_at_all(self):
        """A shared code makes both pins the same button on ONE controller.

        Confirmed the hard way: with identical codes, player 2's presses
        arrived on player 1's node and its buttons mirrored player 1's.
        """
        data = self.raw[4:]
        one = {data[ic.PIN_TABLE[n][0]] for n in ic.PIN_ORDER if n.startswith("1")}
        two = {data[ic.PIN_TABLE[n][0]] for n in ic.PIN_ORDER if n.startswith("2")}
        self.assertEqual(one & two, set())

    def test_player_two_is_player_one_shifted_by_one_block(self):
        data = self.raw[4:]
        for suffix in ("sw1", "sw6", "coin", "start", "up", "down", "left", "right"):
            with self.subTest(control=suffix):
                self.assertEqual(
                    data[ic.PIN_TABLE["2" + suffix][0]]
                    - data[ic.PIN_TABLE["1" + suffix][0]],
                    ic.PLAYER_BLOCK)

    def test_no_pin_uses_an_unmapped_code(self):
        self.assertIsNone(ic.unconfirmed_code_warning(self.raw))

    def test_it_tells_you_to_switch_mode_by_hand(self):
        """Because the documented automatic switch did not fire on hardware."""
        info = ic.DeviceInfo("/dev/hidraw0", ic.VENDOR_2015,
                             ic.PRODUCT_IPAC2, 0x0055, 2, "1-1")
        self.assertIn("by hand", ic.mode_switch_note(self.raw, info))

    def test_start_and_coin_have_the_low_numbers(self):
        """They are wired on this panel and sw7/sw8 are not."""
        data = self.raw[4:]
        for player, prefix in ((1, ""), (2, "P2 ")):
            with self.subTest(player=player):
                self.assertEqual(
                    ic.code_to_name(data[ic.PIN_TABLE["%dcoin" % player][0]]),
                    prefix + "GAMEPAD 6")
                self.assertEqual(
                    ic.code_to_name(data[ic.PIN_TABLE["%dstart" % player][0]]),
                    prefix + "GAMEPAD 7")

    def test_the_unwired_pins_are_still_assigned(self):
        """An unassigned pin keeps the board's old keycode, which makes the
        whole download mixed and stops it being a gamepad config."""
        data = self.raw[4:]
        for player, first in ((1, ic.GAMEPAD_FIRST_CODE), (2, ic.P2_FIRST_CODE)):
            buttons = ic.control_span(first)[0]
            for suffix in ("sw7", "sw8"):
                pin = "%d%s" % (player, suffix)
                with self.subTest(pin=pin):
                    self.assertIn(data[ic.PIN_TABLE[pin][0]], buttons)

    def test_no_wired_control_shares_a_code(self):
        """Only the two admin pins may collide - everything else is distinct."""
        data = self.raw[4:]
        wired = ["sw1","sw2","sw3","sw4","sw5","sw6","sw7","sw8","coin","start","a"]
        for player in (1, 2):
            codes = [data[ic.PIN_TABLE["%d%s" % (player, s)][0]] for s in wired]
            with self.subTest(player=player):
                self.assertEqual(len(set(codes)), len(codes))

    def test_each_player_uses_at_most_eleven_button_codes(self):
        """Eleven per block. Claiming a twelfth is how "right" broke."""
        data = self.raw[4:]
        for player, first in ((1, ic.GAMEPAD_FIRST_CODE), (2, ic.P2_FIRST_CODE)):
            buttons = ic.control_span(first)[0]
            used = {data[ic.PIN_TABLE[n][0]] for n in ic.PIN_ORDER
                    if n.startswith(str(player))
                    and data[ic.PIN_TABLE[n][0]] in buttons}
            with self.subTest(player=player):
                self.assertLessEqual(len(used), ic.GAMEPAD_BUTTONS_CONFIRMED)

    def test_it_carries_no_home_key(self):
        """A HOME key would send the board to Xinput mode 5 instead of 4."""
        actions = {pin.get("action", "") for pin in self.profile["pins"]}
        self.assertNotIn("HOME", actions)

    def test_start1_is_still_the_shift_control(self):
        data = self.raw[4:]
        shift = [n for n in ic.PIN_ORDER if data[ic.PIN_TABLE[n][2]] & ic.SHIFT_BIT]
        self.assertEqual(shift, ["1start"])

    def test_it_warns_about_the_hotkeys(self):
        """Unavoidable for a gamepad-only map, but it must not be silent."""
        self.assertIsNotNone(ic.hotkey_warning(self.raw))

    def test_it_round_trips(self):
        again = bytes(ic.encode_config(ic.decode_config(self.raw), self.raw))
        self.assertEqual(again[3:256], self.raw[3:256])


class TestGamepadTemplateIsMixed(unittest.TestCase):
    """The shipped gamepad template cannot trigger the automatic switch.

    It assigns the buttons but not the stick pins, and never clears the
    alternate actions - so applied to a factory board the download still
    carries keycodes and the board stays in keyboard mode. This is the
    regression net for the thing that made a correct write look ignored.
    """

    @classmethod
    def setUpClass(cls):
        here = os.path.dirname(os.path.abspath(__file__))
        fixture = os.path.join(here, "fixtures", "ipac2-1.55-keyboard.json")
        template = os.path.join(here, "profiles", "batocera-gamepad.template.json")
        for path in (fixture, template):
            if not os.path.exists(path):
                raise unittest.SkipTest("missing %s" % path)
        cls.base = bytes.fromhex(ic.load_profile(fixture)["raw"])
        cls.template = ic.load_profile(template)

    def test_the_factory_board_is_keyboard_only(self):
        self.assertEqual(ic.config_kind(self.base), "keyboard")

    def test_the_template_over_it_is_mixed(self):
        raw = bytes(ic.encode_config(self.template, self.base))
        self.assertEqual(ic.config_kind(raw), "mixed")

    def test_and_the_note_says_why(self):
        raw = bytes(ic.encode_config(self.template, self.base))
        info = ic.DeviceInfo("/dev/hidraw0", ic.VENDOR_2015,
                             ic.PRODUCT_IPAC2, 0x0055, 2, "1-1")
        note = ic.mode_switch_note(raw, info)
        self.assertIn("mixes keyboard and gamepad", note)

    def test_the_mixed_note_names_the_profile_that_is_not_mixed(self):
        raw = bytes(ic.encode_config(self.template, self.base))
        info = ic.DeviceInfo("/dev/hidraw0", ic.VENDOR_2015,
                             ic.PRODUCT_IPAC2, 0x0055, 2, "1-1")
        self.assertIn("gamepad.json", ic.mode_switch_note(raw, info))


class TestHotkeyWarning(unittest.TestCase):
    """A gamepad profile can disarm the escape hatch it needs you to use.

    Confirmed on hardware: after the gamepad template was written, the only
    thing that still worked was holding P1SW1 while plugging in usb. The six
    pins the mode hotkeys use had all been assigned gamepad actions, which do
    nothing while the board is in keyboard mode.
    """

    @staticmethod
    def _raw(actions, shift="1start"):
        buf = bytearray(ic.CONFIG_SIZE)
        buf[0], buf[1], buf[2] = ic.HEADER_WRITE
        for name in ic.PIN_ORDER:
            buf[4 + ic.PIN_TABLE[name][2]] = 0x01
        if shift:
            buf[4 + ic.PIN_TABLE[shift][2]] = 0x01 | ic.SHIFT_BIT
        for name, action in actions.items():
            buf[4 + ic.PIN_TABLE[name][0]] = ic.name_to_code(action)
        return bytes(buf)

    def test_keycodes_on_the_hotkey_pins_are_fine(self):
        raw = self._raw({"1start": "1", "1sw1": "CTRL L", "1sw4": "SHIFT L"})
        self.assertIsNone(ic.hotkey_warning(raw))

    def test_a_gamepad_shift_key_is_flagged(self):
        raw = self._raw({"1start": "GAMEPAD 9"})
        self.assertIn("1start", ic.hotkey_warning(raw))

    def test_a_gamepad_mode_selector_is_flagged(self):
        raw = self._raw({"1sw4": "GAMEPAD 4"})
        self.assertIn("1sw4", ic.hotkey_warning(raw))

    def test_no_shift_key_at_all_is_flagged(self):
        warning = ic.hotkey_warning(self._raw({}, shift=None))
        self.assertIn("no I-PAC shift key", warning)

    def test_the_warning_names_the_way_out(self):
        warning = ic.hotkey_warning(self._raw({"1start": "GAMEPAD 9"}))
        self.assertIn("plugging in usb", warning)

    def test_a_pin_outside_the_hotkeys_is_not_flagged(self):
        self.assertIsNone(ic.hotkey_warning(self._raw({"2sw8": "GAMEPAD 8"})))


class TestShippedProfilesAgainstTheHotkeys(unittest.TestCase):
    """The template disarms the hotkeys; the MAME profile puts them back."""

    @classmethod
    def setUpClass(cls):
        here = os.path.dirname(os.path.abspath(__file__))
        fixture = os.path.join(here, "fixtures", "ipac2-1.55-keyboard.json")
        if not os.path.exists(fixture):
            raise unittest.SkipTest("no board dump present")
        cls.base = bytes.fromhex(ic.load_profile(fixture)["raw"])
        cls.here = here

    def _encoded(self, name):
        path = os.path.join(self.here, "profiles", name)
        if not os.path.exists(path):
            self.skipTest("missing %s" % name)
        return bytes(ic.encode_config(ic.load_profile(path), self.base))

    def test_the_factory_config_keeps_the_hotkeys(self):
        self.assertIsNone(ic.hotkey_warning(self.base))

    def test_the_gamepad_template_warns(self):
        warning = ic.hotkey_warning(self._encoded("batocera-gamepad.template.json"))
        self.assertIsNotNone(warning)
        for pin in ("1start", "1sw1", "1sw4"):
            self.assertIn(pin, warning)

    def test_the_mame_profile_does_not(self):
        self.assertIsNone(ic.hotkey_warning(self._encoded("mame-keyboard.json")))


class TestModeSwitchNote(unittest.TestCase):

    @staticmethod
    def _info(product):
        return ic.DeviceInfo("/dev/hidraw0", ic.VENDOR_2015, product, 0x0055, 2, "1-1")

    @staticmethod
    def _raw(action):
        buf = bytearray(ic.CONFIG_SIZE)
        buf[0], buf[1], buf[2] = ic.HEADER_WRITE
        for name in ic.PIN_ORDER:
            buf[4 + ic.PIN_TABLE[name][0]] = ic.name_to_code(action)
        return bytes(buf)

    def test_gamepad_download_in_keyboard_mode_says_switch_by_hand(self):
        """The documented automatic switch did not happen on a 1.55 board.

        Predicting one it does not perform is worse than saying nothing: it
        turns "the write worked, now switch by hand" into "the write failed".
        """
        note = ic.mode_switch_note(self._raw("GAMEPAD 1"), self._info(0x0420))
        self.assertIn("did not happen", note)
        self.assertIn("by hand", note)

    def test_keyboard_download_in_gamepad_mode_predicts_mode_1(self):
        note = ic.mode_switch_note(self._raw("A"), self._info(0x0421))
        self.assertIn("keyboard mode 1", note)

    def test_nothing_to_say_when_the_mode_already_matches(self):
        self.assertIsNone(ic.mode_switch_note(self._raw("A"), self._info(0x0420)))


class TestDumpRecordsMode(unittest.TestCase):
    """A read returns different bytes per mode, so a dump must say which one.

    fixtures/ipac2-1.55-dinput.json has no mode recorded and is byte-identical
    to the keyboard fixture, which is how a mislabelled capture became a wrong
    claim in the README.
    """

    def test_dump_records_the_mode_it_was_taken_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = os.path.join(tmp, "board.json")
            out = os.path.join(tmp, "dump.json")
            args = types.SimpleNamespace(
                fake_device=board, device=None, output=out, raw=None)
            ic.cmd_dump(args)
            with open(out) as fh:
                dumped = json.load(fh)
        self.assertEqual(dumped["capturedIn"], "keyboard")
        self.assertEqual(dumped["capturedProduct"], "0420")

    def test_the_extra_fields_do_not_break_loading_it_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = os.path.join(tmp, "board.json")
            out = os.path.join(tmp, "dump.json")
            ic.cmd_dump(types.SimpleNamespace(
                fake_device=board, device=None, output=out, raw=None))
            profile = ic.load_profile(out)
            self.assertEqual(len(ic.raw_from_profile(profile, out)), ic.CONFIG_SIZE)


class TestConfigCandidates(unittest.TestCase):
    """Dinput mode adds a fourth interface, so the firmware rule is a guess."""

    @staticmethod
    def _nodes(interfaces, bcd=0x0055):
        return [
            ic.DeviceInfo("/dev/hidraw%d" % i, ic.VENDOR_2015, 0x0421, bcd, i, "1-1")
            for i in interfaces
        ]

    def test_the_firmware_rule_is_tried_first(self):
        order = ic.config_candidates(self._nodes([0, 1, 2, 3]))
        self.assertEqual(order[0].interface, 2)  # 0x55 -> interface 2

    def test_every_interface_is_still_a_candidate(self):
        order = ic.config_candidates(self._nodes([0, 1, 2, 3]))
        self.assertEqual(sorted(d.interface for d in order), [0, 1, 2, 3])

    def test_highest_interface_is_tried_before_the_low_ones(self):
        order = ic.config_candidates(self._nodes([0, 1, 2, 3]))
        self.assertEqual([d.interface for d in order], [2, 3, 1, 0])

    def test_no_devices_is_not_an_error(self):
        self.assertEqual(ic.config_candidates([]), [])


class TestFakeBoard(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "board.json")

    def test_creates_a_default_config(self):
        board = ic.FakeBoard(self.path)
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(len(board.read_config()), ic.CONFIG_SIZE)

    def test_writes_persist(self):
        board = ic.FakeBoard(self.path)
        updated = ic.encode_config(
            {"pins": [{"name": "1up", "action": "W"}]}, board.read_config()
        )
        board.write_config(bytes(updated))
        reopened = ic.FakeBoard(self.path)
        profile = ic.decode_config(reopened.read_config())
        pin = next(p for p in profile["pins"] if p["name"] == "1up")
        self.assertEqual(pin["action"], "W")

    def test_reports_the_boards_firmware(self):
        self.assertEqual(ic.FakeBoard(self.path).info.firmware, "0.44")


class TestProfilesOnDisk(unittest.TestCase):
    def test_shipped_profiles_encode(self):
        here = os.path.dirname(os.path.abspath(__file__))
        directory = os.path.join(here, "profiles")
        names = [n for n in os.listdir(directory) if n.endswith(".json")]
        self.assertTrue(names, "no profiles found")
        for name in names:
            with self.subTest(profile=name):
                profile = ic.load_profile(os.path.join(directory, name))
                raw = ic.encode_config(profile, ic.default_config())
                self.assertEqual(len(raw), ic.CONFIG_SIZE)

    def test_a_description_is_a_label_and_nothing_else(self):
        """It must never reach the board or shift a byte."""
        plain = ic.encode_config(
            {"pins": [{"name": "1sw1", "action": "GAMEPAD 1"}]},
            ic.default_config())
        labelled = ic.encode_config(
            {"pins": [{"name": "1sw1", "action": "GAMEPAD 1",
                       "description": "GP1 South (A / Cross)"}]},
            ic.default_config())
        self.assertEqual(bytes(plain), bytes(labelled))

    def test_a_described_pin_with_no_action_keeps_what_the_board_had(self):
        """The gamepad template labels the sticks without assigning them."""
        base = ic.default_config()
        before = {p["name"]: p for p in ic.decode_config(base)["pins"]}
        after = ic.decode_config(bytes(ic.encode_config(
            {"pins": [{"name": "1up", "description": "GP1 Hat0 Up"}]}, base)))
        self.assertEqual({p["name"]: p for p in after["pins"]}["1up"],
                         before["1up"])

    def test_restore_rejects_an_edited_profile(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"pins": []}, fh)
            path = fh.name
        with self.assertRaises(ic.ProtocolError):
            ic.load_raw(path)
        os.unlink(path)

    def test_dump_carries_raw_bytes_for_restore(self):
        profile = ic.decode_config(ic.default_config())
        self.assertIn("raw", profile)
        self.assertEqual(len(bytes.fromhex(profile["raw"])), ic.CONFIG_SIZE)


# --------------------------------------------------------------------------
# Importing saved configurations
# --------------------------------------------------------------------------


HERE = os.path.dirname(os.path.abspath(__file__))


def fixture(name):
    return ic.load_profile(os.path.join(HERE, "fixtures", name))


class SavedDirsCase(unittest.TestCase):
    """A throwaway backup directory plus the shipped presets."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dirs = [
            {"source": "backups", "path": self.tmp, "writable": True},
            {"source": "presets", "path": ic.PRESET_DIR, "writable": False},
        ]

    def tearDown(self):
        for name in os.listdir(self.tmp):
            path = os.path.join(self.tmp, name)
            if os.path.islink(path) or os.path.isfile(path):
                os.unlink(path)
        os.rmdir(self.tmp)

    def save(self, name, profile, mtime=None):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            json.dump(profile, fh)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path


class TestListSaved(SavedDirsCase):
    def test_newest_first(self):
        self.save("a.json", {"pins": []}, mtime=1000)
        self.save("b.json", {"pins": []}, mtime=3000)
        self.save("c.json", {"pins": []}, mtime=2000)
        names = [e["name"] for e in ic.list_saved(self.dirs) if e["source"] == "backups"]
        self.assertEqual(names, ["b.json", "c.json", "a.json"])

    def test_a_dump_is_described_from_its_contents(self):
        self.save("dump.json", fixture("ipac2-1.55-keyboard.json"))
        entry = [e for e in ic.list_saved(self.dirs) if e["name"] == "dump.json"][0]
        self.assertTrue(entry["has_raw"])
        self.assertEqual(entry["firmware"], "0.55")
        self.assertEqual(entry["pins"], 32)
        self.assertTrue(entry["writable"])
        self.assertEqual(entry["id"], "backups/dump.json")

    def test_presets_are_listed_and_are_not_writable(self):
        presets = [e for e in ic.list_saved(self.dirs) if e["source"] == "presets"]
        self.assertTrue(presets, "the shipped profiles should be browsable")
        self.assertTrue(all(not e["writable"] for e in presets))
        self.assertTrue(all(not e["has_raw"] for e in presets))

    def test_an_unreadable_file_does_not_break_the_listing(self):
        with open(os.path.join(self.tmp, "broken.json"), "w") as fh:
            fh.write("{ this is not json")
        self.save("fine.json", {"pins": []})
        entries = {e["name"]: e for e in ic.list_saved(self.dirs)}
        self.assertIn("error", entries["broken.json"])
        self.assertNotIn("error", entries["fine.json"])

    def test_non_json_and_dotfiles_are_ignored(self):
        for name in ("notes.txt", ".hidden.json"):
            with open(os.path.join(self.tmp, name), "w") as fh:
                fh.write("{}")
        self.assertEqual(
            [e for e in ic.list_saved(self.dirs) if e["source"] == "backups"], []
        )

    def test_the_limit_is_honoured(self):
        for i in range(5):
            self.save("f%d.json" % i, {"pins": []})
        self.assertEqual(len(ic.list_saved(self.dirs, limit=3)), 3)


class TestWriteBackup(SavedDirsCase):
    def test_two_backups_in_one_second_do_not_collide(self):
        """Restoring a backup takes a backup - same second, same name."""
        first = ic.write_backup({"pins": [{"name": "1up", "action": "UP"}]}, self.tmp)
        second = ic.write_backup({"pins": [{"name": "1up", "action": "W"}]}, self.tmp)
        self.assertNotEqual(first, second)
        self.assertEqual(ic.load_profile(first)["pins"][0]["action"], "UP")
        self.assertEqual(ic.load_profile(second)["pins"][0]["action"], "W")


class TestFakeBoardHeader(unittest.TestCase):
    """The fake board has to answer reads the way a real one does."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "fake.json")
        with open(self.path, "w") as fh:
            json.dump(fixture("ipac2-1.55-keyboard.json"), fh)

    def tearDown(self):
        os.unlink(self.path)
        os.rmdir(self.dir)

    def test_the_firmware_byte_survives_a_write(self):
        board = ic.FakeBoard(self.path)
        self.assertEqual(board.info.firmware, "0.55")
        board.write_config(ic.as_write_command(board.read_config()))
        self.assertEqual(board.read_config()[2], 0x55)
        self.assertEqual(ic.FakeBoard(self.path).info.firmware, "0.55")

    def test_a_restore_puts_the_file_back_byte_for_byte(self):
        original = fixture("ipac2-1.55-keyboard.json")["raw"]
        board = ic.FakeBoard(self.path)
        board.write_config(
            ic.encode_config({"pins": [{"name": "1b", "alternate_action": "F1"}]},
                             board.read_config()))
        self.assertNotEqual(ic.load_profile(self.path)["raw"], original)
        board.write_config(ic.as_write_command(bytes.fromhex(original)))
        self.assertEqual(ic.load_profile(self.path)["raw"], original)


class TestResolveSaved(SavedDirsCase):
    """The security boundary: `serve` binds 0.0.0.0, so ids come off the LAN."""

    def test_a_real_file_resolves(self):
        path = self.save("ok.json", {"pins": []})
        directory, resolved = ic.resolve_saved(self.dirs, "backups/ok.json")
        self.assertEqual(resolved, os.path.realpath(path))
        self.assertTrue(directory["writable"])

    def test_traversal_is_refused(self):
        for ident in (
            "backups/../../etc/passwd",
            "backups/../ipacconf.py",
            "backups//etc/passwd",
            "backups/subdir/x.json",
            "backups/%s" % os.path.join(HERE, "ipacconf.py"),
            "../fixtures/before-1.44.json",
        ):
            with self.assertRaises(ic.ProtocolError, msg=ident):
                ic.resolve_saved(self.dirs, ident)

    def test_a_symlink_out_of_the_directory_is_refused(self):
        os.symlink(os.path.join(HERE, "ipacconf.py"),
                   os.path.join(self.tmp, "escape.json"))
        with self.assertRaises(ic.ProtocolError):
            ic.resolve_saved(self.dirs, "backups/escape.json")

    def test_non_json_names_are_refused(self):
        with open(os.path.join(self.tmp, "notes.txt"), "w") as fh:
            fh.write("hello")
        with self.assertRaises(ic.ProtocolError):
            ic.resolve_saved(self.dirs, "backups/notes.txt")

    def test_unknown_sources_and_missing_files_are_refused(self):
        for ident in ("elsewhere/x.json", "backups/nope.json", "backups", ""):
            with self.assertRaises(ic.ProtocolError, msg=ident):
                ic.resolve_saved(self.dirs, ident)

    def test_a_preset_resolves_but_is_read_only(self):
        directory, path = ic.resolve_saved(self.dirs, "presets/mame-keyboard.json")
        self.assertFalse(directory["writable"])
        self.assertTrue(os.path.isfile(path))


class TestSetLabel(SavedDirsCase):
    def test_round_trip(self):
        path = self.save("x.json", {"pins": []})
        ic.set_label(path, "good mame setup")
        self.assertEqual(ic.load_profile(path)["label"], "good mame setup")
        entry = [e for e in ic.list_saved(self.dirs) if e["name"] == "x.json"][0]
        self.assertEqual(entry["label"], "good mame setup")

    def test_a_label_does_not_disturb_the_raw_bytes(self):
        dump = fixture("ipac2-1.55-keyboard.json")
        path = self.save("dump.json", dump)
        ic.set_label(path, "before the flash")
        self.assertEqual(ic.load_profile(path)["raw"], dump["raw"])

    def test_blank_clears_it(self):
        path = self.save("x.json", {"pins": [], "label": "old"})
        ic.set_label(path, "   ")
        self.assertNotIn("label", ic.load_profile(path))

    def test_labelling_does_not_restamp_the_file(self):
        """Naming a backup is not saving it again - it must keep its place."""
        path = self.save("x.json", {"pins": []}, mtime=1000)
        ic.set_label(path, "keep")
        self.assertEqual(int(os.stat(path).st_mtime), 1000)

    def test_over_long_labels_are_trimmed(self):
        path = self.save("x.json", {"pins": []})
        ic.set_label(path, "z" * 500)
        self.assertEqual(len(ic.load_profile(path)["label"]), ic.LABEL_MAX)


class TestRestorePayload(unittest.TestCase):
    def test_an_edited_profile_cannot_be_restored(self):
        with self.assertRaises(ic.ProtocolError) as caught:
            ic.raw_from_profile({"pins": []}, "edited.json")
        self.assertIn("apply", str(caught.exception))

    def test_the_wrong_number_of_bytes_is_refused(self):
        with self.assertRaises(ic.ProtocolError):
            ic.raw_from_profile({"raw": "50dd0f00"}, "short.json")

    def test_non_hex_is_refused(self):
        with self.assertRaises(ic.ProtocolError):
            ic.raw_from_profile({"raw": "not hex at all"}, "junk.json")

    def test_a_dump_restores_to_itself(self):
        """Restoring a dump onto the board it came from must be a no-op."""
        raw = bytes.fromhex(fixture("ipac2-1.55-keyboard.json")["raw"])
        self.assertEqual(ic.diff_config(raw, ic.as_write_command(raw)), [])


class TestMergeProfile(unittest.TestCase):
    def test_a_partial_profile_leaves_other_pins_alone(self):
        base = fixture("ipac2-1.55-keyboard.json")
        incoming = ic.load_profile(
            os.path.join(HERE, "profiles", "write-test.json"))
        merged = ic.merge_profile(base, incoming)

        before = {p["name"]: p for p in base["pins"]}
        after = {p["name"]: p for p in merged["pins"]}
        self.assertEqual(after["1b"]["alternate_action"], "F1")
        self.assertEqual(after["1b"]["action"], before["1b"]["action"])
        for name in after:
            if name != "1b":
                self.assertEqual(after[name], before[name], name)

    def test_only_the_named_fields_are_reported_as_changed(self):
        base = fixture("ipac2-1.55-keyboard.json")
        incoming = ic.load_profile(
            os.path.join(HERE, "profiles", "write-test.json"))
        changed = ic.profile_changes(base, ic.merge_profile(base, incoming))
        self.assertEqual(changed,
                         [{"pin": "1b", "field": "alternate_action",
                           "before": "", "after": "F1"}])

    def test_a_description_survives_the_merge_without_counting_as_a_change(self):
        base = {"pins": [{"name": "1sw1", "action": "GAMEPAD 1"}]}
        incoming = {"pins": [{"name": "1sw1", "action": "GAMEPAD 1",
                              "description": "GP1 South (A / Cross)"}]}
        merged = ic.merge_profile(base, incoming)
        self.assertEqual(merged["pins"][0]["description"],
                         "GP1 South (A / Cross)")
        self.assertEqual(ic.profile_changes(base, merged), [])

    def test_a_pin_the_base_never_had_is_added(self):
        base = {"pins": [{"name": "1up", "action": "UP"}]}
        merged = ic.merge_profile(base, {"pins": [{"name": "1sw1", "action": "A"}]})
        self.assertEqual([p["name"] for p in merged["pins"]], ["1up", "1sw1"])

    def test_the_incoming_files_own_identity_does_not_survive(self):
        """raw/firmware/label describe the file, not the merge."""
        base = {"pins": []}
        merged = ic.merge_profile(base, fixture("ipac2-1.55-keyboard.json"))
        for key in ("raw", "firmware", "label"):
            self.assertNotIn(key, merged)

    def test_merging_a_full_dump_reproduces_it(self):
        dump = fixture("ipac2-1.55-keyboard.json")
        merged = ic.merge_profile(fixture("before-1.44.json"), dump)
        self.assertEqual(ic.profile_changes(dump, merged), [])

    def test_debounce_and_paclink_follow_the_import(self):
        base = {"pins": [], "debounce": "standard", "paclink": False}
        merged = ic.merge_profile(base, {"pins": [], "debounce": "long",
                                         "paclink": True})
        self.assertEqual(merged["debounce"], "long")
        self.assertTrue(merged["paclink"])


class TestImportNotes(unittest.TestCase):
    def board(self, bcd=0x0055):
        return ic.DeviceInfo("/dev/hidraw9", ic.VENDOR_2015, ic.PRODUCT_IPAC2,
                             bcd, 2, "usb")

    def raw(self, version=0x55):
        """Raw bytes headed as a read response from that firmware."""
        buf = bytearray(ic.CONFIG_SIZE)
        buf[0], buf[1], buf[2] = 0x50, 0xDD, version
        return bytes(buf).hex()

    def test_a_matching_dump_says_nothing(self):
        self.assertEqual(
            ic.import_notes(fixture("ipac2-1.55-keyboard.json"), self.board()), [])

    def test_a_firmware_mismatch_is_flagged(self):
        notes = ic.import_notes(fixture("before-1.44.json"), self.board(0x0055))
        self.assertTrue(any("0.44" in n and "0.55" in n for n in notes))

    def test_a_profile_without_raw_says_it_cannot_be_restored(self):
        notes = ic.import_notes({"pins": [{"name": "1b", "action": "F1"}]},
                                self.board())
        self.assertTrue(any("not restored byte for byte" in n
                            or "byte for byte" in n for n in notes))

    def test_a_partial_profile_says_how_many_pins_it_names(self):
        notes = ic.import_notes({"raw": self.raw(),
                                 "pins": [{"name": "1b", "action": "F1"}]},
                                self.board())
        self.assertTrue(any("1 of the 32 pins" in n for n in notes))

    def test_macros_are_called_out(self):
        notes = ic.import_notes(
            {"raw": self.raw(),
             "pins": [{"name": p, "action": "A"} for p in ic.PIN_ORDER],
             "macros": [{"name": "MACRO 1", "action": ["A", "B"]}]},
            self.board())
        self.assertEqual(len(notes), 1)
        self.assertIn("Restore exactly", notes[0])

    def test_gamepad_codes_on_keyboard_firmware_warn(self):
        notes = ic.import_notes(
            {"raw": self.raw(0x44),
             "pins": [{"name": p, "action": "GAMEPAD 1"} for p in ic.PIN_ORDER]},
            self.board(0x0044))
        self.assertTrue(any("keyboard-only" in n for n in notes))

    def test_no_board_means_no_firmware_note(self):
        self.assertEqual(ic.import_notes(fixture("before-1.44.json"), None), [])


# --------------------------------------------------------------------------
# Input monitor
# --------------------------------------------------------------------------


class TestKeycodeTable(unittest.TestCase):
    """Reversing a press depends entirely on this table being right."""

    def test_the_kernel_table_is_intact(self):
        self.assertEqual(len(ic.USB_KBD_KEYCODE), 256)
        # Spot checks against linux/input-event-codes.h, at the boundaries
        # that would move if a row were lost: KEY_A, KEY_ENTER, KEY_LEFTCTRL.
        self.assertEqual(ic.USB_KBD_KEYCODE[0x04], 30)
        self.assertEqual(ic.USB_KBD_KEYCODE[0x28], 28)
        self.assertEqual(ic.USB_KBD_KEYCODE[0xE0], 29)

    def test_keys_reverse_to_the_right_action(self):
        for keycode, name in [
            (30, "A"), (2, "1"), (6, "5"), (28, "ENTER"), (1, "ESC"),
            (57, "SPACE"), (105, "LEFT"), (106, "RIGHT"), (103, "UP"),
            (108, "DOWN"), (67, "F9"), (88, "F12"),
        ]:
            self.assertEqual(ic.code_to_name(ic.LINUX_TO_BOARD[keycode]), name)

    def test_modifiers_use_ultimarcs_numbering_not_hids(self):
        """The board stores CTRL L as 0x70; HID would call that F21."""
        self.assertEqual(ic.LINUX_TO_BOARD[29], ic.KEY_CODES["CTRL L"])
        self.assertEqual(ic.LINUX_TO_BOARD[126], ic.KEY_CODES["WIN MENU"])
        self.assertNotIn(191, ic.LINUX_TO_BOARD)  # KEY_F21 must not claim 0x70

    def test_media_keys_use_the_boards_own_codes(self):
        self.assertEqual(ic.LINUX_TO_BOARD[116], ic.SYSTEM_CODES["POWER"])
        self.assertEqual(ic.LINUX_TO_BOARD[113], ic.SYSTEM_CODES["MUTE"])
        self.assertNotIn(93, ic.LINUX_TO_BOARD)  # KEY_KATAKANA is not POWER

    def test_no_two_keys_reverse_to_the_same_action(self):
        """A collision would name a pin that is not the one being pressed."""
        seen = {}
        for keycode, value in ic.LINUX_TO_BOARD.items():
            self.assertNotIn(
                value, seen,
                "keycodes %s and %s both give %s"
                % (seen.get(value), keycode, ic.code_to_name(value)),
            )
            seen[value] = keycode

    def test_every_answer_is_something_the_board_can_store(self):
        for value in ic.LINUX_TO_BOARD.values():
            self.assertIn(value, ic.CODE_NAMES)

    def test_the_inverse_round_trips(self):
        for keycode, value in ic.LINUX_TO_BOARD.items():
            self.assertEqual(ic.BOARD_TO_LINUX[value], keycode)


class TestEventParsing(unittest.TestCase):
    def event(self, etype, code, value):
        return struct.pack(ic.INPUT_EVENT_FORMAT, 1700000000, 500, etype, code, value)

    def test_a_batch_splits_into_records(self):
        blob = self.event(ic.EV_KEY, 30, 1) + self.event(ic.EV_SYN, 0, 0)
        self.assertEqual(
            ic.parse_input_events(blob),
            [(1700000000, 500, ic.EV_KEY, 30, 1), (1700000000, 500, 0, 0, 0)],
        )

    def test_a_partial_trailing_record_is_dropped(self):
        blob = self.event(ic.EV_KEY, 30, 1) + b"\x00" * 7
        self.assertEqual(len(ic.parse_input_events(blob)), 1)

    def test_nothing_read_is_no_events(self):
        self.assertEqual(ic.parse_input_events(b""), [])


class TestEventAction(unittest.TestCase):
    def test_a_key_becomes_its_board_code(self):
        self.assertEqual(ic.event_action(ic.EV_KEY, 30), ("key", ic.KEY_CODES["A"]))

    def test_the_first_joystick_button_is_gamepad_zero(self):
        """No offset. Both scales start at zero, so the mapping is identity.

        It used to add one, left over from 1-based code names. After the
        renumbering that made the monitor name every button one too high and
        then point at whichever pin carried THAT code - pressing start
        reported the pin beside it.
        """
        self.assertEqual(
            ic.event_action(ic.EV_KEY, ic.BTN_JOYSTICK),
            ("gamepad", ic.GAME_CODES["GAMEPAD 0"]),
        )

    def test_every_button_index_maps_to_its_own_number(self):
        for index in range(0, ic.BTN_LAST - ic.BTN_JOYSTICK):
            with self.subTest(index=index):
                _, code = ic.event_action(ic.EV_KEY, ic.BTN_JOYSTICK + index)
                self.assertEqual(code, ic.GAME_CODES["GAMEPAD %d" % index])

    def test_the_buttons_that_matter_land_where_the_profile_puts_them(self):
        """The eleven real buttons, end to end: index -> code -> name."""
        for index in range(0, ic.GAMEPAD_BUTTONS_CONFIRMED):
            with self.subTest(index=index):
                _, code = ic.event_action(ic.EV_KEY, ic.BTN_JOYSTICK + index)
                self.assertEqual(code, ic.GAMEPAD_FIRST_CODE + index)
                self.assertEqual(ic.code_to_name(code), "GAMEPAD %d" % index)

    def test_an_axis_carries_no_board_code(self):
        """It cannot: several board codes drive one axis, in both directions.

        This used to map the evdev axis number through the board's code table
        and answer "HAT 0" - a board code the config need not contain, which
        the monitor then printed and reported as belonging to no pin, about a
        pin that plainly did carry one.
        """
        self.assertEqual(ic.event_action(ic.EV_ABS, ic.ABS_HAT0X), ("hat", None))
        self.assertEqual(ic.event_action(ic.EV_ABS, 0), ("axis", None))

    def test_an_axis_line_names_the_axis_and_its_value(self):
        import time
        line = ic.monitor_line({
            "ts": time.time(), "node": "event2", "pins": [], "code": None,
            "name": None, "muted": False, "kind": "axis", "raw": 0x00,
            "type": 3, "value": -1, "held": None,
        })
        self.assertIn("axis X", line)
        self.assertIn("=-1", line)
        self.assertNotIn("ANALOG", line)

    def test_buttons_still_name_their_pin(self):
        """The one translation that is confirmed, and still the useful one."""
        kind, code = ic.event_action(ic.EV_KEY, ic.BTN_JOYSTICK)
        self.assertEqual(kind, "gamepad")
        self.assertEqual(ic.code_to_name(code), "GAMEPAD 0")

    def test_mouse_buttons_keep_their_order(self):
        self.assertEqual(
            ic.event_action(ic.EV_KEY, ic.BTN_MOUSE), ("mouse", ic.MOUSE_CODES["MOUSE L"])
        )
        self.assertEqual(
            ic.event_action(ic.EV_KEY, ic.BTN_MOUSE + 1),
            ("mouse", ic.MOUSE_CODES["MOUSE R"]),
        )

    def test_an_unstorable_event_has_no_code(self):
        kind, code = ic.event_action(ic.EV_KEY, 0x100)  # BTN_0
        self.assertIsNone(code)


class TestPinsForAction(unittest.TestCase):
    PROFILE = {
        "pins": [
            {"name": "1sw1", "action": "CTRL L", "alternate_action": "5"},
            {"name": "1coin", "action": "5", "alternate_action": ""},
            {"name": "1start", "action": "1", "alternate_action": "1"},
            {"name": "2sw1", "action": "GAMEPAD 1", "alternate_action": ""},
            {"name": "1sw2", "action": "GAMEPAD 1", "alternate_action": ""},
        ]
    }

    def test_a_plain_hit(self):
        self.assertEqual(
            ic.pins_for_action(self.PROFILE, "CTRL L"),
            [{"pin": "1sw1", "field": "action"}],
        )

    def test_a_shifted_hit_names_the_alternate(self):
        hits = ic.pins_for_action(self.PROFILE, "5")
        self.assertIn({"pin": "1sw1", "field": "alternate_action"}, hits)
        self.assertIn({"pin": "1coin", "field": "action"}, hits)

    def test_a_pin_repeating_its_action_is_reported_once(self):
        self.assertEqual(
            ic.pins_for_action(self.PROFILE, "1"),
            [{"pin": "1start", "field": "action"}],
        )

    def test_an_unmapped_code_matches_nothing(self):
        self.assertEqual(ic.pins_for_action(self.PROFILE, None), [])
        self.assertEqual(ic.pins_for_action(self.PROFILE, "F9"), [])

    def test_no_profile_matches_nothing(self):
        self.assertEqual(ic.pins_for_action(None, "CTRL L"), [])

    def test_the_event_node_breaks_a_dinput_tie(self):
        """Both players share GAMEPAD 1..32, so the pad it arrived on decides."""
        both = ic.pins_for_action(self.PROFILE, "GAMEPAD 1")
        self.assertEqual(len(both), 2)
        self.assertEqual(
            ic.pins_for_action(self.PROFILE, "GAMEPAD 1", player=2),
            [{"pin": "2sw1", "field": "action"}],
        )

    def test_an_unknown_player_falls_back_to_every_candidate(self):
        self.assertEqual(
            len(ic.pins_for_action(self.PROFILE, "GAMEPAD 1", player=9)), 2
        )


class TestEventStream(unittest.TestCase):
    def setUp(self):
        self.stream = ic.EventStream(size=3)

    def test_sequence_numbers_count_up(self):
        first = self.stream.publish({"name": "A"})
        second = self.stream.publish({"name": "B"})
        self.assertEqual((first["seq"], second["seq"]), (1, 2))
        self.assertEqual(self.stream.latest, 2)

    def test_since_returns_only_what_came_after(self):
        for name in "ABC":
            self.stream.publish({"name": name})
        self.assertEqual([e["name"] for e in self.stream.since(1)], ["B", "C"])
        self.assertEqual(self.stream.since(3), [])

    def test_the_buffer_is_bounded(self):
        for name in "ABCDE":
            self.stream.publish({"name": name})
        self.assertEqual([e["name"] for e in self.stream.since(0)], ["C", "D", "E"])

    def test_subscribers_get_events_as_they_land(self):
        sub = self.stream.subscribe()
        self.stream.publish({"name": "A"})
        self.assertEqual(sub.get_nowait()["name"], "A")
        self.stream.unsubscribe(sub)
        self.stream.publish({"name": "B"})
        self.assertTrue(sub.empty())

    def test_a_stalled_subscriber_loses_events_rather_than_blocking(self):
        self.stream.subscribe(maxsize=1)
        for name in "ABC":
            self.stream.publish({"name": name})  # must not hang
        self.assertEqual(self.stream.latest, 3)


class TestSseFrame(unittest.TestCase):
    def test_a_plain_event(self):
        self.assertEqual(ic.sse_frame({"a": 1}), b'data: {"a": 1}\n\n')

    def test_a_named_event(self):
        self.assertEqual(
            ic.sse_frame({"a": 1}, "watching"),
            b'event: watching\ndata: {"a": 1}\n\n',
        )

    def test_frames_end_blank_line_delimited(self):
        """Two frames back to back must not run into one another."""
        blob = ic.sse_frame({"a": 1}) + ic.sse_frame({"b": 2})
        self.assertEqual(len(blob.split(b"\n\n")), 3)


class TestFakeInputMonitor(unittest.TestCase):
    """The replay path, which is what makes the UI developable off-cabinet."""

    PROFILE = {"pins": [{"name": "1sw1", "action": "CTRL L",
                         "alternate_action": ""}]}

    def script(self, *lines):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".jsonl", delete=False)
        handle.write("\n".join(lines) + "\n")
        handle.close()
        self.addCleanup(os.remove, handle.name)
        return handle.name

    def drain(self, path, expected):
        monitor = ic.FakeInputMonitor(path, profile=self.PROFILE, loop=False)
        sub = monitor.stream.subscribe()
        monitor.start()
        try:
            return [sub.get(timeout=2.0) for _ in range(expected)]
        finally:
            monitor.close()

    def test_a_named_action_replays_as_the_real_keycode(self):
        path = self.script('{"after": 0, "action": "CTRL L", "value": 1}')
        (event,) = self.drain(path, 1)
        self.assertEqual(event["name"], "CTRL L")
        self.assertEqual(event["raw"], 29)  # KEY_LEFTCTRL, as the kernel sends
        self.assertEqual(event["pins"], [{"pin": "1sw1", "field": "action"}])
        self.assertTrue(event["held"])

    def test_raw_evdev_numbers_work_too(self):
        path = self.script('{"after": 0, "type": 1, "code": 29, "value": 0}')
        (event,) = self.drain(path, 1)
        self.assertEqual(event["name"], "CTRL L")
        self.assertFalse(event["held"])

    def test_comments_and_blank_lines_are_skipped(self):
        path = self.script(
            "# a comment", "", '{"after": 0, "action": "CTRL L", "value": 1}')
        self.assertEqual(len(self.drain(path, 1)), 1)

    def test_an_action_no_keyboard_can_send_is_refused(self):
        path = self.script('{"after": 0, "action": "GAMEPAD 1", "value": 1}')
        with self.assertRaises(ic.ProtocolError):
            ic.FakeInputMonitor(path, loop=False)

    def test_an_empty_script_is_refused(self):
        with self.assertRaises(ic.ProtocolError):
            ic.FakeInputMonitor(self.script("# nothing here"), loop=False)


# Not in ipacconf: an event type it deliberately has no reading of, which is
# the point of the tests below. From linux/input-event-codes.h.
EV_MSC = 0x04
MSC_SCAN = 0x04


class TestTranslate(unittest.TestCase):
    """The bits of translation that carry state between events."""

    def setUp(self):
        self.device = ic._fake_device("/dev/input/event9")
        self.monitor = ic.BaseMonitor([self.device])

    def translate(self, etype, code, value):
        return self.monitor.translate(self.device, etype, code, value)

    def test_syn_events_are_dropped(self):
        self.assertIsNone(self.translate(ic.EV_SYN, 0, 0))

    def test_autorepeat_is_dropped(self):
        """Otherwise a held button floods the log with itself."""
        self.assertIsNotNone(self.translate(ic.EV_KEY, 30, 1))
        self.assertIsNone(self.translate(ic.EV_KEY, 30, 2))

    def test_an_axis_reports_every_change_and_never_an_edge(self):
        """No press, no release - just where the axis went.

        The old behaviour took the first value seen as the resting point. An
        evdev axis only emits when it changes, so the first event is always a
        press: it defined rest, was swallowed as a non-event, and the release
        that followed then read as a press. Confirmed on hardware, mapping a
        stick, where it made every direction report the wrong edge.
        """
        first = self.translate(ic.EV_ABS, ic.ABS_HAT0X, 255)
        self.assertIsNotNone(first, "the first press must not be swallowed")
        self.assertIsNone(first["held"], "an axis has no press/release")
        self.assertEqual(first["value"], 255)

        self.assertIsNone(self.translate(ic.EV_ABS, ic.ABS_HAT0X, 255),
                          "an unchanged value is not an event")

        back = self.translate(ic.EV_ABS, ic.ABS_HAT0X, 128)
        self.assertIsNotNone(back)
        self.assertEqual(back["value"], 128)

    def test_axes_are_tracked_separately(self):
        self.assertIsNotNone(self.translate(ic.EV_ABS, ic.ABS_HAT0X, 255))
        self.assertIsNotNone(self.translate(ic.EV_ABS, ic.ABS_HAT0X + 1, 255))

    def test_a_key_still_has_edges(self):
        self.assertTrue(self.translate(ic.EV_KEY, ic.BTN_JOYSTICK, 1)["held"])
        self.assertFalse(self.translate(ic.EV_KEY, ic.BTN_JOYSTICK, 0)["held"])

    def test_a_keyboard_event_is_not_pinned_to_a_player(self):
        """Player only disambiguates the shared GAMEPAD code space."""
        self.assertIsNone(self.translate(ic.EV_KEY, 30, 1)["player"])
        self.assertEqual(
            self.translate(ic.EV_KEY, ic.BTN_JOYSTICK, 1)["player"], 1)

    def test_an_unreadable_event_type_is_reported_once_then_hidden(self):
        """EV_MSC shadows every press, so reporting each one buries the log."""
        first = self.translate(EV_MSC, MSC_SCAN, 458756)
        self.assertEqual(first["kind"], "other")
        self.assertTrue(first["muted"])
        for value in (458757, 458758):
            self.assertIsNone(self.translate(EV_MSC, MSC_SCAN, value))

    def test_each_node_gets_its_own_first_report(self):
        other = ic._fake_device("/dev/input/event8")
        self.assertTrue(self.translate(EV_MSC, MSC_SCAN, 1)["muted"])
        self.assertTrue(
            self.monitor.translate(other, EV_MSC, MSC_SCAN, 1)["muted"])

    def test_hiding_a_type_does_not_swallow_real_presses(self):
        self.translate(EV_MSC, MSC_SCAN, 458756)
        press = self.translate(ic.EV_KEY, 30, 1)
        self.assertEqual(press["name"], "A")
        self.assertFalse(press["muted"])


class TestMonitorLine(unittest.TestCase):
    """The CLI's rendering of one event."""

    def line(self, **over):
        event = {
            "ts": 0, "node": "event9", "kind": "key", "raw": 30, "held": True,
            "name": "A", "code": 0x04, "muted": False, "pins": [],
        }
        event.update(over)
        return ic.monitor_line(event)

    def test_a_press_names_its_pin(self):
        line = self.line(pins=[{"pin": "1sw1", "field": "action"}])
        self.assertIn("A (0x04)", line)
        self.assertIn("1sw1", line)
        self.assertIn("down", line)

    def test_a_shifted_pin_says_so(self):
        line = self.line(pins=[{"pin": "1sw1", "field": "alternate_action"}])
        self.assertIn("1sw1 (shifted)", line)

    def test_several_pins_are_flagged_as_ambiguous(self):
        line = self.line(pins=[
            {"pin": "1sw1", "field": "action"},
            {"pin": "2sw1", "field": "action"},
        ])
        self.assertIn("several pins carry this code", line)

    def test_a_code_no_pin_carries_says_so(self):
        self.assertIn("no pin carries this code", self.line())

    def test_a_hidden_type_explains_its_own_silence(self):
        line = self.line(kind="other", raw=4, name=None, code=None, muted=True)
        self.assertIn("hiding the rest", line)
        self.assertNotIn("hiding the rest", self.line(
            kind="other", raw=4, name=None, code=None))


class TestXinputIdentity(unittest.TestCase):
    """In Xinput mode the board answers to 045e:028e - a Microsoft Xbox 360
    pad's ids, shared with the genuine pad and every clone of it. The only
    thing that still says Ultimarc is the usb string descriptors, so they are
    what decides, and a real controller must never be taken for a board."""

    @staticmethod
    def _info(vendor, product, manufacturer=None, product_name=None):
        return ic.DeviceInfo("/dev/hidraw0", vendor, product, 0x0055, 2, "1-1",
                             manufacturer=manufacturer,
                             product_name=product_name)

    def test_ultimarc_strings_make_it_our_board(self):
        info = self._info(ic.VENDOR_XINPUT, ic.PRODUCT_XINPUT,
                          "Ultimarc", "I-PAC 2")
        self.assertTrue(info.is_ipac2)
        self.assertIn("Xinput", info.mode)

    def test_a_genuine_xbox_pad_is_not_our_board(self):
        info = self._info(ic.VENDOR_XINPUT, ic.PRODUCT_XINPUT,
                          "Microsoft", "Controller")
        self.assertFalse(info.is_ipac2)
        self.assertNotIn("Xinput", info.mode)

    def test_missing_strings_are_not_enough(self):
        """A device that reports no strings at all stays unidentified: the
        borrowed ids alone can never be the evidence."""
        self.assertFalse(self._info(ic.VENDOR_XINPUT, ic.PRODUCT_XINPUT).is_ipac2)

    def test_the_ultimarc_ids_need_no_strings(self):
        """d209 is Ultimarc's own; nothing else answers to it."""
        self.assertTrue(self._info(ic.VENDOR_2015, ic.PRODUCT_IPAC2).is_ipac2)

    def test_only_the_borrowed_identity_is_flagged_as_disguised(self):
        self.assertTrue(self._info(ic.VENDOR_XINPUT, ic.PRODUCT_XINPUT,
                                   "Ultimarc", "I-PAC 2").disguised)
        self.assertFalse(self._info(ic.VENDOR_2015, ic.PRODUCT_IPAC2).disguised)

    def test_xinput_writes_are_not_trusted_to_flash(self):
        reason = ic.flash_write_blocked(
            self._info(ic.VENDOR_XINPUT, ic.PRODUCT_XINPUT, "Ultimarc", "I-PAC 2"))
        self.assertIsNotNone(reason)
        self.assertIn("Start1+P1SW1", reason)

    def test_a_genuine_xbox_pad_is_never_offered_a_write(self):
        """Not a real scenario - discovery drops it first - but if one ever
        reached here, warning is the only safe answer."""
        self.assertIsNotNone(ic.flash_write_blocked(
            self._info(ic.VENDOR_XINPUT, ic.PRODUCT_XINPUT, "Microsoft", "Controller")))


class TestFindDevicesAcrossModes(unittest.TestCase):
    """Discovery against a fake /sys tree. This is the path that failed
    outright in Xinput mode: the vendor filter dropped the board before
    anything else got a chance to look at it."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.next_node = 0

    def _add(self, vendor, product, manufacturer, product_name,
             interface=2, bcd="0055"):
        """Build one hidraw node hanging off a usb device, as sysfs lays it
        out: hidraw/device -> .../<usb>/<interface>/<hid>."""
        node = "hidraw%d" % self.next_node
        self.next_node += 1
        usb_dir = os.path.join(self.root, "devices", "usb-%s" % node)
        iface_dir = os.path.join(usb_dir, "1-1:1.%d" % interface)
        hid_dir = os.path.join(iface_dir, "0003:%04X:%04X.0001" % (vendor, product))
        os.makedirs(hid_dir)
        for name, value in (("idVendor", "%04x" % vendor),
                            ("idProduct", "%04x" % product),
                            ("bcdDevice", bcd),
                            ("manufacturer", manufacturer),
                            ("product", product_name)):
            if value is None:
                continue
            with open(os.path.join(usb_dir, name), "w") as fh:
                fh.write(value + "\n")
        with open(os.path.join(iface_dir, "bInterfaceNumber"), "w") as fh:
            fh.write("%02d\n" % interface)

        link_dir = os.path.join(self.root, "class", "hidraw", node)
        os.makedirs(link_dir)
        os.symlink(hid_dir, os.path.join(link_dir, "device"))
        return node

    def find(self, **kwargs):
        return ic.find_devices(sys_root=self.root, **kwargs)

    def test_keyboard_mode_is_found(self):
        self._add(ic.VENDOR_2015, ic.PRODUCT_IPAC2, "Ultimarc", "I-PAC 2")
        found = self.find()
        self.assertEqual([d.mode for d in found], ["keyboard"])

    def test_xinput_mode_is_found(self):
        self._add(ic.VENDOR_XINPUT, ic.PRODUCT_XINPUT, "Ultimarc", "I-PAC 2")
        found = self.find()
        self.assertEqual(len(found), 1)
        self.assertIn("Xinput", found[0].mode)
        self.assertTrue(found[0].disguised)

    def test_a_genuine_xbox_pad_is_not_returned(self):
        self._add(ic.VENDOR_XINPUT, ic.PRODUCT_XINPUT, "Microsoft",
                  "Controller")
        self.assertEqual(self.find(), [])
        # Not even as an unsupported board: it must never become a candidate
        # for a config probe, which is what include_unsupported feeds.
        self.assertEqual(self.find(include_unsupported=True), [])

    def test_the_board_is_picked_out_from_beside_a_real_pad(self):
        self._add(ic.VENDOR_XINPUT, ic.PRODUCT_XINPUT, "Microsoft", "Controller")
        self._add(ic.VENDOR_XINPUT, ic.PRODUCT_XINPUT, "Ultimarc", "I-PAC 2")
        found = self.find()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].path, "/dev/hidraw1")

    def test_a_pre_2015_board_is_still_recognised_as_unsupported(self):
        self._add(ic.VENDOR_PRE2015, ic.PRODUCT_PRE2015, "Ultimarc", "I-PAC")
        self.assertEqual(self.find(), [])
        self.assertEqual(len(self.find(include_unsupported=True)), 1)

    def test_the_borrowed_firmware_version_is_not_reported_as_the_boards(self):
        """bcdDevice is borrowed along with the ids: in Xinput the board
        reports 1.00, which is the Xbox pad's. Printing that as firmware sends
        someone hunting a firmware fault that does not exist."""
        self._add(ic.VENDOR_XINPUT, ic.PRODUCT_XINPUT, "Ultimarc", "I-PAC 2",
                  bcd="0100")
        found = self.find()[0]
        self.assertIn("not reported in Xinput", found.firmware_summary)
        self.assertIn("1.50+", found.firmware_summary)
        self.assertNotIn("unrecognised", found.firmware_summary)

    def test_a_board_in_xinput_is_a_gamepad_whatever_bcddevice_says(self):
        """1.00 fails the bcdDevice gamepad rule, yet the board is acting as a
        gamepad right now - the rule cannot apply to a borrowed version."""
        self.assertFalse(ic.firmware_supports_gamepad(0x00))
        self._add(ic.VENDOR_XINPUT, ic.PRODUCT_XINPUT, "Ultimarc", "I-PAC 2",
                  bcd="0100")
        self.assertTrue(self.find()[0].supports_gamepad)

    def test_a_real_version_is_still_reported_in_keyboard_mode(self):
        self._add(ic.VENDOR_2015, ic.PRODUCT_IPAC2, "Ultimarc", "I-PAC 2",
                  bcd="0155")
        summary = self.find()[0].firmware_summary
        self.assertIn("1.55", summary)
        self.assertNotIn("not reported", summary)

    def test_the_strings_are_carried_through(self):
        self._add(ic.VENDOR_XINPUT, ic.PRODUCT_XINPUT, "Ultimarc", "I-PAC 2")
        found = self.find()[0]
        self.assertEqual(found.manufacturer, "Ultimarc")
        self.assertEqual(found.product_name, "I-PAC 2")


class TestBoardOnTheBusWithNoHidNode(unittest.TestCase):
    """In Xinput the board is bound by xpad and may expose no hid interface at
    all, so it never appears in the hidraw scan. It is still plugged in, and
    saying "no board found" sends someone hunting a cable fault."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.devices = os.path.join(self.root, "bus", "usb", "devices")
        os.makedirs(self.devices)

    def _add(self, name, vendor, product, manufacturer, product_name):
        usb_dir = os.path.join(self.devices, name)
        os.makedirs(usb_dir)
        for key, value in (("idVendor", "%04x" % vendor),
                           ("idProduct", "%04x" % product),
                           ("bcdDevice", "0155"),
                           ("manufacturer", manufacturer),
                           ("product", product_name)):
            if value is None:
                continue
            with open(os.path.join(usb_dir, key), "w") as fh:
                fh.write(value + "\n")
        return usb_dir

    def find(self):
        return ic.find_usb_boards(sys_root=self.root)

    def test_an_xinput_board_is_found_on_the_bus(self):
        self._add("1-1", ic.VENDOR_XINPUT, ic.PRODUCT_XINPUT,
                  "Ultimarc", "I-PAC 2")
        found = self.find()
        self.assertEqual(len(found), 1)
        self.assertIn("Xinput", found[0].mode)
        self.assertIsNone(found[0].path)  # no config node to point at

    def test_a_genuine_xbox_pad_is_not_found_on_the_bus_either(self):
        self._add("1-1", ic.VENDOR_XINPUT, ic.PRODUCT_XINPUT,
                  "Microsoft", "Controller")
        self.assertEqual(self.find(), [])

    def test_interface_directories_are_skipped(self):
        """1-1:1.0 sits beside 1-1 and carries no idVendor; a board must not
        be counted once per interface."""
        self._add("1-1", ic.VENDOR_XINPUT, ic.PRODUCT_XINPUT,
                  "Ultimarc", "I-PAC 2")
        iface = os.path.join(self.devices, "1-1:1.0")
        os.makedirs(iface)
        for key, value in (("idVendor", "045e"), ("idProduct", "028e"),
                           ("manufacturer", "Ultimarc"), ("product", "I-PAC 2")):
            with open(os.path.join(iface, key), "w") as fh:
                fh.write(value + "\n")
        self.assertEqual(len(self.find()), 1)

    def test_the_reason_names_xinput_and_the_way_out(self):
        self._add("1-1", ic.VENDOR_XINPUT, ic.PRODUCT_XINPUT,
                  "Ultimarc", "I-PAC 2")
        reason = ic.no_config_node_reason(self.find())
        self.assertIn("Xinput", reason)
        self.assertIn("Start1+P1SW1", reason)
        self.assertNotIn("cable", reason)

    def test_a_keyboard_mode_board_with_no_node_is_a_driver_problem(self):
        """Same symptom, completely different cause - keyboard mode does
        expose a hid interface, so a missing node means nothing bound it."""
        self._add("1-1", ic.VENDOR_2015, ic.PRODUCT_IPAC2, "Ultimarc", "I-PAC 2")
        reason = ic.no_config_node_reason(self.find())
        self.assertIn("usbhid", reason)
        self.assertNotIn("Start1+P1SW1", reason)


if __name__ == "__main__":
    unittest.main()
