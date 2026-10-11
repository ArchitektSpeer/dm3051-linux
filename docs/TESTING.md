# Testing and acceptance

## Unit tests (no hardware)

```sh
python3 -m unittest discover -s tests -v
```

41 tests:
- `test_protocol.py`: exact bytes of the three separate writes, unpadded payload, raw segmented continuation reads, bad reply headers, `E` errors, command boundaries, short writes, setters that never read.
- `test_control.py`: empty reply = busy; lone `R` skipped; function aliases (FREQ/FREQUENCY and long names such as RESISTANCE); overload (9.9e37 and -8.888e3) and unparseable readings; SI formatting; `E` while switching function; resending dropped setters; `R` never triggers a resend; range/resolution readback and bounds; readback failure; function change during a reading; expected function; overload readings; trigger check; status; long resistance name and ranges 0–5; misplaced replies are not fatal and do not trigger a resend; sampler grace period, misplaced replies treated as busy, a late range index not taken as a reading, switching to single trigger mid-run, refusing held readings; statistics; `open()` EIO recovery (reset and reopen / ask for a power cycle / no retry for other errors); lone and consecutive `R` treated as busy; long deadline for capacitance readings; CLI demo CSV output and refusal to overwrite; arguments checked before any setter runs; device released even when closing the CSV fails; `--demo --list` rejected.

The GUI was also tested automatically under Xvfb, driving buttons and combo boxes, both against the simulator and the real meter (0.2.1 additionally tested with the simulator: instrument busy at connect and then recovering, switching to single trigger mid-run and back with the button, the auto range notice, switching to resistance/continuity). Those scripts are not in the repository because they depend on desktop screenshot tools.

## Hardware acceptance on Linux (2026-10-10)

```text
Arch Linux, kernel 7.2.8-arch1-1
Python 3.14 / PyUSB 1.3.1 / libusb 1.0.30 / Tk 8.6
Instrument: DM3051, firmware 03.03.00.03.02.00, serial DM3H1035xxxxx
USB: 0400:09c4, interface 0 class FE/03/01, EP OUT 0x01 / IN 0x82, 64 bytes, no USB serial number
Permissions: udev/70-dm3051.rules (uaccess), regular user
```

| # | Item | Result |
|---|---|---|
| 1 | `--list` finds exactly one device | Pass |
| 2 | `*IDN?` | Pass: `RIGOL Technoligies,DM3051,DM3H1035xxxxx,03.03.00.03.02.00` |
| 3 | Every bulk transfer fails with EIO on first connect | Seen twice, both after the meter was power-cycled: the first time a port reset fixed it; the second time (2026-10-11) the reset did not help and power-cycling the meter did → the code now releases, resets and reopens once automatically, and asks for a power cycle if that still fails |
| 4 | `:FUNCtion?` = DCV; CLI 10-point CSV | Pass: header + 10 rows, correct summary |
| 5 | Function changed on the panel to ACV/DCI/ACI/FREQ/PERI, with readings | Pass (found the FREQUENCY/PERIOD token difference) |
| 6 | DCV manual ranges 0–4 read back, auto range, resolution 1↔2 | Pass |
| 7 | Stress: ACV↔DCV 8 times + range 3 times + resolution; many rounds during development, 2 rounds on the final code | Pass; observed dropped setters (confirmed after resend) and late duplicate replies (discarded by the drain), with `*IDN?` in step afterwards |
| 8 | Sampling with single trigger | Correctly refused; `--trigger AUTO` restores it, confirmed by readback |
| 9 | Two processes at once | The second gets Resource busy and the first is unaffected; connecting works normally afterwards |
| 10 | GUI: connect, ACV→DCV, 20 V range→auto, 6½, upper limit alarm, live record, stop, save | Pass: live record 37 rows = export 37 rows, function column correct |
| 11 | Kernel driver takes over after close | Pass: repeated open/close leaves nothing behind |
| 12 | Unplugging during a session, real overload signal | **Not done** (nobody present to unplug; no overload signal that could be applied safely) |
| 13 | 2WR/4WR/CONT/DIODE/CAP/RATIO switching on the real meter (2026-10-11, all leads disconnected) | Pass: switching and readings; resistance/capacitance range readback. Found the long function names, the `-8.888e3` open value and the missing 100 MΩ resistance range (all fixed). The full rerun on the final code was stopped halfway; **no known resistor, capacitor or diode was measured** |
| 14 | Retest with shorted leads (2026-10-11, 0.2.2 code) | Pass: 2WR about 1.53 Ω, CONT about 1.5 Ω, DIODE about 1.8 mV, CAP OVLD; every 2WR/4WR/CAP range read back; resolution readback; CLI 2WR 6-point CSV; GUI on the real meter DCV→2WR→10 MΩ→CONT→DCV, live record 46 rows = export 46 rows. Found and fixed along the way: RATIO answering `R` after switching caused a timeout and disconnect; capacitance readings (about 3.1 s) exceeded the 3 s deadline; a new connection's identity query received a late `R`. After the fixes, 3 rounds of RATIO switching and 8 consecutive cross-process switches all passed |
