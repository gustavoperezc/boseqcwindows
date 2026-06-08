import unittest

import bose_protocol
from hotkey_manager import HotkeyError, parse_hotkey, MOD_ALT, MOD_CONTROL


class BoseProtocolTests(unittest.TestCase):
    def test_build_anc_packets(self):
        self.assertEqual(bose_protocol.build_set_anc_packet("high"), bytes.fromhex("01 06 02 01 01"))
        self.assertEqual(bose_protocol.build_set_anc_packet("low"), bytes.fromhex("01 06 02 01 03"))
        self.assertEqual(bose_protocol.build_set_anc_packet("off"), bytes.fromhex("01 06 02 01 00"))

    def test_build_init_packet(self):
        self.assertEqual(bose_protocol.build_qc35_init_packet(), bytes.fromhex("00 01 01 00"))

    def test_parse_response(self):
        response = bose_protocol.parse_response(bytes.fromhex("01 06 02 02 03 0B"))
        self.assertIsNotNone(response)
        self.assertEqual(response.fblock, 1)
        self.assertEqual(response.function, 6)
        self.assertEqual(response.operator, bose_protocol.OP_SETGET)
        self.assertEqual(bose_protocol.parse_anc_mode(response.payload), "low")


class HotkeyParserTests(unittest.TestCase):
    def test_parse_default_hotkey(self):
        parsed = parse_hotkey("Ctrl+Alt+N")
        self.assertEqual(parsed.label, "Ctrl+Alt+N")
        self.assertEqual(parsed.modifiers, MOD_CONTROL | MOD_ALT)
        self.assertEqual(parsed.vk, ord("N"))

    def test_rejects_missing_modifier(self):
        with self.assertRaises(HotkeyError):
            parse_hotkey("N")


if __name__ == "__main__":
    unittest.main()
