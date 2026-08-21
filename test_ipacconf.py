#!/usr/bin/env python3
"""Tests for ipacconf. Stdlib only, no hardware needed.

    python3 -m unittest -v test_ipacconf.py
"""

import json
import os
import struct
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

    def test_the_padding_is_zeros(self):
        self.assertEqual(self.frames[-1][1:], b"\x00" * ic.CHUNK)

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

    def test_joystick_buttons_span_gamepad_1_to_32(self):
        self.assertEqual(
            ic.event_action(ic.EV_KEY, ic.BTN_JOYSTICK),
            ("gamepad", ic.GAME_CODES["GAMEPAD 1"]),
        )
        self.assertEqual(
            ic.event_action(ic.EV_KEY, ic.BTN_LAST - 1),
            ("gamepad", ic.GAME_CODES["GAMEPAD 32"]),
        )

    def test_hat_axes_become_hat_codes(self):
        self.assertEqual(
            ic.event_action(ic.EV_ABS, ic.ABS_HAT0X),
            ("hat", ic.GAME_CODES["HAT 0"]),
        )

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

    def test_an_axis_reports_only_when_it_leaves_or_returns_to_rest(self):
        # The first value seen is what counts as released, so a stick centred
        # at 128 works the same as one centred at 0.
        self.assertIsNone(self.translate(ic.EV_ABS, ic.ABS_HAT0X, 128))
        moved = self.translate(ic.EV_ABS, ic.ABS_HAT0X, 255)
        self.assertTrue(moved["held"])
        self.assertIsNone(self.translate(ic.EV_ABS, ic.ABS_HAT0X, 255))
        self.assertFalse(self.translate(ic.EV_ABS, ic.ABS_HAT0X, 128)["held"])

    def test_a_keyboard_event_is_not_pinned_to_a_player(self):
        """Player only disambiguates the shared GAMEPAD code space."""
        self.assertIsNone(self.translate(ic.EV_KEY, 30, 1)["player"])
        self.assertEqual(
            self.translate(ic.EV_KEY, ic.BTN_JOYSTICK, 1)["player"], 1)


if __name__ == "__main__":
    unittest.main()
