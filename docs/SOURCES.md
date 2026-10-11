# Sources and references

The project's own documentation, code and tests are all in this repository. For full command parameters and model details, consult the vendor documents below; this project only links to them and does not reproduce the copyrighted manuals. Chapter and page pointers into the manual are in CONTROL-ROADMAP.

- RIGOL DM3000 Programming Guide (full SCPI, compatibility command sets): https://www.batronix.com/files/Rigol/Multimeter/DM3000/DM3000_Programming_Guide.pdf
- RIGOL DM3000 User Guide (front panel, measurements, trigger, accessories): https://www.batronix.com/files/Rigol/Multimeter/DM3000/DM3000_UserGuide_EN.pdf
- RIGOL DM3051 Service Guide (service reference): https://gotroot.ca/rigol/service%20manuals/123957-an-01-en-ServiceGuide_RIGOL_DM3051.pdf
- PyUSB project and tutorial: https://github.com/pyusb/pyusb/blob/master/docs/tutorial.rst
- PyUSB API implementation: https://github.com/pyusb/pyusb/blob/master/usb/core.py
- Arch python-pyusb: https://archlinux.org/packages/extra/any/python-pyusb/
- Arch tk: https://archlinux.org/packages/extra/x86_64/tk/
- libusb documentation: https://libusb.sourceforge.io/api-1.0/

## Open-source meter software consulted

These projects target general-purpose multimeters or instruments of the same family. This project borrowed feature ideas from them (no code was copied):

- sigrok / sigrok-meter / SmuView (GPL-3.0+): multi-vendor meter framework; the statistics (min/max/mean) and trend plot interaction. https://sigrok.org/wiki/SmuView
- QtDMM (GPL-3.0): Qt meter readout and logging; limit alarms and continuous logging to disk. https://github.com/tuxmaster/QtDMM
- dmm-logger (Rust): SCPI meter CSV logger; row-by-row writes and a CLI with configurable interval and count. https://github.com/mrothNET/dmm-logger
- psc-datalogger (Diamond Light Source): PyVISA GUI logging several meters. https://pypi.org/project/psc-datalogger
- Scripts for the related DM3058/DM3068 (their transport was not checked one by one; the DM3051 needs this project's legacy protocol): https://github.com/bveina/DM3058E-Calibration, https://github.com/ocecelja/logger_DM3058
- The Rigol-specific quirks in the Linux kernel usbtmc driver, which show that early Rigol USB implementations were generally not fully USBTMC compliant. https://github.com/torvalds/linux/blob/master/drivers/usb/class/usbtmc.c

The legacy USB framing used here comes from debugging the real instrument and analysing the behaviour of the vendor DLL; it is not a Linux protocol guaranteed by the vendor. On Windows, the real instrument first confirmed `*IDN?`, `:FUNCtion?` and `:MEASure:VOLTage:DC?`; what has been verified on Linux is listed in TESTING. No system drivers or vendor software are included in this project.
