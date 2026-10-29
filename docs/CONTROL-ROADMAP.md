# Feature roadmap

Exact command spelling, parameter values and model differences follow the vendor Programming Guide (see SOURCES); page numbers are the manual's footer numbers. Where firmware 03.03 of the DM3051 disagrees with the manual, it is recorded in PROTOCOL.

| Area | Manual section | Status (0.3.0) |
|---|---|---|
| Identity and common commands | Common Commands 2-2 | `*IDN?` and `CMDSet?` used; `*RST` not offered (it restores factory settings); `*OPT?` rejected by the firmware |
| Function selection | :FUNCtion 2-4 | Done: all 12 functions, panel function detected automatically, setter + readback + resend |
| Measurement and range | :MEAsure 2-7 | Done: readings and units for every function, manual range (confirmed by readback), auto range (sent only; the firmware cannot read back auto mode), overload detection. `DIGIt` replies do not match the manual and are not used |
| Resolution | :RESOlution 2-21 | Done: 4½/5½/6½ set and read back |
| System information | :SYSTem 2-26 | Shown in the status; `EDITion?`/`TYPE?`/`SERIal?`/`SCANserial?` work, `ERRor?`/`VERSion?` are rejected |
| Utility and interface settings | :UTILity 2-31 | Not changed, to avoid cutting off the interface in use |
| Trigger | :TRIGger 2-35 | Trigger source checked; sampling refused unless AUTO (readings are held values otherwise); switching back to AUTO offered. Readings after a remote single trigger cannot be retrieved, so it is not offered |
| Math | :CALCulate 2-39 | `CALCulate:FUNCtion?` shown with a warning; statistics and limits are done on the host instead (the firmware rejects `*:STATe?`) |
| On-instrument logging | :DATAlog 2-45 | Not available on this model (the manual restricts it to DM3054/DM3064; `RUN?`/`DATA?` are rejected) |
| Scanning | :SCAN 2-50 | Needs a scanner card; this unit's `SCANserial?` returns NONE; not implemented |
| Agilent / Fluke compatibility sets | Chapter 3 | Stays in the default RIGOL mode; never switches command set automatically |

## Definition of done for each feature

1. Hardware and firmware support are detected correctly; when an accessory is missing or the firmware does not support something, the software says so.
2. Command parameters come from the manual for that mode; the current state is queried before anything is set.
3. Success is judged by instrument readback or observable behaviour, never by the number of bytes a bulk write accepted.
4. There is one USB owner; control and sampling are serialised so a late reply never leaks into the next query.
5. Panel and host agree, overload and invalid values are recognisable, and disconnects or timeouts never look like normal operation.
6. Logs trace commands, responses and times; unit tests cover parsing and hardware tests cover behaviour.

## Next

- Verify the values of 2WR, 4WR, CAP and DIODE with a known resistor (including a 4-wire connection), capacitor and diode; open and shorted behaviour is already verified.
- The secondary frequency display of AC voltage/current (`:MEASure:VOLTage:AC:FREQuency?`) as an extra column.
- Automatic reconnect after a disconnect (first confirm that a reconnect cannot receive replies from an old transaction).
- Packaging (AUR PKGBUILD). GUI settings (interval, limits, plot, window size) are already remembered since 0.3.0.

Calibration and firmware updates are outside the scope of this project. Never use a calibration password or a factory reset as a connection fix.

Known reference unit: DM3051 / serial DM3H1035xxxxx / firmware 03.03.00.03.02.00. The project only requires the model to be DM3051 and does not hard-code serial numbers; other firmware versions are not proven to behave identically.
