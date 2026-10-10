"""Legacy packet transport reconstructed and tested on DM3051 firmware 03.03."""
from collections import namedtuple
import logging
import math
import struct
import time

log = logging.getLogger(__name__)


class InstrumentError(RuntimeError):
    pass


class SettingError(InstrumentError):
    """A setting was refused, unsupported, or not confirmed by readback; the link is still usable."""


class RejectedError(InstrumentError):
    """The instrument answered E (or R for an inactive range); often transient after a mode change."""


Function = namedtuple("Function", "token label unit path ranges resolution excites verified")
Function.__doc__ = """One RIGOL-mode measurement function.

token: canonical :FUNCtion? reply; path: SCPI node after :FUNCtion/:MEASure;
ranges: labels for range indices 0..n (empty = no range control here);
resolution: has :RESOlution:<path>; excites: drives a test current through the inputs;
verified: switch and reading checked on Linux hardware (firmware 03.03.00.03.02.00)."""

FUNCTIONS = (
    Function("DCV", "直流电压", "V", "VOLTage:DC", ("200 mV", "2 V", "20 V", "200 V", "1000 V"), True, False, True),
    Function("ACV", "交流电压", "V", "VOLTage:AC", ("200 mV", "2 V", "20 V", "200 V", "750 V"), True, False, True),
    Function("DCI", "直流电流", "A", "CURRent:DC", ("2 mA", "20 mA", "200 mA", "1 A", "10 A"), True, False, True),
    Function("ACI", "交流电流", "A", "CURRent:AC", ("20 mA", "200 mA", "2 A", "10 A"), True, False, True),
    Function("2WR", "两线电阻", "Ω", "RESistance",
             ("200 Ω", "2 kΩ", "20 kΩ", "200 kΩ", "1 MΩ", "10 MΩ", "100 MΩ"), True, True, False),
    Function("4WR", "四线电阻", "Ω", "FRESistance",
             ("200 Ω", "2 kΩ", "20 kΩ", "200 kΩ", "1 MΩ", "10 MΩ", "100 MΩ"), True, True, False),
    # The manual says FREQ/PERI; firmware 03.03 answers FREQUENCY/PERIOD. Their RANGe? replies are not ranges.
    Function("FREQUENCY", "频率", "Hz", "FREQuency", (), False, False, True),
    Function("PERIOD", "周期", "s", "PERiod", (), False, False, True),
    Function("CONT", "通断", "Ω", "CONTinuity", (), False, True, False),
    Function("DIODE", "二极管", "V", "DIODe", (), False, True, False),
    Function("CAP", "电容", "F", "CAPacitance",
             ("2 nF", "20 nF", "200 nF", "2 µF", "20 µF", "200 µF"), True, True, False),
    Function("RATIO", "直流电压比", "", "VOLTage:DC:RATIo", (), True, False, False),
)
_ALIASES = {"FREQ": "FREQUENCY", "PERI": "PERIOD"}
RESOLUTIONS = ("4½", "5½", "6½")


def lookup_function(name):
    """Find a function by :FUNCtion? token (or manual alias)."""
    token = name.strip().upper()
    token = _ALIASES.get(token, token)
    for function in FUNCTIONS:
        if function.token == token:
            return function
    raise InstrumentError(f"Unknown measurement function: {name}")


Reading = namedtuple("Reading", "function value raw")
Reading.__doc__ = "value is None when the instrument reports overload (|x| >= 9e37 or non-finite)."


def parse_reading(raw):
    try:
        value = float(raw)
    except ValueError:
        raise InstrumentError(f"Unparseable reading: {raw!r}") from None
    return None if not math.isfinite(value) or abs(value) >= 9e37 else value


def header(message, tag, length):
    return struct.pack("<BBBBIBBBB", message, tag, tag ^ 255, 0, length, 1, 10 if message == 2 else 0, 0, 0)


class LegacyProtocol:
    """A synchronous, single-owner transport; do not call concurrently."""
    def __init__(self, device, out_endpoint=0x01, in_endpoint=0x82, timeout_ms=3000):
        self.device = device
        self.out_endpoint = out_endpoint
        self.in_endpoint = in_endpoint
        self.timeout_ms = timeout_ms

    def _remaining(self, deadline):
        remaining = math.ceil((deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            raise TimeoutError("USB transaction exceeded its deadline")
        return remaining

    def _write(self, data, deadline):
        log.debug("OUT %s", data.hex(" "))
        n = self.device.write(self.out_endpoint, data, timeout=self._remaining(deadline))
        if n != len(data):
            raise InstrumentError(f"Short USB write: {n}/{len(data)}")

    def _read(self, size, deadline):
        data = bytes(self.device.read(self.in_endpoint, size, timeout=self._remaining(deadline)))
        log.debug("IN %s", data.hex(" "))
        if not data:
            raise InstrumentError("Empty USB response")
        return data

    @staticmethod
    def _command(command):
        if not command.strip() or any(c in command for c in "\r\n\x00;"):
            raise ValueError("Only one nonempty SCPI command is allowed")
        data = (command.strip() + "\n").encode("ascii")
        if len(data) > 4096:
            raise ValueError("Command too long")
        return data

    def _before_query(self):
        """Hook for the USB device to discard stray replies; fakes in tests need nothing."""

    def send_command(self, command):
        """Send a setter without reading. Successful USB write is not command acceptance."""
        if "?" in command:
            raise ValueError("Use query() for queries")
        deadline = time.monotonic() + self.timeout_ms / 1000
        data = self._command(command)
        self._write(header(1, 1, len(data)), deadline)
        self._write(data, deadline)

    def _read_reply(self, deadline):
        first = self._read(64, deadline)
        if len(first) < 12:
            raise InstrumentError("Incomplete legacy reply header")
        length = struct.unpack_from("<I", first, 4)[0]
        if first[:3] == bytes((2, 2, 253)) and length == 0:
            # Seen after :TRIGger:SINGle:TRIGger: an empty reply while no reading is available.
            raise RejectedError("Empty reply: instrument has no reading available (busy)")
        if first[:3] != bytes((2, 2, 253)) or not 1 <= length <= 4096:
            raise InstrumentError(f"Invalid legacy header: {first[:12].hex()}")
        payload = bytearray(first[12:12 + length])
        while len(payload) < length:
            remaining = length - len(payload)
            payload.extend(self._read((remaining + 3) // 4 * 4, deadline)[:remaining])
        return payload.decode("ascii").strip()

    def query(self, command):
        if "?" not in command:
            raise ValueError("query() requires a query; use send_command() for setters")
        deadline = time.monotonic() + self.timeout_ms / 1000
        data = self._command(command)
        self._before_query()
        # This device requires separate transfers, no payload alignment padding.
        self._write(header(1, 1, len(data)), deadline)
        self._write(data, deadline)
        self._write(header(2, 2, 64), deadline)
        answer = self._read_reply(deadline)
        if answer == "R" and "RANG" not in command.upper():
            # A late busy marker from an earlier RANGe? query; our own reply follows it.
            log.warning("Skipping stray R reply before answer to %s", command)
            answer = self._read_reply(deadline)
        if answer == "E":
            raise RejectedError(f"Instrument rejected command: {command}")
        return answer


class MeterControl:
    """RIGOL-mode measurement and settings on top of query()/send_command().

    Every setter is followed by a readback poll; the firmware has no usable :SYSTem:ERRor?
    and silently ignores rejected setters. Shared by the USB device and the simulator."""
    settle_s = 5.0
    poll_s = 0.1
    resend_s = 1.0

    def _apply(self, setter, command, accept, what):
        """Send an absolute (idempotent) setter and poll its readback. Firmware 03.03 sometimes
        drops a setter while busy, so it is resent when the readback still shows a different
        definite value. E (and R from RANGe?) means "busy": wait, a resend would restart ranging."""
        self.send_command(setter)
        deadline = time.monotonic() + self.settle_s
        resend = time.monotonic() + self.resend_s
        last = None
        while True:
            try:
                last = self.query(command)
                if accept(last):
                    return last
                if last != "R" and time.monotonic() >= resend:
                    log.info("Readback %r; resending %s", last, setter)
                    self.send_command(setter)
                    resend = time.monotonic() + self.resend_s
            except RejectedError:
                last = "E"
            if time.monotonic() >= deadline:
                raise SettingError(f"{what} not confirmed by readback (last reply {last!r})")
            time.sleep(self.poll_s)

    def function(self):
        return lookup_function(self.query(":FUNCtion?"))

    def measure(self, expected=None):
        """One reading of the current panel function. Any :MEASure:<x>? returns the
        displayed value on this firmware, so the function is read before and after."""
        function = self.function()
        if expected and function.token != expected:
            raise InstrumentError(f"Current mode is {function.token}, expected {expected}.")
        raw = self.query(f":MEASure:{function.path}?")
        if self.function() != function:
            raise RejectedError("Function changed during the reading; discarded")
        return Reading(function, parse_reading(raw), raw)

    def measure_dcv(self):
        reading = self.measure("DCV")
        if reading.value is None:
            raise InstrumentError(f"Overrange or invalid reading: {reading.raw}")
        return reading.value, reading.raw

    def set_function(self, name):
        function = lookup_function(name)
        self._apply(f":FUNCtion:{function.path}", ":FUNCtion?", lambda r: lookup_function(r) == function, f"Function {function.token}")
        return function

    def range_index(self, function=None):
        function = function or self.function()
        return self._read_index(f":MEASure:{function.path}:RANGe?") if function.ranges else None

    def _read_index(self, command, attempts=8):
        """Integer readback; R/E mean busy (about one RANGe? in three answers R), so retry briefly."""
        for _ in range(attempts):
            try:
                reply = self.query(command)
                if reply.isdigit():
                    return int(reply)
            except RejectedError:
                pass
            time.sleep(self.poll_s)
        return None

    def set_range(self, index):
        """index: int for a manual range, or "AUTO". Manual is confirmed by readback;
        AUTO cannot be read back on this firmware, only the range it settles on."""
        function = self.function()
        if not function.ranges:
            raise SettingError(f"{function.token} has no range control here")
        if str(index).upper() == "AUTO":
            self.send_command(":MEASure AUTO")
            return "AUTO"
        index = int(index)
        if not 0 <= index < len(function.ranges):
            raise ValueError(f"{function.token} range must be 0..{len(function.ranges) - 1}")
        self._apply(f":MEASure:{function.path} {index}", f":MEASure:{function.path}:RANGe?", lambda r: r == str(index), f"Range {index}")
        return index

    def resolution(self, function=None):
        function = function or self.function()
        return self._read_index(f":RESOlution:{function.path}?") if function.resolution else None

    def set_resolution(self, index):
        function = self.function()
        index = int(index)
        if not function.resolution:
            raise SettingError(f"{function.token} has no resolution setting")
        if not 0 <= index < len(RESOLUTIONS):
            raise ValueError("Resolution must be 0 (4½), 1 (5½) or 2 (6½)")
        self._apply(f":RESOlution:{function.path} {index}", f":RESOlution:{function.path}?", lambda r: r == str(index), f"Resolution {index}")
        return index

    def trigger_source(self):
        return self.query(":TRIGger:SOURce?").upper()

    def set_trigger_auto(self):
        """Only AUTO is offered: in SINGLE mode firmware 03.03 keeps returning the held reading,
        and after a remote single trigger :MEASure? answers empty until the mode changes."""
        self._apply(":TRIGger:SOURce AUTO", ":TRIGger:SOURce?", lambda r: r.upper() == "AUTO", "Trigger AUTO")
        return "AUTO"

    def check_live(self):
        """Raise unless the instrument updates readings by itself (auto trigger)."""
        source = self.trigger_source()
        if source != "AUTO":
            raise InstrumentError(f"Trigger source is {source}; readings would be a held value. "
                                  "Switch to auto trigger (--trigger AUTO or the GUI button).")

    def status(self):
        """Read-only snapshot for display and diagnostics; unsupported items are None."""
        function = self.function()
        result = {"identity": self.identity, "function": function.token, "unit": function.unit,
                  "range": self.range_index(function), "resolution": self.resolution(function)}
        for key, command in (("trigger", ":TRIGger:SOURce?"), ("interval", ":TRIGger:AUTO:INTErval?"),
                             ("calculate", ":CALCulate:FUNCtion?"), ("cmdset", "CMDSet?")):
            try:
                result[key] = self.query(command)
            except RejectedError:
                result[key] = None
        return result


class DM3051(MeterControl, LegacyProtocol):
    def __init__(self, device, interface, out_ep, in_ep, detached, util, timeout_ms):
        super().__init__(device, out_ep, in_ep, timeout_ms)
        self.interface, self.detached, self.util = interface, detached, util
        self.closed = False
        self.identity = ""

    @classmethod
    def open(cls, bus=None, address=None, timeout_ms=3000):
        import usb.core
        import usb.util
        candidates = [d for d in usb.core.find(find_all=True)
                      if d.idVendor in (0x0400, 0x1AB1) and d.idProduct == 0x09C4
                      and (bus is None or d.bus == bus) and (address is None or d.address == address)]
        if len(candidates) != 1:
            raise InstrumentError(f"Found {len(candidates)} matching devices. Use --list / --bus / --address.")
        d = candidates[0]
        detached, claimed, interface = False, False, None
        try:
            try:
                config = d.get_active_configuration()
            except usb.core.USBError:
                d.set_configuration()
                config = d.get_active_configuration()
            interfaces = []
            for i in config:
                if i.bInterfaceClass == 0xFE and i.bInterfaceSubClass == 3 and i.bAlternateSetting == 0:
                    bulk = [e for e in i if e.bmAttributes & 3 == 2]
                    out_ep = [e.bEndpointAddress for e in bulk if not e.bEndpointAddress & 128]
                    in_ep = [e.bEndpointAddress for e in bulk if e.bEndpointAddress & 128]
                    if len(out_ep) == len(in_ep) == 1:
                        interfaces.append((i.bInterfaceNumber, out_ep[0], in_ep[0]))
            if len(interfaces) != 1:
                raise InstrumentError("Expected one USBTMC interface with one bulk IN/OUT pair")
            interface, out_ep, in_ep = interfaces[0]
            if d.is_kernel_driver_active(interface):
                d.detach_kernel_driver(interface)
                detached = True
            usb.util.claim_interface(d, interface)
            claimed = True
            instrument = cls(d, interface, out_ep, in_ep, detached, usb.util, timeout_ms)
            instrument.drain()
            try:
                instrument.identity = instrument.query("*IDN?")
            except usb.core.USBError as exc:
                # Seen on Linux: every bulk transfer fails with EIO although control requests
                # work, until a USB port reset. The reset keeps the instrument's settings.
                log.warning("Identity query failed (%s); resetting the USB port once", exc)
                d.reset()
                instrument.identity = instrument.query("*IDN?")
            fields = instrument.identity.split(",")
            if len(fields) < 3 or fields[1].strip() != "DM3051":
                raise InstrumentError(f"Unexpected instrument: {instrument.identity}")
            return instrument
        except BaseException:
            if claimed:
                try:
                    usb.util.release_interface(d, interface)
                except Exception:
                    log.exception("Release after open failure")
            if detached:
                try:
                    d.attach_kernel_driver(interface)
                except Exception:
                    log.exception("Reattach after open failure")
            usb.util.dispose_resources(d)
            raise

    def _before_query(self):
        # Rarely a reply arrives one query late (seen once in hundreds of mode switches);
        # discarding it here keeps the following answers aligned. Costs about 1 ms.
        self.drain(timeout_ms=1)

    def drain(self, limit=64, timeout_ms=100):
        """Discard replies left queued by an earlier, interrupted client, so they cannot
        be taken as the answer to our first query. Returns the discarded bytes."""
        import usb.core
        stale = bytearray()
        for _ in range(limit):
            try:
                stale.extend(self.device.read(self.in_endpoint, 64, timeout=timeout_ms))
            except usb.core.USBTimeoutError:
                break
            except usb.core.USBError as exc:
                log.debug("Drain stopped: %s", exc)
                break
        if stale:
            log.warning("Discarded %d stale bytes: %s", len(stale), bytes(stale[:64]).hex(" "))
        return bytes(stale)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            # Best effort only; this firmware may need the physical Single key.
            self.device.ctrl_transfer(0xA1, 161, 0, self.interface, 1, timeout=500)
        except Exception as exc:
            log.debug("Local request unavailable: %s", exc)
        try:
            self.util.release_interface(self.device, self.interface)
        finally:
            try:
                if self.detached:
                    self.device.attach_kernel_driver(self.interface)
            finally:
                self.util.dispose_resources(self.device)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def list_devices():
    import usb.core
    return [{"bus": d.bus, "address": d.address, "vid": f"{d.idVendor:04x}", "pid": f"{d.idProduct:04x}"}
            for d in usb.core.find(find_all=True)
            if d.idVendor in (0x0400, 0x1AB1) and d.idProduct == 0x09C4]
