# Troubleshooting

## --list finds no device

```sh
lsusb
lsusb -d 0400:09c4
lsusb -d 1ab1:09c4
```

If the system cannot see it either, check power, cable and port, and re-plug. If a different VID/PID shows up, record the descriptors; do not drop the USB model filter just to get a connection. `lsusb` comes from the optional `usbutils` package.

## No backend available / No module named usb

Install python-pyusb and libusb. Make sure you are running the system `python3` and not some other virtual environment. The CLI does not import Tk, so USB can be tested without Tk.

## Access denied / Permission denied

Check the rule path, re-plug, check that you are the local logged-in user, and check the permissions of the USB device node. uaccess only grants access to an active local session; over SSH, use the dedicated group described in INSTALL. Do not run the GUI as root to hide a permission problem.

## Resource busy / detach failed

Close other VISA, usbtmc or Python processes. The program can usually detach the kernel driver temporarily; if that fails, note the exact error. Do not uninstall libusb/usbtmc system-wide first; a process holding the device is solved at the process/interface level.

## Input/Output Error (EIO)

Symptom: `--list` sees the device and permissions are fine, but the first query fails with `[Errno 5] Input/Output Error`. This was seen twice on the real meter, both times after it was power-cycled or re-plugged: every bulk transfer fails while control transfers work. The first time, a USB port reset from an unclaimed handle, a 2 s wait and reopening fixed it (settings unchanged). The second time, neither a reset nor USBTMC INITIATE_CLEAR (the meter answered `0x80`, failure) helped, and **only power-cycling the meter** did. The program automatically does one "release → reset → wait 2 s → reopen" (log: `resetting the USB port and reopening once`); if that still fails it reports `Power-cycle the instrument`. Switch the meter off for 5 s, switch it on, wait for the measurement screen, then reconnect.

## Unexpected instrument / mismatched answers

A program that was interrupted can leave an unread reply in the instrument. On connect the program drains it first (log: `Discarded N stale bytes`), and it checks quickly again before every query. If it still happens, close every program that accesses the meter and reconnect.

## USB timeout, bad header, `E` replies

- Capacitance readings only answer once a measurement completes: about 3.1 s per reading with shorted leads, longer for large capacitors. Capacitance readings use their own 10 s deadline (or the `--timeout` value if larger); only timeouts on other queries indicate a communication problem.
- Test the identity first with `--query '*IDN?' --trace 2>trace.log`, before running a data log.
- `--timeout 5000` only waits longer; it cannot fix a wrong protocol.
- `E` means the instrument rejected the command or is busy. It is normal for 0.1–2 s after changing function or range, and the program waits it out. If it persists, the command is not available in this firmware or mode (for example `READ?`, `:SYSTem:ERRor?`).
- The header and payload must be separate bulk writes, and the payload must not be padded; do not substitute the standard usbtmc wrapper.
- After a timeout, stop the program, re-plug or restart the instrument, and query the identity before anything else, so no old transaction is left over.
- Record the firmware, VID/PID, bus/address, endpoints and packets. If libusb on Linux behaves differently on real hardware, adjust based on the packets instead of returning simulated readings.

## Current mode is ... / Trigger source is ... / overload

- `Current mode is X, expected Y`: a function was given with `--function`, but the panel was switched during sampling. Without `--function`, the program follows the panel.
- `Trigger source is SINGLE/EXT`: with single or external trigger the readings are held values, so the program refuses to sample. Run `sh run.sh --trigger AUTO --status`, or press "Auto trigger" in the GUI.
- `not confirmed by readback`: the instrument did not confirm the setting within 5 s (for example the function has no such range, or the meter is in a menu). The connection remains usable; try again.
- Overload shows OVLD; in the CSV the value is empty and raw keeps the original text. An unparseable or misplaced reply is treated as busy: the CLI retries for up to 10 s and then exits with an error (the rows written so far stay in the CSV); the GUI keeps retrying and shows "Instrument busy or reply out of step" in the status line. Small fluctuations with open leads are not a software error.

## Remote mode, waiting on stop

Stopping waits for the current USB operation to finish; one sample (three queries) can take up to about three transaction timeouts, plus cleanup. Closing the window waits the same way. On close the program tries GO_TO_LOCAL; if the panel stays in remote mode, press Single. This program never resets the instrument or changes its calibration.

## The GUI does not open

Install tk and start the program from a working graphical desktop. `no display name` means there is no display available. Use the CLI for readings in the meantime; on a minimal Arch install you do not need a whole desktop just to test USB.

## Reporting a problem

Please include the `--list` output, the firmware version, the packets around the error from your identity/trace log, and what the CLI and the front panel showed. Logs contain the instrument's serial number; the program never uploads anything, and you can hide the serial number before sharing. The simulator can only validate the UI and export; it proves nothing about the hardware connection.
