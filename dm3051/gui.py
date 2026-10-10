"""Tk front end. Only the worker thread touches the instrument; the Tk thread talks to it via queues."""
from collections import deque
import csv
import logging
import math
import queue
import threading
import time
from .app import FIELDS, Sampler, Stats, format_value, open_device
from .usb_device import FUNCTIONS, RESOLUTIONS, InstrumentError, RejectedError, SettingError, lookup_function

AUTO = "自动"


def gui(args):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    root = tk.Tk()
    root.title("DM3051 Linux — " + ("模拟仪器" if args.demo else "USB"))
    root.geometry("860x620")
    events = queue.Queue()
    requests = queue.Queue()
    stop = threading.Event()
    rows = deque(maxlen=10000)
    worker = None
    closing = False
    state = {"function": None, "pending": False, "stats": Stats(), "record": None, "recorded": 0}
    status = tk.StringVar(value="点击开始 / 连接；面板当前功能会被自动识别")
    reading = tk.StringVar(value="—")
    detail = tk.StringVar(value="")
    interval = tk.StringVar(value=str(args.interval))
    function_var, range_var, resolution_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
    stats_text, record_text = tk.StringVar(value="统计：—"), tk.StringVar(value="未记录")
    lower_var, upper_var = tk.StringVar(), tk.StringVar()
    function_names = [f"{f.token} {f.label}" for f in FUNCTIONS]

    top = ttk.Frame(root, padding=12)
    top.pack(fill="x")
    reading_label = ttk.Label(top, textvariable=reading, font=("sans", 30))
    reading_label.pack(anchor="w")
    ttk.Label(top, textvariable=detail).pack(anchor="w")
    ttk.Label(top, textvariable=status, wraplength=820).pack(anchor="w", pady=8)
    bar = ttk.Frame(top)
    bar.pack(fill="x")
    ttk.Label(bar, text="采样间隔（秒）").pack(side="left")
    entry = ttk.Entry(bar, textvariable=interval, width=8)
    entry.pack(side="left", padx=8)
    controls = ttk.Frame(top)
    controls.pack(fill="x", pady=(8, 0))
    ttk.Label(controls, text="功能").pack(side="left")
    function_box = ttk.Combobox(controls, textvariable=function_var, values=function_names, width=14, state="disabled")
    function_box.pack(side="left", padx=(4, 12))
    ttk.Label(controls, text="量程").pack(side="left")
    range_box = ttk.Combobox(controls, textvariable=range_var, width=10, state="disabled")
    range_box.pack(side="left", padx=(4, 12))
    ttk.Label(controls, text="分辨率").pack(side="left")
    resolution_box = ttk.Combobox(controls, textvariable=resolution_var, values=RESOLUTIONS, width=6, state="disabled")
    resolution_box.pack(side="left", padx=(4, 12))
    extra = ttk.Frame(top)
    extra.pack(fill="x", pady=(8, 0))
    ttk.Label(extra, text="下限").pack(side="left")
    ttk.Entry(extra, textvariable=lower_var, width=10).pack(side="left", padx=(4, 8))
    ttk.Label(extra, text="上限").pack(side="left")
    ttk.Entry(extra, textvariable=upper_var, width=10).pack(side="left", padx=(4, 4))
    ttk.Label(extra, text="（基本单位，如 0.005 或 5e-3；留空不检查）").pack(side="left", padx=(0, 12))
    ttk.Label(top, textvariable=stats_text).pack(anchor="w", pady=(8, 0))
    canvas = tk.Canvas(root, background="#101820", highlightthickness=0)
    canvas.pack(fill="both", expand=True, padx=12, pady=8)
    ttk.Label(root, text="曲线显示当前功能的最近 300 点；内存保留最近 10000 条，长时间采集请用“实时记录”。设置均经仪器读回确认。").pack(pady=8)

    def limits():
        """Parsed (lower, upper); an unparsable field is ignored and shown as such."""
        result = []
        for var in (lower_var, upper_var):
            try:
                value = float(var.get())
                result.append(value if math.isfinite(value) else None)
            except ValueError:
                result.append(None)
        return result

    def draw(_=None):
        canvas.delete("all")
        w, h = max(canvas.winfo_width(), 100), max(canvas.winfo_height(), 100)
        recent = []
        for row in reversed(rows):
            if recent and row[1] != recent[-1][1] or len(recent) >= 300:
                break
            recent.append(row)
        recent.reverse()
        values = [r[2] for r in recent if r[2] is not None]
        if not values:
            canvas.create_text(w / 2, h / 2, text="过量程" if recent else "等待采样", fill="white")
            return
        unit = recent[-1][3]
        lo, hi = min(values), max(values)
        span = max(hi - lo, abs(hi) * 0.01, 1e-12)
        lo -= span * 0.1
        hi += span * 0.1
        span = hi - lo
        for limit in limits():
            if limit is not None and lo <= limit <= hi:
                y = 25 + (hi - limit) / span * (h - 50)
                canvas.create_line(100, y, w - 20, y, fill="#e0a030", dash=(4, 4))
        canvas.create_text(8, 8, anchor="nw", text=format_value(hi, unit, 6), fill="white")
        canvas.create_text(8, h - 8, anchor="sw", text=format_value(lo, unit, 6), fill="white")
        points = []
        for i, value in enumerate(values):
            points.extend((100 + i * (w - 120) / max(1, len(values) - 1), 25 + (hi - value) / span * (h - 50)))
        if len(values) > 1:
            canvas.create_line(*points, fill="#59dfb0", width=2)
        else:
            x, y = points
            canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill="#59dfb0")

    def read_status(device):
        for _ in range(5):
            try:
                events.put(("status", device.status()))
                return
            except RejectedError:
                time.sleep(0.2)  # Instrument still switching.
        events.put(("unlock", None))

    def apply(device, name, value):
        setter = {"function": device.set_function, "range": device.set_range, "resolution": device.set_resolution,
                  "trigger": lambda _: device.set_trigger_auto()}
        try:
            result = setter[name](value)
            events.put(("notice", f"已确认：{name} = {getattr(result, 'token', result)}"))
        except (SettingError, RejectedError, ValueError) as exc:
            events.put(("notice", f"设置未生效：{exc}"))
        read_status(device)

    def run(period):
        device = None
        try:
            device = open_device(args)
            events.put(("identity", device.identity))
            read_status(device)
            take = Sampler(device)
            take.checked = True  # Checked below without ending the session.
            live = False
            known = None
            checked_at = 0.0
            due = time.monotonic()
            while not stop.is_set():
                try:
                    request = requests.get(timeout=max(0, due - time.monotonic()))
                except queue.Empty:
                    request = None
                if stop.is_set():
                    break
                if request:
                    apply(device, *request)
                    take.rejected_since = None
                    live = live and request[0] != "trigger"
                    due = time.monotonic()
                    continue
                due = time.monotonic() + period
                if time.monotonic() - checked_at > 30:
                    live = False  # The trigger can also be changed on the panel.
                if not live:
                    try:
                        device.check_live()
                        live, checked_at = True, time.monotonic()
                    except RejectedError:
                        continue
                    except InstrumentError as exc:
                        events.put(("notice", f"暂停采样：{exc}"))
                        due = time.monotonic() + 5
                        continue
                row = take()
                if row is None:
                    events.put(("busy", None))
                    continue
                events.put(("sample", row))
                if row[1] != known:
                    if known is not None:
                        read_status(device)  # Function changed on the panel.
                    known = row[1]
        except Exception as exc:
            logging.exception("Acquisition failed")
            events.put(("error", str(exc)))
        finally:
            if device:
                try:
                    device.close()
                except Exception as exc:
                    events.put(("error", f"USB cleanup: {exc}"))
            events.put(("done", None))

    def set_controls(enabled):
        for box in (function_box, range_box, resolution_box):
            box.config(state="readonly" if enabled and str(box.cget("values")) else "disabled")

    def request(name, value):
        if not worker or not worker.is_alive() or state["pending"]:
            return
        state["pending"] = True
        set_controls(False)
        status.set(f"正在设置 {name}…")
        requests.put((name, value))

    def on_function(_):
        function = lookup_function(function_var.get().split()[0])
        if function.token == state["function"]:
            return
        if function.excites and not messagebox.askyesno(
                "确认", f"{function.label} 会从输入端输出测试电流。\n确认被测电路已断电并允许继续？"):
            show_status(state["status"])
            return
        request("function", function.token)

    def on_range(_):
        choice = range_var.get()
        function = lookup_function(state["function"])
        request("range", "AUTO" if choice == AUTO else function.ranges.index(choice))

    def on_resolution(_):
        request("resolution", RESOLUTIONS.index(resolution_var.get()))

    function_box.bind("<<ComboboxSelected>>", on_function)
    range_box.bind("<<ComboboxSelected>>", on_range)
    resolution_box.bind("<<ComboboxSelected>>", on_resolution)

    def show_status(info):
        state["status"] = info
        function = lookup_function(info["function"])
        state["function"] = function.token
        function_var.set(f"{function.token} {function.label}")
        range_box.config(values=(AUTO,) + function.ranges if function.ranges else ())
        range_var.set(function.ranges[info["range"]] if info["range"] is not None else "")
        resolution_box.config(values=RESOLUTIONS if function.resolution else ())
        resolution_var.set(RESOLUTIONS[info["resolution"]] if info["resolution"] is not None else "")
        trigger, calculate = (info.get("trigger") or "—").upper(), (info.get("calculate") or "none").lower()
        detail.set(f"{function.label}  量程 {range_var.get() or '—'}  分辨率 {resolution_var.get() or '—'}  "
                   f"触发 {trigger} {info.get('interval') or ''}"
                   + ("" if calculate == "none" else f"  ⚠ 仪器运算 {calculate}：读数可能已经过处理"))
        trigger_button.config(state="normal" if trigger not in ("AUTO", "—") else "disabled")
        set_controls(True)

    def start():
        nonlocal worker
        if worker and worker.is_alive():
            return
        try:
            period = float(interval.get())
            if not math.isfinite(period) or period < 0.2:
                raise ValueError()
        except ValueError:
            messagebox.showerror("采样间隔", "请输入不小于 0.2 的秒数")
            return
        if rows and not messagebox.askyesno("重新开始", f"将清除已缓存的 {len(rows)} 条记录，继续？"):
            return
        rows.clear()
        reset_stats(None)
        draw()
        stop.clear()
        while not requests.empty():
            requests.get_nowait()
        state["pending"] = False
        start_button.config(state="disabled")
        entry.config(state="disabled")
        stop_button.config(state="normal")
        status.set("正在连接…" + ("（模拟）" if args.demo else ""))
        worker = threading.Thread(target=run, args=(period,), daemon=True)
        worker.start()

    def halt():
        stop.set()
        requests.put(None)
        status.set("正在停止，等待当前 USB 操作结束…")

    def export():
        if not rows:
            messagebox.showinfo("保存 CSV", "还没有采样数据")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if path:
            try:
                with open(path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(FIELDS)
                    writer.writerows(list(rows))
                messagebox.showinfo("保存 CSV", f"已保存 {len(rows)} 条记录")
            except OSError as exc:
                messagebox.showerror("保存失败", str(exc))

    start_button = ttk.Button(bar, text="开始 / 连接", command=start)
    start_button.pack(side="left", padx=4)
    stop_button = ttk.Button(bar, text="停止 / 断开", command=halt, state="disabled")
    stop_button.pack(side="left", padx=4)
    ttk.Button(bar, text="保存 CSV", command=export).pack(side="left", padx=4)
    record_button = ttk.Button(bar, text="实时记录…", command=lambda: stop_recording() if state["record"] else start_recording())
    record_button.pack(side="left", padx=4)
    ttk.Label(bar, textvariable=record_text).pack(side="left", padx=4)
    ttk.Button(extra, text="清零统计", command=lambda: reset_stats(state["function"])).pack(side="left")
    trigger_button = ttk.Button(extra, text="切回自动触发", command=lambda: request("trigger", "AUTO"), state="disabled")
    trigger_button.pack(side="left", padx=8)

    def reset_stats(function):
        unit = lookup_function(function).unit if function else ""
        state["stats"] = Stats(function, unit)
        stats_text.set("统计：—")

    def on_sample(row):
        rows.append(row)
        stats = state["stats"]
        if stats.function != row[1]:
            reset_stats(row[1])
            stats = state["stats"]
        stats.add(row[2])
        unit = row[3]
        if stats.n:
            sigma = format_value(stats.stdev, unit) if stats.stdev is not None else "—"
            stats_text.set(f"统计 n={stats.n}  最小 {format_value(stats.min, unit)}  最大 {format_value(stats.max, unit)}  "
                           f"平均 {format_value(stats.mean, unit)}  σ {sigma}"
                           + (f"  过量程 {stats.overloads}" if stats.overloads else ""))
        lower, upper = limits()
        out = row[2] is None or (lower is not None and row[2] < lower) or (upper is not None and row[2] > upper)
        reading.set(format_value(row[2], unit) + ("  超限" if out and (lower, upper) != (None, None) else ""))
        reading_label.config(foreground="#d03030" if out and (lower, upper) != (None, None) else "")
        if state["record"]:
            try:
                state["record"][1].writerow(row)
                state["record"][0].flush()
                state["recorded"] += 1
                record_text.set(f"记录中：{state['recorded']} 条")
            except OSError as exc:
                stop_recording()
                messagebox.showerror("实时记录失败", str(exc))

    def start_recording():
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                            title="实时记录到 CSV（每条立即写盘）")
        if not path:
            return
        try:
            f = open(path, "w", newline="", encoding="utf-8")
            writer = csv.writer(f)
            writer.writerow(FIELDS)
            f.flush()
        except OSError as exc:
            messagebox.showerror("实时记录失败", str(exc))
            return
        state["record"], state["recorded"] = (f, writer, path), 0
        record_button.config(text="停止记录")
        record_text.set("记录中：0 条")

    def stop_recording():
        if not state["record"]:
            return
        f, _, path = state["record"]
        state["record"] = None
        try:
            f.close()
        except OSError:
            pass
        record_button.config(text="实时记录…")
        record_text.set(f"已记录 {state['recorded']} 条 → {path}")
    canvas.bind("<Configure>", draw)

    def drain():
        redraw = False
        while True:
            try:
                kind, data = events.get_nowait()
            except queue.Empty:
                break
            if kind == "identity":
                status.set(data)
            elif kind == "status":
                state["pending"] = False
                show_status(data)
            elif kind == "unlock":
                state["pending"] = False
                set_controls("status" in state)
            elif kind == "notice":
                status.set(data)
            elif kind == "busy":
                status.set("仪器忙（回复 E，可能正在切换功能/量程），稍后重试…")
            elif kind == "sample":
                on_sample(data)
                redraw = True
            elif kind == "error":
                status.set("错误：" + data)
            elif kind == "done":
                start_button.config(state="normal")
                entry.config(state="normal")
                stop_button.config(state="disabled")
                set_controls(False)
                trigger_button.config(state="disabled")
                stop_recording()
                if stop.is_set():
                    status.set(f"已停止；缓存 {len(rows)} 条")
        if redraw:
            draw()
        if closing and (not worker or not worker.is_alive()):
            stop_recording()
            root.destroy()
        else:
            root.after(100, drain)

    def close():
        nonlocal closing
        closing = True
        halt()
    root.protocol("WM_DELETE_WINDOW", close)
    drain()
    root.mainloop()
