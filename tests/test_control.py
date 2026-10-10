import csv
import io
import os
import struct
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from dm3051 import app
from dm3051.app import Sampler, Stats, format_value
from dm3051.simulator import SimulatedMeter
from dm3051.usb_device import (InstrumentError, LegacyProtocol, RejectedError, SettingError,
                               lookup_function, parse_reading)


def reply(payload):
    return bytes((2, 2, 253, 0)) + struct.pack("<I", len(payload)) + bytes(4) + payload


class Device:
    def __init__(self, replies):
        self.replies = list(replies)
    def write(self, ep, data, timeout):
        return len(data)
    def read(self, ep, size, timeout):
        return self.replies.pop(0)


def meter(**kw):
    m = SimulatedMeter(latency=0, **kw)
    m.poll_s = 0.001
    return m


class ProtocolQuirkTests(unittest.TestCase):
    def test_empty_reply_is_busy(self):
        with self.assertRaises(RejectedError):
            LegacyProtocol(Device([reply(b"")])).query(":MEASure:VOLTage:DC?")

    def test_stray_r_skipped_except_for_range(self):
        self.assertEqual(LegacyProtocol(Device([reply(b"R"), reply(b"DCV")])).query(":FUNCtion?"), "DCV")
        self.assertEqual(LegacyProtocol(Device([reply(b"R")])).query(":MEASure:VOLTage:DC:RANGe?"), "R")


class ParsingTests(unittest.TestCase):
    def test_aliases_and_firmware_tokens(self):
        self.assertEqual(lookup_function("freq").token, "FREQUENCY")
        self.assertEqual(lookup_function("PERIOD").token, "PERIOD")
        self.assertEqual(lookup_function(" dcv ").unit, "V")
        with self.assertRaises(InstrumentError):
            lookup_function("1")

    def test_overload_and_garbage(self):
        self.assertEqual(parse_reading("-1.5e-03"), -1.5e-3)
        self.assertIsNone(parse_reading("9.9e37"))
        self.assertIsNone(parse_reading("nan"))
        with self.assertRaises(InstrumentError):
            parse_reading("RIGOL Technologies")

    def test_format(self):
        self.assertEqual(format_value(5.436313e-4, "V"), "543.6313 µV")
        self.assertEqual(format_value(1.2e3, "Ω"), "1.2 kΩ")
        self.assertEqual(format_value(0.0, "V"), "0 V")
        self.assertEqual(format_value(0.5, ""), "0.5")
        self.assertIn("OVLD", format_value(None, "V"))


class ControlTests(unittest.TestCase):
    def test_function_switch_waits_through_rejections(self):
        m = meter(switch_rejects=3)
        self.assertEqual(m.set_function("ACV").token, "ACV")
        self.assertEqual(m.measure().function.token, "ACV")

    def test_dropped_setter_is_resent(self):
        m = meter()
        m.resend_s = 0.01
        original, dropped = m.send_command, []
        def flaky(command):
            if not dropped:
                dropped.append(command)  # firmware ignores the first one
                return
            original(command)
        m.send_command = flaky
        m.set_function("DCI")
        self.assertEqual(m.function_token, "DCI")
        self.assertEqual(dropped, [":FUNCtion:CURRent:DC"])

    def test_busy_range_is_not_resent(self):
        m = meter()
        m.settle_s, m.resend_s = 0.05, 0.0
        m.query = lambda c: "DCV" if c == ":FUNCtion?" else "R"
        sent = []
        m.send_command = sent.append
        with self.assertRaises(SettingError):
            m.set_range(1)
        self.assertEqual(sent, [":MEASure:VOLTage:DC 1"])

    def test_range_and_resolution_readback(self):
        m = meter()
        self.assertEqual(m.set_range(4), 4)
        self.assertEqual(m.range_index(), 4)
        self.assertEqual(m.set_range("auto"), "AUTO")
        self.assertEqual(m.set_resolution(2), 2)
        with self.assertRaises(ValueError):
            m.set_range(5)
        m.set_function("FREQ")
        with self.assertRaises(SettingError):
            m.set_range(1)
        with self.assertRaises(SettingError):
            m.set_resolution(1)

    def test_unconfirmed_setting(self):
        m = meter()
        m.settle_s = 0.05
        m.send_command = lambda c: None
        with self.assertRaises(SettingError):
            m.set_function("ACV")

    def test_function_change_during_reading_is_discarded(self):
        m = meter()
        answers = iter(["DCV", "1.0", "ACV"])
        m.query = lambda c: next(answers)
        with self.assertRaises(RejectedError):
            m.measure()

    def test_expected_function(self):
        m = meter()
        with self.assertRaises(InstrumentError):
            m.measure("ACV")
        self.assertAlmostEqual(m.measure_dcv()[0], 1.0, places=1)

    def test_overload_reading(self):
        m = meter()
        m.set_function("CONT")
        self.assertIsNone(m.measure().value)
        with self.assertRaises(InstrumentError):
            m.measure_dcv()

    def test_trigger(self):
        m = meter()
        m.send_command(":TRIGger:SOURce SINGle")
        with self.assertRaises(InstrumentError):
            m.check_live()
        self.assertEqual(m.set_trigger_auto(), "AUTO")
        m.check_live()

    def test_status(self):
        info = meter().status()
        self.assertEqual((info["function"], info["unit"], info["trigger"], info["calculate"]),
                         ("DCV", "V", "auto", "none"))


class SamplerTests(unittest.TestCase):
    def test_grace_then_failure(self):
        m = meter()
        take = Sampler(m, grace=0.05)
        self.assertEqual(take()[1], "DCV")
        m.busy = 10**6
        self.assertIsNone(take())
        import time
        time.sleep(0.06)
        with self.assertRaises(RejectedError):
            take()

    def test_refuses_held_readings(self):
        m = meter()
        m.trigger = "single"
        with self.assertRaises(InstrumentError):
            Sampler(m)()

    def test_stats(self):
        s = Stats("DCV", "V")
        for v in (1.0, 2.0, None, 3.0):
            s.add(v)
        self.assertEqual((s.n, s.overloads, s.min, s.max, s.mean), (3, 1, 1.0, 3.0, 2.0))
        self.assertAlmostEqual(s.stdev, 1.0)
        self.assertIsNone(Stats().stdev)


class CliTests(unittest.TestCase):
    def test_demo_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.csv")
            args = SimpleNamespace(list=False, demo=True, query=None, command=None, status=False,
                                   configure_only=False, function="ACV", range="1", resolution=2, trigger=None,
                                   count=3, interval=0, csv=path)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                app.cli(args)
            with open(path, encoding="utf-8") as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0], list(app.FIELDS))
            self.assertEqual(len(rows), 4)
            self.assertTrue(all(r[1] == "ACV" and r[3] == "V" for r in rows[1:]))
            self.assertIn("ACV: n=3", err.getvalue())
            with self.assertRaises(FileExistsError), redirect_stdout(out), redirect_stderr(err):
                app.cli(args)


if __name__ == "__main__":
    unittest.main()
