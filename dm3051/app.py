import argparse
import csv
from datetime import datetime
import json
import logging
import math
import sys
import time
from .usb_device import DM3051, InstrumentError, RESOLUTIONS, RejectedError, list_devices, lookup_function

FIELDS = ("timestamp", "function", "value", "unit", "raw")
REJECT_GRACE_S = 10.0  # transient E replies tolerated while the panel changes mode
_PREFIXES = {-12: "p", -9: "n", -6: "µ", -3: "m", 0: "", 3: "k", 6: "M", 9: "G"}


def format_value(value, unit, digits=7):
    if value is None:
        return "OVLD"
    if value == 0 or not unit:
        return f"{value:.{digits}g} {unit}".strip()
    exponent = min(max(math.floor(math.log10(abs(value)) / 3) * 3, -12), 9)
    return f"{value / 10 ** exponent:.{digits}g} {_PREFIXES[exponent]}{unit}"


class Stats:
    """Running min/max/mean/σ (Welford) for one function; overloads are counted separately."""
    def __init__(self, function=None, unit=""):
        self.function, self.unit = function, unit
        self.n = self.overloads = 0
        self.mean = self._m2 = 0.0
        self.min = self.max = None

    def add(self, value):
        if value is None:
            self.overloads += 1
            return
        self.n += 1
        delta = value - self.mean
        self.mean += delta / self.n
        self._m2 += delta * (value - self.mean)
        self.min = value if self.min is None else min(self.min, value)
        self.max = value if self.max is None else max(self.max, value)

    @property
    def stdev(self):
        return math.sqrt(self._m2 / (self.n - 1)) if self.n > 1 else None


def summary(st):
    if not st.n:
        return f"{st.function}: {st.overloads} overload"
    return (f"{st.function}: n={st.n} min={format_value(st.min, st.unit)} max={format_value(st.max, st.unit)} "
            f"mean={format_value(st.mean, st.unit)} σ={format_value(st.stdev, st.unit) if st.stdev is not None else '—'}"
            + (f" overload={st.overloads}" if st.overloads else ""))


def open_device(args):
    if args.demo:
        from .simulator import SimulatedMeter
        return SimulatedMeter()
    return DM3051.open(args.bus, args.address, args.timeout)


def sample(device, expected=None):
    """One CSV row; overload keeps value empty and raw as the instrument sent it."""
    reading = device.measure(expected)
    stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
    return (stamp, reading.function.token, reading.value, reading.function.unit, reading.raw)


class Sampler:
    """Retries transient E replies (mode change on the panel) for a bounded time, never inventing data.
    The trigger source is checked after every reading (raises NotLiveError), so a switch to
    single trigger on the panel cannot turn held values into new rows."""
    def __init__(self, device, expected=None, grace=REJECT_GRACE_S):
        self.device, self.expected, self.grace = device, expected, grace
        self.rejected_since = None
        self.last_rejection = None

    def __call__(self):
        try:
            row = sample(self.device, self.expected)
            self.device.check_live()
        except RejectedError as exc:
            self.last_rejection = exc
            now = time.monotonic()
            self.rejected_since = self.rejected_since or now
            if now - self.rejected_since > self.grace:
                raise
            return None
        self.rejected_since = None
        return row


def configure(device, args):
    """Explicit setters from the command line, confirmed by readback (except AUTO range).
    Everything that can be checked is checked before the first setter changes the instrument."""
    if args.range is not None or args.resolution is not None:
        target = lookup_function(args.function) if args.function else device.function()
        if args.range is not None and str(args.range).upper() != "AUTO":
            if not 0 <= int(args.range) < len(target.ranges):
                raise ValueError(f"{target.token} has no range {args.range}"
                                 + (f" (0..{len(target.ranges) - 1} or AUTO)" if target.ranges else ""))
        elif args.range is not None and not target.ranges:
            raise ValueError(f"{target.token} has no range control")
        if args.resolution is not None and not (target.resolution and 0 <= args.resolution < len(RESOLUTIONS)):
            raise ValueError(f"{target.token} has no resolution setting")
    if args.trigger:
        print(f"trigger -> {device.set_trigger_auto()}", file=sys.stderr)
    if args.function:
        print(f"function -> {device.set_function(args.function).token}", file=sys.stderr)
    if args.range is not None:
        result = device.set_range(args.range)
        print(f"range -> {result}" + (" (sent; the firmware cannot read back auto mode)" if result == "AUTO" else ""),
              file=sys.stderr)
    if args.resolution is not None:
        print(f"resolution -> {device.set_resolution(args.resolution)}", file=sys.stderr)


def cli(args):
    if args.list:
        print(json.dumps(list_devices(), indent=2))
        return
    device = open_device(args)
    output = None
    try:
        print(device.identity, file=sys.stderr)
        if args.query:
            print(device.query(args.query))
            return
        if args.command:
            device.send_command(args.command)
            print("USB write completed; verify acceptance with a related query.")
            return
        configure(device, args)
        if args.status:
            print(json.dumps(device.status(), ensure_ascii=False, indent=2))
            return
        if args.configure_only:
            return
        writer = None
        if args.csv:
            output = open(args.csv, "x", newline="", encoding="utf-8")
            writer = csv.writer(output)
            writer.writerow(FIELDS)
        expected = lookup_function(args.function).token if args.function else None
        take = Sampler(device, expected)
        stats = {}
        done = 0
        while done < args.count:
            started = time.monotonic()
            row = take()
            if row:
                done += 1
                stats.setdefault(row[1], Stats(row[1], row[3])).add(row[2])
                print(f"{row[0]}  {row[1]:>9}  {format_value(row[2], row[3])}", flush=True)
                if writer:
                    writer.writerow(row)
                    output.flush()
            else:
                print(f"instrument busy, retrying… ({take.last_rejection})", file=sys.stderr)
            time.sleep(max(0, args.interval - (time.monotonic() - started)))
        for st in stats.values():
            print(summary(st), file=sys.stderr)
    finally:
        try:
            if output:
                output.close()
        finally:
            device.close()


def main():
    parser = argparse.ArgumentParser(description="DM3051 legacy USB GUI and headless reader")
    parser.add_argument("--cli", action="store_true")
    parser.add_argument("--demo", action="store_true", help="Simulated instrument, no USB access")
    parser.add_argument("--list", action="store_true", help="List matching USB devices")
    parser.add_argument("--bus", type=int)
    parser.add_argument("--address", type=int)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=3000, help="Transaction deadline in ms")
    parser.add_argument("--csv", help="CLI output; existing files are not overwritten")
    parser.add_argument("--function", help="Switch function first (DCV ACV DCI ACI 2WR 4WR FREQ PERI CONT "
                        "DIODE CAP RATIO) and require it while sampling")
    parser.add_argument("--range", help="Range index for the current function, or AUTO")
    parser.add_argument("--resolution", type=int, choices=(0, 1, 2), help="0=4½ 1=5½ 2=6½ digits")
    parser.add_argument("--trigger", choices=("AUTO",), type=str.upper,
                        help="Switch the trigger source back to AUTO (SINGLE/EXT cannot be polled)")
    parser.add_argument("--status", action="store_true", help="Print function/range/resolution/trigger as JSON")
    parser.add_argument("--configure-only", action="store_true", help="Apply --function/--range/--resolution, no sampling")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--query", help="One SCPI query")
    group.add_argument("--command", help="Explicit setter; acceptance must be queried separately")
    parser.add_argument("--trace", action="store_true", help="Log packet hex to stderr")
    args = parser.parse_args()
    if not math.isfinite(args.interval) or args.interval < 0.2 or args.count < 1 or not 100 <= args.timeout <= 30000:
        parser.error("interval >= 0.2, count >= 1, timeout 100..30000 required")
    if args.demo and args.list:
        parser.error("--list enumerates real USB devices; it cannot be combined with --demo")
    if args.function:
        try:
            lookup_function(args.function)
        except InstrumentError as exc:
            parser.error(str(exc))
    if args.range is not None and args.range.upper() != "AUTO" and not args.range.isdigit():
        parser.error("--range takes an index (see docs) or AUTO")
    configuring = args.function or args.range or args.resolution is not None or args.trigger
    if configuring and not (args.cli or args.status or args.configure_only):
        parser.error("configuration options need --cli, --status or --configure-only")
    if (args.query or args.command) and configuring:
        parser.error("--query/--command cannot be combined with configuration options")
    logging.basicConfig(level=logging.DEBUG if args.trace else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.cli or args.list or args.query or args.command or args.status or args.configure_only:
            cli(args)
        else:
            from .gui import gui
            gui(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logging.error("%s: %s", type(exc).__name__, exc)
        if args.trace:
            logging.exception("Details")
        return 1
    return 0
