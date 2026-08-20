#!/usr/bin/env python3
"""Tests for ipacconf. Stdlib only, no hardware needed.

    python3 -m unittest -v test_ipacconf.py
"""

import json
import os
import tempfile
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

    def test_gamepad_codes_cover_the_documented_range(self):
        self.assertEqual(ic.ALL_CODES["GAMEPAD 1"], 0x90)
        self.assertEqual(ic.ALL_CODES["GAMEPAD 32"], 0xAF)
        self.assertEqual(ic.ALL_CODES["HAT 0"], 0xBA)
        self.assertEqual(ic.ALL_CODES["ANALOG 0"], 0xB0)


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


if __name__ == "__main__":
    unittest.main()
