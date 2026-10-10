# 来源与参考

项目自己的文档、代码与测试均包含在 ZIP 中。设备完整命令的参数表和型号细节请查以下原厂文件；这里只提供链接，不转写整本受版权保护的手册。手册章节与页面导航见 CONTROL-ROADMAP。

- RIGOL DM3000 Programming Guide（完整 SCPI、兼容命令集）：https://www.batronix.com/files/Rigol/Multimeter/DM3000/DM3000_Programming_Guide.pdf
- RIGOL DM3000 User Guide（面板、测量、触发、配件）：https://www.batronix.com/files/Rigol/Multimeter/DM3000/DM3000_UserGuide_EN.pdf
- RIGOL DM3051 Service Guide（维修参考）：https://gotroot.ca/rigol/service%20manuals/123957-an-01-en-ServiceGuide_RIGOL_DM3051.pdf
- PyUSB 官方项目与教程：https://github.com/pyusb/pyusb/blob/master/docs/tutorial.rst
- PyUSB API 实现：https://github.com/pyusb/pyusb/blob/master/usb/core.py
- Arch python-pyusb：https://archlinux.org/packages/extra/any/python-pyusb/
- Arch tk：https://archlinux.org/packages/extra/x86_64/tk/
- libusb 官方文档：https://libusb.sourceforge.io/api-1.0/

## 参考过的开源上位机

这些项目面向通用万用表或同系列仪器，本项目借鉴了其中的功能设计（未复制代码）：

- sigrok / sigrok-meter / SmuView（GPL-3.0+）：多厂商万用表框架；借鉴了统计（最小/最大/平均）与趋势图的交互。https://sigrok.org/wiki/SmuView
- QtDMM（GPL-3.0）：Qt 万用表读数与记录软件；借鉴了上下限阈值提示与持续写盘记录。https://github.com/tuxmaster/QtDMM
- dmm-logger（Rust）：SCPI 万用表 CSV 记录工具；借鉴了逐行写盘、可配置间隔/条数的 CLI 形态。https://github.com/mrothNET/dmm-logger
- psc-datalogger（Diamond Light Source）：PyVISA 多台万用表记录 GUI。https://pypi.org/project/psc-datalogger
- 同系列 DM3058/DM3068 的脚本（未逐一核实其传输方式；DM3051 需要本项目的旧协议）：https://github.com/bveina/DM3058E-Calibration 、https://github.com/ocecelja/logger_DM3058
- Linux 内核 usbtmc 驱动中针对 Rigol 的特殊处理，说明 Rigol 早期 USB 实现普遍不完全符合 USBTMC。https://github.com/torvalds/linux/blob/master/drivers/usb/class/usbtmc.c

此项目的旧 USB 分包规则来自本次实机调试和原厂 DLL 行为分析，不是原厂保证的 Linux 协议；Windows 真机最初验证了 *IDN?、:FUNCtion?、:MEASure:VOLTage:DC?；Linux 真机验证范围见 TESTING。系统驱动与厂商软件不包含在本项目中。
