# 故障排查

## --list 找不到设备

```sh
lsusb
lsusb -d 0400:09c4
lsusb -d 1ab1:09c4
```

若系统也看不到，检查电源、线缆、接口并重新插拔。若看到不同 VID/PID，记录描述符，不要为了“能连接”放开所有 USB 型号筛选。`lsusb` 来自可选 `usbutils` 包。

## No backend available / No module named usb

安装 python-pyusb 和 libusb。确认运行的是系统 `python3` 而不是其他虚拟环境。CLI 不导入 Tk，缺 Tk 时仍可测试 USB。

## Access denied / Permission denied

检查规则路径、重新插拔、本地登录用户和 USB 设备节点权限。uaccess 只授权有效本地会话；SSH 请用 INSTALL 的专用组方案。不要把整个 GUI 以 root 运行来掩盖权限配置问题。

## Resource busy / detach 失败

关闭其他 VISA、usbtmc、Python 进程。本程序通常可临时分离内核驱动，失败时记录具体错误。不要先卸载全系统 libusb/usbtmc；一个进程占用的问题应在进程/接口层解决。

## Input/Output Error（EIO）

现象：`--list` 能看到设备、权限正常，但第一条查询就报 `[Errno 5] Input/Output Error`。真机上观察到两次，都在仪器重新上电/插拔之后：所有 bulk 传输失败、控制传输正常。第一次，从未占用的句柄做 USB 端口复位、等 2 秒后重新打开即恢复（设置不变）；第二次复位、USBTMC INITIATE_CLEAR（仪器回 `0x80` 失败）都无效，**只有重启仪器**才恢复。程序会自动做一次“释放 → 复位 → 等 2 秒 → 重新打开”（日志 `resetting the USB port and reopening once`）；仍失败时报 `Power-cycle the instrument`，请关机 5 秒再开，等面板回到测量画面后重连。

## Unexpected instrument / 答非所问

上一个程序中断时可能在仪器里留下未读回复。程序连接时会先读空（日志 `Discarded N stale bytes`），每条查询前也会快速检查一次。若仍出现，关闭所有访问仪器的程序后重新连接。

## USB 超时、错误头、回复 E

- 电容读数要等一次测量完成才回复：表笔短接时每个读数约 3.1 秒，大电容更久。程序对电容读数单独使用 10 秒（或更长的 `--timeout`）截止时间；其他查询超时才说明通信有问题。

- 用 `--query '*IDN?' --trace 2>trace.log` 先测身份，不先运行数据记录。
- `--timeout 5000` 仅延长等待，不能修复错误协议。
- `E` 是设备拒绝命令或正忙。切换功能/量程后的 0.1–2 秒内出现属正常，程序会等待；持续出现说明该命令在当前固件/模式下不可用（如 `READ?`、`:SYSTem:ERRor?`）。
- 确保头与 payload 是分开的 bulk 写入，payload 不补齐；不要使用标准 usbtmc 包装替代。
- 超时后先停止程序、重新插拔或重启设备，再查询身份，避免旧事务残留。
- 记录固件、VID/PID、bus/address、端点、报文。Linux libusb 实机若行为不同，需据报文调整，而不是返回模拟读数。

## Current mode is ... / Trigger source is ... / 过量程

- `Current mode is X, expected Y`：用 `--function` 指定了功能，但采样期间面板被切换。不指定 `--function` 时程序跟随面板。
- `Trigger source is SINGLE/EXT`：单次或外部触发下读数是保持值，程序拒绝采样。运行 `sh run.sh --trigger AUTO --status` 或在 GUI 点“切回自动触发”。
- `not confirmed by readback`：设置在 5 秒内没有被仪器读回确认（例如该功能没有此量程、仪器处于菜单中）。连接仍可用，可重试。
- 过量程显示 OVLD，CSV 中 value 为空、raw 保留原文；无法解析的回复会停止采集并保留已有数据。导线开路时的小波动不是程序错误。

## 远程模式、停止等待

停止需等待当前 USB 操作完成，一组采样（三条查询）最坏需要约三倍事务超时，再加清理。关闭窗口采用同样等待机制。关闭时尝试 GO_TO_LOCAL，若面板仍远程，按 Single。本程序不会复位或改变校准。

## GUI 无法打开

安装 tk，并从有效图形桌面启动。`no display name` 表示无可用显示服务。先用 CLI 读数；只安装 Arch 最小系统时不必为 USB 测试安装整个桌面。

## 回传诊断

请提供 `--list` 输出、固件版本和 identity.log / usb-trace.log 的错误附近报文，以及 CLI 与面板结果。日志会含仪器序列号，没有联网上传功能；分享前可自行隐藏序列号。模拟模式只能验证 UI/导出，不能证明硬件连接。
