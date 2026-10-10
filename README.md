# DM3051 Linux 上位机

RIGOL DM3051 台式万用表的 Linux 上位机。原厂已停止支持，旧驱动/Ultra Sigma 只能在 Windows 上运行；本项目用 Python 3.10+、PyUSB/libusb 直接实现仪器的旧版 USB 分包协议，无需 NI VISA、Wine 或 32 位运行库。提供 Tk 图形界面和无图形环境的 CLI。

**验证状态（0.2.0）：** 已在 Arch Linux（内核 7.x、libusb 1.0.30、PyUSB 1.3.1）+ DM3051 固件 03.03.00.03.02.00 真机上验证：身份、全部被动测量功能切换、量程/自动量程、分辨率、触发检查、CSV、GUI 全流程、并发占用、陈旧回复恢复。会输出测试电流的功能（电阻、通断、二极管、电容）命令来自原厂手册、在模拟器中测试，**尚未在真机上切换验证**。详见 [TESTING](docs/TESTING.md)。

## 功能

- 自动识别面板当前功能：DCV、ACV、DCI、ACI、2WR、4WR、频率、周期、通断、二极管、电容、电压比；读数带单位与 SI 前缀，过量程显示 OVLD。
- 从上位机切换功能、量程（含自动）、分辨率（4½/5½/6½），每项设置都经仪器**读回确认**；固件偶尔丢弃设置命令，会自动重发直到确认或超时。
- 实时曲线、最小/最大/平均/σ 统计、上下限超限提示、实时写盘记录（长时间采集不受内存缓存限制）、CSV 导出。
- 检测非自动触发（单次/外部触发下读数是保持值，不会当新数据记录），一键切回自动触发；显示仪器运算（CALCulate）状态。
- CLI：定量采集写 CSV、状态 JSON、配置、单条 SCPI 查询/命令、报文跟踪。
- 模拟器（`--demo`）复现真机的已知固件怪癖，GUI/CLI/单元测试都走同一套控制逻辑。

## 1. 安装（Arch）

```sh
sudo pacman -S --needed python python-pyusb libusb tk
git clone https://github.com/ArchitektSpeer/dm3051-linux.git
cd dm3051-linux
sudo install -m 644 udev/70-dm3051.rules /etc/udev/rules.d/70-dm3051.rules
sudo udevadm control --reload-rules
```

重新插拔 USB。`uaccess` 规则只给当前本地登录用户访问这台仪器；SSH/无桌面环境的权限方案见 [安装文档](docs/INSTALL.md)。不要用 pip 修改 Arch 的系统 Python（其他发行版可用 venv 安装 `requirements.txt`）。

## 2. 真机快速上手

```sh
sh run.sh --list                         # 找到设备
sh run.sh --status                       # 身份、功能、量程、分辨率、触发（JSON）
sh run.sh --cli --count 10 --interval 1 --csv first-run.csv
sh run.sh                                # GUI
```

GUI：开始 / 连接 → 在“功能/量程/分辨率”下拉框中切换（或直接按面板按键，程序会跟随）→ 需要长时间采集时点“实时记录…” → 停止 / 断开 → 保存 CSV。

CLI 配置示例（每项都读回确认，失败退出码为 1）：

```sh
sh run.sh --cli --function ACV --range AUTO --resolution 2 --count 20 --csv acv.csv
sh run.sh --configure-only --function DCV --range 2      # DCV 20 V 量程
sh run.sh --trigger AUTO --status                         # 从单次触发切回自动
sh run.sh --query ':MEASure:VOLTage:DC:IMPEdance?'
```

量程索引见 [DEVELOPMENT](docs/DEVELOPMENT.md#功能与量程表)。多个候选 USB 设备时必须指定 `--bus N --address N`。实际 VID/PID 是 **0400:09c4**，同时兼容 **1ab1:09c4**；最终以 `*IDN?` 返回的型号 DM3051 为准。

## 3. 不接仪器的演示

```sh
sh run.sh --demo
sh run.sh --demo --cli --function ACV --count 5 --interval 0.2 --csv demo.csv
python3 -m unittest discover -s tests -v
```

模拟模式在标题和身份信息里明确标识，数值不是硬件测量结果。

## 4. 边界与安全

- 采样是主机轮询：每点先读功能，再读数值，再确认功能未变；面板自动触发间隔（默认 400 ms）决定新读数的产生速度，主机 sleep 不是精确采样时钟。
- 仪器端 DATAlog 与扫描卡命令在本型号/固件上不可用（`DATAlog:RUN?`、`DATAlog:DATA?` 被拒绝；本机无扫描卡），因此未实现。
- 切换到电阻/通断/二极管/电容前，GUI 会提示该功能会从输入端输出测试电流。
- 不复位、不改校准、不刷固件、不改接口设置；`--command` 只发送用户明确给出的命令。
- CSV 列：`timestamp,function,value,unit,raw`（0.1 版是 `timestamp,volts,raw`）；过量程行 value 为空、raw 保留原文。CLI 拒绝覆盖已有文件。
- USB 超时/传输错误即停止并断开，不自动重放设置。退出时尝试返回本地模式并恢复内核 `usbtmc` 驱动；必要时按面板 **Single**。

## 文档目录

| 文档 | 用途 |
|---|---|
| [INSTALL](docs/INSTALL.md) | Arch 环境、图形/纯终端、权限、全部运行参数 |
| [PROTOCOL](docs/PROTOCOL.md) | 旧 USB 协议逐字段说明、Linux 下的固件怪癖与对策 |
| [DEVELOPMENT](docs/DEVELOPMENT.md) | 模块结构、API、线程模型、功能/量程表、扩展方法 |
| [CONTROL-ROADMAP](docs/CONTROL-ROADMAP.md) | 功能域完成状态与后续计划 |
| [TROUBLESHOOTING](docs/TROUBLESHOOTING.md) | USB 不识别、权限、占用、EIO、超时、错误回复 |
| [TESTING](docs/TESTING.md) | 单元测试、Linux 真机验收记录与模板 |
| [SOURCES](docs/SOURCES.md) | 原厂文档、依赖与参考过的开源上位机 |

项目源码使用 MIT 许可；第三方依赖和手册保留各自许可。
