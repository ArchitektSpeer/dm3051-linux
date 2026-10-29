"""Tk front end. Only the worker thread touches the instrument; the Tk thread talks to it via queues."""
from bisect import bisect_left
from collections import deque
import csv
from datetime import datetime
import json
import logging
import math
import os
import queue
import re
import threading
import time
from .app import FIELDS, Sampler, Stats, format_value, open_device
from .texts import TEXT as T
from .usb_device import FUNCTIONS, RESOLUTIONS, NotLiveError, RejectedError, SettingError, lookup_function

# Palette (dark). Tk canvas has no alpha, so "tinted" fills are precomputed colours.
BG, PANEL, PANEL2, HOVER, BORDER = "#0b0f14", "#121821", "#19212c", "#222c39", "#243041"
TEXT, MUTED, DIM = "#e6edf3", "#8b98a8", "#566375"
ACCENT, ACCENT_DIM, ACCENT_FILL = "#2dd4bf", "#123f3b", "#0d2624"
BLUE, OK, WARN, ERR = "#60a5fa", "#34d399", "#fbbf24", "#f87171"
ERR_FILL, PLOT_BG, GRID = "#241418", "#0e141c", "#1b2431"
PILL = {"idle": DIM, "connecting": BLUE, "live": OK, "busy": WARN, "paused": WARN, "error": ERR, "stopping": MUTED}

WINDOWS = (100, 300, 1000, None)  # plot window in points; None = everything buffered
DIGITS = {0: 5, 1: 6, 2: 7}  # significant digits shown for 4½ / 5½ / 6½
SHORT = {"FREQUENCY": "FREQ", "PERIOD": "PERI"}
CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "dm3051", "gui.json")
_PREFIXES = {-12: "p", -9: "n", -6: "µ", -3: "m", 0: "", 3: "k", 6: "M", 9: "G"}
_SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "": 1.0, "k": 1e3, "M": 1e6, "G": 1e9}
_NUMBER = re.compile(r"\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*([pnuµmkMG]?)\s*[A-Za-zΩ]*\s*$")


def parse_number(text):
    """Limit entry: '5m' -> 0.005, '1.2 kΩ' -> 1200.0, '' -> None; ValueError otherwise."""
    if not text.strip():
        return None
    match = _NUMBER.match(text)
    if not match:
        raise ValueError(text)
    value = float(match.group(1)) * _SI[match.group(2)]
    if not math.isfinite(value):
        raise ValueError(text)
    return value


def _exponent(value):
    return min(max(math.floor(math.log10(abs(value)) / 3) * 3, -12), 9) if value else 0


def readout(value, unit, digits=7, exponent=None):
    """(number, prefixed unit) with a fixed number of significant digits, trailing zeros kept.
    With `exponent`, the number is scaled to that SI prefix and keeps the same decimal places
    it would have at full scale, so a column of values lines up."""
    if exponent is None:
        value = float(f"{value:.{digits}g}")  # round first: 999.9999 at 5 digits is 1.0000 k
        exponent = _exponent(value)
        return f"{value / 10 ** exponent:#.{digits}g}".rstrip("."), _PREFIXES[exponent] + unit
    scaled = value / 10 ** exponent
    whole = max(1, len(str(int(abs(scaled))))) if abs(scaled) >= 1 else 1
    return f"{scaled:.{max(0, digits - whole)}f}", _PREFIXES[exponent] + unit


def limit_state(value, lower, upper):
    if lower is None and upper is None:
        return "ovld" if value is None else "none"
    if value is None:
        return "ovld"
    if lower is not None and value < lower:
        return "low"
    if upper is not None and value > upper:
        return "high"
    return "ok"


def nice_ticks(lo, hi, count=5):
    """Round tick values covering [lo, hi] and the step between them."""
    span = hi - lo
    if not span > 0:
        return [lo], 1.0
    raw = span / count
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw * 0.999)
    first = math.ceil(lo / step - 1e-9)
    return [(first + i) * step for i in range(int((hi - first * step) / step + 1e-9) + 1)], step


def axis_labels(ticks, step, unit):
    """One common SI prefix for the whole axis; returns (labels, prefixed unit)."""
    exponent = _exponent(max((abs(t) for t in ticks), default=0) or step)
    scale = 10 ** exponent
    decimals = min(max(0, -math.floor(math.log10(step / scale) + 1e-9)), 6)
    return [f"{t / scale:.{decimals}f}" for t in ticks], _PREFIXES[exponent] + unit


def decimate(points, buckets):
    """Keep the min and max of each bucket so spikes survive when there are more points than pixels."""
    if len(points) <= 2 * buckets:
        return points
    size = len(points) / buckets
    out = []
    for b in range(buckets):
        chunk = points[int(b * size):int((b + 1) * size)]
        if chunk:
            low, high = min(chunk, key=lambda p: p[1]), max(chunk, key=lambda p: p[1])
            out.extend(sorted((low, high)) if low is not high else (low,))
    return out


def split_runs(points):
    """Split (time, function, value) points into runs of (time, value) for drawing: an overload
    or a gap much longer than the usual interval (paused, busy) breaks the line."""
    times = [p[0] for p in points]
    steps = sorted(b - a for a, b in zip(times, times[1:]))
    limit = max(3 * steps[len(steps) // 2], 1.0) if steps else math.inf
    runs, run, previous = [], [], None
    for when, _, value in points:
        if value is None or (previous is not None and when - previous > limit):
            if run:
                runs.append(run)
            run = []
        if value is not None:
            run.append((when, value))
        previous = when
    if run:
        runs.append(run)
    return runs


def ago(seconds):
    if seconds < 1e-9:
        return "0"
    if seconds < 120:
        return f"−{seconds:.0f}s" if seconds >= 10 else f"−{seconds:.1f}s".replace(".0s", "s")
    if seconds < 7200:
        return f"−{seconds / 60:.0f}m"
    return f"−{seconds / 3600:.1f}h"


def load_settings():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data):
    try:
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        with open(CONFIG + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(CONFIG + ".tmp", CONFIG)
    except OSError:
        logging.warning("Could not save GUI settings to %s", CONFIG)


def gui(args):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, font as tkfont
    settings = load_settings()
    root = tk.Tk()
    root.title(T["title"] + (T["title_demo"] if args.demo else ""))
    root.configure(bg=BG)
    root.minsize(1100, 760)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(settings.get("geometry") or f"{min(1280, sw - 40)}x{min(840, sh - 60)}")
    families = set(tkfont.families(root))

    def pick(*names, default):
        return next((n for n in names if n in families), default)
    ui = pick("Inter", "Noto Sans CJK SC", "Noto Sans", "DejaVu Sans", default="sans")
    mono = pick("JetBrains Mono", "Noto Sans Mono", "DejaVu Sans Mono", default="monospace")
    F = {"ui": (ui, 10), "bold": (ui, 10, "bold"), "small": (ui, 9), "caps": (ui, 8, "bold"),
         "title": (ui, 14, "bold"), "fn": (ui, 11, "bold"), "chip": (ui, 11, "bold"), "unit": (ui, 26),
         "big": (mono, 58), "num": (mono, 13), "mono": (mono, 9), "axis": (mono, 8)}

    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=PANEL, foreground=TEXT, font=F["ui"], bordercolor=BORDER,
                    lightcolor=PANEL2, darkcolor=PANEL2, troughcolor=PANEL, focuscolor=ACCENT)
    style.configure("TButton", background=PANEL2, foreground=TEXT, padding=(10, 6), relief="flat",
                    bordercolor=BORDER, lightcolor=PANEL2, darkcolor=PANEL2, focusthickness=0)
    style.map("TButton", background=[("disabled", "#151b24"), ("pressed", BORDER), ("active", HOVER)],
              foreground=[("disabled", DIM)], bordercolor=[("disabled", PANEL2)],
              lightcolor=[("active", HOVER), ("disabled", PANEL)], darkcolor=[("active", HOVER), ("disabled", PANEL)])
    for name, bg, fg, active in (("Accent", ACCENT, "#04201d", "#5eead4"), ("Danger", "#3a1a1f", ERR, "#4a2128"),
                                 ("On", ACCENT_DIM, ACCENT, "#18524c")):
        style.configure(f"{name}.TButton", background=bg, foreground=fg, bordercolor=bg, lightcolor=bg, darkcolor=bg,
                        font=F["bold"])
        style.map(f"{name}.TButton", background=[("disabled", PANEL), ("pressed", bg), ("active", active)],
                  foreground=[("disabled", DIM)], bordercolor=[("disabled", PANEL2), ("active", active)],
                  lightcolor=[("active", active), ("disabled", PANEL)], darkcolor=[("active", active), ("disabled", PANEL)])
    style.configure("Fn.TButton", padding=(4, 6), font=F["bold"])
    style.configure("FnOn.TButton", padding=(4, 6), font=F["bold"], background=ACCENT_DIM, foreground=ACCENT,
                    bordercolor=ACCENT, lightcolor=ACCENT_DIM, darkcolor=ACCENT_DIM)
    style.map("FnOn.TButton", background=[("disabled", ACCENT_FILL), ("active", "#18524c")],
              foreground=[("disabled", "#2a8f84")], bordercolor=[("disabled", "#1d6b63")])
    style.configure("Seg.TButton", padding=(6, 4), font=F["small"])
    style.configure("SegOn.TButton", padding=(6, 4), font=F["small"], background=ACCENT_DIM, foreground=ACCENT,
                    bordercolor=ACCENT_DIM, lightcolor=ACCENT_DIM, darkcolor=ACCENT_DIM)
    style.map("SegOn.TButton", background=[("disabled", ACCENT_FILL), ("active", "#18524c")],
              foreground=[("disabled", "#2a8f84")])
    for widget in ("TEntry", "TSpinbox", "TCombobox"):
        style.configure(widget, fieldbackground=PANEL2, background=PANEL2, foreground=TEXT, insertcolor=TEXT,
                        bordercolor=BORDER, lightcolor=PANEL2, darkcolor=PANEL2, arrowcolor=MUTED,
                        selectbackground=ACCENT_DIM, selectforeground=TEXT, padding=(6, 4))
        style.map(widget, bordercolor=[("focus", ACCENT)], fieldbackground=[("readonly", PANEL2), ("disabled", PANEL)],
                  foreground=[("disabled", DIM)], arrowcolor=[("disabled", DIM)],
                  selectbackground=[("readonly", PANEL2)], selectforeground=[("readonly", TEXT)])
    style.configure("Bad.TEntry", bordercolor=ERR, lightcolor=PANEL2, darkcolor=PANEL2)
    style.map("Bad.TEntry", bordercolor=[("focus", ERR)])
    root.option_add("*TCombobox*Listbox.background", PANEL2)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT_DIM)
    root.option_add("*TCombobox*Listbox.selectForeground", TEXT)
    root.option_add("*TCombobox*Listbox.font", F["ui"])

    events = queue.Queue()
    requests = queue.Queue()
    stop = threading.Event()
    rows = deque(maxlen=10000)
    series = deque(maxlen=10000)  # (epoch, function token, value) for plotting
    worker = None
    closing = False
    state = {"function": None, "pending": False, "stats": Stats(), "record": None, "recorded": 0, "out": 0,
             "rel": None, "geom": None, "mouse": None, "frozen": False, "error": False, "last": None,
             "window": settings.get("window", 300) if settings.get("window", 300) in WINDOWS else 300,
             "hist": bool(settings.get("hist", True)), "arrivals": deque(maxlen=20)}
    interval = tk.StringVar(value=str(settings.get("interval", args.interval)))
    lower_var = tk.StringVar(value=str(settings.get("lower", "")))
    upper_var = tk.StringVar(value=str(settings.get("upper", "")))
    range_var = tk.StringVar()

    def label(parent, text="", font="ui", fg=TEXT, bg=PANEL, **kw):
        return tk.Label(parent, text=text, font=F[font], fg=fg, bg=bg, **kw)

    def card(parent, **kw):
        return tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=BORDER, **kw)

    class Tip:
        """Hover tooltip; text may be a callable so it can follow the current state."""
        def __init__(self, widget, text):
            self.widget, self.text, self.window, self.job = widget, text, None, None
            widget.bind("<Enter>", self.schedule, add="+")
            widget.bind("<Leave>", self.hide, add="+")
            widget.bind("<ButtonPress>", self.hide, add="+")

        def schedule(self, _):
            self.job = self.widget.after(550, self.show)

        def show(self):
            text = self.text() if callable(self.text) else self.text
            if not text:
                return
            self.window = tk.Toplevel(self.widget, bg=BORDER)
            self.window.wm_overrideredirect(True)
            tk.Label(self.window, text=text, font=F["small"], fg=TEXT, bg=PANEL2, justify="left",
                     padx=8, pady=5, wraplength=320).pack(padx=1, pady=1)
            self.window.wm_geometry(f"+{self.widget.winfo_rootx() + 8}+{self.widget.winfo_rooty() + self.widget.winfo_height() + 6}")

        def hide(self, _=None):
            if self.job:
                self.widget.after_cancel(self.job)
                self.job = None
            if self.window:
                self.window.destroy()
                self.window = None

    # ---- header -------------------------------------------------------------------------------
    header = tk.Frame(root, bg=BG)
    header.pack(fill="x", padx=18, pady=(14, 10))
    brand = tk.Frame(header, bg=BG)
    brand.pack(side="left")
    logo = tk.Canvas(brand, width=34, height=34, bg=BG, highlightthickness=0)
    logo.create_rectangle(2, 2, 32, 32, outline=ACCENT, width=2)
    logo.create_line(7, 22, 12, 22, 15, 10, 19, 26, 22, 16, 27, 16, fill=ACCENT, width=2)
    logo.pack(side="left", padx=(0, 10))
    titles = tk.Frame(brand, bg=BG)
    titles.pack(side="left")
    label(titles, T["title"], "title", bg=BG).pack(anchor="w")
    subtitle = label(titles, T["subtitle_demo"] if args.demo else T["subtitle_idle"], "small", MUTED, BG)
    subtitle.pack(anchor="w")
    actions = tk.Frame(header, bg=BG)
    actions.pack(side="right")
    pill_frame = tk.Frame(actions, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
    pill_frame.pack(side="left", padx=(0, 12), ipady=2)
    pill_dot = tk.Canvas(pill_frame, width=12, height=12, bg=PANEL, highlightthickness=0)
    pill_dot.create_oval(2, 2, 10, 10, fill=DIM, outline="", tags="dot")
    pill_dot.pack(side="left", padx=(10, 6), pady=6)
    pill_text = label(pill_frame, T["pill"]["idle"], "bold", DIM)
    pill_text.pack(side="left", padx=(0, 12))
    start_button = ttk.Button(actions, text=T["start"], style="Accent.TButton")
    start_button.pack(side="left")
    stop_button = ttk.Button(actions, text=T["stop"], state="disabled")
    stop_button.pack(side="left", padx=(8, 0))
    Tip(start_button, T["tip_start"])
    Tip(stop_button, T["tip_stop"])

    # ---- status bar ---------------------------------------------------------------------------
    statusbar = tk.Frame(root, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
    statusbar.pack(side="bottom", fill="x")
    status_label = label(statusbar, T["ready"], "small", MUTED, anchor="w")
    status_label.pack(side="left", fill="x", expand=True, padx=12, pady=5)
    rec_label = label(statusbar, "", "small", ERR)
    rec_label.pack(side="right", padx=12)
    info_label = label(statusbar, "", "mono", MUTED)
    info_label.pack(side="right", padx=12)

    # ---- body ---------------------------------------------------------------------------------
    body = tk.Frame(root, bg=BG)
    body.pack(fill="both", expand=True, padx=18, pady=(0, 14))
    body.columnconfigure(0, weight=1)
    body.columnconfigure(1, minsize=300)
    body.rowconfigure(2, weight=1)

    # Readout card
    hero = card(body)
    hero.grid(row=0, column=0, sticky="nsew")
    hero.columnconfigure(0, weight=1)
    left = tk.Frame(hero, bg=PANEL)
    left.grid(row=0, column=0, sticky="nsew", padx=22, pady=(16, 14))
    fn_row = tk.Frame(left, bg=PANEL)
    fn_row.pack(anchor="w")
    fn_label = label(fn_row, T["no_function"], "fn", DIM)
    fn_label.pack(side="left")
    fn_name = label(fn_row, "", "ui", MUTED)
    fn_name.pack(side="left", padx=(10, 0))
    excite_label = label(fn_row, "", "small", WARN)
    excite_label.pack(side="left", padx=(10, 0))
    value_row = tk.Frame(left, bg=PANEL)
    value_row.pack(anchor="w", pady=(2, 6))
    big_font = tkfont.Font(root, family=mono, size=58)
    value_label = tk.Label(value_row, text="------", font=big_font, fg=DIM, bg=PANEL)
    value_label.pack(side="left", anchor="s")
    unit_label = label(value_row, "", "unit", MUTED)
    unit_label.pack(side="left", anchor="s", padx=(12, 0), pady=(0, 12))
    badges = tk.Frame(left, bg=PANEL)
    badges.pack(anchor="w")

    def badge(fg=MUTED, bg=PANEL2):
        return tk.Label(badges, text="", font=F["small"], fg=fg, bg=bg, padx=8, pady=2)
    badge_range, badge_res, badge_trigger, badge_interval = badge(), badge(), badge(), badge()
    badge_rel, badge_calc = badge(ACCENT, ACCENT_FILL), badge(WARN, "#2a2210")
    badge_wait = badge(DIM)
    badge_wait.config(text=T["badge_wait"])
    badge_wait.pack(side="left")
    right = tk.Frame(hero, bg=PANEL)
    right.grid(row=0, column=1, sticky="ne", padx=22, pady=18)
    chip = tk.Label(right, text=T["chip"]["none"], font=F["chip"], fg=MUTED, bg=PANEL2, padx=16, pady=7)
    chip.pack(anchor="e")
    last_label = label(right, "", "mono", MUTED)
    last_label.pack(anchor="e", pady=(12, 0))
    count_label = label(right, "", "mono", MUTED)
    count_label.pack(anchor="e", pady=(2, 0))

    # Statistics strip
    strip = tk.Frame(body, bg=BG)
    strip.grid(row=1, column=0, sticky="ew", pady=12)
    stat_values = []
    stat_font = tkfont.Font(root, family=mono, size=13)
    for i, name in enumerate(T["stats"]):
        cell = card(strip)
        cell.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0))
        strip.columnconfigure(i, weight=1, uniform="stat")
        label(cell, name, "caps", MUTED).pack(anchor="w", padx=12, pady=(9, 0))
        value = tk.Label(cell, text="—", font=stat_font, fg=TEXT, bg=PANEL)
        value.pack(anchor="w", padx=12, pady=(1, 9))
        stat_values.append(value)

    # Plot card
    plot_card = card(body)
    plot_card.grid(row=2, column=0, sticky="nsew")
    toolbar = tk.Frame(plot_card, bg=PANEL)
    toolbar.pack(fill="x", padx=14, pady=(10, 0))
    plot_title = label(toolbar, T["plot"], "bold")
    plot_title.pack(side="left")
    Tip(plot_title, T["footer"])
    plot_unit = label(toolbar, "", "small", MUTED)
    plot_unit.pack(side="left", padx=(8, 0))
    tools = tk.Frame(toolbar, bg=PANEL)
    tools.pack(side="right")
    window_buttons = {}
    for n in WINDOWS:
        b = ttk.Button(tools, text=str(n) if n else T["plot_all"], width=5,
                       command=lambda n=n: set_window(n))
        b.pack(side="left", padx=(0, 2))
        window_buttons[n] = b
    hist_button = ttk.Button(tools, text=T["plot_hist"], command=lambda: toggle("hist"))
    hist_button.pack(side="left", padx=(10, 2))
    freeze_button = ttk.Button(tools, text=T["plot_freeze"], command=lambda: toggle("frozen"))
    freeze_button.pack(side="left", padx=(0, 0))
    Tip(hist_button, T["tip_hist"])
    Tip(freeze_button, T["tip_freeze"])
    canvas = tk.Canvas(plot_card, bg=PLOT_BG, highlightthickness=0, height=240)
    canvas.pack(fill="both", expand=True, padx=1, pady=(8, 1))

    # Sidebar
    side = card(body, width=310)
    side.grid(row=0, column=1, rowspan=3, sticky="nsew", padx=(14, 0))
    side.pack_propagate(False)  # fixed width; the content column takes the rest
    inner = tk.Frame(side, bg=PANEL)
    inner.pack(fill="both", expand=True, padx=16, pady=12)

    def section(text, first=False):
        if not first:
            tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", pady=(9, 0))
        label(inner, text, "caps", MUTED).pack(anchor="w", pady=(8, 5))

    section(T["sec_function"], first=True)
    grid = tk.Frame(inner, bg=PANEL)
    grid.pack(fill="x")
    fn_buttons = {}
    for i, f in enumerate(FUNCTIONS):
        b = ttk.Button(grid, text=SHORT.get(f.token, f.token), style="Fn.TButton", state="disabled",
                       command=lambda f=f: on_function(f))
        b.grid(row=i // 4, column=i % 4, sticky="ew", padx=2, pady=2)
        Tip(b, f"{f.token} · {f.label}" + (T["tip_excites"] if f.excites else ""))
        fn_buttons[f.token] = b
    for c in range(4):
        grid.columnconfigure(c, weight=1, uniform="fn")

    section(T["sec_range"])
    range_row = tk.Frame(inner, bg=PANEL)
    range_row.pack(fill="x")
    range_box = ttk.Combobox(range_row, textvariable=range_var, state="disabled", width=9)
    range_box.pack(side="left", fill="x", expand=True)
    seg = tk.Frame(range_row, bg=PANEL)
    seg.pack(side="left", padx=(8, 0))
    res_buttons = []
    for i, text in enumerate(RESOLUTIONS):
        b = ttk.Button(seg, text=text, style="Seg.TButton", state="disabled", width=4,
                       command=lambda i=i: on_resolution(i))
        b.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 2, 0))
        seg.columnconfigure(i, weight=1, uniform="res")
        res_buttons.append(b)

    section(T["sec_trigger"])
    trig_row = tk.Frame(inner, bg=PANEL)
    trig_row.pack(fill="x")
    trigger_text = label(trig_row, T["trigger_state"].format("—"), "ui", MUTED)
    trigger_text.pack(side="left")
    trigger_button = ttk.Button(trig_row, text=T["trigger_auto"], state="disabled",
                                command=lambda: request("trigger", "AUTO"))
    trigger_button.pack(side="right")

    samp_row = tk.Frame(inner, bg=PANEL)
    samp_row.pack(fill="x", pady=(8, 0))
    label(samp_row, T["sec_sampling"], "small", MUTED).pack(side="left", padx=(0, 8))
    entry = ttk.Spinbox(samp_row, textvariable=interval, from_=0.2, to=3600, increment=0.1, width=8)
    entry.pack(side="left")
    label(samp_row, T["interval_unit"], "small", MUTED).pack(side="left", padx=8)

    section(T["sec_limits"])
    lim = tk.Frame(inner, bg=PANEL)
    lim.pack(fill="x")
    lim.columnconfigure(1, weight=1)
    lim.columnconfigure(3, weight=1)
    label(lim, T["lower"], "small", MUTED).grid(row=0, column=0, padx=(0, 6))
    lower_entry = ttk.Entry(lim, textvariable=lower_var, width=8)
    lower_entry.grid(row=0, column=1, sticky="ew")
    label(lim, T["upper"], "small", MUTED).grid(row=0, column=2, padx=(10, 6))
    upper_entry = ttk.Entry(lim, textvariable=upper_var, width=8)
    upper_entry.grid(row=0, column=3, sticky="ew")
    Tip(lower_entry, T["limits_hint"])
    Tip(upper_entry, T["limits_hint"])

    section(T["sec_data"])
    data_row = tk.Frame(inner, bg=PANEL)
    data_row.pack(fill="x")
    data_row.columnconfigure(0, weight=1, uniform="d")
    data_row.columnconfigure(1, weight=1, uniform="d")
    save_button = ttk.Button(data_row, text=T["save"], command=lambda: export())
    save_button.grid(row=0, column=0, sticky="ew", padx=(0, 3))
    reset_button = ttk.Button(data_row, text=T["reset"], command=lambda: reset_stats(state["function"]))
    reset_button.grid(row=0, column=1, sticky="ew", padx=(3, 0))
    rel_button = ttk.Button(data_row, text=T["rel_on"], command=lambda: toggle_rel())
    rel_button.grid(row=1, column=0, sticky="ew", padx=(0, 3), pady=(6, 0))
    Tip(rel_button, T["rel_hint"])
    record_button = ttk.Button(data_row, text=T["record"],
                               command=lambda: stop_recording() if state["record"] else start_recording())
    record_button.grid(row=1, column=1, sticky="ew", padx=(3, 0), pady=(6, 0))
    record_info = label(inner, "", "small", MUTED, wraplength=262, justify="left")
    record_info.pack(anchor="w", pady=(5, 0))
    Tip(save_button, T["tip_save"])
    Tip(record_button, T["tip_record"])
    Tip(record_info, lambda: state.get("record_path"))
    Tip(reset_button, T["tip_reset"])

    # ---- helpers for the Tk thread ------------------------------------------------------------
    def message(text, level="info"):
        status_label.config(text=text, fg={"info": MUTED, "ok": OK, "warn": WARN, "err": ERR}[level])

    def pill(kind):
        pill_dot.itemconfig("dot", fill=PILL[kind])
        pill_text.config(text=T["pill"][kind], fg=PILL[kind])
        if kind not in ("live", "connecting") and state["last"]:
            value_label.config(fg=MUTED)  # the value shown is no longer live

    def limits():
        """Parsed (lower, upper); an unparsable field is ignored and its entry outlined red."""
        result = []
        for var, widget in ((lower_var, lower_entry), (upper_var, upper_entry)):
            try:
                result.append(parse_number(var.get()))
                widget.configure(style="TEntry")
            except ValueError:
                result.append(None)
                widget.configure(style="Bad.TEntry")
        return result

    def offset():
        return state["rel"] or 0.0

    def digits():
        res = (state.get("status") or {}).get("resolution")
        return DIGITS.get(res, 7)

    def plot_points():
        """Contiguous tail of the current function, limited to the chosen window."""
        recent = []
        for point in reversed(series):
            if recent and point[1] != recent[-1][1]:
                break
            recent.append(point)
            if state["window"] and len(recent) >= state["window"]:
                break
        recent.reverse()
        return recent

    def draw(_=None):
        canvas.delete("all")
        w, h = max(canvas.winfo_width(), 200), max(canvas.winfo_height(), 120)
        recent = plot_points()
        values = [p[2] - offset() for p in recent if p[2] is not None]
        state["geom"] = None
        if not values:
            canvas.create_text(w / 2, h / 2 - 10, text=T["overload_only"] if recent else T["waiting"],
                               fill=MUTED, font=F["bold"])
            if not recent:
                canvas.create_text(w / 2, h / 2 + 14, text=T["waiting_hint"], fill=DIM, font=F["small"])
            plot_unit.config(text="")
            return
        unit = lookup_function(recent[-1][1]).unit
        lower, upper = limits()
        lo, hi = min(values), max(values)
        pad = max(hi - lo, abs(hi) * 1e-4, 1e-15) * 0.12
        ticks, step = nice_ticks(lo - pad, hi + pad, max(3, int(h / 55)))
        lo, hi = min(ticks[0], lo - pad), max(ticks[-1], hi + pad)
        labels_, axis_unit = axis_labels(ticks, step, unit)
        plot_unit.config(text=axis_unit)
        x0, y0 = 14 + max(len(s) for s in labels_) * 7 + 10, 12
        x1, y1 = w - (96 if state["hist"] else 18), h - 26
        span = hi - lo

        def Y(v):
            return y1 - (v - lo) / span * (y1 - y0)
        t0, t1 = recent[0][0], recent[-1][0]
        tspan = max(t1 - t0, 1e-6)

        def X(t):
            return x0 + (t - t0) / tspan * (x1 - x0) if t1 > t0 else x1

        # Out-of-limit bands, then grid, then data on top.
        if upper is not None and upper < hi:
            canvas.create_rectangle(x0, y0, x1, max(y0, Y(upper)), fill=ERR_FILL, outline="")
        if lower is not None and lower > lo:
            canvas.create_rectangle(x0, min(y1, Y(lower)), x1, y1, fill=ERR_FILL, outline="")
        for tick, text in zip(ticks, labels_):
            y = Y(tick)
            canvas.create_line(x0, y, x1, y, fill=GRID)
            canvas.create_text(x0 - 8, y, text=text, anchor="e", fill=MUTED, font=F["axis"])
        tticks, tstep = nice_ticks(0, tspan, max(2, int((x1 - x0) / 110)))
        for back in tticks:
            x = X(t1 - back)
            canvas.create_line(x, y0, x, y1, fill=GRID)
            canvas.create_text(x, y1 + 13, text=ago(back), fill=DIM, font=F["axis"])
        canvas.create_rectangle(x0, y0, x1, y1, outline=BORDER)

        segments = [decimate([(X(t), Y(v - offset())) for t, v in seg], max(2, int((x1 - x0) * len(seg) / len(recent))))
                    for seg in split_runs(recent)]
        for seg in segments:
            if len(seg) > 1:
                canvas.create_polygon(seg[0][0], y1, *[c for p in seg for c in p], seg[-1][0], y1,
                                      fill=ACCENT_FILL, outline="")
        for tick in ticks:  # grid stays visible through the area fill
            canvas.create_line(x0, Y(tick), x1, Y(tick), fill=GRID, dash=(2, 3))
        for seg in segments:
            if len(seg) > 1:
                canvas.create_line(*[c for p in seg for c in p], fill=ACCENT, width=2, joinstyle="round")
            else:
                canvas.create_oval(seg[0][0] - 2, seg[0][1] - 2, seg[0][0] + 2, seg[0][1] + 2, fill=ACCENT, outline="")
        points = [p for seg in segments for p in seg]
        for p in recent:
            if p[2] is None:
                x = X(p[0])
                canvas.create_polygon(x - 4, y0, x + 4, y0, x, y0 + 7, fill=WARN, outline="")
        mean = sum(values) / len(values)
        ym = Y(mean)
        canvas.create_line(x0, ym, x1, ym, fill=BLUE, dash=(6, 4))
        canvas.create_text(x0 + 6, ym - 9, text="μ", anchor="w", fill=BLUE, font=F["small"])
        for value, name in ((upper, T["upper"]), (lower, T["lower"])):
            if value is not None and lo <= value <= hi:
                canvas.create_line(x0, Y(value), x1, Y(value), fill=ERR, dash=(4, 3))
                canvas.create_text(x0 + 6, Y(value) - 8, text=name, anchor="w", fill=ERR, font=F["small"])
        lx, ly = points[-1]
        canvas.create_oval(lx - 7, ly - 7, lx + 7, ly + 7, fill=ACCENT_DIM, outline="")
        canvas.create_oval(lx - 3.5, ly - 3.5, lx + 3.5, ly + 3.5, fill=ACCENT, outline=PLOT_BG)

        if state["hist"]:
            bins = 24
            counts = [0] * bins
            for v in values:
                counts[min(bins - 1, max(0, int((v - lo) / span * bins)))] += 1
            peak = max(counts)
            hx0, hx1 = x1 + 10, w - 14
            for i, count in enumerate(counts):
                if count:
                    yb, yt = Y(lo + i * span / bins), Y(lo + (i + 1) * span / bins)
                    canvas.create_rectangle(hx0, yt + 1, hx0 + (hx1 - hx0) * count / peak, yb, fill=ACCENT_DIM,
                                            outline="")
            canvas.create_line(hx0, ym, hx1, ym, fill=BLUE, dash=(6, 4))
            canvas.create_line(hx0, y0, hx0, y1, fill=BORDER)
        if state["frozen"]:
            canvas.create_text(x1 - 8, y0 + 10, text="❄ " + T["plot_frozen"], anchor="ne", fill=BLUE, font=F["small"])
        state["geom"] = {"recent": recent, "X": X, "Y": Y, "box": (x0, y0, x1, y1), "unit": unit, "mean": mean,
                         "times": [p[0] for p in recent]}
        if state["mouse"]:
            hover(*state["mouse"])

    def hover(x, y):
        canvas.delete("hover")
        g = state["geom"]
        if not g:
            return
        x0, y0, x1, y1 = g["box"]
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            return
        t = g["times"][0] + (x - x0) / max(x1 - x0, 1) * (g["times"][-1] - g["times"][0])
        i = bisect_left(g["times"], t)
        i = min(range(max(0, i - 1), min(len(g["times"]), i + 1)), key=lambda k: abs(g["times"][k] - t))
        when, _, value = g["recent"][i]
        px = g["X"](when)
        canvas.create_line(px, y0, px, y1, fill=MUTED, dash=(2, 2), tags="hover")
        if value is None:
            lines = [T["ovld"]]
            py = y0 + 12
        else:
            shown = value - offset()
            py = g["Y"](shown)
            number, unit = readout(shown, g["unit"], digits())
            delta = format_value(shown - g["mean"], g["unit"], 4)
            lines = [f"{number} {unit}", T["hover_delta"].format(("+" if shown >= g["mean"] else "") + delta)]
            canvas.create_oval(px - 4, py - 4, px + 4, py + 4, fill=TEXT, outline=PLOT_BG, width=2, tags="hover")
        lines.append(datetime.fromtimestamp(when).strftime("%H:%M:%S.%f")[:-3])
        text = canvas.create_text(0, 0, text="\n".join(lines), anchor="nw", fill=TEXT, font=F["mono"], tags="hover")
        bx0, by0, bx1, by1 = canvas.bbox(text)
        bw, bh = bx1 - bx0 + 16, by1 - by0 + 10
        bx = px + 12 if px + 12 + bw < x1 else px - 12 - bw
        by = min(max(py - bh / 2, y0 + 2), y1 - bh - 2)
        canvas.coords(text, bx + 8, by + 5)
        box = canvas.create_rectangle(bx, by, bx + bw, by + bh, fill=PANEL2, outline=BORDER, tags="hover")
        canvas.tag_raise(text, box)

    def on_motion(e):
        state["mouse"] = (e.x, e.y)
        hover(e.x, e.y)

    def on_leave(_):
        state["mouse"] = None
        canvas.delete("hover")
    canvas.bind("<Motion>", on_motion)
    canvas.bind("<Leave>", on_leave)
    canvas.bind("<Configure>", draw)

    def set_window(n):
        state["window"] = n
        for k, b in window_buttons.items():
            b.configure(style="SegOn.TButton" if k == n else "Seg.TButton")
        draw()

    def toggle(key):
        state[key] = not state[key]
        style_toggles()
        draw()

    def style_toggles():
        hist_button.configure(style="SegOn.TButton" if state["hist"] else "Seg.TButton")
        freeze_button.configure(style="SegOn.TButton" if state["frozen"] else "Seg.TButton")

    def toggle_rel():
        if state["rel"] is None:
            last = state["last"]
            if not last or last[2] is None:
                return
            state["rel"] = last[2]
        else:
            state["rel"] = None
        refresh_rel()

    def refresh_rel():
        if state["rel"] is None:
            rel_button.configure(text=T["rel_on"], style="TButton")
            badge_rel.pack_forget()
        else:
            unit = lookup_function(state["function"]).unit if state["function"] else ""
            rel_button.configure(text=T["rel_off"], style="On.TButton")
            badge_rel.config(text=T["rel_badge"].format(format_value(state["rel"], unit, 6)))
            first = badges.pack_slaves()
            badge_rel.pack(side="left", padx=(0, 6), **({"before": first[0]} if first else {}))
        show_reading()
        show_stats()
        draw()

    # ---- worker thread (the only code that touches the instrument) ----------------------------
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
                events.put(("notice", T["auto_sent"]))
            else:
                events.put(("confirmed", T["confirmed"].format(T["names"][name], getattr(result, "token", result))))
        except (SettingError, RejectedError, ValueError) as exc:
            # Recoverable only; transport/framing errors end the session through run().
            events.put(("warn", T["not_applied"].format(exc)))
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
                    events.put(("paused", str(exc)))
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
                    events.put(("error", T["usb_cleanup"].format(exc)))
            events.put(("done", None))

    # ---- controls -----------------------------------------------------------------------------
    def set_controls(enabled):
        info = state.get("status")
        function = lookup_function(info["function"]) if info else None
        for token, b in fn_buttons.items():
            b.state(["!disabled"] if enabled else ["disabled"])
        range_box.config(state="readonly" if enabled and function and function.ranges else "disabled")
        for b in res_buttons:
            b.state(["!disabled"] if enabled and function and function.resolution else ["disabled"])
        live_trigger = info and (info.get("trigger") or "—").upper() in ("AUTO", "—")
        trigger_button.state(["!disabled"] if enabled and info and not live_trigger else ["disabled"])

    def request(name, value):
        if not worker or not worker.is_alive() or state["pending"]:
            return
        state["pending"] = True
        set_controls(False)
        message(T["setting"].format(T["names"][name]))
        requests.put((name, value))

    def on_function(function):
        if function.token == state["function"] or state["pending"]:
            return
        if function.excites and not messagebox.askyesno(T["confirm"], T["excite_question"].format(function.label)):
            return
        request("function", function.token)

    def on_range(_):
        choice = range_var.get()
        function = lookup_function(state["function"])
        request("range", "AUTO" if choice == T["auto"] else function.ranges.index(choice))
    range_box.bind("<<ComboboxSelected>>", on_range)

    def on_resolution(index):
        if (state.get("status") or {}).get("resolution") != index:
            request("resolution", index)

    def show_status(info):
        state["status"] = info
        function = lookup_function(info["function"])
        state["function"] = function.token
        for token, b in fn_buttons.items():
            b.configure(style="FnOn.TButton" if token == function.token else "Fn.TButton")
        fn_label.config(text=function.token, fg=ACCENT)
        fn_name.config(text=function.label)
        excite_label.config(text="⚡ " + T["excites"] if function.excites else "")
        range_box.config(values=(T["auto"],) + function.ranges if function.ranges else ())
        rng, res = info["range"], info["resolution"]
        range_text = function.ranges[rng] if rng is not None and 0 <= rng < len(function.ranges) else ""
        range_var.set(range_text)
        res_text = RESOLUTIONS[res] if res is not None and 0 <= res < len(RESOLUTIONS) else ""
        for i, b in enumerate(res_buttons):
            b.configure(style="SegOn.TButton" if i == res and function.resolution else "Seg.TButton")
        trigger, calculate = (info.get("trigger") or "—").upper(), (info.get("calculate") or "none").lower()
        trigger_text.config(text=T["trigger_state"].format(trigger), fg=OK if trigger == "AUTO" else WARN)
        for widget in (badge_wait, badge_range, badge_res, badge_trigger, badge_interval, badge_rel, badge_calc):
            widget.pack_forget()
        if state["rel"] is not None:
            badge_rel.pack(side="left", padx=(0, 6))
        for widget, text in ((badge_calc, "" if calculate == "none" else T["badge_calc"].format(calculate)),
                             (badge_range, T["badge_range"].format(range_text) if range_text else ""),
                             (badge_res, T["badge_res"].format(res_text) if res_text else ""),
                             (badge_trigger, T["badge_trigger"].format(trigger)),
                             (badge_interval, T["badge_interval"].format(info.get("interval")) if info.get("interval") else "")):
            if text:
                widget.config(text=text)
                widget.pack(side="left", padx=(0, 6))
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
            messagebox.showerror(T["interval_title"], T["interval_error"])
            return
        if rows and not messagebox.askyesno(T["restart_title"], T["restart_question"].format(len(rows))):
            return
        rows.clear()
        series.clear()
        state["arrivals"].clear()
        state["error"] = False
        reset_stats(None)
        draw()
        stop.clear()
        while not requests.empty():
            requests.get_nowait()
        state["pending"] = False
        start_button.state(["disabled"])
        entry.state(["disabled"])
        stop_button.state(["!disabled"])
        pill("connecting")
        message(T["connecting"])
        worker = threading.Thread(target=run, args=(period,), daemon=True)
        worker.start()

    def halt():
        stop.set()
        requests.put(None)
        if worker and worker.is_alive():
            pill("stopping")
            message(T["stopping"])

    start_button.configure(command=start)
    stop_button.configure(command=halt)

    def export():
        if not rows:
            messagebox.showinfo(T["save_title"], T["no_data"])
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                            initialfile=f"dm3051-{datetime.now():%Y%m%d-%H%M%S}.csv")
        if path:
            try:
                with open(path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(FIELDS)
                    writer.writerows(list(rows))
                message(T["saved"].format(len(rows), path), "ok")
            except OSError as exc:
                messagebox.showerror(T["save_failed"], str(exc))

    def reset_stats(function):
        unit = lookup_function(function).unit if function else ""
        state["stats"] = Stats(function, unit)
        state["out"] = 0
        show_stats()

    def show_stats():
        stats, unit, off = state["stats"], state["stats"].unit, offset()
        if stats.n:
            n_digits, exponent = digits(), _exponent(max(abs(stats.min - off), abs(stats.max - off)))
            cells = tuple(" ".join(readout(v - off, unit, n_digits, exponent))
                                            for v in (stats.min, stats.max, stats.mean)) + (
                     format_value(stats.stdev, unit, 4) if stats.stdev is not None else "—",
                     format_value(stats.max - stats.min, unit, 4))
        else:
            cells = ("—",) * 5
        for widget, text in zip(stat_values, cells + (f"{state['out']} / {stats.overloads}",)):
            widget.config(text=text)
        stat_values[-1].config(fg=ERR if state["out"] or stats.overloads else TEXT)
        fit_stats()
        count_label.config(text=T["count"].format(stats.n + stats.overloads) if stats.n + stats.overloads else "")

    def fit_stats(_=None):
        """Shrink the statistics font until the longest value fits the narrowest card."""
        room = min(v.master.winfo_width() for v in stat_values) - 26
        if room < 20:
            return
        longest = max((v.cget("text") for v in stat_values), key=len)
        size = 13
        while size > 8 and tkfont.Font(root, family=mono, size=size).measure(longest) > room:
            size -= 1
        if stat_font.cget("size") != size:
            stat_font.configure(size=size)
    strip.bind("<Configure>", lambda e: root.after_idle(fit_stats))

    def fit_value(_=None):
        """Shrink the big readout until number and unit fit beside the limit chip."""
        # hero minus the chip column, both columns' padding (2 × 44), the border and the unit beside it
        room = hero.winfo_width() - right.winfo_width() - 92 - unit_label.winfo_reqwidth() - 12
        if room < 50:
            return
        size = 58
        while size > 30 and tkfont.Font(root, family=mono, size=size).measure(value_label.cget("text")) > room:
            size -= 2
        if big_font.cget("size") != size:
            big_font.configure(size=size)
        if badges.winfo_reqwidth() > room + unit_label.winfo_reqwidth() + 12 and badge_interval.winfo_ismapped():
            badge_interval.pack_forget()  # least important badge goes first on a narrow window
    hero.bind("<Configure>", lambda e: root.after_idle(fit_value))

    def show_reading():
        root.after_idle(fit_value)
        row = state["last"]
        if not row:
            value_label.config(text="------", fg=DIM)
            unit_label.config(text="")
            chip.config(text=T["chip"]["none"], fg=MUTED, bg=PANEL2)
            return
        lower, upper = limits()
        shown = None if row[2] is None else row[2] - offset()
        kind = limit_state(shown, lower, upper)
        if shown is None:
            value_label.config(text=T["ovld"], fg=WARN)
            unit_label.config(text="")
        else:
            number, unit = readout(shown, row[3], digits())
            value_label.config(text=number, fg=ERR if kind in ("low", "high") else TEXT)
            unit_label.config(text=unit)
        colours = {"none": (MUTED, PANEL2), "ok": (OK, "#0f2a20"), "low": (ERR, "#2c1418"),
                   "high": (ERR, "#2c1418"), "ovld": (WARN, "#2a2210")}
        fg, bg = colours[kind]
        chip.config(text=T["chip"][kind], fg=fg, bg=bg)

    def on_sample(row):
        rows.append(row)
        try:
            when = datetime.fromisoformat(row[0]).timestamp()
        except ValueError:
            when = time.time()
        series.append((when, row[1], row[2]))
        stats = state["stats"]
        if stats.function != row[1]:
            if stats.function is not None and state["rel"] is not None:
                state["rel"] = None  # A reference from another function is meaningless.
                refresh_rel()
            reset_stats(row[1])
            stats = state["stats"]
        stats.add(row[2])
        state["last"] = row
        lower, upper = limits()
        shown = None if row[2] is None else row[2] - offset()
        if limit_state(shown, lower, upper) in ("low", "high"):
            state["out"] += 1
        state["arrivals"].append(time.monotonic())
        show_reading()
        show_stats()
        last_label.config(text=T["last"].format(row[0][11:23]))
        if state["record"]:
            try:
                state["record"][1].writerow(row)
                state["record"][0].flush()
                state["recorded"] += 1
                rec_label.config(text=T["rec"].format(state["recorded"]))
            except OSError as exc:
                stop_recording()
                messagebox.showerror(T["record_failed"], str(exc))

    def show_info():
        arrivals = state["arrivals"]
        rate = (len(arrivals) - 1) / (arrivals[-1] - arrivals[0]) if len(arrivals) > 2 and arrivals[-1] > arrivals[0] else 0
        info_label.config(text=(T["rate"].format(rate) + "  ·  " if rate else "") + T["buffer"].format(len(rows)))

    def start_recording():
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                            title=T["record_title"],
                                            initialfile=f"dm3051-live-{datetime.now():%Y%m%d-%H%M%S}.csv")
        if not path:
            return
        f = None
        try:
            f = open(path, "w", newline="", encoding="utf-8")
            writer = csv.writer(f)
            writer.writerow(FIELDS)
            f.flush()
        except OSError as exc:
            if f:
                f.close()
            messagebox.showerror(T["record_failed"], str(exc))
            return
        state["record"], state["recorded"] = (f, writer, path), 0
        record_button.configure(text=T["record_stop"], style="Danger.TButton")
        state["record_path"] = path
        record_info.config(text=T["recording"].format(os.path.basename(path)), fg=ERR)
        rec_label.config(text=T["rec"].format(0))

    def stop_recording():
        if not state["record"]:
            return
        f, _, path = state["record"]
        state["record"] = None
        try:
            f.close()
        except OSError:
            pass
        record_button.configure(text=T["record"], style="TButton")
        record_info.config(text=T["recorded"].format(state["recorded"], os.path.basename(path)), fg=MUTED)
        rec_label.config(text="")

    def on_limits(*_):
        show_reading()
        draw()
    lower_var.trace_add("write", on_limits)
    upper_var.trace_add("write", on_limits)

    def drain():
        redraw = False
        while True:
            try:
                kind, data = events.get_nowait()
            except queue.Empty:
                break
            if kind == "identity":
                subtitle.config(text=data)
            elif kind == "status":
                state["pending"] = False
                show_status(data)
                show_reading()
                show_stats()
            elif kind == "unlock":
                state["pending"] = False
                set_controls("status" in state)
            elif kind in ("notice", "confirmed", "warn"):
                message(data, {"notice": "info", "confirmed": "ok", "warn": "warn"}[kind])
            elif kind == "paused":
                pill("paused")
                message(T["paused"].format(data), "warn")
                state["stale"] = True
            elif kind == "busy":
                pill("busy")
                message(T["busy"].format(data), "warn")
                state["stale"] = True
            elif kind == "sample":
                if state.get("stale") or not state.get("live"):
                    pill("live")
                    message(T["sampling"])
                    state["stale"], state["live"] = False, True
                on_sample(data)
                redraw = True
            elif kind == "error":
                state["error"] = True
                pill("error")
                message(T["error"].format(data), "err")
            elif kind == "done":
                state["live"] = False
                start_button.state(["!disabled"])
                entry.state(["!disabled"])
                stop_button.state(["disabled"])
                set_controls(False)
                stop_recording()
                if not state["error"]:
                    pill("idle")
                if stop.is_set() and not state["error"]:
                    message(T["stopped"].format(len(rows)))
        if redraw and not state["frozen"]:
            draw()
        show_info()
        if closing and (not worker or not worker.is_alive()):
            stop_recording()
            remember()
            root.destroy()
        else:
            root.after(100, drain)

    def remember():
        data = load_settings()
        data.update({"geometry": root.winfo_geometry(), "interval": interval.get(), "lower": lower_var.get(),
                     "upper": upper_var.get(), "window": state["window"], "hist": state["hist"]})
        save_settings(data)

    def close():
        nonlocal closing
        closing = True
        halt()

    def toggle_run(_=None):
        if worker and worker.is_alive():
            halt()
        else:
            start()

    root.bind("<F5>", toggle_run)
    root.bind("<Control-s>", lambda _: export())
    root.bind("<Control-r>", lambda _: stop_recording() if state["record"] else start_recording())
    root.bind("<Control-l>", lambda _: reset_stats(state["function"]))
    root.bind("<Control-q>", lambda _: close())
    root.protocol("WM_DELETE_WINDOW", close)
    set_window(state["window"])
    style_toggles()
    show_stats()
    drain()
    root.mainloop()
