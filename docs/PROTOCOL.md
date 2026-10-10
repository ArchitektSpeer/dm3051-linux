# DM3051 旧 USB 协议

## 为什么不直接使用标准 VISA/usbtmc

本次实机具有 USBTMC 接口描述符，但标准组合报文写入后读取超时。原厂旧 DLL 的行为是把头和未填充的 SCPI 数据作为独立 bulk 写入，之后发送特有读取请求。Windows 上照此实现后成功取得身份、当前功能和 DCV 数值。Linux（libusb 1.0.30）下同样的传输边界已经真机验证可用。

不要把这里的实现改成标准 USBTMC 然后假定等效。Linux `/dev/usbtmcN` 的内核包装会改变传输组织，因此本项目使用 libusb 直接访问端点。

## 发现和连接

实际硬件 VID=0400 PID=09c4；面板曾显示 1ab1:09c4，所以两者作为发现候选。唯一匹配后读取当前 configuration，从 class FE/subclass 03/alternate 0 的接口发现恰好一对 bulk OUT/IN。实机端点 OUT=01、IN=82，最大包长 64，接口 protocol=01，没有 USB 序列号。

只在没有活动 configuration 时尝试 set_configuration，不在正常连接中重新设置配置。发现多个候选就拒绝连接，需用户指定 bus/address。临时 detach 仅用于目标接口，claim 失败或身份不符时释放资源并尝试 reattach；USB 身份用 `*IDN?` 校验，型号必须 DM3051。

## 一次查询的三个写入

以 `*IDN?` 为例，SCPI 结尾为 LF，长度 6：

```text
bulk OUT #1（12 字节）: 01 01 FE 00 06 00 00 00 01 00 00 00
bulk OUT #2（6 字节） : 2A 49 44 4E 3F 0A
bulk OUT #3（12 字节）: 02 02 FD 00 40 00 00 00 01 0A 00 00
bulk IN #1           : 最多读取 64 字节
```

头结构，小端序，12 字节：

| 偏移 | 长度 | 含义与实现 |
|---|---|---|
| 0 | 1 | 消息号：1 命令，2 读取请求/回复 |
| 1 | 1 | tag：发送命令固定 1，读取固定 2；沿用已验证实现 |
| 2 | 1 | tag 按位取反 |
| 3 | 1 | 保留，0 |
| 4 | 4 | 命令字节数；读取请求为 64；回复为实际 payload 长度 |
| 8 | 1 | 实机旧 DLL 使用 1；不同于通常标准 USBTMC 的 term-char 位值 |
| 9 | 1 | 命令头为 0，读取请求为 LF=10 |
| 10 | 2 | 保留，0 |

命令 payload **不补齐至四字节**，必须单独写。回复最初 12 字节是头，后面是 payload。如果头给出的长度超过第一包带来的 payload，继续读取原始 payload；后续分段没有新的头。最终多余对齐字节忽略，正文按 ASCII 解码并去首尾空白。

限制：回复长度 0..4096，必须消息 2、tag 2、反码 FD；短头、错误长度、空分段、短写入均抛异常。长度 0 与字符串 `E` 都按“拒绝/忙”（`RejectedError`）处理，不能当读数。每条事务的所有 USB 操作共用一个截止时间，防止分段读取无限等待。

## 设置与状态

`send_command()` 只发送头 + LF 结尾 payload，不发读取请求。后续用相关 query 验证设置实际生效，别对没有回复的命令调用 query。复合命令、换行、NUL 被拒绝，以免破坏查询/回复对应关系。

本次 Windows 实机验证过 `IDNSET RIGOL`、`CMDSET RIGOL` 的传输，但 Linux GUI 不自动改变仪器命令集。默认 RIGOL 命令模式下 `READ?`、`*OPT?`、`:SYSTem:ERRor?`、`:SYSTem:VERSion?` 回复 `E`。被拒绝的 setter 没有任何回复，也不会留下待读数据；因为没有可用的错误队列，设置是否生效只能靠读回判断。

关闭时尽力发送 class/interface IN 控制请求 A1/A1（bmRequestType=0xA1、bRequest=161）、value=0、index=接口号、长度=1，即 GO_TO_LOCAL。其成功响应不保证面板解除远程；必要时按 Single。随后 release、reattach、dispose。内核权限问题不能靠发送校准口令解决。

## 日志和时序

`--trace` 逐条记录 OUT/IN 十六进制。不要同时从两个线程或进程访问同一设备；第二个进程 claim 接口会得到 `Resource busy`，不会干扰第一个。遇到 USB 超时不自动重试设置，停止采集并关闭连接。

## Linux 真机观察到的固件行为（03.03.00.03.02.00）

以下均在 Arch Linux 真机上复现过，代码中有对应处理，单元测试和模拟器覆盖：

| 现象 | 处理 |
|---|---|
| 设备处于异常状态时，控制传输正常但**所有 bulk 传输 EIO**；USB 端口复位（`libusb_reset_device`）后恢复，且仪器设置不变 | `open()` 在身份查询遇到 `USBError` 时复位端口一次再重试 |
| 上一个客户端中断后，IN 端点里留有未读回复；新会话的第一条查询会读到它 | `open()` 先以 100 ms 超时读空 IN 端点并记录丢弃的字节 |
| 偶尔一条回复会重复/迟到一条查询（数百次功能切换中出现数次），导致答非所问 | 每条查询前以 1 ms 超时读空 IN 端点（约 1 ms 代价）；非 `RANGe?` 查询收到孤立 `R` 时跳过并读下一帧 |
| 功能/量程切换后约 0.1–2 s 内查询回复 `E` | 设置后轮询读回；采样时 `E` 视为忙，最多容忍 10 s 后报错 |
| `RANGe?` 即使在稳态也约每三次回复一次 `R`（忙） | 读量程时短暂重试；`R` 不触发重发 setter |
| setter 偶尔被静默丢弃（读回仍为旧值） | 读回出现**确定的其他值**时每 1 s 重发（这些 setter 是幂等的绝对值） |
| 任何 `:MEASure:<x>?` 都返回当前显示的读数，与 `<x>` 是否为当前功能无关，**也不会切换功能** | 单位只依据 `:FUNCtion?`；读数前后各读一次功能，变化则丢弃该点 |
| `:FUNCtion?` 对频率/周期返回 `FREQUENCY`/`PERIOD`，手册写的是 `FREQ`/`PERI` | 两种写法都接受 |
| 频率/周期的 `RANGe?` 回复的不是量程索引；`DIGIt?` 返回值与手册不符 | 这些功能不提供量程控制；不使用 `DIGIt` |
| `:MEASure AUTO` 无法读回“自动/手动”状态，只能读到自动选出的量程 | GUI/CLI 如实显示当前量程，不声称“已确认自动” |
| 单次触发（`:TRIGger:SOURce SINGle`）下 `:MEASure?` 一直返回保持的旧值；远程 `:TRIGger:SINGle:TRIGger` 之后 `:MEASure?` 持续返回长度 0 的回复，直到切回自动 | 采样前检查触发源，非 AUTO 拒绝采样；只提供“切回自动触发” |
| `:DATAlog:CONFigure:*?` 有回复，但 `:DATAlog:RUN?`、`:DATAlog:DATA?` 被拒绝（手册注明 DATAlog 仅 DM3054/DM3064） | 不实现仪器端记录 |
| `:CALCulate:FUNCtion?` 可用，各 `:CALCulate:*:STATe?` 被拒绝 | 状态中显示运算功能，非 `none` 时 GUI 提示读数可能已被处理 |
| 身份字符串厂商名拼写为 `RIGOL Technoligies` | 只校验第二字段型号 |

每次自动触发读数约 400 ms（`:TRIGger:AUTO:INTErval?`，分辨率 5½ 时 200 ms），`:MEASure:<x>?` 会等待下一次读数，因此单次查询本身约需 0.4 s。
