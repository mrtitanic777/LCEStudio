#!/usr/bin/env python3
"""LCE Studio -- desktop GUI for editing Minecraft: Xbox 360 (LCE) saves.

Runs on the `lce` engine. Open a save (a Save*.bin folder or a savegame.dat),
edit inventory / blocks / entities / raw NBT / spawn, apply one-click tools
(inject Giant, Giant spawner, spawner blocks, Locked Chest), and Save -- writing
console-exact chunks (5-byte trailer) with an automatic .bak backup.

    python lce_studio.py [save-path]
"""
import os
import queue
import re
import sys
import threading
import traceback

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

from .world import World
from . import nbt as N
from . import viz as VIZ
try:
    from PIL import ImageTk
    _HAVE_PIL = True
except Exception:
    _HAVE_PIL = False

# a few friendly id->name hints (not exhaustive; ids still shown)
BLOCKS = {0: "Air", 1: "Stone", 2: "Grass", 3: "Dirt", 4: "Cobble", 5: "Planks",
          7: "Bedrock", 8: "Water", 10: "Lava", 12: "Sand", 17: "Wood", 18: "Leaves",
          20: "Glass", 41: "Gold", 42: "Iron", 45: "Brick", 46: "TNT", 49: "Obsidian",
          52: "Spawner", 57: "Diamond", 89: "Glowstone", 95: "LockedChest"}
ITEMS = {264: "Diamond", 265: "Iron", 266: "Gold", 276: "Diamond Sword",
         278: "Diamond Pickaxe", 46: "TNT", 52: "Spawner", 89: "Glowstone",
         41: "Gold Block", 57: "Diamond Block", 264: "Diamond"}


class _InputError(Exception):
    """A user-facing bad-input problem — shown as a friendly note, never a crash."""


class Studio(tk.Tk):
    THEMES = {
        "light": dict(BG="#f5f6f8", INK="#20262e", MUTED="#6b7785", LINE="#e2e5ea",
                      FIELD="#ffffff", TAB="#e6e9ee", BTN="#e7e9ee", BTN_HI="#dbdee4",
                      CANVAS="#1b1f26", SEL="#d7e3f2", ACCENT="#2e7d32", ACCENT_HI="#388e3c",
                      DISABLED="#9aa79b", MENU_BG="#ffffff", MENU_FG="#20262e"),
        "dark":  dict(BG="#20242b", INK="#e7ebf1", MUTED="#8d99a8", LINE="#333b46",
                      FIELD="#2b313b", TAB="#2b313b", BTN="#2f3742", BTN_HI="#3a434f",
                      CANVAS="#12151a", SEL="#33465e", ACCENT="#38a349", ACCENT_HI="#43bd56",
                      DISABLED="#5b6470", MENU_BG="#2b313b", MENU_FG="#e7ebf1"),
    }

    def __init__(self, initial=None):
        super().__init__()
        self.title("LCE Studio")
        self.geometry("1280x860")
        self.minsize(1000, 660)
        self.world = None
        self.path = None
        self._save_meta = None
        self._busy = False
        self._async_q = queue.Queue()
        self._hairlines = []
        self._menus = []
        self.theme = "light"
        self.status = tk.StringVar(value="")
        self._style = ttk.Style(self)
        try:
            self._style.theme_use("clam")
        except tk.TclError:
            pass
        self._apply_theme(self.theme)
        self._build_topbar()                            # File/View + tabs merged in one header
        self._build_body()
        self._build_statusbar()
        self._apply_theme(self.theme)                   # recolour menus/tab buttons now they exist
        self._update_chrome()
        self._poll_async()                              # main-thread completion pump
        if initial:
            self.after(120, lambda: self.load(initial))

    # ---------------------------------------------------------------- theming
    def _apply_theme(self, name):
        self.theme = name
        p = self.THEMES[name]
        self.BG, self.INK, self.MUTED, self.LINE = p["BG"], p["INK"], p["MUTED"], p["LINE"]
        self.ACCENT, self.ACCENT_HI = p["ACCENT"], p["ACCENT_HI"]
        st = self._style
        base = ("Segoe UI", 10)
        self.configure(background=p["BG"])
        for w in ("TFrame", "TLabelframe", "TNotebook", "TPanedwindow"):
            st.configure(w, background=p["BG"], borderwidth=0)
        st.configure("TLabel", background=p["BG"], foreground=p["INK"], font=base)
        for w in ("TCheckbutton", "TRadiobutton"):
            st.configure(w, background=p["BG"], foreground=p["INK"], font=base)
            st.map(w, background=[("active", p["BG"])], foreground=[("active", p["INK"])])
        st.configure("TLabelframe.Label", background=p["BG"], foreground=p["MUTED"],
                     font=("Segoe UI Semibold", 9))
        st.configure("TButton", padding=(12, 6), font=base, background=p["BTN"],
                     foreground=p["INK"], borderwidth=0)
        st.map("TButton", background=[("active", p["BTN_HI"]), ("disabled", p["BTN"])],
               foreground=[("disabled", p["MUTED"])])
        st.configure("TEntry", fieldbackground=p["FIELD"], foreground=p["INK"],
                     insertcolor=p["INK"], padding=4, borderwidth=1)
        st.configure("TCombobox", fieldbackground=p["FIELD"], foreground=p["INK"],
                     background=p["BTN"], arrowcolor=p["INK"])
        st.map("TCombobox", fieldbackground=[("readonly", p["FIELD"])])
        st.configure("TSpinbox", fieldbackground=p["FIELD"], foreground=p["INK"], arrowcolor=p["INK"])
        st.configure("TNotebook.Tab", padding=(18, 9), font=base, background=p["TAB"],
                     foreground=p["MUTED"], borderwidth=0)
        st.map("TNotebook.Tab", background=[("selected", p["BG"])], foreground=[("selected", p["INK"])])
        st.configure("TScale", background=p["BG"], troughcolor=p["TAB"])
        for sb in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
            st.configure(sb, background=p["BTN"], troughcolor=p["BG"], borderwidth=0, arrowcolor=p["MUTED"])
        st.configure("Treeview", background=p["FIELD"], fieldbackground=p["FIELD"],
                     foreground=p["INK"], borderwidth=0, rowheight=22)
        st.map("Treeview", background=[("selected", p["SEL"])], foreground=[("selected", p["INK"])])
        st.configure("Treeview.Heading", background=p["TAB"], foreground=p["INK"], borderwidth=0)
        # accent + semantic styles
        st.configure("Accent.TButton", foreground="#ffffff", background=p["ACCENT"],
                     font=("Segoe UI Semibold", 10), padding=(16, 7), borderwidth=0)
        st.map("Accent.TButton", background=[("active", p["ACCENT_HI"]), ("disabled", p["DISABLED"])])
        st.configure("Ghost.TButton", padding=(14, 7))
        st.configure("Card.TButton", padding=(7, 3), font=("Segoe UI", 9))
        st.map("Card.TButton", background=[("active", p["BTN_HI"])])
        st.configure("Section.TLabel", background=p["BG"], foreground=p["MUTED"],
                     font=("Segoe UI Semibold", 9))
        st.configure("Muted.TLabel", background=p["BG"], foreground=p["MUTED"])
        st.configure("H1.TLabel", background=p["BG"], foreground=p["INK"], font=("Segoe UI Light", 22))
        st.configure("Value.TLabel", background=p["BG"], foreground=p["INK"], font=("Segoe UI", 11))
        st.configure("Bar.TFrame", background=p["BG"])
        st.configure("Card.TLabelframe", padding=12)
        st.configure("TSeparator", background=p["LINE"])
        # merged-header widgets
        st.configure("Menu.TMenubutton", background=p["BG"], foreground=p["INK"],
                     padding=(10, 6), borderwidth=0, arrowcolor=p["BG"], font=base)
        st.map("Menu.TMenubutton", background=[("active", p["BTN_HI"])])
        st.configure("Tab.TButton", background=p["BG"], foreground=p["MUTED"],
                     padding=(13, 7), borderwidth=0, font=("Segoe UI", 10))
        st.map("Tab.TButton", background=[("active", p["BTN_HI"])], foreground=[("active", p["INK"])])
        st.configure("TabOn.TButton", background=p["BG"], foreground=p["ACCENT"],
                     padding=(13, 7), borderwidth=0, font=("Segoe UI Semibold", 10))
        st.map("TabOn.TButton", background=[("active", p["BG"]), ("!active", p["BG"])],
               foreground=[("active", p["ACCENT"])])
        # recolour widgets that don't follow ttk styles live
        for hr in self._hairlines:
            try: hr.configure(background=p["LINE"])
            except tk.TclError: pass
        for mn in self._menus:
            try: mn.configure(background=p["MENU_BG"], foreground=p["MENU_FG"],
                              activebackground=p["SEL"], activeforeground=p["INK"])
            except tk.TclError: pass
        for attr in ("map_canvas", "inv_grid"):
            c = getattr(self, attr, None)
            if c is not None:
                try: c.configure(background=p["CANVAS"], highlightthickness=0)
                except tk.TclError: pass

    def _toggle_theme(self):
        self._apply_theme("dark" if self.theme == "light" else "light")
        if self.world is not None:
            self.render_map()                            # re-render for the new canvas bg

    def _hr(self, parent):
        f = tk.Frame(parent, height=1, background=self.LINE)
        self._hairlines.append(f)
        return f

    # ------------------------------------------------ merged top header
    def _build_topbar(self):
        bar = ttk.Frame(self, style="Bar.TFrame", padding=(6, 4))
        bar.pack(side="top", fill="x")
        self._topbar = bar
        # File menu
        fm = tk.Menu(self, tearoff=0)
        fm.add_command(label="Open…", command=self.on_open, accelerator="Ctrl+O")
        fm.add_command(label="Save", command=self.on_save, accelerator="Ctrl+S")
        fm.add_command(label="Save As…", command=self.on_save_as)
        fm.add_command(label="Close save", command=self.on_close)
        fm.add_separator()
        fm.add_command(label="Quit", command=self.destroy)
        ttk.Menubutton(bar, text="File", menu=fm, style="Menu.TMenubutton").pack(side="left")
        # View menu
        vm = tk.Menu(self, tearoff=0)
        self._dark_var = tk.BooleanVar(value=(self.theme == "dark"))
        vm.add_checkbutton(label="Dark mode", variable=self._dark_var,
                           command=self._toggle_theme, accelerator="Ctrl+D")
        ttk.Menubutton(bar, text="View", menu=vm, style="Menu.TMenubutton").pack(side="left", padx=(2, 6))
        self._menus = [fm, vm]
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=(2, 8), pady=5)
        # right-hand save controls (packed only when a save is open)
        self.btn_save = ttk.Button(bar, text="Save", style="Accent.TButton", command=self.on_save)
        self.btn_close = ttk.Button(bar, text="Close", style="Ghost.TButton", command=self.on_close)
        self.ab_name = tk.StringVar(value="")
        self.ab_lbl = ttk.Label(bar, textvariable=self.ab_name, font=("Segoe UI Semibold", 10))
        # tab buttons live here (filled by _build_body)
        self._tabwrap = ttk.Frame(bar, style="Bar.TFrame")
        self._tabwrap.pack(side="left")
        # shortcuts
        self.bind("<Control-o>", lambda e: self.on_open())
        self.bind("<Control-s>", lambda e: self.on_save())
        self.bind("<Control-d>", lambda e: (self._dark_var.set(not self._dark_var.get()),
                                            self._toggle_theme()))

    def _select_tab(self, frame):
        self.nb.select(frame)
        for fr, b in self._tabbtns:
            b.configure(style="TabOn.TButton" if fr is frame else "Tab.TButton")

    def _build_statusbar(self):
        self._statusbar = ttk.Frame(self, style="Bar.TFrame")
        self.progress = ttk.Progressbar(self._statusbar, mode="indeterminate", length=160)
        ttk.Label(self._statusbar, textvariable=self.status, style="Muted.TLabel",
                  anchor="w").pack(side="left", fill="x", expand=True, padx=12, pady=3)
        self.status.trace_add("write", lambda *a: self._status_vis())
        self._status_vis()

    def _status_vis(self):
        show = bool(self.status.get().strip()) or self._busy
        mapped = self._statusbar.winfo_ismapped()
        if show and not mapped:
            self._statusbar.pack(side="bottom", fill="x")
        elif not show and mapped:
            self._statusbar.pack_forget()

    # ------------------------------------------------------- async plumbing
    def _run_async(self, fn, on_done=None, msg="Working…"):
        """Run fn() on a worker thread; the worker only touches a thread-safe queue
        (never Tk), and a main-thread poller delivers the result. Keeps the window
        responsive during long saves / renders / conversions."""
        if self._busy:
            messagebox.showinfo("Please wait", "Another operation is still running.")
            return
        self._set_busy(True, msg)

        def worker():
            try:
                res = fn()
                self._post(lambda: self._finish(res, on_done, None))
            except Exception as e:                       # marshal the error, no Tk here
                self._post(lambda e=e: self._finish(None, None, e))

        threading.Thread(target=worker, daemon=True).start()

    def _post(self, cb):
        """Thread-safe: enqueue a callable to run on the main Tk thread."""
        self._async_q.put(cb)

    def _poll_async(self):
        """Runs on the main (Tk) thread; drains completed background jobs safely."""
        try:
            while True:
                cb = self._async_q.get_nowait()
                try:
                    cb()
                except Exception as e:
                    self._err(e)
        except queue.Empty:
            pass
        self.after(60, self._poll_async)

    def _finish(self, res, on_done, err):
        self._set_busy(False)
        if err is not None:
            self._err(err)
        elif on_done is not None:
            try:
                on_done(res)
            except Exception as e:
                self._err(e)

    def _set_busy(self, busy, msg=""):
        self._busy = busy
        if busy:
            self.status.set(msg)                          # trace shows the status bar
            self.progress.configure(mode="indeterminate", maximum=100)  # start as a spinner
            self.progress.pack(side="right", padx=10, pady=2)
            self.progress.start(12)
            self.config(cursor="watch")
            self.btn_save.state(["disabled"])
        else:
            self.progress.stop()
            self.progress.pack_forget()
            self.config(cursor="")
            if self.world is not None:
                self.btn_save.state(["!disabled"])
            self._status_vis()

    def _progress(self, done, total, text=None):
        """Thread-safe progress reporter handed to worker threads and heavy functions
        (World.save, converters, relight…). `total>0` shows a determinate %-bar; a
        `total<=0` phase keeps the spinner but updates the label. Marshalled onto the
        Tk thread so callers can invoke it from any thread."""
        self._post(lambda: self._apply_progress(done, total, text))

    def _apply_progress(self, done, total, text):
        if not self._busy:
            return
        try:
            if total and total > 0:                      # determinate: flip to a %-bar
                if str(self.progress.cget("mode")) != "determinate":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", maximum=100)
                self.progress["value"] = max(0.0, min(100.0, 100.0 * done / total))
            else:                                        # phase with no count -> spinner
                if str(self.progress.cget("mode")) != "indeterminate":
                    self.progress.configure(mode="indeterminate")
                    self.progress.start(12)
        except tk.TclError:
            pass
        if text is not None:
            self.status.set(text)

    def _logcb(self):
        """A log(message) callback for heavy functions (repair/relight/convert/…) that
        surfaces each milestone as a live status phase instead of a silent spinner."""
        return lambda m: self._progress(0, 0, str(m))

    def _update_chrome(self):
        """The Save/Close controls (right of the merged header) appear only when a
        save is open; the window title carries the name."""
        if self.world is None:
            self.title("LCE Studio")
            for w in (self.btn_save, self.btn_close, self.ab_lbl):
                w.pack_forget()
            self.btn_save.state(["disabled"])
        else:
            nm = os.path.basename(str(self.path).rstrip("/\\")) or str(self.path)
            self.title("LCE Studio  —  %s" % nm)
            self.ab_name.set(nm)
            if not self.btn_save.winfo_ismapped():
                self.btn_save.pack(side="right", padx=(6, 2))
                self.btn_close.pack(side="right", padx=(0, 6))
                self.ab_lbl.pack(side="right", padx=12)
            if not self._busy:
                self.btn_save.state(["!disabled"])

    def on_close(self):
        """Close the current save and return to the welcome screen."""
        if self._busy:
            return
        self.world = None
        self.path = None
        self.status.set("")
        self._update_chrome()
        self._show_overview(False)

    # ---------------------------------------------------------------- body
    def _build_body(self):
        self._style.layout("Tabless.TNotebook.Tab", [])     # hide the native tab strip
        self.nb = ttk.Notebook(self, style="Tabless.TNotebook")
        self.nb.pack(fill="both", expand=True, padx=6, pady=(2, 2))
        self._tabbtns = []
        specs = [("Library", "tab_library"), ("Overview", "tab_overview"), ("Map", "tab_map"),
                 ("Inventory", "tab_inv"), ("Players", "tab_players"), ("Blocks", "tab_blocks"),
                 ("Entities", "tab_ent"), ("NBT", "tab_nbt"), ("Tools", "tab_tools"),
                 ("Convert", "tab_convert"), ("Import / Export", "tab_recover")]
        for label, attr in specs:
            fr = ttk.Frame(self.nb)
            self.nb.add(fr, text=label)
            setattr(self, attr, fr)
            b = ttk.Button(self._tabwrap, text=label, style="Tab.TButton",
                           command=lambda f=fr: self._select_tab(f))
            b.pack(side="left")
            self._tabbtns.append((fr, b))
        self._build_library(); self._build_overview(); self._build_map(); self._build_inv()
        self._build_players(); self._build_blocks(); self._build_ent(); self._build_nbt()
        self._build_tools(); self._build_convert(); self._build_io()
        self._select_tab(self.tab_library)              # the gallery is the front-door
        self.after(400, self._lib_scan)                 # populate the Library shortly after launch

    # ---------------------------------------------------------------- Overview
    def _build_overview(self):
        wrap = ttk.Frame(self.tab_overview)
        wrap.pack(fill="both", expand=True)

        # --- empty state: one centered block ---
        self.ov_welcome = ttk.Frame(wrap)
        wc = ttk.Frame(self.ov_welcome)
        wc.place(relx=0.5, rely=0.44, anchor="center")
        ttk.Label(wc, text="Welcome to LCE Studio", style="H1.TLabel", anchor="center").pack()
        ttk.Label(wc, style="Muted.TLabel", anchor="center", justify="center",
                  text="Edit and convert Minecraft: Xbox 360 (Legacy Console) worlds.").pack(
            pady=(8, 26))
        row = ttk.Frame(wc); row.pack()
        ttk.Button(row, text="Open a save…", style="Accent.TButton", width=17,
                   command=self.on_open).pack(side="left", padx=8)
        ttk.Button(row, text="Convert a world  →", width=18,
                   command=lambda: self.nb.select(self.tab_convert)).pack(side="left", padx=8)
        ttk.Label(wc, style="Muted.TLabel", anchor="center", justify="center", wraplength=470,
                  text="You can also bring in a .zip, an STFS package, or a Save*.bin "
                       "from the Import / Export tab.").pack(pady=(28, 0))

        # --- info: a dashboard that fills the page ---
        self.ov_info = ttk.Frame(wrap)
        pad = ttk.Frame(self.ov_info, padding=(34, 28)); pad.pack(fill="both", expand=True)

        head = ttk.Frame(pad); head.pack(fill="x", anchor="n")
        self.ov_thumb = ttk.Label(head, style="Muted.TLabel")
        self.ov_thumb.pack(side="left", padx=(0, 22), anchor="n")
        htxt = ttk.Frame(head); htxt.pack(side="left", fill="x", expand=True, anchor="n")
        self.ov_title = tk.StringVar(value="")
        ttk.Label(htxt, textvariable=self.ov_title, style="H1.TLabel").pack(anchor="w")
        self.ov_summary = tk.StringVar(value="")
        ttk.Label(htxt, textvariable=self.ov_summary, style="Value.TLabel",
                  foreground=self.ACCENT).pack(anchor="w", pady=(4, 4))
        self.ov_sub = tk.StringVar(value="")
        ttk.Label(htxt, textvariable=self.ov_sub, style="Muted.TLabel", wraplength=760,
                  justify="left").pack(anchor="w")

        self._hr(pad).pack(fill="x", pady=22)

        cols = ttk.Frame(pad); cols.pack(fill="x", anchor="n")
        self.ov = {}

        def section(title, fields, colidx):
            c = ttk.Frame(cols); c.grid(row=0, column=colidx, sticky="nw", padx=(0, 56))
            ttk.Label(c, text=title, style="Section.TLabel").grid(
                row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
            for i, (disp, key) in enumerate(fields, start=1):
                ttk.Label(c, text=disp, style="Muted.TLabel").grid(
                    row=i, column=0, sticky="w", padx=(0, 16), pady=4)
                v = tk.StringVar(); self.ov[key] = v
                ttk.Label(c, textvariable=v, style="Value.TLabel").grid(row=i, column=1, sticky="w", pady=4)

        section("WORLD", [("Name", "Name"), ("Seed", "Seed"), ("Day", "Day"),
                          ("Weather", "Weather"), ("Last played", "Played")], 0)
        section("PLAYER", [("Position", "Player"), ("Health", "Health"),
                           ("Inventory", "Items"), ("Dimensions", "Dims")], 1)
        section("STORAGE", [("Console", "Console"), ("Format", "Format"), ("Profile", "Profile"),
                            ("Regions", "Regions"), ("Chunks", "Chunks"), ("On disk", "Size")], 2)

        self._hr(pad).pack(fill="x", pady=22)
        ttk.Label(pad, text="SPAWN POINT", style="Section.TLabel").pack(anchor="w")
        sp = ttk.Frame(pad); sp.pack(anchor="w", pady=(10, 0))
        self.spawn_vars = (tk.StringVar(), tk.StringVar(), tk.StringVar())
        for i, (lbl, var) in enumerate(zip("XYZ", self.spawn_vars)):
            ttk.Label(sp, text=lbl, style="Muted.TLabel").grid(
                row=0, column=2 * i, padx=(0 if i == 0 else 16, 4))
            ttk.Entry(sp, textvariable=var, width=8, justify="center").grid(row=0, column=2 * i + 1)
        ttk.Button(sp, text="Set spawn", command=self.on_set_spawn).grid(row=0, column=6, padx=(20, 0))

        self.filelist = None                            # (VFS file list retired from Overview)
        self._show_overview(False)

    def _show_overview(self, have):
        self.ov_info.pack(fill="both", expand=True) if have else self.ov_info.pack_forget()
        self.ov_welcome.pack_forget() if have else self.ov_welcome.pack(fill="both", expand=True)

    # ---------------------------------------------------------------- Map
    def _build_map(self):
        f = self.tab_map
        ctl = ttk.Frame(f); ctl.pack(fill="x", padx=6, pady=4)
        self.map_mode = tk.StringVar(value="Surface")
        ttk.Radiobutton(ctl, text="Surface", variable=self.map_mode, value="Surface",
                        command=self.render_map).pack(side="left")
        ttk.Radiobutton(ctl, text="Slice @ Y", variable=self.map_mode, value="Slice",
                        command=self.render_map).pack(side="left")
        self.map_y = tk.IntVar(value=64)
        ttk.Scale(ctl, from_=0, to=127, variable=self.map_y, orient="horizontal", length=140,
                  command=lambda e: self._map_slice_live()).pack(side="left", padx=6)
        self.map_ylbl = tk.StringVar(value="Y=64")
        ttk.Label(ctl, textvariable=self.map_ylbl, width=6).pack(side="left")
        self.map_fit = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctl, text="Fit", variable=self.map_fit,
                        command=self.render_map).pack(side="left", padx=(12, 4))
        ttk.Label(ctl, text="Zoom").pack(side="left", padx=(2, 2))
        self.map_scale = tk.IntVar(value=2)
        ttk.Spinbox(ctl, from_=1, to=8, width=3, textvariable=self.map_scale,
                    command=self._zoom_manual).pack(side="left")
        ttk.Label(ctl, text="World").pack(side="left", padx=(10, 2))
        self.map_size = tk.IntVar(value=54)
        ttk.Spinbox(ctl, from_=16, to=128, increment=2, width=4, textvariable=self.map_size,
                    command=self.render_map).pack(side="left")
        self.map_grid = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctl, text="Grid", variable=self.map_grid,
                        command=self.render_map).pack(side="left", padx=(10, 2))
        self.map_boundary = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctl, text="Boundary", variable=self.map_boundary,
                        command=self.render_map).pack(side="left")
        ttk.Button(ctl, text="Render", command=self.render_map).pack(side="left", padx=8)
        ttk.Button(ctl, text="🔍 Find structures", style="Accent.TButton",
                   command=self.on_find_pois).pack(side="left")
        ttk.Button(ctl, text="📐 3D portrait", command=self.on_export_iso).pack(side="left", padx=(4, 0))
        self.map_read = tk.StringVar(value="click the map to inspect  ·  double-click to edit that block")
        ttk.Label(ctl, textvariable=self.map_read).pack(side="left", padx=10)

        wrap = ttk.Frame(f); wrap.pack(fill="both", expand=True, padx=6, pady=4)
        self.map_canvas = tk.Canvas(wrap, background="#20242c", highlightthickness=0)
        hb = ttk.Scrollbar(wrap, orient="horizontal", command=self.map_canvas.xview)
        vb = ttk.Scrollbar(wrap, orient="vertical", command=self.map_canvas.yview)
        self.map_canvas.configure(xscrollcommand=hb.set, yscrollcommand=vb.set)
        self.map_canvas.grid(row=0, column=0, sticky="nsew")
        vb.grid(row=0, column=1, sticky="ns"); hb.grid(row=1, column=0, sticky="ew")
        # POI panel (right column) -- structures list + filter
        poip = ttk.Frame(wrap, width=250); poip.grid(row=0, column=2, rowspan=2, sticky="ns", padx=(6, 0))
        poip.grid_propagate(False)
        top = ttk.Frame(poip); top.pack(fill="x")
        ttk.Label(top, text="Structures", style="Section.TLabel").pack(side="left")
        self.poi_filter = tk.StringVar(value="all")
        fcb = ttk.Combobox(top, textvariable=self.poi_filter, width=10, state="readonly",
                           values=("all", "dungeon", "loot", "stronghold", "portal", "build", "sign"))
        fcb.pack(side="right"); fcb.bind("<<ComboboxSelected>>", lambda e: self._poi_fill_list())
        self.poi_tree = ttk.Treeview(poip, columns=("t", "pos"), show="headings", height=20)
        self.poi_tree.heading("t", text="What"); self.poi_tree.heading("pos", text="X / Z")
        self.poi_tree.column("t", width=150); self.poi_tree.column("pos", width=88)
        self.poi_tree.pack(fill="both", expand=True, pady=4)
        self.poi_tree.bind("<<TreeviewSelect>>", self._on_poi_pick)
        self._pois = []
        wrap.rowconfigure(0, weight=1); wrap.columnconfigure(0, weight=1)
        self.map_canvas.bind("<Button-1>", self.on_map_click)
        self.map_canvas.bind("<Double-Button-1>", self.on_map_dblclick)
        self.map_canvas.bind("<Configure>", self._on_canvas_resize)
        self._map_photo = None
        self._map_origin = (0, 0)
        self._map_place = (0, 0)
        self._resize_job = None

    def _zoom_manual(self):
        self.map_fit.set(False)
        self.render_map()

    def _on_canvas_resize(self, _ev):
        # debounce; only auto-refit when Fit is on
        if self.world is None or not self.map_fit.get():
            return
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(120, self.render_map)

    def _map_slice_live(self):
        self.map_ylbl.set("Y=%d" % self.map_y.get())
        if self.world is not None and self.map_mode.get() == "Slice":
            self.render_map()

    def render_map(self):
        """Render the map on a worker thread (keeps the UI smooth); coalesces
        rapid re-render requests instead of piling them up."""
        if self.world is None or not _HAVE_PIL:
            return
        if getattr(self, "_rendering", False):
            self._render_pending = True
            return
        try:                                             # snapshot UI params on this thread
            size = max(16, self._iv(self.map_size, 54))
            mode = self.map_mode.get(); ycut = self._iv(self.map_y, 64)
            cw = self.map_canvas.winfo_width(); ch = self.map_canvas.winfo_height()
            fit = self.map_fit.get(); manual = max(1, self._iv(self.map_scale, 2))
            grid = self.map_grid.get(); boundary = self.map_boundary.get()
        except Exception:
            return
        if fit and (cw <= 1 or ch <= 1):                 # canvas not laid out yet
            self.after(80, self.render_map); return
        self._rendering = True
        self.status.set("Rendering map…")
        self._map_prog = {}                              # VW.load fills phase/done/total here
        w = self.world

        def poll_prog():                                 # surface the slow first-time world load
            if not self._rendering:
                return
            p = self._map_prog
            ph, d, t = p.get("phase"), p.get("done"), p.get("total")
            if ph:
                self.status.set("%s… %d/%d regions" % (ph, (d or 0) + 1, t) if t
                                else "%s…" % ph)
            self.after(150, poll_prog)
        self.after(150, poll_prog)

        def work():
            from . import atlas
            vw = self._map_world(self._map_prog)            # format-complete (all TU formats)
            img = atlas.render_slice(vw, ycut) if mode == "Slice" else atlas.render_world(vw)
            x0, z0 = atlas.world_origin(vw)
            scale = max(0.1, min(min(cw / img.width, ch / img.height), 12)) if fit else manual
            ents = [(x, z) for (_k, _n, x, _y, z) in vw.entities]
            big = VIZ.overlay(img, x0, z0, spawn=w.get_spawn(), player=w.get_player_pos(),
                              entities=ents, scale=scale, grid=grid, boundary=boundary, size=size)
            return big, x0, z0, scale

        def done(res):
            big, x0, z0, scale = res
            self._map_photo = ImageTk.PhotoImage(big)
            self._map_origin = (x0, z0); self._map_scale_used = scale
            ix = max(0, (cw - big.width) // 2); iy = max(0, (ch - big.height) // 2)
            self._map_place = (ix, iy)
            self.map_canvas.delete("all")
            self.map_canvas.create_image(ix, iy, anchor="nw", image=self._map_photo)
            self.map_canvas.configure(scrollregion=(0, 0, max(cw, ix + big.width),
                                                    max(ch, iy + big.height)))
            self._draw_poi_markers()
            self.status.set("Map rendered.")
            self._rendering = False
            if getattr(self, "_render_pending", False):
                self._render_pending = False
                self.after(10, self.render_map)

        def worker():
            try:
                r = work()
                self._post(lambda: done(r))
            except Exception as e:
                def fail(e=e):
                    self._rendering = False
                    self.status.set("Map render failed.")
                    self._err(e)
                self._post(fail)

        threading.Thread(target=worker, daemon=True).start()

    def _map_world(self, progress=None):
        """Cached view3d World for the map (handles EVERY chunk format, unlike the
        engine's region reader). Loaded once per save (the first render is slow, so a
        progress dict can be passed to surface region-by-region load progress)."""
        from .view3d.world import World as VW
        if getattr(self, "_map_vw", None) is None or getattr(self, "_map_vw_key", None) != self.path:
            self._map_vw = VW.load(self.path, progress=progress)
            self._map_vw_key = self.path
        return self._map_vw

    def _canvas_to_world(self, ev):
        ix, iy = getattr(self, "_map_place", (0, 0))
        cx = self.map_canvas.canvasx(ev.x) - ix
        cy = self.map_canvas.canvasy(ev.y) - iy
        s = getattr(self, "_map_scale_used", 1)
        x0, z0 = self._map_origin
        return int(x0 + cx / s), int(z0 + cy / s)

    def on_map_click(self, ev):
        if self.world is None:
            return
        wx, wz = self._canvas_to_world(ev)
        try:
            top = self.world.chunk(wx >> 4, wz >> 4)
            info = ""
            if top:
                ys = top.top_solid(wx, wz)
                info = "  top solid y=%d id=%d" % (ys, top.get_block(wx, ys, wz))
            self.map_read.set("world (x=%d, z=%d)%s" % (wx, wz, info))
        except Exception:
            self.map_read.set("world (x=%d, z=%d)" % (wx, wz))

    def on_map_dblclick(self, ev):
        wx, wz = self._canvas_to_world(ev)
        self.blk["x"].set(str(wx)); self.blk["z"].set(str(wz))
        c = self.world.chunk(wx >> 4, wz >> 4) if self.world else None
        self.blk["y"].set(str(c.top_solid(wx, wz) if c else 64))
        self.nb.select(self.tab_blocks)
        self.on_block_get()

    # ---- structure / POI finder ----
    _POI_STYLE = {"dungeon": ("#e23b3b", "D"), "loot": ("#f0b429", "$"),
                  "stronghold": ("#9b59d0", "S"), "portal": ("#c04bd8", "P"),
                  "build": ("#3aa0ff", "B"), "sign": ("#8a9099", "•")}

    def on_find_pois(self):
        if not self._guard():
            return
        from . import poi

        def work():
            return poi.find_pois(self.world, log=self._logcb())

        def done(res):
            self._pois = res
            self._poi_fill_list()
            self._draw_poi_markers()
            from collections import Counter
            by = Counter(p["type"] for p in res)
            self.status.set("Found %d structures: %s" % (len(res),
                            ", ".join("%d %s" % (n, t) for t, n in by.most_common())))
        self._run_async(work, on_done=done, msg="Scanning for structures…")

    def _poi_fill_list(self):
        if not hasattr(self, "poi_tree"):
            return
        self.poi_tree.delete(*self.poi_tree.get_children())
        flt = self.poi_filter.get()
        for i, p in enumerate(self._pois):
            if flt != "all" and p["type"] != flt:
                continue
            _, mark = self._POI_STYLE.get(p["type"], ("#fff", "?"))
            self.poi_tree.insert("", "end", iid=str(i),
                                 values=("%s %s" % (mark, p["label"]), "%d, %d" % (p["x"], p["z"])))

    def _world_to_canvas(self, wx, wz):
        ix, iy = getattr(self, "_map_place", (0, 0))
        s = getattr(self, "_map_scale_used", 1)
        x0, z0 = getattr(self, "_map_origin", (0, 0))
        return ix + (wx - x0) * s, iy + (wz - z0) * s

    def _draw_poi_markers(self):
        c = self.map_canvas
        c.delete("poi")
        if not getattr(self, "_pois", None):
            return
        flt = self.poi_filter.get()
        sel = self.poi_tree.selection()
        selid = int(sel[0]) if sel else -1
        for i, p in enumerate(self._pois):
            if flt != "all" and p["type"] != flt:
                continue
            if p.get("dim", 0) != 0:                       # markers are overworld-only
                continue
            col, mark = self._POI_STYLE.get(p["type"], ("#fff", "?"))
            px, py = self._world_to_canvas(p["x"], p["z"])
            r = 6 if i == selid else 4
            c.create_oval(px - r, py - r, px + r, py + r, fill=col, outline="#000",
                          width=2 if i == selid else 1, tags="poi")
            if i == selid:
                c.create_text(px, py - 12, text=p["label"][:28], fill="#fff",
                              font=("Segoe UI", 8, "bold"), tags="poi")

    def _on_poi_pick(self, ev=None):
        sel = self.poi_tree.selection()
        if not sel:
            return
        p = self._pois[int(sel[0])]
        self._draw_poi_markers()
        px, py = self._world_to_canvas(p["x"], p["z"])       # scroll it into view
        try:
            reg = self.map_canvas.cget("scrollregion").split()
            self.map_canvas.xview_moveto(max(0, (px - 200)) / max(1, float(reg[2])))
            self.map_canvas.yview_moveto(max(0, (py - 150)) / max(1, float(reg[3])))
        except Exception:
            pass
        self.map_read.set("%s  @  X %d  Y %d  Z %d  (dim %d)" %
                          (p["label"], p["x"], p["y"], p["z"], p.get("dim", 0)))

    def on_export_iso(self):
        if not self._guard():
            return
        from . import iso
        out = filedialog.asksaveasfilename(
            title="Save isometric portrait", defaultextension=".png",
            filetypes=[("PNG image", "*.png")],
            initialfile=(self.world.name or "world") + "_iso.png")
        if not out:
            return

        def work():
            vw = self._map_world()                   # view3d numpy world (surface heights)
            img = iso.render_iso(vw, tile=12, log=self._logcb())
            img.save(out)
            return out, img.width, img.height

        def done(r):
            path, w, h = r
            self.status.set("Saved isometric portrait %dx%d -> %s" % (w, h, path))
            if messagebox.askyesno("3D portrait", "Saved %d×%d portrait.\n\nOpen it now?" % (w, h)):
                try:
                    import os
                    os.startfile(path)
                except Exception:
                    pass
        self._run_async(work, on_done=done, msg="Rendering isometric portrait…")

    # ---------------------------------------------------------------- Inventory
    def _build_inv(self):
        wrap = ttk.Frame(self.tab_inv, padding=(14, 12)); wrap.pack(fill="both", expand=True)

        # LEFT -- the slot grid is the main, click-to-edit view
        gcol = ttk.Frame(wrap); gcol.pack(side="left", fill="both", expand=True)
        ttk.Label(gcol, text="INVENTORY   ·   click a slot to edit",
                  style="Section.TLabel").pack(anchor="w", pady=(0, 8))
        self.inv_grid = tk.Canvas(gcol, background=self.THEMES[self.theme]["CANVAS"],
                                  highlightthickness=0)
        self.inv_grid.pack(fill="both", expand=True)
        self.inv_grid.bind("<Configure>", lambda e: self._draw_inv_grid())
        self.inv_grid.bind("<Button-1>", self._on_grid_click)
        self._grid_cells = {}
        self._grid_sel = None

        # RIGHT -- a clean editor for the selected slot
        side = ttk.Frame(wrap, width=270); side.pack(side="right", fill="y", padx=(22, 0))
        side.pack_propagate(False)
        self.inv_slotlbl = tk.StringVar(value="No slot selected")
        ttk.Label(side, textvariable=self.inv_slotlbl, style="H1.TLabel",
                  font=("Segoe UI Light", 18)).pack(anchor="w")
        self.inv_itemname = tk.StringVar(value="Click a slot in the grid to edit it.")
        ttk.Label(side, textvariable=self.inv_itemname, style="Muted.TLabel",
                  wraplength=250, justify="left").pack(anchor="w", pady=(2, 18))
        self.add_id = tk.StringVar(); self.add_cnt = tk.StringVar(value="1")
        self.add_dmg = tk.StringVar(value="0")
        irow = ttk.Frame(side); irow.pack(fill="x", pady=4)
        ttk.Label(irow, text="Item", style="Muted.TLabel", width=8).pack(side="left")
        self._id_search(irow, self.add_id, "item", width=20).pack(side="left")
        for lbl, var in (("Count", self.add_cnt), ("Damage", self.add_dmg)):
            row = ttk.Frame(side); row.pack(fill="x", pady=4)
            ttk.Label(row, text=lbl, style="Muted.TLabel", width=8).pack(side="left")
            ttk.Entry(row, textvariable=var, width=12).pack(side="left")
        br = ttk.Frame(side); br.pack(anchor="w", pady=(16, 0))
        ttk.Button(br, text="Set slot", style="Accent.TButton", command=self.on_inv_add).pack(side="left")
        ttk.Button(br, text="Remove", command=self.on_inv_remove).pack(side="left", padx=8)
        self._hr(side).pack(fill="x", pady=20)
        self.inv_summary = tk.StringVar(value="")
        ttk.Label(side, textvariable=self.inv_summary, style="Muted.TLabel").pack(anchor="w", pady=(0, 12))
        ttk.Button(side, text="Clear all items", command=self.on_inv_clear).pack(anchor="w")

        # ---- player stats + profile ----
        self._hr(side).pack(fill="x", pady=16)
        ttk.Label(side, text="PLAYER", style="Section.TLabel").pack(anchor="w", pady=(0, 6))
        self.profile_var = tk.StringVar()
        prow = ttk.Frame(side); prow.pack(fill="x", pady=2)
        ttk.Label(prow, text="Profile", style="Muted.TLabel", width=10).pack(side="left")
        self.profile_cb = ttk.Combobox(prow, textvariable=self.profile_var, state="readonly", width=16)
        self.profile_cb.pack(side="left")
        self.profile_cb.bind("<<ComboboxSelected>>", self.on_profile_switch)
        self.ps_vars = {}
        for lbl, key in (("Health 0-20", "Health"), ("Food 0-20", "foodLevel"),
                         ("XP level", "XpLevel"), ("Air", "Air")):
            row = ttk.Frame(side); row.pack(fill="x", pady=3)
            ttk.Label(row, text=lbl, style="Muted.TLabel", width=10).pack(side="left")
            v = tk.StringVar(); self.ps_vars[key] = v
            ttk.Entry(row, textvariable=v, width=8).pack(side="left")
        ttk.Button(side, text="Set stats", style="Accent.TButton",
                   command=self.on_player_stats_set).pack(anchor="w", pady=(8, 0))

    def _on_grid_click(self, ev):
        for slot, (x0, y0, x1, y1) in self._grid_cells.items():
            if x0 <= ev.x <= x1 and y0 <= ev.y <= y1:
                self._select_slot(slot)
                return

    def _select_slot(self, slot):
        self._grid_sel = slot
        self.inv_slotlbl.set("Slot %d%s" % (slot, "  · hotbar" if slot < 9 else ""))
        it = next((i for i in (self.world.inventory() if self.world else []) if i["Slot"] == slot), None)
        if it:
            self.add_id.set(str(it["id"])); self.add_cnt.set(str(it["Count"]))
            self.add_dmg.set(str(it["Damage"]))
            from . import names as NM
            self.inv_itemname.set(NM.name_for(it["id"], "item") or "Item id %d" % it["id"])
        else:
            self.add_id.set(""); self.add_cnt.set("1"); self.add_dmg.set("0")
            self.inv_itemname.set("Empty — enter an item id and Set slot to fill it.")
        self._draw_inv_grid()

    def _draw_inv_grid(self):
        c = self.inv_grid
        c.delete("all"); self._grid_cells = {}
        cw, chh = c.winfo_width(), c.winfo_height()
        if cw < 30 or chh < 30:
            return
        cols, pad, gap = 9, 8, 0.6
        cell = max(20, min((cw - 2 * pad) / cols, (chh - 2 * pad) / (3 + 1 + gap)))
        gx0 = (cw - cell * cols) / 2
        gy0 = pad
        by_slot = {it["Slot"]: it for it in (self.world.inventory() if self.world else [])}
        fs = lambda k: ("TkDefaultFont", max(6, int(cell * k)))

        def draw(slot, col, rowf):
            x, y = gx0 + col * cell, gy0 + rowf * cell
            it = by_slot.get(slot)
            fill = "#555"
            if it:
                rgb = VIZ.BLOCK_RGB.get(it["id"]) or (90 + (it["id"] * 37) % 140,
                                                      90 + (it["id"] * 61) % 140,
                                                      90 + (it["id"] * 17) % 140)
                fill = "#%02x%02x%02x" % rgb
            selp = (slot == self._grid_sel)
            c.create_rectangle(x, y, x + cell - 2, y + cell - 2,
                               fill=fill, outline="#ffd24a" if selp else "#222",
                               width=3 if selp else 1)
            c.create_text(x + 3, y + 2, anchor="nw", text=str(slot), fill="#bbb", font=fs(0.15))
            if it:
                c.create_text(x + (cell - 2) / 2, y + (cell - 2) / 2, text=str(it["id"]),
                              fill="#fff", font=fs(0.26) + ("bold",))
                c.create_text(x + cell - 4, y + cell - 4, anchor="se", text="x%s" % it["Count"],
                              fill="#ffe", font=fs(0.17))
            self._grid_cells[slot] = (x, y, x + cell, y + cell)

        for i in range(27):
            draw(9 + i, i % 9, i // 9)            # main inventory, rows 0..2
        for i in range(9):
            draw(i, i, 3 + gap)                   # hotbar row, below a gap

    # ---------------------------------------------------------------- Players
    def _build_players(self):
        f = self.tab_players
        bar = ttk.Frame(f); bar.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Button(bar, text="Refresh", command=self._players_refresh).pack(side="left")
        ttk.Button(bar, text="Add (clone)", command=self.on_player_add).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Duplicate", command=self.on_player_duplicate).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Rename…", command=self.on_player_rename).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Remove", command=self.on_player_remove).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Import from save…", command=self.on_player_import).pack(side="left", padx=(6, 0))

        cols = ("xuid", "pos", "hp", "spawn", "items", "gt")
        self.pl_tree = ttk.Treeview(f, columns=cols, show="headings", height=12, selectmode="extended")
        for c, t, wd in (("xuid", "XUID (account)", 210), ("pos", "Position", 150),
                         ("hp", "HP", 45), ("spawn", "Bed spawn", 130), ("items", "Items", 50),
                         ("gt", "Mode", 55)):
            self.pl_tree.heading(c, text=t); self.pl_tree.column(c, width=wd)
        self.pl_tree.pack(fill="both", expand=True, padx=8, pady=4)
        self.pl_tree.tag_configure("active", foreground="#207a3c")

        # ---- position / spawn editor ----
        pe = ttk.LabelFrame(f, text="Set position + bed spawn")
        pe.pack(fill="x", padx=8, pady=6)
        self.pl_xyz = {k: tk.StringVar() for k in ("x", "y", "z")}
        for i, k in enumerate(("x", "y", "z")):
            ttk.Label(pe, text=k.upper()).grid(row=0, column=2 * i, padx=(8, 2), pady=6)
            ttk.Entry(pe, textvariable=self.pl_xyz[k], width=8).grid(row=0, column=2 * i + 1)
        ttk.Button(pe, text="Apply to selected", command=lambda: self.on_player_set_pos(False)).grid(row=0, column=6, padx=8)
        ttk.Button(pe, text="Apply to ALL", style="Accent.TButton",
                   command=lambda: self.on_player_set_pos(True)).grid(row=0, column=7, padx=(0, 4))
        ttk.Button(pe, text="Use world spawn", command=self._pl_fill_world_spawn).grid(row=0, column=8, padx=4)
        ttk.Label(pe, text="Sets Pos + SpawnX/Y/Z (and zeroes fall) so players load & respawn here.",
                  foreground="#777").grid(row=1, column=0, columnspan=9, sticky="w", padx=8, pady=(0, 4))

        # ---- quick stats ----
        se = ttk.LabelFrame(f, text="Quick stats (blank = leave unchanged)")
        se.pack(fill="x", padx=8, pady=(0, 8))
        self.pl_stat = {k: tk.StringVar() for k in ("health", "food", "xp", "gt")}
        for i, (k, lbl) in enumerate((("health", "Health"), ("food", "Food"), ("xp", "XP lvl"), ("gt", "GameType"))):
            ttk.Label(se, text=lbl).grid(row=0, column=2 * i, padx=(8, 2), pady=6)
            ttk.Entry(se, textvariable=self.pl_stat[k], width=7).grid(row=0, column=2 * i + 1)
        ttk.Button(se, text="Apply to selected", command=lambda: self.on_player_set_stats(False)).grid(row=0, column=8, padx=8)
        ttk.Button(se, text="Apply to ALL", command=lambda: self.on_player_set_stats(True)).grid(row=0, column=9)

    def _players_refresh(self):
        if not hasattr(self, "pl_tree"):
            return
        self.pl_tree.delete(*self.pl_tree.get_children())
        if self.world is None:
            return
        GT = {0: "Surv", 1: "Creat", 2: "Adv", None: "—"}
        for pi in self.world.list_players():
            pos = "%.0f %.0f %.0f" % tuple(pi["pos"]) if pi["pos"] else "—"
            sp = "%s %s %s" % pi["spawn"] if all(v is not None for v in pi["spawn"]) else "world"
            name = pi["xuid"] + ("  ●" if pi["active"] else "")
            self.pl_tree.insert("", "end", iid=pi["key"], tags=("active",) if pi["active"] else (),
                                values=(name, pos, pi["health"] if pi["health"] is not None else "—",
                                        sp, pi["items"], GT.get(pi["gametype"], pi["gametype"])))

    def _pl_selected(self):
        return list(self.pl_tree.selection())

    def _pl_fill_world_spawn(self):
        if self.world is None:
            return
        for k in ("x", "y", "z"):
            self.pl_xyz[k].set(str(self.world.level.get_value("Spawn" + k.upper())))

    def on_player_add(self):
        if not self._guard():
            return
        xuid = simpledialog.askstring("Add profile", "New XUID (account id) for the cloned profile:", parent=self)
        if not xuid:
            return
        try:
            self.world.add_player(xuid.strip()); self._players_refresh()
            self.status.set("Added profile %s (clone of active). Save to keep." % xuid.strip())
        except Exception as e:
            self._err(e)

    def on_player_duplicate(self):
        if not self._guard():
            return
        sel = self._pl_selected()
        if not sel:
            messagebox.showinfo("Duplicate", "Select a profile to duplicate."); return
        xuid = simpledialog.askstring("Duplicate profile", "New XUID for the copy:", parent=self)
        if not xuid:
            return
        try:
            self.world.add_player(xuid.strip(), from_pkey=sel[0]); self._players_refresh()
            self.status.set("Duplicated -> %s. Save to keep." % xuid.strip())
        except Exception as e:
            self._err(e)

    def on_player_rename(self):
        if not self._guard():
            return
        sel = self._pl_selected()
        if len(sel) != 1:
            messagebox.showinfo("Rename", "Select exactly one profile to rename."); return
        new = simpledialog.askstring("Rename / reassign", "New XUID (account id):",
                                     initialvalue=self.world.player_xuid(sel[0]), parent=self)
        if not new:
            return
        try:
            self.world.rename_player(sel[0], new.strip()); self._players_refresh()
            self.status.set("Reassigned to %s. Save to keep." % new.strip())
        except Exception as e:
            self._err(e)

    def on_player_remove(self):
        if not self._guard():
            return
        sel = self._pl_selected()
        if not sel:
            messagebox.showinfo("Remove", "Select profile(s) to remove."); return
        who = ", ".join(self.world.player_xuid(k) for k in sel)
        if not messagebox.askyesno("Remove profiles", "Delete %d profile(s)?\n%s\n\nSave to make it permanent." % (len(sel), who)):
            return
        try:
            for k in sel:
                self.world.remove_player(k)
            self._players_refresh(); self.status.set("Removed %d profile(s). Save to keep." % len(sel))
        except Exception as e:
            self._err(e)

    def on_player_import(self):
        if not self._guard():
            return
        src = filedialog.askopenfilename(title="Import a player from another save (savegame.dat / CON)",
                                         filetypes=[("Console save", "*.dat *.bin"), ("All files", "*.*")])
        if not src:
            return

        def work():
            other = World.open(src)
            return self.world.import_player(other), other.name

        def done(r):
            key, nm = r
            self._players_refresh()
            self.status.set("Imported %s from '%s'. Save to keep." % (self.world.player_xuid(key), nm))
        self._run_async(work, on_done=done, msg="Importing player…")

    def on_player_set_pos(self, everyone):
        if not self._guard():
            return
        try:
            x, y, z = (self._num(self.pl_xyz[k], k.upper()) for k in ("x", "y", "z"))
            targets = self.world.players() if everyone else self._pl_selected()
            if not targets:
                messagebox.showinfo("Set position", "Select profile(s), or use Apply to ALL."); return
            for k in targets:
                self.world.edit_player(k, pos=(x + 0.5, float(y), z + 0.5), spawn=(x, y, z))
            self._players_refresh()
            self.status.set("Placed %d player(s) at (%d, %d, %d). Save to keep." % (len(targets), x, y, z))
        except Exception as e:
            self._err(e)

    def on_player_set_stats(self, everyone):
        if not self._guard():
            return
        try:
            def opt(k):
                s = self.pl_stat[k].get().strip()
                return int(s) if s else None
            fields = dict(health=opt("health"), food=opt("food"), xp=opt("xp"), gametype=opt("gt"))
            if all(v is None for v in fields.values()):
                messagebox.showinfo("Quick stats", "Fill at least one field."); return
            targets = self.world.players() if everyone else self._pl_selected()
            if not targets:
                messagebox.showinfo("Quick stats", "Select profile(s), or use Apply to ALL."); return
            for k in targets:
                self.world.edit_player(k, **fields)
            self._players_refresh()
            self.status.set("Updated stats on %d player(s). Save to keep." % len(targets))
        except Exception as e:
            self._err(e)

    # ---------------------------------------------------------------- Blocks
    def _id_search(self, parent, id_var, kind="block", width=24):
        """A searchable name↔id combobox. Type a name ("diamond") or a number to filter;
        picking an entry writes the NUMERIC id into id_var (what the on_* handlers read),
        while the box displays "Name  (id)". id_var stays numeric, and if it changes
        elsewhere (e.g. Get, or clicking an inventory slot) the box updates to match."""
        from . import names as NM
        disp = tk.StringVar()
        guard = {"on": False}
        cb = ttk.Combobox(parent, textvariable=disp, width=width)
        cb["values"] = NM.labels(kind)

        def canon(i):
            nm = NM.name_for(i, kind)
            return "%s  (%d)" % (nm, i) if nm else str(i)

        def resolve(text):
            text = text.strip()
            if not text:
                return None
            if text.isdigit():
                return int(text)
            m = re.search(r"\((\d+)\)\s*$", text)          # "Name  (46)"
            if m:
                return int(m.group(1))
            return NM.id_for_label(text, kind)

        def commit(*_):
            i = resolve(disp.get())
            if i is not None:
                guard["on"] = True
                id_var.set(str(i)); disp.set(canon(i))
                guard["on"] = False

        def on_type(ev):
            if ev.keysym in ("Return", "Up", "Down", "Escape", "Tab"):
                return
            cb["values"] = NM.search(disp.get(), kind)     # live filter as you type

        def from_id(*_):                                    # id_var set elsewhere -> sync the label
            if guard["on"]:
                return
            v = id_var.get().strip()
            if v.isdigit():
                guard["on"] = True; disp.set(canon(int(v))); guard["on"] = False
            elif not v:
                guard["on"] = True; disp.set(""); guard["on"] = False

        cb.bind("<KeyRelease>", on_type)
        cb.bind("<<ComboboxSelected>>", commit)
        cb.bind("<Return>", commit)
        cb.bind("<FocusOut>", commit)
        id_var.trace_add("write", from_id)
        from_id()
        return cb

    def _name_search(self, parent, val_var, pairs, width=20):
        """Searchable combobox over (label, value) STRING pairs — for the mob field, where
        the stored id is a string and some differ from the display (Iron Golem →
        VillagerGolem). Shows the label, writes the value to val_var; a custom typed
        string is kept verbatim so power users can enter any entity id."""
        disp = tk.StringVar()
        guard = {"on": False}
        labels = [lbl for lbl, _v in pairs]
        lab2val = {lbl: v for lbl, v in pairs}
        val2lab = {v: lbl for lbl, v in pairs}
        cb = ttk.Combobox(parent, textvariable=disp, width=width)
        cb["values"] = labels

        def commit(*_):
            t = disp.get().strip()
            v = lab2val.get(t, t)                       # label -> id, else keep typed text
            guard["on"] = True
            val_var.set(v); disp.set(val2lab.get(v, v))
            guard["on"] = False

        def on_type(ev):
            if ev.keysym in ("Return", "Up", "Down", "Escape", "Tab"):
                return
            q = disp.get().strip().lower()
            cb["values"] = [l for l in labels if q in l.lower()] or labels

        def from_val(*_):
            if guard["on"]:
                return
            v = val_var.get().strip()
            guard["on"] = True; disp.set(val2lab.get(v, v)); guard["on"] = False

        cb.bind("<KeyRelease>", on_type)
        cb.bind("<<ComboboxSelected>>", commit)
        cb.bind("<Return>", commit)
        cb.bind("<FocusOut>", commit)
        val_var.trace_add("write", from_val)
        from_val()
        return cb

    def _coord_fields(self, parent, label, vars_keys, store, width=7):
        """A labelled row of X/Y/Z (or more) entry boxes, spaced consistently."""
        row = ttk.Frame(parent); row.pack(fill="x", pady=3)
        ttk.Label(row, text=label, style="Muted.TLabel", width=8).pack(side="left")
        for ax, k in vars_keys:
            ttk.Label(row, text=ax, style="Muted.TLabel").pack(side="left", padx=(6, 3))
            ttk.Entry(row, textvariable=store[k], width=width).pack(side="left")
        return row

    def _build_blocks(self):
        wrap = ttk.Frame(self.tab_blocks, padding=(16, 14)); wrap.pack(fill="both", expand=True)
        ttk.Label(wrap, text="BLOCK EDITOR", style="Section.TLabel").pack(anchor="w")
        ttk.Label(wrap, text="Read or change blocks by coordinate. Edits apply in memory — Save to write.",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 14))

        self.blk = {k: tk.StringVar() for k in ("x", "y", "z", "id", "data")}
        self.blk_result = tk.StringVar()

        # -- single block --
        ttk.Label(wrap, text="Get or set one block", style="Value.TLabel").pack(anchor="w")
        self._coord_fields(wrap, "Position", (("X", "x"), ("Y", "y"), ("Z", "z")), self.blk)
        br = ttk.Frame(wrap); br.pack(fill="x", pady=3)
        ttk.Label(br, text="Block", style="Muted.TLabel", width=8).pack(side="left")
        self._id_search(br, self.blk["id"], "block", width=26).pack(side="left")
        ttk.Label(br, text="Data", style="Muted.TLabel").pack(side="left", padx=(14, 3))
        ttk.Entry(br, textvariable=self.blk["data"], width=5).pack(side="left")
        ttk.Label(br, text="type a name or id", style="Muted.TLabel").pack(side="left", padx=12)
        ab = ttk.Frame(wrap); ab.pack(fill="x", pady=(10, 0))
        ttk.Button(ab, text="Set block", style="Accent.TButton", command=self.on_block_set).pack(side="left")
        ttk.Button(ab, text="Get", command=self.on_block_get).pack(side="left", padx=8)
        ttk.Label(wrap, textvariable=self.blk_result, style="Muted.TLabel").pack(anchor="w", pady=(8, 0))

        self._hr(wrap).pack(fill="x", pady=18)

        # -- fill a box --
        self.fillv = {k: tk.StringVar() for k in ("x1", "y1", "z1", "x2", "y2", "z2", "id")}
        ttk.Label(wrap, text="Fill a box with one block", style="Value.TLabel").pack(anchor="w")
        self._coord_fields(wrap, "From", (("X", "x1"), ("Y", "y1"), ("Z", "z1")), self.fillv)
        self._coord_fields(wrap, "To", (("X", "x2"), ("Y", "y2"), ("Z", "z2")), self.fillv)
        fb = ttk.Frame(wrap); fb.pack(fill="x", pady=(3, 0))
        ttk.Label(fb, text="Block", style="Muted.TLabel", width=8).pack(side="left")
        self._id_search(fb, self.fillv["id"], "block", width=26).pack(side="left")
        ttk.Button(fb, text="Fill box", style="Accent.TButton", command=self.on_fill).pack(side="left", padx=18)
        ttk.Label(wrap, text="Fills the whole box with one block on layers y 0–127. Large boxes run in the "
                           "background with a progress bar.", style="Muted.TLabel",
                  wraplength=560, justify="left").pack(anchor="w", pady=(10, 0))

    # ---------------------------------------------------------------- Entities
    def _build_ent(self):
        wrap = ttk.Frame(self.tab_ent, padding=(16, 14)); wrap.pack(fill="both", expand=True)
        ttk.Label(wrap, text="ENTITIES", style="Section.TLabel").pack(anchor="w")
        ttk.Label(wrap, text="Mobs, items and other entities stored in the world.",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 12))

        bar = ttk.Frame(wrap); bar.pack(fill="x", pady=(0, 10))
        self.ent_cx = tk.StringVar(); self.ent_cz = tk.StringVar()
        ttk.Label(bar, text="Chunk", style="Muted.TLabel").pack(side="left")
        ttk.Label(bar, text="X", style="Muted.TLabel").pack(side="left", padx=(10, 3))
        ttk.Entry(bar, textvariable=self.ent_cx, width=6).pack(side="left")
        ttk.Label(bar, text="Z", style="Muted.TLabel").pack(side="left", padx=(10, 3))
        ttk.Entry(bar, textvariable=self.ent_cz, width=6).pack(side="left")
        ttk.Button(bar, text="List", style="Accent.TButton", command=self.on_ent_list).pack(side="left", padx=(14, 0))
        ttk.Label(bar, text="leave both blank to list the whole world",
                  style="Muted.TLabel").pack(side="left", padx=12)

        tw = ttk.Frame(wrap); tw.pack(fill="both", expand=True)
        self.ent_tree = ttk.Treeview(tw, columns=("id", "pos"), show="headings")
        self.ent_tree.heading("id", text="Entity"); self.ent_tree.heading("pos", text="Position  (x, y, z)")
        self.ent_tree.column("id", width=190); self.ent_tree.column("pos", width=280)
        vs = ttk.Scrollbar(tw, orient="vertical", command=self.ent_tree.yview)
        self.ent_tree.configure(yscrollcommand=vs.set)
        self.ent_tree.pack(side="left", fill="both", expand=True); vs.pack(side="right", fill="y")

        self._hr(wrap).pack(fill="x", pady=16)
        ttk.Label(wrap, text="Add a mob", style="Value.TLabel").pack(anchor="w")
        self.mob = {k: tk.StringVar() for k in ("name", "x", "y", "z")}
        self.mob["name"].set("Giant")
        from . import names as NM
        nr = ttk.Frame(wrap); nr.pack(fill="x", pady=3)
        ttk.Label(nr, text="Mob", style="Muted.TLabel", width=8).pack(side="left")
        self._name_search(nr, self.mob["name"], NM.MOBS, width=18).pack(side="left")
        ttk.Label(nr, text="type to search, or enter any entity id",
                  style="Muted.TLabel").pack(side="left", padx=12)
        self._coord_fields(wrap, "At", (("X", "x"), ("Y", "y"), ("Z", "z")), self.mob)
        ttk.Button(wrap, text="Add mob", style="Accent.TButton",
                   command=self.on_mob_add).pack(anchor="w", pady=(10, 0))

    # ---------------------------------------------------------------- NBT tree
    def _build_nbt(self):
        wrap = ttk.Frame(self.tab_nbt, padding=(16, 14)); wrap.pack(fill="both", expand=True)
        ttk.Label(wrap, text="NBT INSPECTOR", style="Section.TLabel").pack(anchor="w")
        ttk.Label(wrap, text="Search the whole save for anything, or read the raw NBT tree of a file.",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 10))

        # ---- global search ----
        self._nbt_index = None; self._nbt_hits = []
        sr = ttk.Frame(wrap); sr.pack(fill="x")
        ttk.Label(sr, text="Search entire save", style="Value.TLabel").pack(side="left")
        self.nbt_query = tk.StringVar()
        se = ttk.Entry(sr, textvariable=self.nbt_query, width=30)
        se.pack(side="left", padx=(10, 6)); se.bind("<Return>", lambda e: self.on_nbt_search())
        ttk.Button(sr, text="Search", style="Accent.TButton", command=self.on_nbt_search).pack(side="left")
        self.nbt_search_status = tk.StringVar(value="")
        ttk.Label(sr, textvariable=self.nbt_search_status, style="Muted.TLabel").pack(side="left", padx=12)
        ttk.Label(wrap, text="e.g. spawner · diamond sword · minecraft:chest · a sign’s text · a player name",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 4))
        rw = ttk.Frame(wrap); rw.pack(fill="x", pady=(0, 4))
        self.nbt_results = ttk.Treeview(rw, columns=("what", "where", "pos"), show="headings", height=6)
        self.nbt_results.heading("what", text="Match"); self.nbt_results.heading("where", text="Where")
        self.nbt_results.heading("pos", text="Position")
        self.nbt_results.column("what", width=300); self.nbt_results.column("where", width=200)
        self.nbt_results.column("pos", width=110)
        rvs = ttk.Scrollbar(rw, orient="vertical", command=self.nbt_results.yview)
        self.nbt_results.configure(yscrollcommand=rvs.set)
        self.nbt_results.pack(side="left", fill="x", expand=True); rvs.pack(side="right", fill="y")
        self.nbt_results.bind("<<TreeviewSelect>>", self._on_nbt_result)
        self._hr(wrap).pack(fill="x", pady=12)

        bar = ttk.Frame(wrap); bar.pack(fill="x", pady=(0, 10))
        ttk.Label(bar, text="File", style="Muted.TLabel").pack(side="left")
        self.nbt_file = ttk.Combobox(bar, width=38, state="readonly")
        self.nbt_file.pack(side="left", padx=(8, 10))
        ttk.Button(bar, text="Load", style="Accent.TButton", command=self.on_nbt_load).pack(side="left")
        ttk.Button(bar, text="Expand all", command=lambda: self._nbt_expand(True)).pack(side="left", padx=(14, 0))
        ttk.Button(bar, text="Collapse all", command=lambda: self._nbt_expand(False)).pack(side="left", padx=4)

        tw = ttk.Frame(wrap); tw.pack(fill="both", expand=True)
        self.nbt_tree = ttk.Treeview(tw, columns=("type", "value"))
        self.nbt_tree.heading("#0", text="Tag"); self.nbt_tree.heading("type", text="Type")
        self.nbt_tree.heading("value", text="Value")
        self.nbt_tree.column("#0", width=300); self.nbt_tree.column("type", width=120)
        self.nbt_tree.column("value", width=340)
        vs = ttk.Scrollbar(tw, orient="vertical", command=self.nbt_tree.yview)
        self.nbt_tree.configure(yscrollcommand=vs.set)
        self.nbt_tree.pack(side="left", fill="both", expand=True); vs.pack(side="right", fill="y")

    def _nbt_expand(self, open_):
        def walk(item):
            self.nbt_tree.item(item, open=open_)
            for ch in self.nbt_tree.get_children(item):
                walk(ch)
        for it in self.nbt_tree.get_children(""):
            walk(it)

    def on_nbt_search(self):
        if not self._guard():
            return
        q = self.nbt_query.get().strip()
        if not q:
            self.nbt_search_status.set("Type something to search for."); return
        if self._nbt_index is not None:                 # already indexed -> instant
            self._nbt_do_filter(q); return
        world = self.world
        from . import nbtsearch as S                     # first search: index the save once
        self._run_async(lambda: S.build_index(world, log=self._logcb()),
                        on_done=lambda idx: (setattr(self, "_nbt_index", idx), self._nbt_do_filter(q)),
                        msg="Indexing save for search…")

    def _nbt_do_filter(self, q):
        from . import nbtsearch as S
        hits = S.filter_index(self._nbt_index or [], q)
        self._nbt_hits = hits
        self.nbt_results.delete(*self.nbt_results.get_children())
        for i, h in enumerate(hits):
            pos = h.get("pos")
            postxt = ("%d, %d, %d" % pos) if pos and all(v is not None for v in pos) else ""
            self.nbt_results.insert("", "end", iid=str(i),
                                    values=(h["summary"][:52], h["where"], postxt))
        self.nbt_search_status.set("%d match%s for “%s”  (click one to inspect)"
                                   % (len(hits), "" if len(hits) == 1 else "es", q))

    def _on_nbt_result(self, _ev=None):
        sel = self.nbt_results.selection()
        if not sel:
            return
        h = self._nbt_hits[int(sel[0])]
        self.nbt_tree.delete(*self.nbt_tree.get_children())
        label = "%s  @  %s" % (h["id"], h["where"])
        self._insert_nbt("", label, N.Tag(N.COMPOUND, h["tag"]))
        for it in self.nbt_tree.get_children(""):
            self.nbt_tree.item(it, open=True)

    # ---------------------------------------------------------------- Tools
    def _build_tools(self):
        f = self.tab_tools
        box = ttk.LabelFrame(f, text="One-click content"); box.pack(fill="x", padx=10, pady=10)
        rows = [
            ("Give 16 Giant spawner blocks (id 52)", self.tool_give_spawners),
            ("Inject a Giant at spawn", self.tool_giant_at_spawn),
            ("Build a Giant spawner den at spawn", self.tool_giant_den),
            ("Give a Locked Chest (id 95)", self.tool_locked_chest),
            ("Give a stack of TNT (id 46)", self.tool_tnt),
        ]
        for i, (label, cmd) in enumerate(rows):
            ttk.Button(box, text=label, command=cmd, width=42).grid(row=i, column=0, padx=8, pady=5, sticky="w")
        ttk.Label(f, text="Tools apply immediately in memory; use File → Save to write.",
                  foreground="#666").pack(padx=10, pady=6, anchor="w")
        v3 = ttk.LabelFrame(f, text="3D view"); v3.pack(fill="x", padx=10, pady=10)
        ttk.Button(v3, text="🧊  Open in 3D fly-through viewer", width=42,
                   command=self.tool_view3d).grid(row=0, column=0, padx=8, pady=6, sticky="w")
        ttk.Label(v3, text="Opens a separate window. WASD + mouse to fly; a moment to build the mesh.",
                  foreground="#666").grid(row=1, column=0, padx=8, sticky="w")

        mp = ttk.LabelFrame(f, text="World atlas"); mp.pack(fill="x", padx=10, pady=10)
        ttk.Button(mp, text="🗺  Render top-down world map…", width=34,
                   command=self.tool_world_map).grid(row=0, column=0, padx=8, pady=6, sticky="w")
        ttk.Button(mp, text="🧭  Export in-game map items…", width=30,
                   command=self.tool_map_items).grid(row=0, column=1, padx=4, pady=6, sticky="w")
        ttk.Button(mp, text="📊  World analytics report…", width=26,
                   command=self.tool_report).grid(row=0, column=2, padx=4, pady=6, sticky="w")
        ttk.Label(mp, text="A satellite-style PNG of the whole world (all TU formats), plus the "
                           "in-game maps as images.", foreground="#666").grid(
            row=1, column=0, columnspan=2, padx=8, sticky="w")

        tg = ttk.LabelFrame(f, text="Terrain generator  (writes to the open save, .bak kept)")
        tg.pack(fill="x", padx=10, pady=10)
        ttk.Button(tg, text="⛰  Flatten world to sea level…", width=30,
                   command=self.tool_flatten).grid(row=0, column=0, padx=8, pady=6, sticky="w")
        ttk.Button(tg, text="🏔  Generate mountains…", width=26,
                   command=self.tool_mountains).grid(row=0, column=1, padx=4, pady=6, sticky="w")
        ttk.Label(tg, text="Flatten strips terrain above the water line (keeps your builds); "
                           "mountains raises noise peaks back in. Old-NBT worlds only.",
                  foreground="#666", wraplength=820, justify="left").grid(
            row=1, column=0, columnspan=2, padx=8, sticky="w")

        pa = ttk.LabelFrame(f, text="Pixel art & 3D objects  →  buildable schematics")
        pa.pack(fill="x", padx=10, pady=10)
        ttk.Button(pa, text="🎨  Image → pixel art…", width=26,
                   command=self.tool_pixelart).grid(row=0, column=0, padx=8, pady=6, sticky="w")
        ttk.Button(pa, text="🔮  Sphere / orb…", width=20,
                   command=lambda: self.tool_shape("sphere")).grid(row=0, column=1, padx=4, pady=6, sticky="w")
        ttk.Button(pa, text="🛢  Cylinder…", width=16,
                   command=lambda: self.tool_shape("cylinder")).grid(row=0, column=2, padx=4, pady=6, sticky="w")
        ttk.Button(pa, text="🔺  Pyramid…", width=16,
                   command=lambda: self.tool_shape("pyramid")).grid(row=0, column=3, padx=4, pady=6, sticky="w")
        ttk.Button(pa, text="🔤  Text / word art…", width=22,
                   command=self.tool_text).grid(row=1, column=0, padx=8, pady=(0, 6), sticky="w")
        ttk.Button(pa, text="🧊  3D model (.obj)…", width=20,
                   command=self.tool_obj).grid(row=1, column=1, padx=4, pady=(0, 6), sticky="w")
        ttk.Button(pa, text="📐  Scale a schematic…", width=24,
                   command=self.tool_scale_schematic).grid(row=1, column=2, padx=4, pady=(0, 6), sticky="w")
        ttk.Label(pa, text="Photo/logo → pixel art (transparent pixels skipped), text → block letters, a .obj "
                           "model → voxels, or a sphere/cylinder/pyramid. Save it or stamp straight into the "
                           "open world; import with the 3D viewer (Ctrl+I).",
                  foreground="#666", wraplength=820, justify="left").grid(
            row=2, column=0, columnspan=4, padx=8, sticky="w")

    # ---------------------------------------------------------------- Import / Export
    # ---------------------------------------------------------------- Convert
    # ---------------------------------------------------------------- World Library
    def _lib_cfg_path(self):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "LCEStudio")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        return os.path.join(d, "library.json")

    def _lib_folders(self):
        import json
        try:
            with open(self._lib_cfg_path(), encoding="utf-8") as f:
                fld = json.load(f).get("folders", [])
            if fld:
                return fld
        except Exception:
            pass
        return [g for g in ("C:/Nexia360/Library",) if os.path.isdir(g)]

    def _lib_save_folders(self, folders):
        import json
        try:
            with open(self._lib_cfg_path(), "w", encoding="utf-8") as f:
                json.dump({"folders": folders}, f, indent=2)
        except Exception:
            pass

    def _build_library(self):
        outer = ttk.Frame(self.tab_library, padding=(16, 12)); outer.pack(fill="both", expand=True)
        top = ttk.Frame(outer); top.pack(fill="x")
        ttk.Label(top, text="WORLD LIBRARY", style="Section.TLabel").pack(side="left")
        ttk.Button(top, text="Rescan", command=self._lib_scan).pack(side="right")
        ttk.Button(top, text="Add folder…", command=self._lib_add_folder).pack(side="right", padx=(0, 8))
        self.lib_folders_lbl = tk.StringVar()
        ttk.Label(outer, textvariable=self.lib_folders_lbl, style="Muted.TLabel",
                  wraplength=920, justify="left").pack(anchor="w", pady=(2, 6))
        sr = ttk.Frame(outer); sr.pack(fill="x", pady=(0, 4))
        ttk.Label(sr, text="Search", style="Muted.TLabel").pack(side="left")
        self.lib_query = tk.StringVar()
        se = ttk.Entry(sr, textvariable=self.lib_query, width=32)
        se.pack(side="left", padx=(8, 6))
        se.bind("<KeyRelease>", lambda e: self._lib_apply_filter())
        ttk.Button(sr, text="Clear", style="Card.TButton",
                   command=lambda: (self.lib_query.set(""), self._lib_apply_filter())).pack(side="left")
        ttk.Label(sr, text="matches name · platform · TU", style="Muted.TLabel").pack(side="left", padx=10)
        self.lib_status = tk.StringVar(value="")
        ttk.Label(outer, textvariable=self.lib_status, style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
        self.lib_grid = self._vscroll(outer)
        self._lib_photos = []
        self._lib_worlds = []; self._lib_imgs = []
        self._lib_tip = None; self._lib_tip_label = None; self._lib_hover_w = None
        self._lib_show_job = None; self._lib_hide_job = None
        self._lib_detail_cache = {}; self._lib_detail_pending = set()
        self._refresh_folders_label()

    def _refresh_folders_label(self):
        f = self._lib_folders()
        self.lib_folders_lbl.set(("Folders:  " + "    ".join(f)) if f else
                                 "No folders yet — click “Add folder…” to point at your saves "
                                 "(e.g. C:\\Nexia360\\Library or your emulator/Windows LCE saves).")

    def _lib_add_folder(self):
        d = filedialog.askdirectory(title="Add a saves folder to the library")
        if not d:
            return
        folders = self._lib_folders()
        if d not in folders:
            folders.append(d); self._lib_save_folders(folders); self._refresh_folders_label()
        self._lib_scan()

    def _lib_scan(self):
        if getattr(self, "_lib_busy", False):
            return
        self._lib_busy = True
        folders = self._lib_folders()
        self.lib_status.set("Scanning…")

        def worker():
            try:
                from . import library as L
                worlds = L.scan(folders)[:500]
                imgs = [None] * len(worlds)
                if _HAVE_PIL:
                    from PIL import Image
                    import io
                    for i, w in enumerate(worlds):
                        if w["thumbnail"]:
                            try:
                                imgs[i] = Image.open(io.BytesIO(w["thumbnail"])).convert("RGB").resize((80, 80))
                            except Exception:
                                imgs[i] = None
                self._post(lambda: self._lib_done(worlds, imgs))
            except Exception as e:
                self._post(lambda e=e: (setattr(self, "_lib_busy", False), self._err(e)))
        threading.Thread(target=worker, daemon=True).start()

    def _lib_done(self, worlds, imgs):
        self._lib_busy = False
        self._lib_worlds = worlds
        self._lib_imgs = imgs
        self._lib_apply_filter()

    def _lib_apply_filter(self):
        from . import library as L
        q = (self.lib_query.get().strip().lower() if hasattr(self, "lib_query") else "")
        pairs = list(zip(self._lib_worlds, self._lib_imgs))
        if q:
            toks = q.split()

            def hay(w):
                return ("%s %s %s" % (w["name"], L.PLATFORM_LABEL.get(w["platform"], w["platform"]),
                                      w.get("tu") or "")).lower()
            pairs = [(w, im) for (w, im) in pairs if all(t in hay(w) for t in toks)]
        self._lib_render(pairs, q)

    def _lib_render(self, pairs, q=""):
        self._lib_hide_tip()
        for ch in self.lib_grid.winfo_children():
            ch.destroy()
        self._lib_photos = []
        total = len(self._lib_worlds)
        if not total:
            ttk.Label(self.lib_grid, text="No worlds found in the configured folders.",
                      style="Muted.TLabel").grid(row=0, column=0, padx=8, pady=8)
            self.lib_status.set("0 worlds")
            return
        self.lib_status.set(("showing %d of %d worlds" % (len(pairs), total)) if q
                            else "%d worlds" % total)
        if not pairs:
            ttk.Label(self.lib_grid, text="No worlds match “%s”." % q,
                      style="Muted.TLabel").grid(row=0, column=0, padx=8, pady=8)
            return
        cols = 4
        for i, (w, im) in enumerate(pairs):
            self._lib_card(self.lib_grid, w, im).grid(
                row=i // cols, column=i % cols, padx=8, pady=8, sticky="n")

    def _lib_card(self, parent, w, im):
        from . import library as L
        card = ttk.Frame(parent, padding=8)
        if im is not None and _HAVE_PIL:
            ph = ImageTk.PhotoImage(im); self._lib_photos.append(ph)
            thumb = ttk.Label(card, image=ph); thumb.image = ph
        else:
            thumb = tk.Canvas(card, width=80, height=80, highlightthickness=0,
                              background=self.THEMES[self.theme]["CANVAS"])
        thumb.pack()
        thumb.bind("<Button-1>", lambda e, ww=w: self._lib_open(ww))
        ttk.Label(card, text=w["name"], style="Value.TLabel", wraplength=150,
                  justify="left").pack(anchor="w", pady=(6, 0))
        meta = "%s · %s" % (L.PLATFORM_LABEL.get(w["platform"], w["platform"]), w["tu"] or "TU ?")
        ttk.Label(card, text=meta, style="Muted.TLabel").pack(anchor="w")
        bar = ttk.Frame(card); bar.pack(anchor="w", pady=(5, 0))
        ttk.Button(bar, text="Open", style="Card.TButton",
                   command=lambda ww=w: self._lib_open(ww)).pack(side="left")
        ttk.Button(bar, text="Convert", style="Card.TButton",
                   command=lambda ww=w: self._lib_convert(ww)).pack(side="left", padx=3)
        ttk.Button(bar, text="Repair", style="Card.TButton",
                   command=lambda ww=w: self._lib_repair(ww)).pack(side="left")
        self._lib_bind_hover(card, w)
        return card

    def _lib_bind_hover(self, card, w):
        """Show a detail tooltip after a short hover; debounced so moving between the
        card and its child widgets doesn't flicker it off."""
        def enter(_e):
            self._lib_hover_w = w
            if self._lib_hide_job:
                self.after_cancel(self._lib_hide_job); self._lib_hide_job = None
            if self._lib_show_job:
                self.after_cancel(self._lib_show_job)
            self._lib_show_job = self.after(400, lambda: self._lib_hover_show(w, card))

        def leave(_e):
            if self._lib_show_job:
                self.after_cancel(self._lib_show_job); self._lib_show_job = None
            if self._lib_hide_job:
                self.after_cancel(self._lib_hide_job)
            self._lib_hide_job = self.after(140, self._lib_hide_tip)

        def bind_deep(widget):
            widget.bind("<Enter>", enter, add="+")
            widget.bind("<Leave>", leave, add="+")
            for ch in widget.winfo_children():
                bind_deep(ch)
        bind_deep(card)

    def _lib_hover_show(self, w, card):
        self._lib_show_job = None
        if not card.winfo_exists() or self._lib_hover_w is not w:
            return
        detail = self._lib_detail_cache.get(w["path"])
        if detail is None and w["path"] not in self._lib_detail_pending:
            self._lib_detail_pending.add(w["path"])
            self._lib_describe_async(w)
        x = self.winfo_pointerx() + 16
        y = self.winfo_pointery() + 14
        if x + 300 > self.winfo_screenwidth():
            x = self.winfo_pointerx() - 312
        self._lib_show_tip(x, y, self._lib_detail_text(w, detail))

    def _lib_describe_async(self, w):
        def worker():
            from . import library as L
            d = L.describe(w["path"], w["platform"])
            self._post(lambda: self._lib_describe_done(w, d))
        threading.Thread(target=worker, daemon=True).start()

    def _lib_describe_done(self, w, d):
        self._lib_detail_pending.discard(w["path"])
        self._lib_detail_cache[w["path"]] = d if d else "FAILED"
        if (self._lib_hover_w is w and self._lib_tip is not None
                and self._lib_tip.winfo_exists() and self._lib_tip.winfo_viewable()):
            self._lib_tip_label.config(text=self._lib_detail_text(w, self._lib_detail_cache[w["path"]]))

    def _lib_detail_text(self, w, detail):
        from . import library as L
        head = "%s\n%s  ·  %s" % (w["name"], L.PLATFORM_LABEL.get(w["platform"], w["platform"]),
                                  w.get("tu") or "TU ?")
        if detail is None:
            return head + "\n\nReading world…"
        if detail == "FAILED":
            return head + "\n\n(details unavailable for this platform)"
        rows = []
        lvl = detail.get("level_name")
        if lvl and lvl.strip() and lvl != w["name"]:
            rows.append(("Level name", lvl))
        tur = detail.get("title_update_range") or detail.get("title_update")
        if tur:
            rows.append(("Title update", tur))
        sz = detail.get("size_label"); ch = detail.get("size_chunks")
        if sz:
            rows.append(("Size", "%s%s" % (sz, ("  (%s² chunks)" % ch) if ch else "")))
        dims = detail.get("dimensions")
        if dims:
            rows.append(("Dimensions", ", ".join(dims)))
        cf = detail.get("chunk_format")
        if cf:
            rows.append(("Chunk format", cf))
        seed = detail.get("seed")
        if seed is not None:
            rows.append(("Seed", str(seed)))
        sp = detail.get("spawn")
        if sp and any(v is not None for v in sp):
            rows.append(("Spawn", "%s, %s, %s" % tuple(sp)))
        gen = detail.get("generator_name")
        if gen:
            rows.append(("Generator", gen))
        body = "\n".join("%-13s %s" % (k + ":", v) for k, v in rows)
        return head + "\n\n" + body if body else head

    def _lib_show_tip(self, x, y, text):
        if self._lib_tip is None or not self._lib_tip.winfo_exists():
            p = self.THEMES[self.theme]
            self._lib_tip = tk.Toplevel(self)
            self._lib_tip.wm_overrideredirect(True)
            frm = tk.Frame(self._lib_tip, background=p["FIELD"],
                           highlightbackground=p["LINE"], highlightthickness=1)
            frm.pack()
            self._lib_tip_label = tk.Label(frm, justify="left", anchor="w",
                                           background=p["FIELD"], foreground=p["INK"],
                                           font=("Segoe UI", 9), padx=11, pady=9)
            self._lib_tip_label.pack()
        self._lib_tip_label.config(text=text)
        self._lib_tip.wm_geometry("+%d+%d" % (max(0, x), max(0, y)))
        self._lib_tip.deiconify(); self._lib_tip.lift()

    def _lib_hide_tip(self):
        self._lib_hide_job = None
        self._lib_hover_w = None
        if self._lib_tip is not None and self._lib_tip.winfo_exists():
            self._lib_tip.withdraw()

    def _lib_open(self, w):
        from . import library as L
        if w["platform"] != "xbox360":
            messagebox.showinfo("Open", "The editor reads Xbox 360 saves. Convert this %s world to "
                                "Xbox 360 first (opening the Convert tab)." % L.PLATFORM_LABEL.get(w["platform"]))
            return self._lib_convert(w)
        p = w["path"]
        if os.path.basename(p).lower() == "savegame.dat":
            p = os.path.dirname(p)
        self.load(p)
        self._select_tab(self.tab_overview)

    def _lib_convert(self, w):
        for var in ("xc_src", "xj_src", "xt_src"):
            if hasattr(self, var):
                getattr(self, var).set(w["path"])
        self._select_tab(self.tab_convert)

    def _lib_repair(self, w):
        from . import library as L
        if w["platform"] != "xbox360":
            messagebox.showinfo("Repair", "Repair works on Xbox 360 saves; convert first."); return
        if hasattr(self, "rp_save"):
            self.rp_save.set(w["path"])
        self.on_repair()

    _PLATCODE = {"Xbox 360": "xbox360", "PS3": "ps3", "Windows LCE": "windows_lce"}

    def _vscroll(self, parent):
        """A vertically-scrollable frame; returns the inner frame to pack content into."""
        bg = self.THEMES[self.theme]["BG"]
        canvas = tk.Canvas(parent, background=bg, highlightthickness=0)
        vs = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=vs.set)
        canvas.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")

        def _wheel(e):
            canvas.yview_scroll(int(-e.delta / 120), "units")
        inner.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _wheel))
        inner.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        return inner

    def _conv_srcrow(self, parent, var):
        """Source-save row: entry + Folder/CON pickers + 'use open save'."""
        r = ttk.Frame(parent); r.pack(fill="x", pady=(2, 2))
        ttk.Label(r, text="Save", style="Muted.TLabel", width=9).pack(side="left")
        ttk.Entry(r, textvariable=var, width=46).pack(side="left", fill="x", expand=True)
        ttk.Button(r, text="Folder\u2026", command=lambda: self._pick_any(var)).pack(side="left", padx=(6, 0))
        ttk.Button(r, text="CON\u2026", command=lambda: self._pick_file(var)).pack(side="left", padx=(4, 0))
        ttk.Button(r, text="Open save", command=lambda: var.set(self.path or "")).pack(side="left", padx=(4, 0))

    def _conv_outrow(self, parent, var, label="Output"):
        r = ttk.Frame(parent); r.pack(fill="x", pady=(2, 2))
        ttk.Label(r, text=label, style="Muted.TLabel", width=9).pack(side="left")
        ttk.Entry(r, textvariable=var, width=46).pack(side="left", fill="x", expand=True)
        ttk.Button(r, text="Browse\u2026", command=lambda: self._pick_dir(var)).pack(side="left", padx=(6, 0))
        ttk.Label(r, text="blank = default Output folder", style="Muted.TLabel").pack(side="left", padx=8)

    def _conv_out(self, src, out, suffix):
        """A safe output path that never overwrites the source in place."""
        if out:
            return out
        import os
        raw = str(src).rstrip("/\\")
        base = os.path.splitext(os.path.basename(raw))[0]
        return os.path.join(os.path.dirname(raw) or ".", "%s %s" % (base, suffix))

    def _detect_platform(self, path):
        """Guess the console platform from a save path's shape: a saveData.ms is
        Windows LCE, a GAMEDATA is PS3, everything else (.bin / savegame.dat) is
        treated as Xbox 360."""
        import os
        p = str(path).strip().rstrip("/\\")
        if not p:
            return None
        if os.path.isfile(p):
            low = p.lower()
            if low.endswith("savedata.ms"):
                return "windows_lce"
            if os.path.basename(p).lower() == "gamedata":
                return "ps3"
            return "xbox360"
        if os.path.isdir(p):
            if os.path.exists(os.path.join(p, "saveData.ms")):
                return "windows_lce"
            if os.path.exists(os.path.join(p, "GAMEDATA")):
                return "ps3"
        return "xbox360"

    def _autoplat(self, src_var, plat_var):
        """Auto-fill a platform combobox from the source path (still overridable)."""
        code = self._detect_platform(src_var.get())
        disp = {"xbox360": "Xbox 360", "ps3": "PS3", "windows_lce": "Windows LCE"}.get(code)
        if disp:
            plat_var.set(disp)

    def _build_convert(self):
        wrap = self._vscroll(self.tab_convert)
        pad = ttk.Frame(wrap, padding=(16, 14)); pad.pack(fill="both", expand=True)
        ttk.Label(pad, text="CONVERT", style="Section.TLabel").pack(anchor="w")
        ttk.Label(pad, text="Move a world between platforms and title updates, or export it to Java. "
                            "Each conversion writes a new save \u2014 your source is never changed.",
                  style="Muted.TLabel", wraplength=680, justify="left").pack(anchor="w", pady=(2, 14))

        plats = ["Xbox 360", "PS3", "Windows LCE"]

        # -- change platform (engine) --
        ttk.Label(pad, text="Change platform", style="Value.TLabel").pack(anchor="w")
        ttk.Label(pad, text="Xbox 360 \u00b7 PS3 \u00b7 Windows LCE. Xbox\u2194PS3 goes via Windows LCE (convert twice).",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 4))
        self.xc_src = tk.StringVar(); self.xc_out = tk.StringVar()
        self.xc_profile = tk.StringVar(); self.xc_name = tk.StringVar()
        self._conv_srcrow(pad, self.xc_src)
        fr = ttk.Frame(pad); fr.pack(fill="x", pady=(2, 2))
        ttk.Label(fr, text="From", style="Muted.TLabel", width=9).pack(side="left")
        self.xc_from = tk.StringVar(value="Xbox 360")
        ttk.Combobox(fr, textvariable=self.xc_from, values=plats, state="readonly", width=13).pack(side="left")
        ttk.Label(fr, text="auto", style="Muted.TLabel").pack(side="left", padx=(6, 0))
        ttk.Label(fr, text="To", style="Muted.TLabel").pack(side="left", padx=(16, 4))
        self.xc_to = tk.StringVar(value="Windows LCE")
        ttk.Combobox(fr, textvariable=self.xc_to, state="readonly", width=24,
                     values=["Windows LCE", "Xbox 360 (.bin)", "Xbox 360 (emulator folder)", "PS3"]
                     ).pack(side="left")
        self.xc_src.trace_add("write", lambda *a: self._autoplat(self.xc_src, self.xc_from))
        self._conv_outrow(pad, self.xc_out)
        opt = ttk.Frame(pad); opt.pack(fill="x", pady=(2, 2))
        ttk.Label(opt, text="Options", style="Muted.TLabel", width=9).pack(side="left")
        ttk.Label(opt, text="Expand", style="Muted.TLabel").pack(side="left")
        self.xc_expand = tk.StringVar(value="—")
        ttk.Combobox(opt, textvariable=self.xc_expand, state="readonly", width=8,
                     values=["—", "Small", "Medium", "Large"]).pack(side="left", padx=(4, 12))
        ttk.Label(opt, text="Profile ID", style="Muted.TLabel").pack(side="left")
        ttk.Entry(opt, textvariable=self.xc_profile, width=15).pack(side="left", padx=(4, 10))
        ttk.Label(opt, text="World name", style="Muted.TLabel").pack(side="left")
        ttk.Entry(opt, textvariable=self.xc_name, width=15).pack(side="left", padx=4)
        ttk.Label(pad, text="Expand grows a Classic world to Small/Medium/Large when converting to Windows LCE.",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 0))
        ttk.Button(pad, text="Convert platform  \u2192", style="Accent.TButton",
                   command=self.on_x_platform).pack(anchor="w", pady=(8, 0))

        self._hr(pad).pack(fill="x", pady=16)

        # -- change title update (engine retarget + comprehensive TU0 downgrade) --
        ttk.Label(pad, text="Change title update", style="Value.TLabel").pack(anchor="w")
        ttk.Label(pad, text="Make a console save load on an older/newer TU. TU0 uses the full downgrader "
                            "(any format \u2192 Beta 1.6.6); TU17\u201368 is an exact chunk-version retarget.",
                  style="Muted.TLabel", wraplength=680, justify="left").pack(anchor="w", pady=(0, 4))
        self.xt_src = tk.StringVar(); self.xt_out = tk.StringVar(); self.xt_emu = tk.BooleanVar(value=False)
        self._conv_srcrow(pad, self.xt_src)
        tr = ttk.Frame(pad); tr.pack(fill="x", pady=(2, 2))
        ttk.Label(tr, text="Target TU", style="Muted.TLabel", width=9).pack(side="left")
        self.xt_tu = tk.StringVar(value="0")
        ttk.Combobox(tr, textvariable=self.xt_tu, values=[str(i) for i in range(76)],
                     state="readonly", width=6).pack(side="left")
        ttk.Checkbutton(tr, text="emulator folder output", variable=self.xt_emu).pack(side="left", padx=16)
        self._conv_outrow(pad, self.xt_out)
        ttk.Button(pad, text="Convert title update  \u2192", style="Accent.TButton",
                   command=self.on_x_titleupdate).pack(anchor="w", pady=(8, 0))

        self._hr(pad).pack(fill="x", pady=16)

        # -- export to Java (engine) --
        ttk.Label(pad, text="Export to Java Edition", style="Value.TLabel").pack(anchor="w")
        ttk.Label(pad, text="Writes a Java world folder (Anvil). Every console chunk format is supported; "
                            "blocks, lighting, biomes, entities and chests all come across.",
                  style="Muted.TLabel", wraplength=680, justify="left").pack(anchor="w", pady=(0, 4))
        self.xj_src = tk.StringVar(); self.xj_out = tk.StringVar(); self.xj_plat = tk.StringVar(value="Xbox 360")
        self._conv_srcrow(pad, self.xj_src)
        jr = ttk.Frame(pad); jr.pack(fill="x", pady=(2, 2))
        ttk.Label(jr, text="From", style="Muted.TLabel", width=9).pack(side="left")
        ttk.Combobox(jr, textvariable=self.xj_plat, values=plats, state="readonly", width=13).pack(side="left")
        ttk.Label(jr, text="auto", style="Muted.TLabel").pack(side="left", padx=(6, 0))
        self.xj_src.trace_add("write", lambda *a: self._autoplat(self.xj_src, self.xj_plat))
        self._conv_outrow(pad, self.xj_out)
        ttk.Button(pad, text="Export to Java  \u2192", style="Accent.TButton",
                   command=self.on_x_java).pack(anchor="w", pady=(8, 0))

        self._hr(pad).pack(fill="x", pady=16)

        # -- Java -> Xbox 360 (LCE Studio's own converter; the engine can't read Java) --
        ttk.Label(pad, text="Java  \u2192  Xbox 360 (LCE)", style="Value.TLabel").pack(anchor="w")
        ttk.Label(pad, text="Import a Java world (Alpha / McRegion / Anvil) onto a console template.",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 4))
        self.cv_java = tk.StringVar(); self.cv_name = tk.StringVar()
        jr2 = ttk.Frame(pad); jr2.pack(fill="x", pady=(2, 2))
        ttk.Label(jr2, text="Java world", style="Muted.TLabel", width=9).pack(side="left")
        ttk.Entry(jr2, textvariable=self.cv_java, width=46).pack(side="left", fill="x", expand=True)
        ttk.Button(jr2, text="Browse\u2026", command=lambda: self._pick_dir(self.cv_java)).pack(side="left", padx=(6, 0))
        nr = ttk.Frame(pad); nr.pack(fill="x", pady=(2, 2))
        ttk.Label(nr, text="Name", style="Muted.TLabel", width=9).pack(side="left")
        ttk.Entry(nr, textvariable=self.cv_name, width=22).pack(side="left")
        ttk.Label(nr, text="Base world", style="Muted.TLabel").pack(side="left", padx=(16, 4))
        self._skel_map = {}; self.cv_skel = tk.StringVar()
        self.cv_skel_box = ttk.Combobox(nr, textvariable=self.cv_skel, width=28, state="readonly")
        self.cv_skel_box.pack(side="left")
        ttk.Button(nr, text="Browse\u2026", command=self._pick_skeleton).pack(side="left", padx=(4, 0))
        self._refresh_skeletons()
        ttk.Button(pad, text="Convert to Xbox 360  \u2192", style="Accent.TButton",
                   command=self.on_convert_to_lce).pack(anchor="w", pady=(8, 0))

        self._hr(pad).pack(fill="x", pady=16)

        # -- re-sign an Xbox 360 CON (engine) --
        ttk.Label(pad, text="Re-sign an Xbox 360 .bin (CON)", style="Value.TLabel").pack(anchor="w")
        ttk.Label(pad, text="Rehash + console-sign a CON package so it loads on RGH/JTAG and emulators. "
                            "Optionally refile it under a different Profile ID (gamertag).",
                  style="Muted.TLabel", wraplength=680, justify="left").pack(anchor="w", pady=(0, 4))
        self.xr_src = tk.StringVar(); self.xr_out = tk.StringVar(); self.xr_pid = tk.StringVar()
        rr = ttk.Frame(pad); rr.pack(fill="x", pady=(2, 2))
        ttk.Label(rr, text="CON .bin", style="Muted.TLabel", width=9).pack(side="left")
        ttk.Entry(rr, textvariable=self.xr_src, width=46).pack(side="left", fill="x", expand=True)
        ttk.Button(rr, text="Browse…", command=lambda: self._pick_file(self.xr_src)).pack(side="left", padx=(6, 0))
        pr = ttk.Frame(pad); pr.pack(fill="x", pady=(2, 2))
        ttk.Label(pr, text="Profile ID", style="Muted.TLabel", width=9).pack(side="left")
        ttk.Entry(pr, textvariable=self.xr_pid, width=20).pack(side="left")
        ttk.Label(pr, text="optional — blank keeps the current owner", style="Muted.TLabel").pack(side="left", padx=8)
        orow = ttk.Frame(pad); orow.pack(fill="x", pady=(2, 2))
        ttk.Label(orow, text="Output", style="Muted.TLabel", width=9).pack(side="left")
        ttk.Entry(orow, textvariable=self.xr_out, width=46).pack(side="left", fill="x", expand=True)
        ttk.Label(orow, text="blank = <name>.resigned.bin beside the source",
                  style="Muted.TLabel").pack(side="left", padx=8)
        ttk.Button(pad, text="Re-sign  →", style="Accent.TButton",
                   command=self.on_x_resign).pack(anchor="w", pady=(8, 0))

        self._hr(pad).pack(fill="x", pady=16)

        # -- repair & relight (existing) --
        ttk.Label(pad, text="Repair & relight", style="Value.TLabel").pack(anchor="w")
        ttk.Label(pad, text="Scan & fix the crash hazards that hard-freeze a save (orphaned/duplicate "
                            "tile-entities, broken entities), or rebuild lighting. Blocks are never touched; "
                            "a .bak backup is written.", style="Muted.TLabel",
                  wraplength=680, justify="left").pack(anchor="w", pady=(0, 4))
        self.rp_save = tk.StringVar()
        self._conv_srcrow(pad, self.rp_save)
        rb = ttk.Frame(pad); rb.pack(anchor="w", pady=(8, 0))
        ttk.Button(rb, text="Scan & fix crashes  \u2192", style="Accent.TButton",
                   command=self.on_repair).pack(side="left")
        ttk.Button(rb, text="Recompute lighting  \u2192", command=self.on_relight).pack(side="left", padx=(8, 0))

        self.cv_log = tk.StringVar(value="")
        ttk.Label(pad, textvariable=self.cv_log, style="Muted.TLabel",
                  wraplength=680, justify="left").pack(anchor="w", pady=(14, 0))

    # ---- engine-backed conversion handlers ----
    def on_x_platform(self):
        src = self.xc_src.get().strip() or self.path
        if not src:
            messagebox.showwarning("Convert", "Pick a save (or open one first)."); return
        frm = self._PLATCODE.get(self.xc_from.get(), "xbox360")
        to = self.xc_to.get(); out = self.xc_out.get().strip() or None
        pid = self.xc_profile.get().strip() or None
        name = self.xc_name.get().strip() or None
        exp = {"Small": "small", "Medium": "medium", "Large": "large"}.get(self.xc_expand.get())
        from .converter import lce_engine as E

        def work():
            if to == "Windows LCE":
                if frm == "windows_lce":
                    raise _InputError("Source is already Windows LCE \u2014 choose a console 'From'.")
                return E.convert_console_to_win64(src, frm, out, expand=exp, log=self._logcb())
            if frm != "windows_lce":
                raise _InputError("Xbox 360 \u2194 PS3 goes through Windows LCE: convert this save to "
                                  "Windows LCE first, then convert that to the other console.")
            if to == "PS3":
                return E.convert_win64_to_console(src, "ps3", out_path=out, world_name=name, log=self._logcb())
            emu = (to == "Xbox 360 (emulator folder)")
            return E.convert_win64_to_console(src, "xbox360", out_path=out, emulator=emu,
                                              profile_id=pid, world_name=name, log=self._logcb())
        self._run_async(work, on_done=self._conv_done("Converted platform"), msg="Converting platform\u2026")

    def on_x_titleupdate(self):
        src = self.xt_src.get().strip() or self.path
        if not src:
            messagebox.showwarning("Convert", "Pick a console save (or open one first)."); return
        try:
            tu = int(self.xt_tu.get())
        except ValueError:
            messagebox.showwarning("Convert", "Pick a target title update."); return
        out = self.xt_out.get().strip() or None
        emu = self.xt_emu.get()

        def work():
            if tu == 0:                                   # LCE Studio's comprehensive any-format -> TU0
                from . import convert
                w = World.open(src)
                convert.downgrade_to_tu0(w, log=self._logcb())
                dst = self._conv_out(src, out, "TU0")
                import os
                os.makedirs(dst, exist_ok=True)
                return w.save(out=dst, backup=True, progress=self._progress)
            from .converter import lce_engine as E       # exact chunk-version retarget (TU17-68)
            return E.convert_console_to_console(src, "xbox360", target_tu=tu, out_path=out,
                                                emulator=emu, log=self._logcb())
        self._run_async(work, on_done=self._conv_done("Converted to TU%d" % tu),
                        msg="Converting title update\u2026")

    def on_x_java(self):
        src = self.xj_src.get().strip() or self.path
        if not src:
            messagebox.showwarning("Convert", "Pick a console save (or open one first)."); return
        plat = self._PLATCODE.get(self.xj_plat.get(), "xbox360")
        out = self.xj_out.get().strip() or None
        from .converter import lce_engine as E
        self._run_async(lambda: E.convert_lce_to_java(src, plat, out, log=self._logcb()),
                        on_done=self._conv_done("Exported to Java"), msg="Exporting to Java\u2026")

    def on_x_resign(self):
        src = self.xr_src.get().strip()
        if not src:
            messagebox.showwarning("Re-sign", "Pick a CON .bin package."); return
        out = self.xr_out.get().strip() or (src.rsplit(".", 1)[0] + ".resigned.bin")
        pid = self.xr_pid.get().strip() or None
        from .converter import lce_engine as E

        def work():
            raw = open(src, "rb").read()
            data = E.set_profile_id(raw, pid) if pid else E.resign_con(raw)   # set_profile_id rehashes+signs
            open(out, "wb").write(data)
            return out
        self._run_async(work, on_done=self._conv_done("Re-signed"), msg="Re-signing package\u2026")

    def _conv_done(self, verb):
        def done(res):
            self.cv_log.set("%s \u2192 %s" % (verb, res))
            messagebox.showinfo("Convert", "%s.\n\nWritten to:\n%s" % (verb, res))
        return done

    def _rp_out(self, w):
        import os
        if getattr(w, "_con_src", None):
            base = os.path.splitext(os.path.basename(w._con_src))[0]
            out = os.path.join(os.path.dirname(w._con_src), base + " fixed.bin")
            os.makedirs(out, exist_ok=True); return out
        return None

    def on_repair(self):
        src = self.rp_save.get().strip() or self.path
        if not src:
            messagebox.showwarning("Repair", "Pick a console save (or open one first)."); return

        def work():
            w = World.open(src)                          # auto-unpacks a CON package
            st = w.repair_world(log=self._logcb())
            total = len(st["orphan_te"]) + st["bad_pos_te"] + st["dup_te"] + st["bad_entities"]
            if not total:
                return None, st
            return w.save(out=self._rp_out(w), backup=True, progress=self._progress), st

        def done(r):
            path, st = r
            if path is None:
                self.cv_log.set("Scanned — clean. No crash hazards found.")
                messagebox.showinfo("Repair", "Clean! No orphaned/duplicate tile-entities or broken entities found.")
                return
            n_orph = len(st["orphan_te"])
            summary = ("orphaned tile-entities: %d · out-of-range: %d · duplicate: %d · broken entities: %d"
                       % (n_orph, st["bad_pos_te"], st["dup_te"], st["bad_entities"]))
            self.cv_log.set("Repaired → %s  (.bak kept)\n%s" % (path, summary))
            preview = "\n".join("  • orphaned %s at (%s, %s, %s)" % (t, x, y, z) for t, x, y, z in st["orphan_te"][:10])
            messagebox.showinfo("Repair",
                                "Fixed crash hazards (blocks untouched, .bak written):\n\n%s\n\n%s" % (summary, preview))

        self._run_async(work, on_done=done, msg="Scanning for crash hazards…")

    def on_relight(self):
        src = self.rp_save.get().strip() or self.path
        if not src:
            messagebox.showwarning("Relight", "Pick a console save (or open one first)."); return

        def work():
            w = World.open(src)
            n = w.recompute_lighting(log=self._logcb())
            return w.save(out=self._rp_out(w), backup=True, progress=self._progress), n

        def done(r):
            path, n = r
            self.cv_log.set("Recomputed lighting for %d chunks → %s  (.bak kept)" % (n, path))
            messagebox.showinfo("Recompute lighting",
                                "Rebuilt HeightMap + SkyLight + BlockLight for %d chunks.\n"
                                "A .bak backup was written." % n)

        self._run_async(work, on_done=done, msg="Recomputing lighting (this can take a minute)…")

    def _pick_dir(self, var):
        d = filedialog.askdirectory()
        if d:
            var.set(d)

    def _pick_any(self, var):
        p = filedialog.askdirectory(title="Console save (.bin folder)")
        if not p:
            p = filedialog.askopenfilename(title="or a savegame.dat")
        if p:
            var.set(p)

    def _pick_file(self, var):
        p = filedialog.askopenfilename(title="Raw STFS CON package (or savegame.dat)",
                                       filetypes=[("Console save", "*.bin"), ("All files", "*.*")])
        if p:
            var.set(p)

    def _refresh_skeletons(self):
        """Populate the base-world picker with every usable TU0 Save*.bin found."""
        import glob
        m = {}
        if self.path:
            base = self.path if os.path.isdir(self.path) else os.path.dirname(self.path)
            if os.path.exists(os.path.join(base, "savegame.dat")):
                m["(the open save)   " + os.path.basename(base)] = base
        for g in sorted(glob.glob(r"C:/Nexia360/Library/*/NO_TU/Content/*/00000001/*.bin")):
            if os.path.isdir(g) and os.path.exists(os.path.join(g, "savegame.dat")):
                m.setdefault(os.path.basename(g), g)
        self._skel_map = m
        self.cv_skel_box["values"] = list(m.keys())
        if m and not self.cv_skel.get():
            self.cv_skel.set(next(iter(m)))

    def _pick_skeleton(self):
        p = filedialog.askdirectory(title="Pick a TU0 Save*.bin to use as the base world")
        if not p:
            return
        if not os.path.exists(os.path.join(p, "savegame.dat")):
            messagebox.showwarning("Base world", "That folder has no savegame.dat."); return
        disp = "(custom)   " + os.path.basename(p.rstrip("/\\"))
        self._skel_map[disp] = p
        self.cv_skel_box["values"] = list(self._skel_map.keys())
        self.cv_skel.set(disp)

    def _find_template(self):
        import glob
        for g in glob.glob(r"C:/Nexia360/Library/*/NO_TU/Content/*/00000001/*.bin"):
            if os.path.isdir(g) and os.path.exists(os.path.join(g, "savegame.dat")):
                return g
        return None

    def on_convert_to_lce(self):
        if self._busy:
            return
        java = self.cv_java.get().strip()
        if not java or not os.path.isdir(java):
            messagebox.showwarning("Convert", "Pick a Java world folder first."); return
        template = self._skel_map.get(self.cv_skel.get()) or self._find_template()
        if not template:
            messagebox.showinfo("Template needed", "Pick any existing TU0 Save*.bin to use as the "
                                "skeleton (its level.dat / player structure).")
            template = filedialog.askdirectory(title="Pick a TU0 Save*.bin template")
            if not template:
                return
        out = filedialog.asksaveasfilename(title="Save converted world as", defaultextension=".bin",
                                           initialfile=(self.cv_name.get().strip() or "Converted") + ".bin")
        if not out:
            return
        from . import convert
        name = self.cv_name.get().strip() or None
        self._run_async(lambda: convert.convert(java, template, out, level_name=name, log=self._logcb()) or out,
                        on_done=lambda o: (self.cv_log.set("Converted Java → Xbox 360:\n%s" % o),
                                           messagebox.showinfo("Convert", "Done:\n%s" % o)),
                        msg="Converting Java → Xbox 360…")

    def _build_io(self):
        f = self.tab_recover
        imp = ttk.LabelFrame(f, text="Import a save  —  bring a world into your emulator")
        imp.pack(fill="x", padx=10, pady=10)
        ttk.Label(imp, text="Source may be a Save*.bin folder, a savegame.dat, a .zip backup, "
                            "or an STFS console package.", foreground="#555").grid(
            row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(6, 2))
        ttk.Button(imp, text="Import file  (.dat / .zip / STFS)…", width=32,
                   command=self.on_import_file).grid(row=1, column=0, padx=8, pady=4, sticky="w")
        ttk.Button(imp, text="Import Save*.bin folder…", width=26,
                   command=self.on_import_folder).grid(row=1, column=1, padx=4, pady=4, sticky="w")
        ttk.Label(imp, text="Destination (your saves folder):").grid(row=2, column=0, sticky="w", padx=8, pady=(6, 0))
        self.io_dest = tk.StringVar(value=self._default_saves_dir())
        ttk.Entry(imp, textvariable=self.io_dest, width=78).grid(row=3, column=0, columnspan=2, sticky="we", padx=8, pady=2)
        ttk.Button(imp, text="Change…", command=self.on_io_dest).grid(row=3, column=2, padx=6)

        exp = ttk.LabelFrame(f, text="Export a save  —  back up or share the open world")
        exp.pack(fill="x", padx=10, pady=10)
        ttk.Button(exp, text="Export current world → folder…", width=32,
                   command=self.on_export_current).grid(row=0, column=0, padx=8, pady=6, sticky="w")
        ttk.Button(exp, text="Export current world → .zip…", width=30,
                   command=self.on_export_zip).grid(row=0, column=1, padx=4, pady=6, sticky="w")
        ttk.Label(exp, text="Writes the open world (with your edits) as a console-loadable Save*.bin.",
                  foreground="#555").grid(row=1, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

        pk = ttk.LabelFrame(f, text="Packages  —  STFS/CON pack & unpack (emulator ↔ console)")
        pk.pack(fill="x", padx=10, pady=10)
        ttk.Button(pk, text="Unpack CON → folder…", width=24,
                   command=self.on_stfs_unpack).grid(row=0, column=0, padx=8, pady=6, sticky="w")
        ttk.Button(pk, text="Unpack ALL CONs in a folder…", width=28,
                   command=self.on_stfs_unpackall).grid(row=0, column=1, padx=4, pady=6, sticky="w")
        ttk.Button(pk, text="Pack folder → CON…", width=22,
                   command=self.on_stfs_pack).grid(row=0, column=2, padx=4, pady=6, sticky="w")
        ttk.Label(pk, text="Nexia360 lists saves as unpacked Save*.bin FOLDERS, not raw CON packages — "
                           "unpack CONs to make them appear. Pack rebuilds a CON for RGH/JTAG or sharing "
                           "(retail needs a KV resign).",
                  foreground="#555", wraplength=820, justify="left").grid(
            row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 6))

        pr = ttk.LabelFrame(f, text="Reassign profile  —  move a CON save to a new account")
        pr.pack(fill="x", padx=10, pady=10)
        ttk.Button(pr, text="Load CON/LIVE/PIRS…", command=self.on_stfs_load).grid(
            row=0, column=0, padx=8, pady=6, sticky="w")
        self.stfs_info = tk.StringVar(value="No package loaded.")
        ttk.Label(pr, textvariable=self.stfs_info, foreground="#555").grid(
            row=0, column=1, columnspan=2, sticky="w", padx=8)
        ttk.Label(pr, text="New account XUID:").grid(row=1, column=0, sticky="e", padx=8, pady=4)
        self.stfs_xuid = tk.StringVar()
        ttk.Entry(pr, textvariable=self.stfs_xuid, width=22).grid(row=1, column=1, sticky="w", padx=4)
        ttk.Button(pr, text="Reassign & Save…", command=self.on_stfs_reassign).grid(
            row=1, column=2, padx=8, sticky="w")
        ttk.Label(pr, text="Sets the profile to the new XUID and zeros the console/device IDs, then "
                           "rehashes the header. Loads on Nexia360 / RGH-JTAG; a stock retail console "
                           "additionally needs a KV resign (do that step in Velocity/Horizon).",
                  foreground="#555", wraplength=820, justify="left").grid(
            row=2, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 6))

        chk = ttk.LabelFrame(f, text="Health check"); chk.pack(fill="x", padx=10, pady=10)
        ttk.Button(chk, text="Check the open save", command=self.on_health_open).grid(row=0, column=0, padx=8, pady=6, sticky="w")
        self.io_log = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.io_log, foreground="#207a3c", wraplength=820, justify="left").pack(
            fill="x", padx=12, pady=6, anchor="w")

    def _default_saves_dir(self):
        if self.path:
            p = self.path if os.path.isdir(self.path) else os.path.dirname(self.path)
            return os.path.dirname(p)
        import glob
        for g in glob.glob(r"C:/Nexia360/Library/*/NO_TU/Content/*/00000001"):
            return g
        return os.path.expanduser("~")

    def on_io_dest(self):
        d = filedialog.askdirectory(title="Destination saves folder")
        if d:
            self.io_dest.set(d)

    # ---- import ----
    def _read_source(self, path):
        """(savegame_bytes, thumbnail_or_None, suggested_name) from a folder/file."""
        from . import stfs
        import zipfile, glob as _g
        if os.path.isdir(path):
            dat = os.path.join(path, "savegame.dat")
            if not os.path.exists(dat):
                hits = _g.glob(os.path.join(path, "**", "savegame.dat"), recursive=True)
                dat = hits[0] if hits else None
            if not dat:
                raise ValueError("no savegame.dat found in that folder")
            th = os.path.join(os.path.dirname(dat), "__thumbnail.png")
            thumb = open(th, "rb").read() if os.path.exists(th) else None
            return open(dat, "rb").read(), thumb, os.path.basename(path.rstrip("/\\")).replace(".bin", "")
        raw = open(path, "rb").read()
        if raw[:4] == b"PK\x03\x04":
            z = zipfile.ZipFile(path)
            dn = next(n for n in z.namelist() if n.endswith("savegame.dat"))
            thumb = next((z.read(n) for n in z.namelist() if n.endswith("__thumbnail.png")), None)
            return z.read(dn), thumb, os.path.splitext(os.path.basename(path))[0]
        if raw[:4] in (b"CON ", b"LIVE", b"PIRS"):
            s = stfs.STFS(raw)
            name = next((n for n in s.files if n.endswith("savegame.dat")), None) \
                or next((n for n in s.files if n.endswith(".dat")), None)
            if not name:
                raise ValueError("no savegame.dat inside the STFS package")
            return s.read_file(name), s.thumbnail, (s.display_name or "imported").strip() or "imported"
        return raw, None, os.path.splitext(os.path.basename(path))[0]

    def _do_import(self, path):
        from . import recover
        dest = self.io_dest.get()

        def work():
            data, thumb, name = self._read_source(path)
            recover.decode(data, tolerant=True)             # validate it decodes
            safe = "".join(c for c in name if c.isalnum() or c in " _-").strip() or "Imported"
            outdir = os.path.join(dest, "%s.bin" % safe); i = 1
            while os.path.exists(outdir):
                outdir = os.path.join(dest, "%s (%d).bin" % (safe, i)); i += 1
            os.makedirs(outdir, exist_ok=True)
            open(os.path.join(outdir, "savegame.dat"), "wb").write(data)
            if thumb:
                open(os.path.join(outdir, "__thumbnail.png"), "wb").write(thumb)
            return outdir

        def done(outdir):
            self.io_log.set("Imported → %s" % outdir)
            if messagebox.askyesno("Import", "Imported to:\n%s\n\nOpen it now?" % outdir):
                self.load(outdir)

        self._run_async(work, on_done=done, msg="Importing save…")

    def on_import_file(self):
        p = filedialog.askopenfilename(title="Import a save file",
                                       filetypes=[("saves", "*.dat *.zip *.bin"), ("all", "*.*")])
        if p:
            self._do_import(p)

    def on_import_folder(self):
        d = filedialog.askdirectory(title="Import a Save*.bin folder")
        if d:
            self._do_import(d)

    # ---- export ----
    def _safe_name(self):
        n = self.world.level.get_value("LevelName") or "world"
        return "".join(c for c in n if c.isalnum() or c in " _-").strip() or "world"

    def _carry_thumb(self, outdir):
        if self.path:
            src = self.path if os.path.isdir(self.path) else os.path.dirname(self.path)
            t = os.path.join(src, "__thumbnail.png")
            if os.path.exists(t):
                import shutil; shutil.copy(t, os.path.join(outdir, "__thumbnail.png"))

    def on_export_current(self):
        if not self._guard():
            return
        d = filedialog.askdirectory(title="Export into this folder")
        if not d:
            return
        w = self.world
        outdir = os.path.join(d, "%s.bin" % self._safe_name())

        def work():
            os.makedirs(outdir, exist_ok=True)
            w.save(out=outdir, backup=False, verify=True, progress=self._progress)
            self._carry_thumb(outdir)
            return outdir

        self._run_async(work,
                        on_done=lambda o: (self.io_log.set("Exported → %s" % o),
                                           messagebox.showinfo("Export", "Exported console-loadable "
                                                               "save to:\n%s" % o)),
                        msg="Exporting world…")

    def on_export_zip(self):
        if not self._guard():
            return
        p = filedialog.asksaveasfilename(title="Export as .zip", defaultextension=".zip",
                                         filetypes=[("zip", "*.zip")])
        if not p:
            return
        w = self.world

        def work():
            import zipfile, tempfile, shutil
            tmp = tempfile.mkdtemp(); binname = "%s.bin" % self._safe_name()
            bindir = os.path.join(tmp, binname); os.makedirs(bindir)
            w.save(out=bindir, backup=False, verify=True, progress=self._progress)
            self._carry_thumb(bindir)
            with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
                for fn in os.listdir(bindir):
                    z.write(os.path.join(bindir, fn), os.path.join(binname, fn))
            shutil.rmtree(tmp, ignore_errors=True)
            return p

        self._run_async(work,
                        on_done=lambda o: (self.io_log.set("Exported → %s" % o),
                                           messagebox.showinfo("Export", "Exported zip:\n%s" % o)),
                        msg="Exporting .zip…")

    # ---- health (single, fast) ----
    def on_health_open(self):
        if not self.path:
            messagebox.showinfo("Health", "Open a save first."); return
        from . import recover
        p = self.path if os.path.isfile(self.path) else os.path.join(self.path, "savegame.dat")

        def work():                                   # decoding the whole save is slow -> off-thread
            return recover.health(open(p, "rb").read())

        def done(h):
            self.io_log.set("%s — format=%s, decoded %d/%d bytes, %d files"
                            % ("HEALTHY" if h.get("healthy") else "CORRUPT", h.get("format"),
                               h.get("recovered", 0), h.get("dec_size", 0), len(h.get("files", []))))
        self._run_async(work, on_done=done, msg="Checking save health…")

    def on_stfs_load(self):
        p = filedialog.askopenfilename(title="STFS package (CON / LIVE / PIRS)",
                                       filetypes=[("STFS package", "*.bin"), ("All files", "*.*")])
        if not p:
            return
        import struct
        from . import stfs

        def work():                                   # reading + parsing a package off-thread
            raw = open(p, "rb").read()
            s = stfs.STFS(raw)
            tid = struct.unpack_from(">I", raw, 0x360)[0]
            pid = raw[0x371:0x379].hex()
            return raw, s.display_name or "?", tid, pid

        def done(r):
            raw, name, tid, pid = r
            self._stfs_raw, self._stfs_path = raw, p
            self.stfs_info.set("%s   |   title %08X   |   profile %s" % (name, tid, pid))
            self.stfs_xuid.set(pid.upper())
        self._run_async(work, on_done=done, msg="Reading STFS package…")

    def on_stfs_reassign(self):
        try:
            if not getattr(self, "_stfs_raw", None):
                raise _InputError("Load a CON/LIVE/PIRS package first.")
            x = self.stfs_xuid.get().strip()
            if not x:
                raise _InputError("Enter the new account's XUID (16 hex digits).")
            from . import stfs
            out = filedialog.asksaveasfilename(
                title="Save reassigned package", defaultextension=".bin",
                initialfile=os.path.basename(self._stfs_path).replace(".bin", "") + ".reassigned.bin")
            if not out:
                return
            raw = self._stfs_raw

            def work():                               # rehash + write off-thread
                data = stfs.reassign_profile(raw, x)
                open(out, "wb").write(data)
                return data[0x371:0x379].hex()

            def done(pid):
                self.io_log.set("Reassigned to profile %s (console/device zeroed), header rehashed.\n"
                                "Saved: %s\nLoads on Nexia360 / RGH-JTAG. For a stock retail console, "
                                "resign this file in Velocity/Horizon with that console's KV.bin."
                                % (pid, out))
            self._run_async(work, on_done=done, msg="Rehashing & writing package…")
        except Exception as e:
            self._err(e)

    def on_stfs_unpack(self):
        p = filedialog.askopenfilename(title="Choose a CON/LIVE/PIRS package",
                                       filetypes=[("STFS package", "*.bin"), ("All files", "*.*")])
        if not p:
            return
        out = filedialog.askdirectory(title="Where to create the unpacked save folder")
        if not out:
            return
        from . import stfs

        def work():
            info = stfs.unpack_con(p, os.path.join(out, os.path.basename(p) if p.lower().endswith(".bin")
                                                    else os.path.basename(p) + ".bin"))
            return info

        self._run_async(work,
                        on_done=lambda info: self.io_log.set("Unpacked %r → folder (%s)."
                                                             % (info["name"], ", ".join(info["files"]))),
                        msg="Unpacking CON…")

    def on_stfs_unpackall(self):
        d = filedialog.askdirectory(title="Folder of CON packages (e.g. your Nexia 00000001 saves dir)")
        if not d:
            return
        if not messagebox.askyesno("Unpack all", "Unpack every CON in:\n%s\n\nEach CON is replaced by a "
                                   "same-named save FOLDER (verified first). Back up the folder before "
                                   "you do this. Continue?" % d):
            return
        import shutil
        from . import stfs

        def work():
            done = skipped = failed = 0
            for e in sorted(os.listdir(d)):
                p = os.path.join(d, e)
                if not (os.path.isfile(p) and stfs.is_con(p)):
                    continue
                tmp = p + ".__unpacking"
                try:
                    s = stfs.STFS(open(p, "rb").read())
                    if not any(n.endswith("savegame.dat") for n in s.files):
                        skipped += 1; continue
                    if os.path.exists(tmp):
                        shutil.rmtree(tmp)
                    stfs.unpack_con(p, tmp)
                    World.open(tmp)
                    final = e if e.lower().endswith(".bin") else e + ".bin"
                    os.remove(p); os.rename(tmp, os.path.join(d, final)); done += 1
                except Exception:
                    try: shutil.rmtree(tmp)
                    except Exception: pass
                    failed += 1
            return (done, skipped, failed)

        self._run_async(work,
                        on_done=lambda r: self.io_log.set("Unpacked %d CONs → folders (skipped %d non-worlds, "
                                                          "%d failed/kept as CON)." % r),
                        msg="Unpacking all CONs…")

    def on_stfs_pack(self):
        folder = filedialog.askdirectory(title="Save folder to pack (contains savegame.dat)")
        if not folder:
            return
        tpl = filedialog.askopenfilename(title="Template CON (any of your CON saves — supplies the certificate)",
                                         filetypes=[("STFS package", "*.bin"), ("All files", "*.*")])
        if not tpl:
            return
        out = filedialog.asksaveasfilename(title="Save packaged CON", defaultextension=".bin",
                                           initialfile=os.path.basename(folder.rstrip("/\\")))
        if not out:
            return
        from . import stfs
        name = self._world_name(self.world) if self.world else None

        self._run_async(lambda: stfs.build_con(folder, out, template=tpl, display_name=name),
                        on_done=lambda o: self.io_log.set("Packed → CON:\n%s\nLoads on Nexia360 / RGH-JTAG "
                                                          "(retail needs a KV resign)." % o),
                        msg="Packing folder → CON…")

    # ================================================================ actions
    def _guard(self):
        if self.world is None:
            messagebox.showwarning("No save", "Open a save first."); return False
        if self._busy:
            self.status.set("Please wait — an operation is running…"); return False
        return True

    def _err(self, e):
        # friendly for user-input problems; full traceback only for real bugs
        if isinstance(e, (_InputError, ValueError, tk.TclError)):
            messagebox.showinfo("Check your input",
                                "Please fill the fields with valid whole numbers.\n\n%s" % e)
            return
        if isinstance(e, (FileNotFoundError, PermissionError, OSError)):
            messagebox.showwarning("File problem", str(e)); return
        messagebox.showerror("Error", "%s\n\n%s" % (e, traceback.format_exc()))

    def _num(self, var, label):
        """Parse a required whole number from an Entry/StringVar."""
        s = str(var.get()).strip()
        if not s:
            raise _InputError("%s is required." % label)
        try:
            return int(s)
        except ValueError:
            raise _InputError("%s must be a whole number (got %r)." % (label, s))

    def _numo(self, var, label, default):
        """Optional whole number: blank -> default."""
        s = str(var.get()).strip()
        if not s:
            return default
        try:
            return int(s)
        except ValueError:
            raise _InputError("%s must be a whole number (got %r)." % (label, s))

    def _iv(self, intvar, default):
        try:
            return int(intvar.get())
        except Exception:
            return default

    def load(self, path):
        self._run_async(lambda: self._open_any(path),
                        on_done=lambda r: self._loaded(r, path),
                        msg="Opening %s…" % os.path.basename(str(path).rstrip("/\\")))

    def _open_any(self, path):
        """Return (World, meta). If path is a CON/LIVE/PIRS package, read the world
        name + profile + thumbnail straight from its STFS header (all from the save)."""
        meta = None
        if os.path.isfile(path):
            raw = open(path, "rb").read()
            if raw[:4] in (b"CON ", b"LIVE", b"PIRS"):
                from . import stfs
                import tempfile
                s = stfs.STFS(raw)
                nm = next((n for n in s.files if n.endswith("savegame.dat")), None)
                if not nm:
                    raise ValueError("no savegame.dat inside the package")
                tmp = os.path.join(tempfile.mkdtemp(), "savegame.dat")
                open(tmp, "wb").write(s.read_file(nm))
                meta = {"name": s.display_name, "profile": s.profile_id,
                        "thumb": s.thumbnail, "con": True}
                # the extracted temp path has no Content\<XUID> folder, so hand the
                # STFS profile id in as the owner to pick the right player of many
                return World.open(tmp, owner=s.profile_id), meta
        return World.open(path), meta

    def _loaded(self, r, path):
        self.world, self._save_meta = r
        self.path = path
        self._map_vw = None                          # drop the cached map world for the old save
        self._nbt_index = None                       # stale search index for the old save
        self.refresh_all()
        self._update_chrome()
        self.status.set("Opened: %s" % path)
        try:                                          # opened from the gallery -> show the editor
            if self.nb.select() == str(self.tab_library):
                self._select_tab(self.tab_overview)
        except Exception:
            pass

    def _world_name(self, w):
        if self._save_meta and self._save_meta.get("name"):
            return self._save_meta["name"]
        ln = w.level.get_value("LevelName")
        if ln and str(ln).strip().lower() != "world":
            return str(ln)
        base = os.path.basename(str(self.path).rstrip("/\\"))          # imported saves are named by world
        return (base[:-4] if base.endswith(".bin") else base) or str(ln) or "World"

    def refresh_all(self):
        w = self.world
        lvl = w.level
        sx, sy, sz = w.get_spawn()
        name = self._world_name(w)
        seed = lvl.get_value("RandomSeed")
        day = ((lvl.get_value("Time") or 0) // 24000) + 1
        weather = ("Thunder" if lvl.get_value("thundering")
                   else "Rain" if lvl.get_value("raining") else "Clear")
        regs = [n for n in w._filedata if n.endswith(".mcr") and not n.startswith("DIM")]
        nitems = len(w.inventory())
        pl = w.get_player_pos()
        hp = w.player.get_value("Health")
        dims = ["Overworld"]
        if any(n.startswith("DIM-1") for n in w._filedata):
            dims.append("Nether")
        if any(n.startswith("DIM1/") for n in w._filedata):
            dims.append("End")
        # header
        self.ov_title.set(str(name) or "World")
        self.ov_summary.set("Seed %s   ·   Day %d   ·   %s" % (seed, day, weather))
        self.ov_sub.set(str(self.path))
        # WORLD
        self.ov["Name"].set(str(name))
        self.ov["Seed"].set(str(seed) if seed is not None else "—")
        self.ov["Day"].set(str(day))
        self.ov["Weather"].set(weather)
        self.ov["Played"].set(self._fmt_time(lvl.get_value("LastPlayed")))
        # PLAYER
        self.ov["Player"].set("x %.0f · y %.0f · z %.0f" % (pl[0], pl[1], pl[2]) if pl else "—")
        self.ov["Health"].set(("%s / 20" % hp) if hp is not None else "—")
        self.ov["Items"].set("%d item%s" % (nitems, "" if nitems == 1 else "s"))
        self.ov["Dims"].set(" · ".join(dims))
        # STORAGE
        self.ov["Console"].set("Xbox 360" + ("  (from CON)" if self._save_meta else ""))
        self.ov["Format"].set("LCE savegame" + ((" · v%d" % w._ver) if w._ver <= 16 else ""))
        prof = self._save_meta and self._save_meta.get("profile")
        self.ov["Profile"].set(prof.upper() if prof and prof != "0" * 16 else "—")
        self.ov["Regions"].set(str(len(regs)))
        self.ov["Chunks"].set("%d generated" % self._count_chunks(w))
        self.ov["Size"].set(self._fmt_size())
        self._load_thumb()
        for var, val in zip(self.spawn_vars, (sx, sy, sz)):
            var.set(str(val))
        self._show_overview(True)
        self.nbt_file["values"] = list(w._filedata.keys())
        if w._filedata:
            self.nbt_file.current(0)
        self.refresh_inv()
        self._players_refresh()
        self._refresh_skeletons()
        self.render_map()

    def _count_chunks(self, w):
        import struct
        n = 0
        for name, raw in w._filedata.items():
            if name.endswith(".mcr") and not name.startswith("DIM") and len(raw) >= 4096:
                for i in range(1024):
                    if struct.unpack_from(">I", raw, i * 4)[0]:
                        n += 1
        return n

    def _fmt_time(self, lp):
        if not lp:
            return "—"
        try:
            import datetime
            dt = datetime.datetime.fromtimestamp(int(lp) / 1000)
            if 2005 <= dt.year <= 2035:                 # LCE LastPlayed epoch is unreliable
                return dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
        return "—"

    def _fmt_size(self):
        try:
            p = self.path if os.path.isfile(self.path) else os.path.join(self.path, "savegame.dat")
            b = float(os.path.getsize(p))
            for u in ("bytes", "KB", "MB", "GB"):
                if b < 1024 or u == "GB":
                    return ("%.0f %s" % (b, u)) if u == "bytes" else ("%.1f %s" % (b, u))
                b /= 1024
        except Exception:
            return "—"

    def _load_thumb(self):
        img = None
        if _HAVE_PIL:
            from PIL import Image
            import io
            data = None
            if self._save_meta and self._save_meta.get("thumb"):   # from the CON package itself
                data = self._save_meta["thumb"]
            elif self.path:                                        # or the folder's __thumbnail.png
                base = self.path if os.path.isdir(self.path) else os.path.dirname(self.path)
                tp = os.path.join(base, "__thumbnail.png")
                if os.path.exists(tp):
                    data = open(tp, "rb").read()
            if data:
                try:
                    im = Image.open(io.BytesIO(data)).convert("RGB"); im.thumbnail((188, 188))
                    img = ImageTk.PhotoImage(im)
                except Exception:
                    img = None
        if img:
            self._ov_thumb_img = img
            self.ov_thumb.configure(image=img, text="")
        else:
            self._ov_thumb_img = None
            self.ov_thumb.configure(image="", text="  no preview  ", foreground="#a7b0bd")

    def refresh_inv(self):
        n = len(self.world.inventory()) if self.world else 0
        self.inv_summary.set("%d of 36 slots used" % n)
        self._draw_inv_grid()
        self._refresh_player_stats()

    # -- File
    def on_open(self):
        p = filedialog.askopenfilename(
            title="Open a save — pick a savegame.dat (or a Save*.bin / .zip / STFS)",
            initialdir=self._default_saves_dir(),
            filetypes=[("Minecraft 360 save", "savegame.dat"),
                       ("Save files", "*.dat"), ("Packages", "*.bin *.zip"),
                       ("All files", "*.*")])
        if not p:                                       # let folks still pick a .bin folder
            p = filedialog.askdirectory(title="…or pick a Save*.bin folder")
        if p:
            self.load(p)

    def on_save(self):
        if not self._guard():
            return
        if self._save_meta and self._save_meta.get("con"):
            messagebox.showinfo("Save a copy", "This world was opened from a CON package "
                                "(read from a temporary copy). Choose a folder to save your "
                                "edits into a console-loadable Save*.bin.")
            return self.on_save_as()
        w = self.world
        self._run_async(
            lambda: w.save(backup=True, progress=self._progress),   # compressor self-verifies
            on_done=lambda out: (self.status.set("Saved (backup .bak): %s" % out),
                                 messagebox.showinfo("Saved", "Wrote %s\n"
                                                     "(original backed up as .bak)" % out)),
            msg="Saving — encoding chunks…")

    def on_save_as(self):
        if not self._guard():
            return
        d = filedialog.askdirectory(title="Save into folder (savegame.dat)")
        if not d:
            return
        w = self.world
        self._run_async(lambda: w.save(out=d, backup=True, progress=self._progress),
                        on_done=lambda out: self.status.set("Saved as: %s" % out),
                        msg="Saving…")

    # -- Overview
    def on_set_spawn(self):
        if not self._guard():
            return
        try:
            x, y, z = (self._num(v, ax) for v, ax in zip(self.spawn_vars, ("Spawn X", "Spawn Y", "Spawn Z")))
            self.world.set_spawn(x, y, z); self.status.set("Spawn set to (%d,%d,%d)" % (x, y, z))
        except Exception as e:
            self._err(e)

    # -- Inventory
    def on_inv_add(self):
        if not self._guard():
            return
        try:
            target = getattr(self, "_grid_sel", None)
            if target is None:
                raise _InputError("Click a slot in the grid first.")
            inv = self.world._inventory
            inv.items[:] = [it for it in inv.items if it.get_value("Slot") != target]
            slot = self.world.inventory_add(self._num(self.add_id, "Item id"),
                                            self._numo(self.add_cnt, "Count", 1),
                                            self._numo(self.add_dmg, "Damage", 0), slot=target)
            self.refresh_inv(); self._select_slot(slot); self.status.set("Set slot %d" % slot)
        except Exception as e:
            self._err(e)

    def on_inv_remove(self):
        if not self._guard():
            return
        target = getattr(self, "_grid_sel", None)
        if target is None:
            return
        inv = self.world._inventory
        inv.items[:] = [it for it in inv.items if it.get_value("Slot") != target]
        self.world._dirty_player = True
        self.refresh_inv(); self._select_slot(target)

    def on_inv_clear(self):
        if not self._guard():
            return
        self.world.inventory_clear(); self.refresh_inv()

    def _refresh_player_stats(self):
        if not self.world or not hasattr(self, "ps_vars"):
            return
        p = self.world.player
        for k, var in self.ps_vars.items():
            v = p.get_value(k)
            var.set("" if v is None else str(int(v)))
        self.profile_cb["values"] = [n.split("/")[-1] for n in self.world.players()]
        self.profile_var.set(self.world._pkey.split("/")[-1])

    def on_player_stats_set(self):
        if not self._guard():
            return
        try:
            for key, var in self.ps_vars.items():
                s = var.get().strip()
                if s != "":
                    self.world.set_player_stat(key, int(float(s)))
            self.status.set("Player stats updated — File → Save to write.")
        except Exception as e:
            self._err(e)

    def on_profile_switch(self, ev=None):
        if not self.world:
            return
        try:
            self.world.switch_player("players/" + self.profile_var.get())
            self.refresh_inv(); self._refresh_player_stats()
            self.status.set("Now editing profile %s" % self.profile_var.get())
        except Exception as e:
            self._err(e)

    # -- Blocks
    def on_block_get(self):
        if not self._guard():
            return
        try:
            x, y, z = (self._num(self.blk[k], k.upper()) for k in ("x", "y", "z"))
            b = self.world.get_block(x, y, z)
            if b is None:
                self.blk_result.set("(%d,%d,%d) is in an un-generated chunk" % (x, y, z)); return
            from . import names as NM
            self.blk_result.set("block(%d,%d,%d) = %d  %s" % (x, y, z, b, NM.name_for(b) or ""))
        except Exception as e:
            self._err(e)

    def on_block_set(self):
        if not self._guard():
            return
        try:
            x, y, z = (self._num(self.blk[k], k.upper()) for k in ("x", "y", "z"))
            bid = self._num(self.blk["id"], "Block id"); dat = self._numo(self.blk["data"], "Data", 0)
            self.world.set_block(x, y, z, bid, dat)
            self.blk_result.set("set (%d,%d,%d) = %d" % (x, y, z, bid))
        except Exception as e:
            self._err(e)

    def on_fill(self):
        if not self._guard():
            return
        try:                                          # parse inputs on the UI thread
            c = [self._num(self.fillv[k], k) for k in ("x1", "y1", "z1", "x2", "y2", "z2")]
            bid = self._num(self.fillv["id"], "Block id")
        except Exception as e:
            self._err(e); return
        # a big box can touch thousands of chunks -> run off-thread so the window stays live
        self._run_async(lambda: self.world.fill(*c, bid),
                        on_done=lambda n: self.status.set("Filled %d blocks" % n),
                        msg="Filling blocks…")

    # -- Entities
    def _ent_row(self, it):
        pos = it.get_value("Pos")
        self.ent_tree.insert("", "end", values=(
            it.get_value("id"),
            [round(p, 1) for p in pos.items] if pos else "?"))

    def on_ent_list(self):
        if not self._guard():
            return
        self.ent_tree.delete(*self.ent_tree.get_children())
        cx, cz = self.ent_cx.get().strip(), self.ent_cz.get().strip()
        try:
            whole = not (cx and cz)                       # both blank -> list everything
            target = None if whole else (int(cx), int(cz))
        except ValueError:
            messagebox.showinfo("Entities", "Chunk X / Z must be whole numbers.\n"
                                            "Leave both blank to list every entity in the world.")
            return
        def _rows_from(items):
            out = []
            for it in items:
                pos = it.get_value("Pos")
                out.append((it.get_value("id"),
                            [round(p, 1) for p in pos.items] if pos else "?"))
            return out

        def collect():                                # the scan (whole-world = every chunk)
            rows = []
            if whole:
                for _wcx, _wcz, ch in VIZ._overworld_chunks(self.world):
                    if ch.entities:
                        rows += _rows_from(ch.entities.items)
            else:
                e = self.world.entities(*target)
                rows += _rows_from(e.items if e else [])
            return rows

        def done(rows):                               # tree insertion stays on the Tk thread
            for idv, posv in rows:
                self.ent_tree.insert("", "end", values=(idv, posv))
            n = len(rows)
            self.status.set("%d entit%s%s" % (n, "y" if n == 1 else "ies",
                                              "" if whole else " in chunk %s,%s" % (cx, cz)))
        if whole:                                     # scanning thousands of chunks -> off-thread
            self._run_async(collect, on_done=done, msg="Scanning world for entities…")
        else:                                         # one chunk is instant -> stay synchronous
            done(collect())

    def on_mob_add(self):
        if not self._guard():
            return
        try:
            name = self.mob["name"].get().strip()
            if not name:
                raise _InputError("Mob name is required (e.g. Giant, Zombie, Cow).")
            x, y, z = (self._num(self.mob[k], k.upper()) for k in ("x", "y", "z"))
            self.world.add_mob(name, x, y, z)
            self.status.set("Added %s at (%d,%d,%d)" % (name, x, y, z))
        except Exception as e:
            self._err(e)

    # -- NBT
    def on_nbt_load(self):
        if not self._guard():
            return
        self.nbt_tree.delete(*self.nbt_tree.get_children())
        vf = self.nbt_file.get()
        data = self.world._filedata.get(vf)
        if not data:
            return
        try:
            name, tag, _ = N.parse_tag(data)
            self._insert_nbt("", name or vf, tag)
        except Exception as e:
            self._err(e)

    def _insert_nbt(self, parent, name, tag):
        if tag.id == N.COMPOUND:
            node = self.nbt_tree.insert(parent, "end", text=name, values=("Compound", ""))
            for n, t in tag.value.items():
                self._insert_nbt(node, n, t)
        elif tag.id == N.LIST:
            node = self.nbt_tree.insert(parent, "end", text=name,
                                        values=("List<%s>" % N.NAMES.get(tag.value.etype, "?"), len(tag.value)))
            for i, v in enumerate(tag.value.items):
                self._insert_nbt(node, "[%d]" % i, N.Tag(tag.value.etype, v))
        elif tag.id in (N.BYTE_ARRAY, N.INT_ARRAY):
            self.nbt_tree.insert(parent, "end", text=name,
                                 values=(N.NAMES[tag.id], "[%d values]" % len(tag.value)))
        else:
            self.nbt_tree.insert(parent, "end", text=name, values=(N.NAMES.get(tag.id), tag.value))

    # -- Tools
    def tool_give_spawners(self):
        if self._guard():
            self.world.inventory_add(52, 16); self.refresh_inv(); self.status.set("Gave 16 spawner blocks")

    def tool_giant_at_spawn(self):
        if not self._guard():
            return
        sx, sy, sz = self.world.get_spawn()
        try:
            self.world.add_mob("Giant", sx - 8, sy, sz + 4)
            self.status.set("Injected Giant near spawn (remember the Stable Giant patch)")
        except Exception as e:
            self._err(e)

    def tool_giant_den(self):
        if not self._guard():
            return
        sx, sy, sz = self.world.get_spawn()
        cx, cy, cz = sx + 6, max(sy - 5, 5), sz + 6
        try:
            for x in range(cx - 3, cx + 4):
                for z in range(cz - 3, cz + 4):
                    for y in range(cy - 1, cy + 6):
                        solid = not (cx - 2 <= x <= cx + 2 and cz - 2 <= z <= cz + 2 and cy <= y < cy + 5)
                        self.world.set_block(x, y, z, 1 if solid else 0)
            self.world.add_spawner(cx, cy, cz, "Giant", 200)
            self.world.set_block(cx, cy + 6, cz, 89)  # glowstone marker
            self.status.set("Built Giant spawner den at (%d,%d,%d)" % (cx, cy, cz))
        except Exception as e:
            self._err(e)

    def tool_locked_chest(self):
        if self._guard():
            self.world.inventory_add(95, 1); self.refresh_inv(); self.status.set("Gave a Locked Chest (id 95)")

    def tool_tnt(self):
        if self._guard():
            self.world.inventory_add(46, 64); self.refresh_inv(); self.status.set("Gave 64 TNT")

    def _pixelart_dialog(self):
        """Modal options dialog. Returns a dict with action in {'save','stamp'} or None."""
        import tkinter as tk
        d = tk.Toplevel(self); d.title("Pixel art options"); d.transient(self)
        d.grab_set(); d.resizable(False, False)
        res = {}
        w_var = tk.IntVar(value=64); pal_var = tk.StringVar(value="smooth")
        ori_var = tk.StringVar(value="wall"); dep_var = tk.IntVar(value=1)
        dith_var = tk.BooleanVar(value=False); rmbg_var = tk.BooleanVar(value=False)
        r = 0
        tk.Label(d, text="Width (blocks):").grid(row=r, column=0, sticky="e", padx=8, pady=6)
        tk.Entry(d, textvariable=w_var, width=8).grid(row=r, column=1, sticky="w"); r += 1
        tk.Label(d, text="Palette:").grid(row=r, column=0, sticky="e", padx=8, pady=6)
        ttk.Combobox(d, textvariable=pal_var, state="readonly", width=12,
                     values=["smooth", "full", "wool", "grays", "concrete"]).grid(row=r, column=1, sticky="w"); r += 1
        tk.Label(d, text="Orientation:").grid(row=r, column=0, sticky="e", padx=8, pady=6)
        of = tk.Frame(d); of.grid(row=r, column=1, sticky="w")
        tk.Radiobutton(of, text="Wall", variable=ori_var, value="wall").pack(side="left")
        tk.Radiobutton(of, text="Floor", variable=ori_var, value="floor").pack(side="left"); r += 1
        tk.Label(d, text="Relief depth (1 = flat):").grid(row=r, column=0, sticky="e", padx=8, pady=6)
        tk.Entry(d, textvariable=dep_var, width=8).grid(row=r, column=1, sticky="w"); r += 1
        tk.Checkbutton(d, text="Dither (smoother gradients)", variable=dith_var).grid(
            row=r, column=1, sticky="w"); r += 1
        tk.Checkbutton(d, text="Remove background (for images with no transparency)",
                       variable=rmbg_var).grid(row=r, column=1, sticky="w"); r += 1
        tk.Label(d, text="'smooth' = flat blocks (cleanest, least noisy). 'full' = widest colours.\n"
                         "'concrete' = newer/Aquatic worlds. Transparent pixels are skipped.",
                 foreground="#666", justify="left").grid(row=r, column=0, columnspan=2, padx=8, sticky="w"); r += 1
        bf = tk.Frame(d); bf.grid(row=r, column=0, columnspan=2, pady=10)

        def choose(action):
            res.update(width=max(16, min(256, w_var.get())), palette=pal_var.get(),
                       orientation=ori_var.get(), depth=max(1, min(64, dep_var.get())),
                       dither=dith_var.get(), remove_bg=rmbg_var.get(), action=action)
            d.destroy()
        tk.Button(bf, text="Save schematic…", command=lambda: choose("save")).pack(side="left", padx=6)
        tk.Button(bf, text="Stamp into open world", command=lambda: choose("stamp"),
                  state=("normal" if self.world else "disabled")).pack(side="left", padx=6)
        tk.Button(bf, text="Cancel", command=d.destroy).pack(side="left", padx=6)
        self.wait_window(d)
        return res if res.get("action") else None

    def _stamp_generated(self, gen_fn, what):
        """Ask a location, stamp (blocks,data)=gen_fn() into the open world, save."""
        if not self.world:
            messagebox.showwarning("Stamp", "Open a save first."); return
        sx, sy, sz = self.world.get_spawn() or (0, 72, 0)
        coords = simpledialog.askstring(
            "Stamp location", "Place lower-corner at  X Y Z\n(blank = spawn %d %d %d):" % (sx, sy, sz))
        if coords is None:
            return
        coords = coords.strip()
        if coords:
            try:
                ox, oy, oz = map(int, coords.split())
            except Exception:
                messagebox.showerror("Location", "Enter three numbers: X Y Z"); return
        else:
            ox, oy, oz = sx, sy, sz
        from . import schematic as S

        def work():
            b, d = gen_fn()
            n, _ = S.stamp(self.world, b, d, ox, oy, oz)
            self.world.save(backup=True, progress=self._progress)
            return n
        self._run_async(work, on_done=lambda n: (self._reload_after_edit(),
                        messagebox.showinfo("Stamped", "Placed %d blocks of %s at (%d,%d,%d).\n"
                                            "(.bak backup kept)" % (n, what, ox, oy, oz))),
                        msg="Stamping %s into world…" % what)

    def tool_pixelart(self):
        img = filedialog.askopenfilename(
            title="Choose an image (photo, logo, pixel art)",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.gif"), ("All files", "*.*")])
        if not img:
            return
        opts = self._pixelart_dialog()
        if not opts:
            return
        from . import artgen
        gen = lambda: artgen.image_to_arrays(img, width=opts["width"], orientation=opts["orientation"],
                                             dither=opts["dither"], depth=opts["depth"],
                                             palette=opts["palette"], remove_bg=opts["remove_bg"])
        if opts["action"] == "stamp":
            self._stamp_generated(gen, "pixel art"); return
        out = filedialog.asksaveasfilename(
            title="Save pixel-art schematic", defaultextension=".schematic",
            initialfile=os.path.splitext(os.path.basename(img))[0] + ".schematic")
        if not out:
            return

        def work():
            b, d = gen()
            from . import schematic as S
            S.save(b, d, out, name=os.path.splitext(os.path.basename(out))[0])
            prev = os.path.splitext(out)[0] + "_preview.png"
            artgen.preview_png(out, prev, scale=6)
            return b.shape, prev

        def done(r):
            shape, prev = r
            self.status.set("Pixel art %s → %s" % (shape, out))
            try:
                os.startfile(prev)
            except Exception:
                pass
            messagebox.showinfo("Pixel art", "Made a %d×%d×%d pixel-art schematic:\n%s\n\n"
                                "A preview opened. Import it into a world in the 3D viewer with Ctrl+I."
                                % (shape[0], shape[1], shape[2], out))
        self._run_async(work, on_done=done, msg="Converting image to pixel art…")

    def tool_scale_schematic(self):
        src = filedialog.askopenfilename(title="Schematic to resize",
                                         filetypes=[("Schematic", "*.schematic"), ("All files", "*.*")])
        if not src:
            return
        pct = simpledialog.askinteger("Scale", "Resize to what %% of current size?\n(50 = half, 200 = double)",
                                      initialvalue=50, minvalue=5, maxvalue=400)
        if not pct:
            return
        out = filedialog.asksaveasfilename(title="Save resized schematic", defaultextension=".schematic",
                                           initialfile=os.path.splitext(os.path.basename(src))[0] + "_%d.schematic" % pct)
        if not out:
            return
        from . import artgen
        self._run_async(lambda: artgen.scale_schematic(src, out, pct / 100.0),
                        on_done=lambda r: (self.status.set("Scaled %s → %s" % (r[0], r[1])),
                                           messagebox.showinfo("Scaled", "Resized %s → %s\n%s\n\n"
                                                               "Import in the 3D viewer with Ctrl+I." % (r[0], r[1], out))),
                        msg="Scaling schematic…")

    def tool_shape(self, kind):
        from . import artgen
        if kind == "sphere":
            r = simpledialog.askinteger("Sphere / orb", "Radius in blocks (2–40):",
                                        initialvalue=10, minvalue=2, maxvalue=40)
            if not r:
                return
            hollow = messagebox.askyesno("Sphere / orb", "Hollow shell?\n\nNo = solid")
            gen = lambda: artgen.sphere_arrays(r, block=(35, 13), hollow=hollow)
        elif kind == "cylinder":
            r = simpledialog.askinteger("Cylinder", "Radius:", initialvalue=6, minvalue=2, maxvalue=40)
            h = simpledialog.askinteger("Cylinder", "Height:", initialvalue=12, minvalue=1, maxvalue=128) if r else None
            if not r or not h:
                return
            gen = lambda: artgen.cylinder_arrays(r, h, block=(35, 11))
        else:
            base = simpledialog.askinteger("Pyramid", "Base width:", initialvalue=15, minvalue=3, maxvalue=64)
            if not base:
                return
            gen = lambda: artgen.pyramid_arrays(base, block=(24, 0))
        if self.world and messagebox.askyesno(
                kind.title(), "Stamp into the open world now?\n\nNo = save as a .schematic file"):
            self._stamp_generated(gen, kind); return
        out = filedialog.asksaveasfilename(title="Save shape schematic", defaultextension=".schematic",
                                           initialfile=kind + ".schematic")
        if not out:
            return

        def work():
            b, d = gen()
            from . import schematic as S
            S.save(b, d, out, name=os.path.splitext(os.path.basename(out))[0])
            return b.shape
        self._run_async(work,
                        on_done=lambda sh: (self.status.set("%s %s → %s" % (kind, sh, out)),
                                            messagebox.showinfo(kind.title(), "Made a %s schematic %s:\n%s\n\n"
                                                                "Import it in the 3D viewer with Ctrl+I." % (kind, sh, out))),
                        msg="Generating %s…" % kind)

    def _deliver_schem(self, gen, default_name, what, preview=True):
        """Shared: stamp gen()=(b,d) into the open world, or save it as a .schematic."""
        if self.world and messagebox.askyesno(
                what.title(), "Stamp into the open world now?\n\nNo = save as a .schematic file"):
            self._stamp_generated(gen, what); return
        out = filedialog.asksaveasfilename(title="Save %s schematic" % what, defaultextension=".schematic",
                                           initialfile=default_name)
        if not out:
            return
        from . import artgen, schematic as S

        def work():
            b, d = gen()
            S.save(b, d, out, name=os.path.splitext(os.path.basename(out))[0])
            prev = None
            if preview:
                prev = os.path.splitext(out)[0] + "_preview.png"
                artgen.preview_png(out, prev, scale=6)
            return b.shape, prev

        def done(r):
            shape, prev = r
            self.status.set("%s %s → %s" % (what, shape, out))
            if prev:
                try:
                    os.startfile(prev)
                except Exception:
                    pass
            messagebox.showinfo(what.title(), "Made %s %s:\n%s\n\n"
                                "Import it into a world in the 3D viewer with Ctrl+I." % (what, shape, out))
        self._run_async(work, on_done=done, msg="Generating %s…" % what)

    def tool_text(self):
        text = simpledialog.askstring("Text / word art", "Text to build in blocks:")
        if not text:
            return
        height = simpledialog.askinteger("Text / word art", "Letter height in blocks (3–64):",
                                         initialvalue=12, minvalue=3, maxvalue=64)
        if not height:
            return
        from . import artgen
        gen = lambda: artgen.text_to_arrays(text, height=height, block=(35, 15))
        self._deliver_schem(gen, "text.schematic", "text")

    def tool_obj(self):
        obj = filedialog.askopenfilename(title="Choose a 3D model (.obj)",
                                         filetypes=[("Wavefront OBJ", "*.obj"), ("All files", "*.*")])
        if not obj:
            return
        size = simpledialog.askinteger("3D model", "Max size in blocks (8–200):",
                                       initialvalue=48, minvalue=8, maxvalue=200)
        if not size:
            return
        solid = messagebox.askyesno("3D model", "Fill solid?\n\nNo = hollow shell (faster, smaller)")
        from . import voxelize
        gen = lambda: voxelize.obj_to_arrays(obj, size=size, block=None, solid=solid)
        self._deliver_schem(gen, os.path.splitext(os.path.basename(obj))[0] + ".schematic", "3D model", preview=False)

    def tool_flatten(self):
        if not self._guard() or not self.world:
            return
        if not messagebox.askyesno("Flatten world",
                                   "Cut all terrain above the water line down to sea level?\n\n"
                                   "Your builds are kept; mountains/hills are removed. This writes to "
                                   "the open save (a .bak backup is kept). Continue?"):
            return
        from . import worldgen
        w = self.world

        def work():
            nc, cl = worldgen.flatten(w, keep_builds=True, log=self._logcb())
            w.save(backup=True, progress=self._progress)
            return (nc, cl)

        self._run_async(work,
                        on_done=lambda r: (self._reload_after_edit(),
                                           messagebox.showinfo("Flatten", "Flattened %d chunks, cleared %d blocks."
                                                               % r)),
                        msg="Flattening world to sea level…")

    def tool_mountains(self):
        if not self._guard() or not self.world:
            return
        from tkinter import simpledialog
        mh = simpledialog.askinteger("Mountains", "Tallest peak above sea level (blocks):",
                                     initialvalue=45, minvalue=5, maxvalue=63, parent=self)
        if mh is None:
            return
        if not messagebox.askyesno("Generate mountains",
                                   "Raise noise mountains up to %d blocks above sea level into the open "
                                   "save? (.bak backup kept)" % mh):
            return
        from . import worldgen
        w = self.world

        def work():
            nc, ad = worldgen.add_mountains(w, max_height=mh, log=self._logcb())
            w.save(backup=True, progress=self._progress)
            return (nc, ad)

        self._run_async(work,
                        on_done=lambda r: (self._reload_after_edit(),
                                           messagebox.showinfo("Mountains", "Raised terrain in %d chunks (%d blocks)."
                                                               % r)),
                        msg="Generating mountains…")

    def _reload_after_edit(self):
        """Re-open the save from disk after a terrain edit so views reflect it. Off-thread:
        re-opening decompresses the whole container, which stutters the UI on a big world."""
        if not self.path:
            return

        def done(w):
            self.world = w
            if hasattr(self, "refresh_all"):
                self.refresh_all()
            self.status.set("Terrain updated and saved (.bak kept).")
        self._run_async(lambda: World.open(self.path), on_done=done, msg="Reloading world…")

    def tool_report(self):
        if not self._guard():
            return
        if not self.path:
            messagebox.showwarning("No file", "Open a save from disk first."); return
        out = filedialog.asksaveasfilename(title="Save world report", defaultextension=".html",
                                           initialfile="%s_report.html" % self._world_name(self.world),
                                           filetypes=[("HTML report", "*.html")])
        if not out:
            return
        from . import analytics
        path = self.path

        def work():
            s = analytics.analyze(path)
            open(out, "w", encoding="utf-8").write(analytics.report_html(s))
            return out

        def done(o):
            self.status.set("World report written: %s" % o)
            try:
                import webbrowser
                webbrowser.open("file:///" + o.replace("\\", "/"))
            except Exception:
                pass

        self._run_async(work, on_done=done, msg="Analyzing world (block/loot/mob census)…")

    def tool_world_map(self):
        if not self._guard():
            return
        if not self.path:
            messagebox.showwarning("No file", "Open a save from disk first."); return
        out = filedialog.asksaveasfilename(title="Save world map", defaultextension=".png",
                                           initialfile="%s_map.png" % self._world_name(self.world),
                                           filetypes=[("PNG image", "*.png")])
        if not out:
            return
        from . import atlas
        path = self.path

        def work():
            img = atlas.render_world(atlas.load(path))
            img.save(out)
            return (out, img.width, img.height)

        self._run_async(work,
                        on_done=lambda r: (self.status.set("Wrote %dx%d world map: %s" % (r[1], r[2], r[0])),
                                           messagebox.showinfo("World map", "Saved %dx%d map:\n%s" % (r[1], r[2], r[0]))),
                        msg="Rendering top-down world map…")

    def tool_map_items(self):
        if not self._guard():
            return
        if not self.path:
            messagebox.showwarning("No file", "Open a save from disk first."); return
        out = filedialog.askdirectory(title="Folder for the exported map images")
        if not out:
            return
        from . import atlas
        world = self.world

        def work():
            n = 0
            for k in [x for x in world._filedata if x.startswith("data/map_")]:
                img = atlas.render_map_item(world._filedata[k])
                if img is not None:
                    img.save(os.path.join(out, k.split("/")[-1] + ".png")); n += 1
            return n

        self._run_async(work,
                        on_done=lambda n: messagebox.showinfo("Map items",
                                                              "Rendered %d in-game map(s) to:\n%s" % (n, out)),
                        msg="Rendering in-game map items…")

    def tool_view3d(self):
        if not self._guard():
            return
        if not self.path:
            messagebox.showwarning("No file", "Open a save from disk first."); return
        import subprocess
        if getattr(sys, "frozen", False):                           # packaged EXE
            cmd, cwd = [sys.executable, "--view3d", self.path], None
        else:                                                        # source
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            cmd, cwd = [sys.executable, os.path.join(root, "LCEStudio.py"),
                        "--view3d", self.path], root
        try:
            # DEVNULL for the std handles -- a --windowed PyInstaller app has no
            # console, and Popen inheriting its invalid handles fails at CreateProcess.
            subprocess.Popen(cmd, cwd=cwd, close_fds=True,
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
            self.status.set("Building 3D fly-through — a window will open shortly (WASD + mouse).")
        except Exception as e:
            messagebox.showerror("3D view", "Couldn't start the 3D viewer.\n\nCommand:\n%s\n\n%s"
                                 % (" ".join(cmd), e))


def main():
    initial = sys.argv[1] if len(sys.argv) > 1 else None
    Studio(initial).mainloop()


if __name__ == "__main__":
    main()
