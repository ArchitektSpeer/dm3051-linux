# Installation and usage

## Requirements

Arch: `sudo pacman -Syu python python-pyusb libusb tk`. This upgrades the system and installs the dependencies; run it as part of a normal Arch upgrade.
The CLI needs neither Tk nor a display server, only Python, PyUSB and libusb.
The GUI needs a running graphical session; on Wayland, Tk usually shows up through XWayland. Without a display, use `--cli`.

Package sources are listed in SOURCES. On other distributions, run `python3 -m pip install -r requirements.txt` inside a virtual environment and install libusb and Tk from the distribution. Do not run a global pip install into the Arch system environment.

## USB permissions

The shipped rule uses `TAG+="uaccess"` and matches only the two vendor IDs with product ID 09c4; it does not grant access to any other USB device.

```sh
sudo install -m 644 udev/70-dm3051.rules /etc/udev/rules.d/70-dm3051.rules
sudo udevadm control --reload-rules
```

Re-plug the meter for the rule to apply. The user logged in to the desktop session can then run the application without sudo. On connect, the program temporarily detaches the kernel `usbtmc` driver from this one USB interface and tries to reattach it on exit. There is no need to blacklist the kernel driver.

Over SSH, or without a logind desktop session, uaccess may not grant access to the remote user. In that case create a dedicated `dm3051` group, add yourself to it, and replace the rule with the two lines below (replace it; do not add a rule for every device):

```sh
sudo groupadd -f dm3051
sudo usermod -aG dm3051 "$USER"
```

```udev
SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="0400", ATTR{idProduct}=="09c4", GROUP="dm3051", MODE="0660"
SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="1ab1", ATTR{idProduct}=="09c4", GROUP="dm3051", MODE="0660"
```

Log in again and re-plug the meter. To undo it, restore the original rule or delete the dedicated one. Do not change it to MODE=0666.

## Options

`python3 -m dm3051 --help` prints the current help.

| Option | Meaning |
|---|---|
| no options | GUI |
| `--cli` | Take a fixed number of readings (default 10), following the function selected on the panel |
| `--list` | List candidate devices with bus/address/vid/pid |
| `--bus` / `--address` | Select an exact USB bus and address; the address can change after re-plugging |
| `--count 10` | Number of CLI readings; must be positive |
| `--interval 1` | Host sampling interval, at least 0.2 s; the real rate is limited by the query time (about 0.4 s) |
| `--timeout 3000` | Deadline for one whole transaction, in ms, 100..30000 |
| `--csv file.csv` | Write CLI readings to a file as they arrive; existing files are never overwritten |
| `--function ACV` | Switch function first (DCV ACV DCI ACI 2WR 4WR FREQ PERI CONT DIODE CAP RATIO) and require it while sampling |
| `--range 2` / `--range AUTO` | Range index for the current function (see the function table in DEVELOPMENT), or auto |
| `--resolution 2` | 0=4½, 1=5½, 2=6½ |
| `--trigger AUTO` | Switch the trigger source back to auto (single/external trigger cannot be polled) |
| `--status` | Print the status as JSON (can be combined with the configuration options above) |
| `--configure-only` | Apply the configuration, do not sample |
| `--query '*IDN?'` | One query; implies CLI mode |
| `--command 'CMDSET RIGOL'` | Send one setter explicitly; implies CLI mode. Check acceptance with a separate query |
| `--trace` | Log packets to stderr; save with `2>trace.log` |
| `--demo` | Use the simulated instrument; no USB access at all |

Configuration options must be combined with `--cli`, `--status` or `--configure-only`. Range and resolution are first checked against the target function (if invalid, nothing is sent), then applied in the order trigger → function → range → resolution. Every step except auto range is confirmed by readback; a failure exits with code 1. `--demo` cannot be combined with `--list`.

CLI exit codes: 0 on success, 1 on error, 130 on Ctrl+C. In the GUI, a communication error is shown in the status line and stops the worker thread; the window stays open so the collected data can still be saved. A setting that does not take effect only shows a notice and keeps the connection.
