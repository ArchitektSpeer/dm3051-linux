"""Tk front end. Only the worker thread touches the instrument; the Tk thread talks to it via queues."""
from collections import deque
import csv
import logging
import math
import queue
import threading
import time
from .app import FIELDS, Sampler, Stats, format_value, open_device
from .usb_device import FUNCTIONS, RESOLUTIONS, NotLiveError, RejectedError, SettingError, lookup_function

AUTO = "Auto"


def gui(args):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    root = tk.Tk()
    root.title("DM3051 Linux — " + ("simulated instrument" if args.demo else "USB"))
    root.geometry("860x620")
    events = queue.Queue()
    requests = queue.Queue()
    stop = threading.Event()
    rows = deque(maxlen=10000)
    worker = None
    closing = False
    state = {"function": None, "pending": False, "stats": Stats(), "record": None, "recorded": 0}
    status = tk.StringVar(value="Press Start / Connect; the function selected on the panel is detected automatically")
    reading = tk.StringVar(value="—")
    detail = tk.StringVar(value="")
    interval = tk.StringVar(value=str(args.interval))
    function_var, range_var, resolution_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
    stats_text, record_text = tk.StringVar(value="Statistics: —"), tk.StringVar(value="Not recording")
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
    ttk.Label(bar, text="Interval (s)").pack(side="left")
    entry = ttk.Entry(bar, textvariable=interval, width=8)
    entry.pack(side="left", padx=8)
    controls = ttk.Frame(top)
    controls.pack(fill="x", pady=(8, 0))
    ttk.Label(controls, text="Function").pack(side="left")
    function_box = ttk.Combobox(controls, textvariable=function_var, values=function_names, width=24, state="disabled")
    function_box.pack(side="left", padx=(4, 12))
    ttk.Label(controls, text="Range").pack(side="left")
    range_box = ttk.Combobox(controls, textvariable=range_var, width=10, state="disabled")
    range_box.pack(side="left", padx=(4, 12))
    ttk.Label(controls, text="Resolution").pack(side="left")
    resolution_box = ttk.Combobox(controls, textvariable=resolution_var, values=RESOLUTIONS, width=6, state="disabled")
    resolution_box.pack(side="left", padx=(4, 12))
    extra = ttk.Frame(top)
    extra.pack(fill="x", pady=(8, 0))
    ttk.Label(extra, text="Lower limit").pack(side="left")
    ttk.Entry(extra, textvariable=lower_var, width=10).pack(side="left", padx=(4, 8))
    ttk.Label(extra, text="Upper limit").pack(side="left")
    ttk.Entry(extra, textvariable=upper_var, width=10).pack(side="left", padx=(4, 4))
    ttk.Label(extra, text="(base units, e.g. 5e-3)").pack(side="left", padx=(0, 12))
    ttk.Label(top, textvariable=stats_text).pack(anchor="w", pady=(8, 0))
    ttk.Label(root, text="The plot shows the last 300 points of the current function; memory keeps the last 10000 rows, "
              "use Live record for long runs. Settings are confirmed by readback (except auto range).",
              wraplength=820).pack(side="bottom", pady=8)
    canvas = tk.Canvas(root, background="#101820", highlightthickness=0)
    canvas.pack(fill="both", expand=True, padx=12, pady=8)

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
            canvas.create_text(w / 2, h / 2, text="Overload" if recent else "Waiting for samples", fill="white")
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
        """True when the panel state was refreshed; on False the worker retries after later samples."""
        for _ in range(5):
            try:
                events.put(("status", device.status()))
                return True
            except RejectedError:
                time.sleep(0.2)  # Instrument still switching.
        events.put(("unlock", None))
        return False

    def apply(device, name, value):
        setter = {"function": device.set_function, "range": device.set_range, "resolution": device.set_resolution,
                  "trigger": lambda _: device.set_trigger_auto()}
        try:
            result = setter[name](value)
            if name == "range" and result == "AUTO":
                events.put(("notice", "Sent: auto range (the firmware cannot read back auto/manual; the range shown below is the one currently in use)"))
            else:
                events.put(("notice", f"Confirmed: {name} = {getattr(result, 'token', result)}"))
        except (SettingError, RejectedError, ValueError) as exc:
            # Recoverable only; transport/framing errors end the session through run().
            events.put(("notice", f"Setting not applied: {exc}"))
        return read_status(device)

    def run(period):
        device = None
        try:
            device = open_device(args)
            events.put(("identity", device.identity))
            need_status = not read_status(device)
            take = Sampler(device, grace=math.inf)  # Stay connected and show "busy" instead.
            known = None
            paused = False
            due = time.monotonic()
            while not stop.is_set():
                try:
                    request = requests.get(timeout=max(0, due - time.monotonic()))
                except queue.Empty:
                    request = None
                if stop.is_set():
                    break
                if request:
                    need_status = not apply(device, *request)
                    take.rejected_since = None
                    due = time.monotonic()
                    continue
                due = time.monotonic() + period
                try:
                    row = take()  # Also checks the trigger after every reading.
                except NotLiveError as exc:
                    events.put(("notice", f"Sampling paused: {exc}"))
                    if not paused or need_status:
                        need_status = not read_status(device)  # Enables the "back to auto" button.
                    paused = True
                    due = time.monotonic() + 2
                    continue
                if paused:
                    paused, need_status = False, True  # Trigger is back to AUTO; refresh below.
                if row is None:
                    events.put(("busy", take.last_rejection))
                    continue
                events.put(("sample", row))
                if row[1] != known or need_status:
                    if known is not None or need_status:
                        need_status = not read_status(device)  # Function changed on the panel.
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
        status.set(f"Setting {name}…")
        requests.put((name, value))

    def on_function(_):
        function = lookup_function(function_var.get().split()[0])
        if function.token == state["function"]:
            return
        if function.excites and not messagebox.askyesno(
                "Confirm", f"{function.label} drives a test current out of the input terminals.\nIs the circuit under test unpowered, and do you want to continue?"):
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
        rng, res = info["range"], info["resolution"]
        range_var.set(function.ranges[rng] if rng is not None and 0 <= rng < len(function.ranges) else "")
        resolution_box.config(values=RESOLUTIONS if function.resolution else ())
        resolution_var.set(RESOLUTIONS[res] if res is not None and 0 <= res < len(RESOLUTIONS) else "")
        trigger, calculate = (info.get("trigger") or "—").upper(), (info.get("calculate") or "none").lower()
        detail.set(f"{function.label}  range {range_var.get() or '—'}  resolution {resolution_var.get() or '—'}  "
                   f"trigger {trigger} {info.get('interval') or ''}"
                   + ("" if calculate == "none" else f"  ⚠ instrument math {calculate}: readings may be processed"))
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
            messagebox.showerror("Interval", "Enter a number of seconds, at least 0.2")
            return
        if rows and not messagebox.askyesno("Restart", f"This clears the {len(rows)} buffered rows. Continue?"):
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
        status.set("Connecting…" + (" (simulated)" if args.demo else ""))
        worker = threading.Thread(target=run, args=(period,), daemon=True)
        worker.start()

    def halt():
        stop.set()
        requests.put(None)
        status.set("Stopping, waiting for the current USB transaction…")

    def export():
        if not rows:
            messagebox.showinfo("Save CSV", "No samples yet")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if path:
            try:
                with open(path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(FIELDS)
                    writer.writerows(list(rows))
                messagebox.showinfo("Save CSV", f"Saved {len(rows)} rows")
            except OSError as exc:
                messagebox.showerror("Save failed", str(exc))

    start_button = ttk.Button(bar, text="Start / Connect", command=start)
    start_button.pack(side="left", padx=4)
    stop_button = ttk.Button(bar, text="Stop / Disconnect", command=halt, state="disabled")
    stop_button.pack(side="left", padx=4)
    ttk.Button(bar, text="Save CSV", command=export).pack(side="left", padx=4)
    record_button = ttk.Button(bar, text="Live record…", command=lambda: stop_recording() if state["record"] else start_recording())
    record_button.pack(side="left", padx=4)
    ttk.Label(bar, textvariable=record_text).pack(side="left", padx=4)
    ttk.Button(extra, text="Reset statistics", command=lambda: reset_stats(state["function"])).pack(side="left")
    trigger_button = ttk.Button(extra, text="Auto trigger", command=lambda: request("trigger", "AUTO"), state="disabled")
    trigger_button.pack(side="left", padx=8)

    def reset_stats(function):
        unit = lookup_function(function).unit if function else ""
        state["stats"] = Stats(function, unit)
        stats_text.set("Statistics: —")

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
            stats_text.set(f"Statistics n={stats.n}  min {format_value(stats.min, unit)}  max {format_value(stats.max, unit)}  "
                           f"mean {format_value(stats.mean, unit)}  σ {sigma}"
                           + (f"  overload {stats.overloads}" if stats.overloads else ""))
        else:
            stats_text.set(f"Statistics: no valid readings  overload/open {stats.overloads}")
        lower, upper = limits()
        out = row[2] is None or (lower is not None and row[2] < lower) or (upper is not None and row[2] > upper)
        reading.set(format_value(row[2], unit) + ("  OUT OF LIMITS" if out and (lower, upper) != (None, None) else ""))
        reading_label.config(foreground="#d03030" if out and (lower, upper) != (None, None) else "")
        if state["record"]:
            try:
                state["record"][1].writerow(row)
                state["record"][0].flush()
                state["recorded"] += 1
                record_text.set(f"Recording: {state['recorded']} rows")
            except OSError as exc:
                stop_recording()
                messagebox.showerror("Live record failed", str(exc))

    def start_recording():
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                            title="Live record to CSV (every row is flushed to disk)")
        if not path:
            return
        try:
            f = open(path, "w", newline="", encoding="utf-8")
            writer = csv.writer(f)
            writer.writerow(FIELDS)
            f.flush()
        except OSError as exc:
            messagebox.showerror("Live record failed", str(exc))
            return
        state["record"], state["recorded"] = (f, writer, path), 0
        record_button.config(text="Stop recording")
        record_text.set("Recording: 0 rows")

    def stop_recording():
        if not state["record"]:
            return
        f, _, path = state["record"]
        state["record"] = None
        try:
            f.close()
        except OSError:
            pass
        record_button.config(text="Live record…")
        record_text.set(f"Recorded {state['recorded']} rows → {path}")
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
                state["stale"] = data.startswith("Sampling paused")
            elif kind == "busy":
                status.set(f"Instrument busy or reply out of step, retrying… ({data})")
                state["stale"] = True
            elif kind == "sample":
                if state.get("stale"):
                    status.set("Sampling")
                    state["stale"] = False
                on_sample(data)
                redraw = True
            elif kind == "error":
                status.set("Error: " + data)
            elif kind == "done":
                start_button.config(state="normal")
                entry.config(state="normal")
                stop_button.config(state="disabled")
                set_controls(False)
                trigger_button.config(state="disabled")
                stop_recording()
                if stop.is_set():
                    status.set(f"Stopped; {len(rows)} rows buffered")
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
