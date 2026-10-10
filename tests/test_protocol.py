import struct
import unittest
from dm3051.usb_device import LegacyProtocol, InstrumentError


class Device:
    def __init__(self, replies):
        self.replies = list(replies)
        self.writes = []
    def write(self, ep, data, timeout):
        self.writes.append((ep, bytes(data)))
        return len(data)
    def read(self, ep, size, timeout):
        return self.replies.pop(0)


def reply(payload):
    return bytes((2, 2, 253, 0)) + struct.pack("<I", len(payload)) + bytes(4) + payload


class ProtocolTests(unittest.TestCase):
    def test_separate_legacy_transfers(self):
        d = Device([reply(b"1.234e-3\n")])
        self.assertEqual(LegacyProtocol(d).query("*IDN?"), "1.234e-3")
        self.assertEqual(len(d.writes), 3)
        self.assertEqual(d.writes[0][1], bytes.fromhex("0101fe000600000001000000"))
        self.assertEqual(d.writes[1][1], b"*IDN?\n")
        self.assertEqual(d.writes[2][1], bytes.fromhex("0202fd0040000000010a0000"))

    def test_raw_continuation_and_padding(self):
        payload = b"a" * 70
        framed = reply(payload)
        d = Device([framed[:64], framed[64:] + bytes(2)])
        self.assertEqual(LegacyProtocol(d).query("*IDN?"), "a" * 70)

    def test_invalid_reply(self):
        for packet in (b"x", bytes(12), bytes((2, 2, 0)) + bytes(9)):
            with self.subTest(packet=packet), self.assertRaises(InstrumentError):
                LegacyProtocol(Device([packet])).query("*IDN?")

    def test_rejected_command(self):
        with self.assertRaises(InstrumentError):
            LegacyProtocol(Device([reply(b"E")])).query("READ?")

    def test_command_boundaries(self):
        for cmd in ("", "*IDN?\n*RST", "*IDN?;*RST", "*RST"):
            with self.subTest(cmd=cmd), self.assertRaises(ValueError):
                LegacyProtocol(Device([])).query(cmd)

    def test_short_write(self):
        d = Device([])
        d.write = lambda *a, **kw: 0
        with self.assertRaises(InstrumentError):
            LegacyProtocol(d).query("*IDN?")

    def test_setter_has_no_read_request(self):
        d = Device([])
        LegacyProtocol(d).send_command("CMDSET RIGOL")
        self.assertEqual(len(d.writes), 2)
        self.assertEqual(d.writes[1][1], b"CMDSET RIGOL\n")


if __name__ == "__main__":
    unittest.main()
