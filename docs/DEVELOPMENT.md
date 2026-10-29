# Development notes

## Layout

```text
dm3051/__main__.py       entry point
dm3051/usb_device.py     legacy protocol transport (LegacyProtocol), measuring and settings (MeterControl),
                         USB discovery/release (DM3051), function table
dm3051/simulator.py      SCPI-level simulator SimulatedMeter: mimics the SCPI-level firmware quirks (not USB timing), for --demo and tests
dm3051/app.py            arguments, CLI, Sampler, Stats, value formatting, CSV fields
dm3051/gui.py            Tk interface and USB worker thread (language-neutral)
dm3051/texts.py          every user-visible GUI string; a translation only replaces this table
tests/test_protocol.py   packet-level protocol regression tests (no hardware)
tests/test_control.py    protocol quirks, setting readback/resend, sampling, statistics, CLI CSV
tests/test_gui_helpers.py  GUI number parsing, readout formatting, axis ticks, decimation, settings file
udev/70-dm3051.rules     permissions for this device only
docs/                    install, protocol, development, roadmap, troubleshooting, testing, sources
```

Layers: `LegacyProtocol` only moves the bytes of one command or query; `MeterControl` depends only on `query()`/`send_command()`, so the real `DM3051` and `SimulatedMeter` share all measuring and settings logic.

## Threading model (GUI)

Only the worker thread touches the instrument. The Tk main thread submits settings (function/range/resolution/trigger) through the `requests` queue; the worker runs them one at a time between samples. Results and events (identity, status, sample, notice, error, done) come back through the `events` queue, which the main thread processes every 100 ms. Stopping sets an Event and puts a sentinel in the request queue to wake the thread; closing the window also waits for the thread to release USB and never kills a USB operation. One sample takes three queries, so stopping can take up to about three transaction timeouts plus cleanup.

When the function is changed on the panel, the worker notices it in the samples and refreshes the status, and the combo boxes follow.

## Public API

```python
from dm3051.usb_device import DM3051, list_devices

print(list_devices())
with DM3051.open(timeout_ms=3000) as meter:
    print(meter.identity)
    meter.set_function("ACV")          # confirmed by readback, raises SettingError on failure
    meter.set_range("AUTO")
    meter.set_resolution(2)            # 6½
    meter.check_live()                 # raises NotLiveError unless the trigger is AUTO
    r = meter.measure()                # Reading(function, value, raw)
    print(r.function.token, r.value, r.function.unit, r.raw)
    print(meter.status())
```

| Method | Description |
|---|---|
| `DM3051.open(bus=None, address=None, timeout_ms=3000)` | Picks the single candidate, claims it, drains stale replies, checks the model (identity tried up to 3 times). On EIO it releases, resets the port, waits 2 s and reopens once; if that fails too, raises `InstrumentError` asking for a power cycle |
| `identity` | The `*IDN?` answer at connect time, not the USB descriptor |
| `query(cmd, timeout_ms=None)` / `send_command(cmd)` | One ASCII query/setter; `E` or an empty reply raises `RejectedError`; setters have no reply and acceptance is not guaranteed |
| `function()` | Current function (a `Function` tuple) |
| `measure(expected=None)` | Reads function → reading → function again; overload gives `value=None`; a function change in between raises `RejectedError` |
| `measure_dcv()` | Compatibility with 0.1: must be DCV and not overloaded, returns `(float, raw)` |
| `set_function(name)` / `set_range(i or "AUTO")` / `set_resolution(i)` | Set and confirm by readback (`AUTO` is only sent, it cannot be read back). Range and resolution need two matching readbacks with no different value in between (busy `R` replies do not count against it). Resent when the readback is another definite value; never resent on busy or misplaced replies. Raises `SettingError` on failure, `ValueError` for out-of-range arguments |
| `range_index()` / `resolution()` | Read back the index, retrying briefly while busy; `None` when not available |
| `trigger_source()` / `set_trigger_auto()` / `check_live()` | Trigger source checks; only switching back to AUTO is offered |
| `status()` | Identity, function, unit, range, resolution, trigger, interval, math, command set |
| `drain()` | Empties the IN endpoint and returns the discarded bytes |
| `close()` | Idempotent; best effort to return to local, release the interface and reattach the kernel driver |

Exception hierarchy: `InstrumentError` ⊃ `RejectedError` (busy, rejected or misplaced reply; usually temporary), `SettingError` (setting not applied or not supported; the connection is still usable), `NotLiveError` (trigger source is not AUTO). Any other `InstrumentError` (short write, bad reply header, non-ASCII) means the transaction boundaries may be broken, and the GUI ends the session. A reading must be in the firmware's scientific format (`d.dddddde±dd`); misplaced replies such as bare integers are treated as `RejectedError`. USB transport errors are PyUSB's `USBError`/`TimeoutError` and should be treated as a lost connection.

`app.Sampler(device, expected=None, grace=10)` wraps one sample. After every reading it checks the trigger source (raises `NotLiveError` if it is not AUTO, and the reading is dropped). Temporary `RejectedError`s (`E`, empty or misplaced replies) return `None` with the reason in `last_rejection`, and are only raised once the grace period runs out; no data is ever invented. The CLI uses a 10 s grace period; the GUI uses an infinite one and keeps showing "busy" instead of disconnecting.

## Functions and ranges

| Function | `:FUNCtion?` | Unit | Range index 0,1,2… | Resolution | Drives test current | Verified on Linux |
|---|---|---|---|---|---|---|
| DC voltage | DCV | V | 200 mV, 2 V, 20 V, 200 V, 1000 V | yes | no | yes (including range and resolution) |
| AC voltage | ACV | V | 200 mV, 2 V, 20 V, 200 V, 750 V | yes | no | yes |
| DC current | DCI | A | 2 mA, 20 mA, 200 mA, 1 A, 10 A | yes | no | switching and readings |
| AC current | ACI | A | 20 mA, 200 mA, 2 A, 10 A | yes | no | switching and readings |
| 2-wire resistance | 2WR (firmware `RESISTANCE`) | Ω | 200 Ω, 2 kΩ, 20 kΩ, 200 kΩ, 1 MΩ, 10 MΩ | yes | yes | switching, readback of ranges 0–5, OVLD when open, about 1.5 Ω shorted (lead and contact resistance, no REL zeroing) |
| 4-wire resistance | 4WR (firmware `FRESISTANCE`) | Ω | same as above | yes | yes | switching and readback of ranges 0–5; with only two leads connected the sense inputs float, so drifting readings or OVLD are expected (no known 4-wire resistor measured yet) |
| Frequency | FREQUENCY | Hz | none | no | no | switching and readings |
| Period | PERIOD | s | none | no | no | switching and readings |
| Continuity | CONT (firmware `CONTINUITY`) | Ω | none | no | yes | OVLD when open, about 1.5 Ω shorted |
| Diode | DIODE | V | none | no | yes | OVLD when open, about 1.8 mV shorted (no real diode measured yet) |
| Capacitance | CAP (firmware `CAPACITANCE`) | F | 2 nF … 200 µF (6 ranges) | yes | yes | readback of ranges 0–5, about 131.6 pF open (stray), OVLD shorted (about 3.1 s per reading); no known capacitor measured yet |
| Voltage ratio | RATIO | — | none | yes | no | switching and readings (answers `R` a few times right after switching) |

Resolution index: 0 = 4½, 1 = 5½, 2 = 6½. The table lives in `usb_device.FUNCTIONS`; adding or correcting one entry updates the CLI, GUI and simulator together. Before marking a function as verified, confirm switching, readings, overload and readback on real hardware as described in TESTING.

## Adding a control

Follow "set → read back → sync the UI". In `MeterControl`, use `_apply(setter, readback_query, accept, what)`, which handles polling, busy replies and resending. Only use `_apply` for settings with **absolute** values (resending must be idempotent); relative commands such as `INC`/`DEC` must not be resent this way. In the GUI, add the setting to the mapping in `apply()` and submit it through `request()`; never touch the instrument from the Tk thread. Do not rewrite range or trigger settings on every poll.

Implement the matching SCPI node in the simulator (reproducing any observed SCPI-level quirk), write unit tests, then verify on real hardware and update the quirk table in PROTOCOL and the function table above.

## CSV and data handling

CSV columns are `timestamp,function,value,unit,raw`: the time is ISO 8601 with milliseconds and the local UTC offset, the value is decimal or scientific notation and empty on overload, raw keeps the instrument's text, and the file is UTF-8. The GUI keeps the last 10000 rows in memory and plots the last 300 points of the current function; Live record writes and flushes every row, which suits long runs. The CLI also writes row by row and refuses to overwrite an existing file.

## Packaging and releases

Runs straight from source; there is no build step. After a change, run `python3 -m unittest discover -s tests -v`, the CLI demo and the GUI demo, then do the hardware acceptance in TESTING. For a release, record the version, firmware, what was verified and the logs; do not claim compatibility with every model.
