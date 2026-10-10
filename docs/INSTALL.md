# 安装与运行

## 环境

Arch：`sudo pacman -Syu python python-pyusb libusb tk`。这会升级系统并安装依赖，应在正常的 Arch 升级流程中执行。
CLI 不需要 Tk 或显示服务器，只需 Python、PyUSB、libusb。
GUI 需要现成的图形会话，Tk 在 Wayland 会话中通常通过 XWayland 显示；没有显示服务请使用 `--cli`。

包来源见 SOURCES。非 Arch 可在虚拟环境运行 `python3 -m pip install -r requirements.txt`，并由发行版安装 libusb 和 Tk；不要在 Arch 系统环境执行全局 pip 安装。

## USB 权限

默认规则是 `TAG+="uaccess"`，仅匹配两种 VID 和 09c4 PID，不给所有 USB 设备授权。

```sh
sudo install -m 644 udev/70-dm3051.rules /etc/udev/rules.d/70-dm3051.rules
sudo udevadm control --reload-rules
```

插拔后生效。登录桌面会话的用户运行应用，无需 sudo。程序在连接时临时分离这一 USB 接口的内核 `usbtmc` 驱动，退出时尝试恢复。不需要黑名单内核驱动。

SSH 或无 logind 桌面会话时，uaccess 可能不会给远程用户授权。可以创建专用 `dm3051` 组并将自己加入，用下面两行替换规则（不是再添加全设备规则）：

```sh
sudo groupadd -f dm3051
sudo usermod -aG dm3051 "$USER"
```

```udev
SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="0400", ATTR{idProduct}=="09c4", GROUP="dm3051", MODE="0660"
SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="1ab1", ATTR{idProduct}=="09c4", GROUP="dm3051", MODE="0660"
```

重新登录并重新插拔。关闭应用后恢复原规则或删除专用规则即可撤销；不要改成 MODE=0666。

## 参数

`python3 -m dm3051 --help` 查看实时帮助。

| 参数 | 含义 |
|---|---|
| 无参数 | GUI |
| `--cli` | 定量采集，默认 10 条；跟随面板当前功能 |
| `--list` | 列出候选设备 bus/address/vid/pid |
| `--bus` / `--address` | 精确选择 USB 总线与地址，插拔后地址可能改变 |
| `--count 10` | CLI 采样条数，必须为正 |
| `--interval 1` | 主机采样间隔，最小 0.2 秒，实际速度受查询时间（约 0.4 s）限制 |
| `--timeout 3000` | 单条命令的整个传输截止时间，毫秒，允许 100..30000 |
| `--csv file.csv` | CLI 实时写文件，不能覆盖已有文件 |
| `--function ACV` | 先切换功能（DCV ACV DCI ACI 2WR 4WR FREQ PERI CONT DIODE CAP RATIO），采样期间要求保持该功能 |
| `--range 2` / `--range AUTO` | 当前功能的量程索引（见 DEVELOPMENT 功能表）或自动 |
| `--resolution 2` | 0=4½，1=5½，2=6½ |
| `--trigger AUTO` | 把触发源切回自动（单次/外部触发下无法轮询） |
| `--status` | 输出状态 JSON（可与上面的配置参数组合） |
| `--configure-only` | 只执行配置，不采样 |
| `--query '*IDN?'` | 单次查询，自动走 CLI |
| `--command 'CMDSET RIGOL'` | 明确发送设置命令，自动走 CLI；是否生效需另行查询 |
| `--trace` | 报文日志输出到 stderr；可用 `2>trace.log` 保存 |
| `--demo` | 使用模拟仪器，完全不访问 USB |

配置参数需要与 `--cli`、`--status` 或 `--configure-only` 一起使用，执行顺序为触发 → 功能 → 量程 → 分辨率，每项读回确认，失败时退出码 1。

CLI 正常退出码 0，运行错误 1，Ctrl+C 中断 130。GUI 通信错误在状态栏显示并停止线程，应用仍保持打开供保存历史数据；设置未生效只显示提示，不断开连接。
