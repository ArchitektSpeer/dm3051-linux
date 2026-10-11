import csv
import math
import io
import os
import struct
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from dm3051 import app
from dm3051.app import Sampler, Stats, format_value
from dm3051.simulator import SimulatedMeter
from dm3051.usb_device import (InstrumentError, LegacyProtocol, NotLiveError, RejectedError, SettingError,
                               lookup_function, parse_reading)


def reply(payload):
    return bytes((2, 2, 253, 0)) + struct.pack("<I", len(payload)) + bytes(4) + payload


class Device:
    def __init__(self, replies):
        self.replies = list(replies)
    def write(self, ep, data, timeout):
        return len(data)
    def read(self, ep, size, timeout):
        if not self.replies:
            raise TimeoutError("no reply")
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

    def test_two_stray_r_are_busy(self):
        with self.assertRaises(RejectedError), redirect_stderr(io.StringIO()):
            LegacyProtocol(Device([reply(b"R"), reply(b"R")])).query("*IDN?")

    def test_lone_r_is_busy_not_timeout(self):
        # Seen on RATIO right after switching: the reading query itself answers R.
        protocol = LegacyProtocol(Device([reply(b"R")]))
        protocol.stray_wait_s = 0.05
        with self.assertRaises(RejectedError), redirect_stderr(io.StringIO()):
            protocol.query(":MEASure:VOLTage:DC:RATIo?")


class ParsingTests(unittest.TestCase):
    def test_aliases_and_firmware_tokens(self):
        self.assertEqual(lookup_function("freq").token, "FREQUENCY")
        self.assertEqual(lookup_function("PERIOD").token, "PERIOD")
        self.assertEqual(lookup_function(" dcv ").unit, "V")
        for reply, token in (("RESISTANCE", "2WR"), ("FRESISTANCE", "4WR"), ("CONTINUITY", "CONT"),
                             ("DIODE", "DIODE"), ("CAPACITANCE", "CAP"), ("RATIO", "RATIO")):
            self.assertEqual(lookup_function(reply).token, token)
        with self.assertRaises(InstrumentError):
            lookup_function("1")

    def test_overload_and_garbage(self):
        self.assertEqual(parse_reading("-1.5e-03"), -1.5e-3)
        self.assertIsNone(parse_reading("9.9e37"))
        self.assertIsNone(parse_reading("-8.888000e+03"))
        self.assertEqual(parse_reading("-8.887e3"), -8887.0)
        for stray in ("nan", "1", "5", "RIGOL Technologies", "DCV"):
            with self.assertRaises(RejectedError):  # not the firmware's reading format
                parse_reading(stray)

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

    def test_resistance_long_reply_and_ranges(self):
        m = meter()
        self.assertEqual(m.set_function("2WR").token, "2WR")  # firmware answers RESISTANCE
        self.assertEqual(m.set_range(5), 5)
        with self.assertRaises(ValueError):
            m.set_range(6)  # 100 MΩ is in the manual but clamped to 5 by firmware 03.03

    def test_stray_function_reply_is_not_fatal(self):
        m = meter(switch_rejects=0)
        real = m.query
        stray = iter(["1"])
        m.query = lambda c: next(stray, None) or real(c)
        self.assertEqual(m.set_function("ACV").token, "ACV")

    def test_stray_reply_never_triggers_resend(self):
        m = meter(switch_rejects=0)
        m.resend_s, m.settle_s = 0.0, 0.3
        real = m.query
        sent = []
        m.send_command = lambda c: sent.append(c)
        m.query = lambda c: "1" if c == ":FUNCtion?" else real(c)
        with self.assertRaises(SettingError):
            m.set_function("ACV")
        self.assertEqual(sent, [":FUNCtion:VOLTage:AC"])

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

    def test_capacitance_reading_gets_long_deadline(self):
        m = meter(switch_rejects=0)
        m.set_function("CAP")
        real, seen = m.query, {}

        def spy(command, timeout_ms=None):
            seen[command] = timeout_ms
            return real(command)
        m.query = spy
        m.measure()
        self.assertGreaterEqual(seen[":MEASure:CAPacitance?"], 10000)
        self.assertIsNone(seen[":FUNCtion?"])

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

    def test_stray_replies_are_transient(self):
        m = meter(switch_rejects=0)
        real = m.query
        stray = iter(["1", "DCV", "RIGOL Technologies"])  # function, then function + unparseable value
        m.query = lambda c: next(stray, None) or real(c)
        take = Sampler(m, grace=math.inf)
        self.assertIsNone(take())
        self.assertIn("'1'", str(take.last_rejection))
        self.assertIsNone(take())
        self.assertIn("format", str(take.last_rejection))
        self.assertEqual(take()[1], "DCV")

    def test_late_index_reply_is_not_a_reading(self):
        m = meter(switch_rejects=0)
        real = m.query
        m.query = lambda c: "1" if c.startswith(":MEASure:VOLT") else real(c)  # DCV, "1", DCV
        self.assertIsNone(Sampler(m, grace=math.inf)())

    def test_trigger_switched_mid_run(self):
        m = meter()
        take = Sampler(m)
        self.assertEqual(take()[1], "DCV")
        m.trigger = "single"  # pressed on the panel
        with self.assertRaises(NotLiveError):
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


class OpenRecoveryTests(unittest.TestCase):
    """DM3051.open() with fake PyUSB modules: EIO -> one reset and reopen -> clear message."""
    def run_open(self, outcomes):
        import sys
        import types
        from dm3051.usb_device import DM3051

        class USBError(Exception):
            def __init__(self, errno):
                super().__init__(f"errno {errno}")
                self.errno = errno
        core = types.SimpleNamespace(USBError=USBError)
        util = types.SimpleNamespace(dispose_resources=lambda d: None)
        usb = types.ModuleType("usb")
        usb.core, usb.util = core, util
        device = mock.Mock()
        results = iter(outcomes)

        def fake_open(d, timeout_ms):
            result = next(results)
            if isinstance(result, int):
                raise USBError(result)
            return result
        with mock.patch.dict(sys.modules, {"usb": usb, "usb.core": core, "usb.util": util}), \
                mock.patch.object(DM3051, "_find", staticmethod(lambda bus, address: device)), \
                mock.patch.object(DM3051, "_open", staticmethod(fake_open)), \
                mock.patch("dm3051.usb_device.time.sleep"), redirect_stderr(io.StringIO()):
            try:
                return DM3051.open(), device
            except Exception as exc:
                return exc, device

    def test_eio_reset_then_success(self):
        result, device = self.run_open([5, "opened"])
        self.assertEqual(result, "opened")
        device.reset.assert_called_once()

    def test_eio_twice_asks_for_power_cycle(self):
        result, device = self.run_open([5, 5])
        self.assertIsInstance(result, InstrumentError)
        self.assertIn("Power-cycle", str(result))

    def test_other_usb_errors_are_not_retried(self):
        result, device = self.run_open([16])  # EBUSY: another program owns the meter
        self.assertEqual(getattr(result, "errno", None), 16)
        device.reset.assert_not_called()


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

    def cli_args(self, **kw):
        base = dict(list=False, demo=True, query=None, command=None, status=False, configure_only=True,
                    function=None, range=None, resolution=None, trigger=None, count=1, interval=0, csv=None)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_arguments_checked_before_any_setter(self):
        for kw in (dict(function="ACV", range="999"), dict(function="FREQ", range="AUTO"),
                   dict(function="CONT", resolution=1), dict(range="7")):
            m = SimulatedMeter(switch_rejects=0)
            with self.assertRaises(ValueError), redirect_stderr(io.StringIO()):
                app.configure(m, self.cli_args(**kw))
            self.assertFalse([c for c in m.log if "?" not in c], kw)  # no setter sent

    def test_device_closed_when_csv_close_fails(self):
        closed = []
        m = SimulatedMeter(switch_rejects=0)
        m.close = lambda: closed.append(True)

        class BadFile(io.StringIO):
            def close(self):
                raise OSError("disk full")
        with mock.patch("dm3051.app.open", lambda *a, **k: BadFile(), create=True), \
                mock.patch("dm3051.app.open_device", lambda args: m), \
                self.assertRaises(OSError), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            app.cli(self.cli_args(configure_only=False, csv="x.csv"))
        self.assertEqual(closed, [True])

    def test_demo_list_rejected(self):
        import sys
        argv = sys.argv
        sys.argv = ["dm3051", "--demo", "--list"]
        try:
            with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                app.main()
        finally:
            sys.argv = argv


if __name__ == "__main__":
    unittest.main()
