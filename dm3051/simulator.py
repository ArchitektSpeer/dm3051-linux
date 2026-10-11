"""SCPI-level stand-in for --demo and tests; mimics the quirks seen on firmware 03.03."""
import math
import random
import time
from .usb_device import FUNCTIONS, OPEN_SENTINEL, MeterControl, RejectedError, lookup_function

NOMINAL = {"DCV": 1.0, "ACV": 0.23, "DCI": 1.5e-3, "ACI": 2e-4, "2WR": 1.0e3, "4WR": 1.0e3,
           "FREQUENCY": 50.0, "PERIOD": 0.02, "CONT": OPEN_SENTINEL, "DIODE": 0.62, "CAP": 1e-7, "RATIO": 0.5}
# :FUNCtion? replies of firmware 03.03 where they differ from the token.
REPLIES = {"2WR": "RESISTANCE", "4WR": "FRESISTANCE", "CONT": "CONTINUITY", "CAP": "CAPACITANCE"}


class SimulatedMeter(MeterControl):
    identity = "DEMO,DM3051,SIMULATED,0.1"
    settle_s = 2.0
    poll_s = 0.02

    def __init__(self, latency=0.02, switch_rejects=2):
        self.latency = latency
        self.switch_rejects = switch_rejects  # queries answered E after a function change
        self.function_token = "DCV"
        self.busy = 0
        self.ranges = {f.token: len(f.ranges) // 2 for f in FUNCTIONS if f.ranges}
        self.resolutions = {f.token: 1 for f in FUNCTIONS if f.resolution}
        self.trigger = "auto"
        self.log = []

    def send_command(self, command):
        if "?" in command:
            raise ValueError("Use query() for queries")
        self.log.append(command)
        head, _, arg = command.strip().partition(" ")
        node = head.upper()
        for f in FUNCTIONS:
            if node == f":FUNCTION:{f.path.upper()}":
                self.function_token, self.busy = f.token, self.switch_rejects
            elif node == f":MEASURE:{f.path.upper()}" and f.ranges and arg.isdigit():
                if f.token == self.function_token:
                    self.ranges[f.token] = min(int(arg), len(f.ranges) - 1)  # the firmware clamps
            elif node == f":RESOLUTION:{f.path.upper()}" and f.resolution and arg in ("0", "1", "2"):
                if f.token == self.function_token:
                    self.resolutions[f.token] = int(arg)
        if node == ":TRIGGER:SOURCE" and arg.upper() in ("AUTO", "SINGLE"):
            self.trigger = arg.lower()
        if node == ":MEASURE" and arg.upper() == "AUTO":
            self.ranges[self.function_token] = 0  # pretend the input is small
        # Anything else is silently ignored, like the real firmware.

    def query(self, command):
        time.sleep(self.latency)
        self.log.append(command)
        node = command.strip().upper()
        if self.busy:
            self.busy -= 1
            raise RejectedError(f"Instrument rejected command: {command}")
        if node == "*IDN?":
            return self.identity
        if node == ":FUNCTION?":
            return REPLIES.get(self.function_token, self.function_token)
        if node == ":TRIGGER:SOURCE?":
            return self.trigger
        if node == ":CALCULATE:FUNCTION?":
            return "none"
        if node == ":TRIGGER:AUTO:INTERVAL?":
            return "400ms"
        if node == "CMDSET?":
            return "RIGOL"
        function = lookup_function(self.function_token)
        for f in FUNCTIONS:
            if node == f":MEASURE:{f.path.upper()}:RANGE?" and f.ranges:
                return str(self.ranges[f.token]) if f == function else "R"
            if node == f":RESOLUTION:{f.path.upper()}?" and f.resolution:
                return str(self.resolutions[f.token])
            if node == f":MEASURE:{f.path.upper()}?":
                # Like the firmware: any :MEASure:<x>? returns the displayed reading.
                value = NOMINAL[function.token]
                if value != OPEN_SENTINEL:
                    value *= 1 + 0.01 * math.sin(time.monotonic()) + random.gauss(0, 1e-4)
                return f"{value:.6e}"
        raise RejectedError(f"Instrument rejected command: {command}")

    def close(self):
        pass
