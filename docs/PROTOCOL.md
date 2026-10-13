# DM3051 legacy USB protocol

## Why not plain VISA/usbtmc

The test instrument has a USBTMC interface descriptor, but writing a standard combined message and then reading times out. The vendor's old DLL instead sends the header and the unpadded SCPI payload as separate bulk writes, followed by its own read request. Implementing that on Windows returned the identity, the current function and a DCV reading. The same transfer boundaries have since been verified on Linux (libusb 1.0.30) with the real instrument.

Do not swap this implementation for standard USBTMC and assume it is equivalent. The Linux `/dev/usbtmcN` kernel wrapper changes how transfers are organised, so this project talks to the endpoints directly through libusb.

## Discovery and connection

The real hardware enumerates as VID=0400 PID=09c4; the panel once showed 1ab1:09c4, so both are discovery candidates. With exactly one match, the active configuration is read and exactly one bulk OUT/IN pair is taken from the interface with class FE / subclass 03 / alternate 0. On the test instrument the endpoints are OUT=01 and IN=82, max packet size 64, interface protocol 01, and there is no USB serial number.

`set_configuration` is only attempted when there is no active configuration; a normal connection never re-sets it. Several candidates means the connection is refused until the user picks a bus/address. The kernel driver is detached from the target interface only. If the claim fails or the identity does not match, resources are released and reattach is attempted. Identity is checked with `*IDN?`, and the model must be DM3051.

## The three writes of one query

For `*IDN?` (terminated by LF, length 6):

```text
bulk OUT #1 (12 bytes): 01 01 FE 00 06 00 00 00 01 00 00 00
bulk OUT #2 (6 bytes) : 2A 49 44 4E 3F 0A
bulk OUT #3 (12 bytes): 02 02 FD 00 40 00 00 00 01 0A 00 00
bulk IN #1            : read up to 64 bytes
```

Header layout, little endian, 12 bytes:

| Offset | Size | Meaning |
|---|---|---|
| 0 | 1 | Message ID: 1 command, 2 read request / reply |
| 1 | 1 | Tag: always 1 for commands, 2 for reads, as in the verified implementation |
| 2 | 1 | Bitwise inverse of the tag |
| 3 | 1 | Reserved, 0 |
| 4 | 4 | Command byte count; 64 for a read request; the payload length in a reply |
| 8 | 1 | 1, as used by the vendor DLL; not the term-char bit value of standard USBTMC |
| 9 | 1 | 0 in a command header, LF=10 in a read request |
| 10 | 2 | Reserved, 0 |

The command payload is **not padded to four bytes** and must be written on its own. The first 12 bytes of a reply are a header, followed by the payload. If the header announces more payload than the first packet carried, the rest is read as raw payload; later segments have no header of their own. Trailing alignment bytes are ignored, and the text is decoded as ASCII and stripped.

Limits: reply length 0..4096, and the header must be message 2, tag 2, inverse FD. Short headers, bad lengths, empty segments and short writes raise exceptions. A zero-length reply and the string `E` are both treated as "rejected/busy" (`RejectedError`) and never as a reading. All USB operations of one transaction share a single deadline, so segmented reads cannot wait forever.

## Settings and status

`send_command()` sends only the header and an LF-terminated payload, with no read request. Whether a setting took effect is checked afterwards with a related query; never call query for a command that has no reply. Compound commands, newlines and NUL are rejected so the query/reply pairing cannot break.

On Windows the transport of `IDNSET RIGOL` and `CMDSET RIGOL` was verified, but the Linux software never changes the instrument's command set on its own. In the default RIGOL command set, `READ?`, `*OPT?`, `:SYSTem:ERRor?` and `:SYSTem:VERSion?` answer `E`. A rejected setter produces no reply at all and leaves nothing to read; since there is no usable error queue, readback is the only way to tell whether a setting took effect.

On close, a best-effort class/interface IN control request A1/A1 is sent (bmRequestType=0xA1, bRequest=161, value=0, index=interface number, length=1), i.e. GO_TO_LOCAL. Success does not guarantee that the panel leaves remote mode; press Single if needed. Then the interface is released, the kernel driver reattached, and resources disposed. Kernel permission problems cannot be fixed by sending a calibration password.

## Logging and timing

`--trace` logs every OUT/IN packet in hex. Never access the same device from two threads or processes at once; a second process that tries to claim the interface gets `Resource busy` and does not disturb the first. On a USB timeout, settings are not retried automatically; sampling stops and the connection is closed.

## Firmware behaviour observed on Linux (03.03.00.03.02.00)

All of these were reproduced on the real instrument under Arch Linux. Each has handling in the code and is covered by unit tests; the SCPI-level ones are also reproduced by the simulator (`--demo`):

| Behaviour | Handling |
|---|---|
| After the meter is power-cycled or re-plugged, control transfers work but **every bulk transfer fails with EIO**. Once a port reset fixed it (settings unchanged); another time neither a reset nor INITIATE_CLEAR helped and only power-cycling the meter did | On EIO, `open()` releases the interface, resets the port, waits 2 s and reopens once; if it still fails, it asks the user to power-cycle the meter |
| Reading queries only answer once the measurement completes: about 3.1 s per reading for shorted capacitance | Capacitance readings use their own 10 s deadline |
| Right after switching to RATIO, a reading query answers `R` (not ready) and nothing else follows | After an `R` to a non-range query, wait up to 1 s more: a reply means `R` was a late stale reply and this is our answer; otherwise treat it as busy. Two `R`s in a row are also busy |
| A late reply from the previous process (such as `R`) can still arrive after the drain, and the new connection's `*IDN?` receives it | The identity query is tried up to 3 times, draining between attempts |
| The continuity function reports an auto-trigger interval of `30ms`; other functions report `400ms` | Display only |
| After a client is interrupted, an unread reply stays in the IN endpoint, and the first query of the next session reads it | `open()` first drains the IN endpoint with a 100 ms timeout and logs the discarded bytes |
| Occasionally a reply is duplicated or arrives one query late (a few times in hundreds of function switches), so answers no longer match questions | Before every query the IN endpoint is drained with a 1 ms timeout (about 1 ms cost); a lone `R` to a non-`RANGe?` query is skipped and the next frame read |
| For about 0.1–2 s after a function or range change, queries answer `E` | Setters poll the readback; while sampling, `E` counts as busy and is tolerated for up to 10 s before an error |
| `RANGe?` answers `R` (busy) about one time in three, even in steady state | Range reads retry briefly; `R` never causes a setter to be resent |
| Setters are occasionally dropped silently (the readback keeps the old value) | When the readback shows a **definite different value**, the setter is resent every 1 s (these setters send absolute values and are idempotent) |
| Any `:MEASure:<x>?` returns the currently displayed reading, whether or not `<x>` is the current function, and **does not switch function** | The unit comes only from `:FUNCtion?`; the function is read before and after each reading and the point is discarded if it changed |
| `:FUNCtion?` returns `FREQUENCY`/`PERIOD` for frequency/period; the manual says `FREQ`/`PERI` | Both spellings are accepted |
| `:FUNCtion?` returns the long names `RESISTANCE`/`FRESISTANCE`/`CONTINUITY`/`CAPACITANCE` | Mapped to the short tokens 2WR/4WR/CONT/CAP (the short tokens are used in CSV) |
| Open resistance, continuity and diode read `-8.888000e+03` (the panel shows -8.888; the manual's diode example uses this value), not `9.9e37` | Treated like `9.9e37` as overload/open; the value is empty |
| The manual lists resistance range 6 (100 MΩ), but the firmware clamps 6 to 5 (10 MΩ); auto range stops at 5 when open | Only ranges 0–5 are offered |
| `:FUNCtion?` or `:MEASure:*?` occasionally receives the late reply of another query (such as `1` or `DCV`) | A reading must be in scientific notation (`d.dddddde±dd`, the format of every real reading) and a function must be a known name; otherwise it is treated like `E` (busy) and retried, without resending setters or ending the session. The format check cannot reject a misplaced reading that is itself in scientific notation, but range, resolution and function replies never pass it. A trigger-source reply must be a known source name (`auto`, `single`, …), so a late `1` cannot pause sampling. Because range and resolution readbacks are bare integers, a late one could equal the target by chance, so these settings need two matching readbacks in a row |
| `RANGe?` for frequency/period does not return a range index; `DIGIt?` does not match the manual | No range control for these functions; `DIGIt` is not used |
| After `:MEASure AUTO`, auto/manual cannot be read back, only the range auto-ranging picked | GUI and CLI show the current range as is and never claim "auto confirmed" |
| With single trigger (`:TRIGger:SOURce SINGle`), `:MEASure?` keeps returning the held value; after a remote `:TRIGger:SINGle:TRIGger`, `:MEASure?` returns zero-length replies until the trigger is set back to auto | The trigger source is checked while sampling and sampling is refused unless it is AUTO; only switching back to auto trigger is offered |
| `:DATAlog:CONFigure:*?` answers, but `:DATAlog:RUN?` and `:DATAlog:DATA?` are rejected (the manual restricts DATAlog to DM3054/DM3064) | On-instrument logging is not implemented |
| `:CALCulate:FUNCtion?` works; every `:CALCulate:*:STATe?` is rejected | The math function is shown in the status; if it is not `none`, the GUI warns that readings may be processed |
| The vendor name in the identity string is spelled `RIGOL Technoligies` | Only the second field (model) is checked |

Each auto-trigger reading takes about 400 ms (`:TRIGger:AUTO:INTErval?`; 200 ms at 5½ digits), and `:MEASure:<x>?` waits for the next reading, so a single query takes about 0.4 s by itself.
