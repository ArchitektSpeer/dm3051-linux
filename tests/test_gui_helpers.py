import math
import os
import tempfile
import unittest
from unittest import mock
from dm3051 import gui


class GuiHelperTests(unittest.TestCase):
    def test_parse_number_si_prefixes(self):
        self.assertIsNone(gui.parse_number("  "))
        self.assertAlmostEqual(gui.parse_number("5m"), 0.005)
        self.assertAlmostEqual(gui.parse_number("1.2 kΩ"), 1200.0)
        self.assertAlmostEqual(gui.parse_number("-3e-3"), -0.003)
        self.assertAlmostEqual(gui.parse_number("10 µF"), 1e-5)
        self.assertAlmostEqual(gui.parse_number("2M"), 2e6)  # M is mega, m is milli
        for bad in ("abc", "1..2", "inf", "1e999"):
            with self.assertRaises(ValueError):
                gui.parse_number(bad)

    def test_readout_keeps_trailing_zeros(self):
        self.assertEqual(gui.readout(1.5, "V", 7), ("1.500000", "V"))
        self.assertEqual(gui.readout(0.0123, "V", 5), ("12.300", "mV"))
        self.assertEqual(gui.readout(0, "Ω", 6), ("0.00000", "Ω"))
        self.assertEqual(gui.readout(999.9999, "V", 5), ("1.0000", "kV"))  # rounding crosses a prefix
        self.assertEqual(gui.readout(0.5, "", 6), ("500.000", "m"))

    def test_readout_with_common_prefix(self):
        self.assertEqual(gui.readout(0.99003, "V", 6, 0), ("0.99003", "V"))
        self.assertEqual(gui.readout(1.01, "V", 6, 0), ("1.01000", "V"))
        self.assertEqual(gui.readout(-8.857e-11, "F", 7, -9), ("-0.088570", "nF"))

    def test_limit_state(self):
        self.assertEqual(gui.limit_state(1.0, None, None), "none")
        self.assertEqual(gui.limit_state(None, None, None), "ovld")
        self.assertEqual(gui.limit_state(None, 0, 1), "ovld")
        self.assertEqual(gui.limit_state(0.5, 0, 1), "ok")
        self.assertEqual(gui.limit_state(-0.1, 0, None), "low")
        self.assertEqual(gui.limit_state(1.1, None, 1), "high")

    def test_nice_ticks_and_labels(self):
        ticks, step = gui.nice_ticks(0.9893, 1.0107, 5)
        self.assertEqual(step, 0.005)
        self.assertTrue(all(0.9893 <= t <= 1.0107 for t in ticks))
        labels, unit = gui.axis_labels(ticks, step, "V")
        self.assertEqual((labels[0], unit), ("0.990", "V"))
        labels, unit = gui.axis_labels([0.0, 5e-9, 1e-8], 5e-9, "F")
        self.assertEqual((labels, unit), (["0", "5", "10"], "nF"))
        self.assertEqual(gui.nice_ticks(1.0, 1.0), ([1.0], 1.0))

    def test_decimate_keeps_extremes(self):
        points = [(i, math.sin(i / 50)) for i in range(5000)]
        points[1234] = (1234, 9.0)  # a spike must survive
        out = gui.decimate(points, 100)
        self.assertLessEqual(len(out), 200)
        self.assertIn((1234, 9.0), out)
        self.assertEqual(out, sorted(out))
        short = points[:50]
        self.assertIs(gui.decimate(short, 100), short)  # nothing to drop

    def test_split_runs_breaks_on_overload_and_gaps(self):
        points = [(0.0, "DCV", 1), (0.4, "DCV", 2), (0.8, "DCV", None), (1.2, "DCV", 3), (1.6, "DCV", 4),
                  (9.0, "DCV", 5), (9.4, "DCV", 6)]
        self.assertEqual(gui.split_runs(points), [[(0.0, 1), (0.4, 2)], [(1.2, 3), (1.6, 4)], [(9.0, 5), (9.4, 6)]])
        self.assertEqual(gui.split_runs([]), [])

    def test_ago(self):
        self.assertEqual(gui.ago(0), "0")
        self.assertEqual(gui.ago(2.5), "−2.5s")
        self.assertEqual(gui.ago(2.0), "−2s")
        self.assertEqual(gui.ago(30), "−30s")
        self.assertEqual(gui.ago(300), "−5m")

    def test_settings_round_trip_and_bad_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "gui.json")
            with mock.patch.object(gui, "CONFIG", path):
                self.assertEqual(gui.load_settings(), {})
                gui.save_settings({"interval": "0.5", "lower": "5m"})
                self.assertEqual(gui.load_settings(), {"interval": "0.5", "lower": "5m"})
                with open(path, "w") as f:
                    f.write("[not json")
                self.assertEqual(gui.load_settings(), {})


if __name__ == "__main__":
    unittest.main()
