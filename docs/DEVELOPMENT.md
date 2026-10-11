# 开发说明

## 文件结构

```text
dm3051/__main__.py       入口
dm3051/usb_device.py     旧协议传输（LegacyProtocol）、测量与设置（MeterControl）、USB 发现/释放（DM3051）、功能表
dm3051/simulator.py      SCPI 级模拟器 SimulatedMeter：复现已知固件怪癖，供 --demo 与测试
dm3051/app.py            参数、CLI、采样器 Sampler、统计 Stats、数值格式化、CSV 字段
dm3051/gui.py            Tk 界面与 USB 工作线程
tests/test_protocol.py   报文级协议回归（无硬件）
tests/test_control.py    协议怪癖、设置读回/重发、采样、统计、CLI CSV
udev/70-dm3051.rules     指定设备权限
docs/                    安装、协议、开发、路线、排错、验收、来源
```

分层：`LegacyProtocol` 只负责一条命令/查询的字节传输；`MeterControl` 只依赖 `query()`/`send_command()`，所以真机 `DM3051` 与 `SimulatedMeter` 共享全部测量和设置逻辑。

## 线程模型（GUI）

只有工作线程访问仪器。Tk 主线程通过 `requests` 队列提交设置（功能/量程/分辨率/触发），工作线程在两次采样之间串行执行，结果和事件（身份、状态、采样、提示、错误、结束）通过 `events` 队列回到主线程，每 100 ms 处理一次。停止时设置 Event 并往请求队列放入哨兵唤醒线程；窗口关闭同样等待线程释放 USB，不强杀 USB 操作。一组采样包含三条查询，停止等待最长约为三倍事务超时加清理时间。

面板上手动切换功能时，工作线程从采样结果中发现功能变化并刷新状态，下拉框随之同步。

## 公共 API

```python
from dm3051.usb_device import DM3051, list_devices

print(list_devices())
with DM3051.open(timeout_ms=3000) as meter:
    print(meter.identity)
    meter.set_function("ACV")          # 读回确认，失败抛 SettingError
    meter.set_range("AUTO")
    meter.set_resolution(2)            # 6½
    meter.check_live()                 # 非自动触发时抛 InstrumentError
    r = meter.measure()                # Reading(function, value, raw)
    print(r.function.token, r.value, r.function.unit, r.raw)
    print(meter.status())
```

| 方法 | 说明 |
|---|---|
| `DM3051.open(bus=None, address=None, timeout_ms=3000)` | 选择唯一候选、claim、读空陈旧回复、校验型号；EIO 时复位端口重试一次 |
| `identity` | 连接时 `*IDN?` 的结果，不是 USB descriptor |
| `query(cmd)` / `send_command(cmd)` | 单条 ASCII 查询/设置；`E` 或空回复抛 `RejectedError`；setter 无回复、不保证被接受 |
| `function()` | 当前功能（`Function` 元组） |
| `measure(expected=None)` | 读功能→读数→再读功能；过量程 `value=None`；功能中途变化抛 `RejectedError` |
| `measure_dcv()` | 0.1 版兼容接口：必须是 DCV 且不过量程，返回 `(float, raw)` |
| `set_function(name)` / `set_range(i or "AUTO")` / `set_resolution(i)` | 设置并读回确认（`AUTO` 只发送，无法读回）；读回为另一个确定值时重发，错位/忙回复不重发；失败抛 `SettingError`，参数越界抛 `ValueError` |
| `range_index()` / `resolution()` | 读回索引，忙时短暂重试，不可用为 `None` |
| `trigger_source()` / `set_trigger_auto()` / `check_live()` | 触发源检查；只提供切回 AUTO |
| `status()` | 身份、功能、单位、量程、分辨率、触发、间隔、运算、命令集 |
| `drain()` | 读空 IN 端点并返回丢弃的字节 |
| `close()` | 幂等；尽力返回本地、释放接口、恢复内核驱动 |

异常层次：`InstrumentError` ⊃ `RejectedError`（忙/拒绝/错位回复，通常暂时）、`SettingError`（设置未生效或不支持，连接仍可用）、`NotLiveError`（触发源非 AUTO）。其余 `InstrumentError`（短写、非法回复头、非 ASCII）表示事务边界可能已损坏，GUI 会结束会话。读数必须是固件的科学记数格式（`d.dddddde±dd`），裸整数等错位回复按 `RejectedError` 处理。USB 传输错误为 PyUSB 的 `USBError`/`TimeoutError`，应视为连接失效。

`app.Sampler(device, expected=None, grace=10)` 包装一次采样：每次读数后检查触发源（非 AUTO 抛 `NotLiveError`，本次读数丢弃）；暂时的 `RejectedError`（`E`、空回复、错位的回复）返回 `None`、原因存于 `last_rejection`，超出宽限期后才抛出，从不生成假数据。CLI 用 10 s 宽限；GUI 用无限宽限，持续显示“忙”而不断开。

## 功能与量程表

| 功能 | `:FUNCtion?` | 单位 | 量程索引 0,1,2… | 分辨率 | 输出测试电流 | Linux 真机 |
|---|---|---|---|---|---|---|
| 直流电压 | DCV | V | 200 mV, 2 V, 20 V, 200 V, 1000 V | 有 | 否 | 已验证（含量程/分辨率） |
| 交流电压 | ACV | V | 200 mV, 2 V, 20 V, 200 V, 750 V | 有 | 否 | 已验证 |
| 直流电流 | DCI | A | 2 mA, 20 mA, 200 mA, 1 A, 10 A | 有 | 否 | 已验证切换与读数 |
| 交流电流 | ACI | A | 20 mA, 200 mA, 2 A, 10 A | 有 | 否 | 已验证切换与读数 |
| 两线电阻 | 2WR（固件 `RESISTANCE`） | Ω | 200 Ω, 2 kΩ, 20 kΩ, 200 kΩ, 1 MΩ, 10 MΩ | 有 | 是 | 已验证切换、量程读回、开路 OVLD（未接已知电阻） |
| 四线电阻 | 4WR（固件 `FRESISTANCE`） | Ω | 同上 | 有 | 是 | 已验证切换、量程读回（未接已知电阻） |
| 频率 | FREQUENCY | Hz | 不提供 | 无 | 否 | 已验证切换与读数 |
| 周期 | PERIOD | s | 不提供 | 无 | 否 | 已验证切换与读数 |
| 通断 | CONT（固件 `CONTINUITY`） | Ω | 不提供 | 无 | 是 | 已验证切换、开路 OVLD |
| 二极管 | DIODE | V | 不提供 | 无 | 是 | 已验证切换、开路 OVLD |
| 电容 | CAP（固件 `CAPACITANCE`） | F | 2 nF … 200 µF（6 档） | 有 | 是 | 已验证切换、0–5 档读回、开路杂散电容读数 |
| 电压比 | RATIO | — | 不提供 | 有 | 否 | 已验证切换与读数（输入悬空） |

分辨率索引：0 = 4½，1 = 5½，2 = 6½。功能表是 `usb_device.FUNCTIONS`，新增或修正一项即可同时影响 CLI、GUI 和模拟器。把“未验证”改为“已验证”前，请按 TESTING 在真机上确认切换、读数、过量程和读回。

## 添加控制功能

按“设置 → 读回 → UI 同步”实现：在 `MeterControl` 中用 `_apply(setter, readback_query, accept, what)`，它负责轮询、忙状态和重发。只对**绝对值**设置使用 `_apply`（重发必须是幂等的），`INC`/`DEC` 之类的相对命令不能这样重发。GUI 中把新设置加入 `apply()` 的映射，通过 `request()` 提交，不要从 Tk 线程直接访问仪器。不要在每次轮询中重写量程或触发配置。

在模拟器中实现对应 SCPI 节点（包括忠实再现观察到的怪癖），再写单元测试；最后真机验证并更新 PROTOCOL 的怪癖表和上面的功能表。

## CSV 与数据管理

CSV 列 `timestamp,function,value,unit,raw`：时间为带本地时区偏移的 ISO8601（毫秒），value 为十进制/科学记数，过量程为空；raw 保留设备原文；UTF-8。GUI 内存保留最近 10000 行、曲线显示当前功能的最近 300 点；“实时记录”逐行写盘并 flush，适合长时间采集。CLI 同样逐行写盘，拒绝覆盖已有文件。

## 打包与发布

直接源码运行，无构建步骤。修改后运行 `python3 -m unittest discover -s tests -v`、CLI demo、GUI demo，再按 TESTING 做真机验收。发布时记录版本、固件、验证范围与日志；不要宣称全型号兼容。
