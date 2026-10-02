#!/usr/bin/env python3
"""
EVO2UE - desktop app for exporting Assetto Corsa EVO tracks to Unreal Engine.

Double-click EVO2UE.exe (built with build_exe.bat) or run:  pythonw evo2ue_gui.py
Needs: Python 3.9+, numpy, pillow  (tkinter ships with Python on Windows)
"""
import json, os, queue, re, subprocess, sys, threading, time, traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import unreal_bridge as UB
except Exception:
    UB = None
try:
    import evo_track_export as EX
except ImportError as e:            # numpy / pillow missing
    root = tk.Tk(); root.withdraw()
    messagebox.showerror("EVO2UE", "Missing dependency: %s\n\nRun:  pip install numpy pillow" % e)
    sys.exit(1)

APP = "EVO2UE"
SETTINGS = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~/.config"), "evo2ue", "settings.json")

# ---- palette (dark) ----
BG, PANEL, FIELD, LINE = "#16181d", "#1e2128", "#262a33", "#323743"
FG, MUTED, ACCENT, ACCENT_HI, OK, WARN, ERR = "#e7e9ee", "#9097a6", "#e8483b", "#ff6153", "#5cc98a", "#e8b84a", "#ff6b6b"

# ------------------------------------------------------------------ helpers
def load_settings():
    try:
        with open(SETTINGS) as f:
            return json.load(f)
    except Exception:
        return {}

def save_settings(d):
    try:
        os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
        with open(SETTINGS, "w") as f:
            json.dump(d, f, indent=1)
    except Exception:
        pass

def steam_libraries():
    """Steam library roots on this machine (Windows registry + libraryfolders.vdf)."""
    roots = []
    if sys.platform == "win32":
        try:
            import winreg
            for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                              (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        for val in ("SteamPath", "InstallPath"):
                            try:
                                roots.append(winreg.QueryValueEx(k, val)[0])
                            except OSError:
                                pass
                except OSError:
                    pass
        except ImportError:
            pass
        for d in "CDEFGHIJKLMN":
            for sub in (r"SteamLibrary", r"Steam", r"Program Files (x86)\Steam", r"Games\Steam"):
                roots.append("%s:\\%s" % (d, sub))
    libs = []
    for r in roots:
        vdf = os.path.join(r, "steamapps", "libraryfolders.vdf")
        if os.path.isfile(vdf):
            try:
                libs += [p.replace("\\\\", "\\") for p in re.findall(r'"path"\s+"([^"]+)"', open(vdf, encoding="utf-8", errors="ignore").read())]
            except OSError:
                pass
        libs.append(r)
    seen, out = set(), []
    for l in libs:
        k = os.path.normcase(os.path.abspath(l))
        if k not in seen and os.path.isdir(l):
            seen.add(k); out.append(l)
    return out

def find_evo_content():
    for lib in steam_libraries():
        c = os.path.join(lib, "steamapps", "common", "Assetto Corsa EVO", "content")
        if os.path.isdir(c):
            return c
    return None

def content_status(path):
    """-> (state, message, tracks)  state: ok | packed | bad"""
    if not path or not os.path.isdir(path):
        return "bad", "Folder not found", []
    if os.path.basename(os.path.normpath(path)).lower() != "content" and os.path.isdir(os.path.join(path, "content")):
        path = os.path.join(path, "content")
    tracks = EX.list_tracks(path)
    if tracks:
        return "ok", "Unpacked content found - %d tracks" % len(tracks), tracks
    if os.path.isfile(os.path.join(path, "content.kspkg")) or os.path.isfile(os.path.join(os.path.dirname(path), "content.kspkg")):
        return "packed", "Game is still packed (content.kspkg). Unpack it first, e.g. with EvoMods Manager.", []
    return "bad", "No tracks found here - pick the game's 'content' folder", []

def open_folder(p):
    if not os.path.isdir(p):
        return
    if sys.platform == "win32":
        os.startfile(p)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])

def pretty(name):
    special = {"cota": "Circuit of the Americas", "redbull_ring": "Red Bull Ring", "nurburgring": "Nürburgring",
               "spa": "Spa-Francorchamps", "mount_panorama": "Mount Panorama"}
    return special.get(name, name.replace("_", " ").title())

# ------------------------------------------------------------------ app
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("EVO2UE - Assetto Corsa EVO track exporter")
        self.geometry("1000x900")
        self.minsize(900, 760)
        self.configure(bg=BG)
        self.s = load_settings()
        self.q = queue.Queue()
        self.worker = None
        self.cancel_flag = threading.Event()
        self.tracks = []
        self.editors = []
        self.ue_busy = False
        self._ue_lock = threading.Lock()
        self._rx = UB.RemoteExecution() if UB else None
        self._style()
        self._build()
        self.after(80, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._close)
        if not self.var_content.get():
            found = find_evo_content()
            if found:
                self.var_content.set(found)
        self._refresh_tracks()
        self.after(500, self._discover_async)

    # ---------------- style
    def _style(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        base = ("Segoe UI", 10) if sys.platform == "win32" else ("DejaVu Sans", 10)
        self.f_base = base
        self.f_small = (base[0], 9)
        self.f_title = (base[0], 17, "bold")
        self.f_head = (base[0], 10, "bold")
        self.f_mono = ("Consolas", 9) if sys.platform == "win32" else ("DejaVu Sans Mono", 9)
        st.configure(".", background=BG, foreground=FG, font=base, bordercolor=LINE, lightcolor=PANEL, darkcolor=PANEL,
                     troughcolor=FIELD, fieldbackground=FIELD, insertcolor=FG)
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Panel.TLabel", background=PANEL)
        st.configure("Muted.TLabel", background=PANEL, foreground=MUTED, font=self.f_small)
        st.configure("Head.TLabel", background=PANEL, foreground=MUTED, font=(base[0], 9, "bold"))
        st.configure("Title.TLabel", background=BG, font=self.f_title)
        st.configure("Sub.TLabel", background=BG, foreground=MUTED)
        st.configure("TEntry", fieldbackground=FIELD, foreground=FG, bordercolor=LINE, padding=6)
        st.map("TEntry", bordercolor=[("focus", ACCENT)])
        st.configure("TButton", background=FIELD, foreground=FG, bordercolor=LINE, padding=(12, 6), focuscolor=FIELD)
        st.map("TButton", background=[("active", LINE), ("disabled", PANEL)], foreground=[("disabled", MUTED)])
        st.configure("Accent.TButton", background=ACCENT, foreground="white", bordercolor=ACCENT, font=(base[0], 11, "bold"), padding=(22, 9))
        st.map("Accent.TButton", background=[("active", ACCENT_HI), ("disabled", LINE)], foreground=[("disabled", MUTED)])
        st.configure("TCheckbutton", background=PANEL, foreground=FG, focuscolor=PANEL)
        st.map("TCheckbutton", background=[("active", PANEL)], foreground=[("disabled", MUTED)])
        self._chk_imgs = self._check_images()
        try:
            st.element_create("evo.indicator", "image", self._chk_imgs[0], ("selected", self._chk_imgs[1]))
            st.layout("TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
                ("evo.indicator", {"side": "left", "sticky": ""}),
                ("Checkbutton.label", {"side": "left", "sticky": "nswe"})]})])
            st.configure("TCheckbutton", padding=(0, 1))
        except tk.TclError:
            pass
        st.configure("Vertical.TScrollbar", background=LINE, troughcolor=FIELD, bordercolor=FIELD, arrowcolor=MUTED,
                     lightcolor=LINE, darkcolor=LINE, gripcount=0)
        st.map("Vertical.TScrollbar", background=[("active", MUTED)])
        st.configure("TCombobox", fieldbackground=FIELD, background=FIELD, foreground=FG, arrowcolor=FG, padding=4)
        st.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", FG)])
        self.option_add("*TCombobox*Listbox.background", FIELD)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        st.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=FIELD, bordercolor=FIELD, lightcolor=ACCENT, darkcolor=ACCENT, thickness=10)

    def _check_images(self):
        import base64, io
        from PIL import Image, ImageDraw
        S, k = 18, 4                                  # draw 4x then downsample for smooth edges
        def mk(on):
            im = Image.new("RGBA", (S * k, S * k), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
            box = (k, k, (S - 1) * k, (S - 1) * k)
            if on:
                d.rounded_rectangle(box, radius=4 * k, fill=ACCENT)
                d.line([(5 * k, 9.5 * k), (8 * k, 12.5 * k), (13.5 * k, 6 * k)], fill="white", width=int(2.2 * k), joint="curve")
            else:
                d.rounded_rectangle(box, radius=4 * k, fill=FIELD, outline=MUTED, width=int(1.5 * k))
            im = im.resize((S, S), Image.LANCZOS)
            b = io.BytesIO(); im.save(b, "PNG")
            return tk.PhotoImage(data=base64.b64encode(b.getvalue()))
        return mk(False), mk(True)

    # ---------------- layout
    def _panel(self, parent, title):
        f = ttk.Frame(parent, style="Panel.TFrame", padding=(14, 10, 14, 14))
        ttk.Label(f, text=title.upper(), style="Head.TLabel").pack(anchor="w", pady=(0, 8))
        return f

    def _build(self):
        hdr = ttk.Frame(self, padding=(18, 14, 18, 6))
        hdr.pack(fill="x")
        ttk.Label(hdr, text="EVO → Unreal", style="Title.TLabel").pack(side="left")
        ttk.Label(hdr, text="   Assetto Corsa EVO track exporter  ·  v%s" % EX.VERSION, style="Sub.TLabel").pack(side="left", pady=(6, 0))

        body = ttk.Frame(self, padding=(18, 6, 18, 0))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=3, uniform="c")
        body.columnconfigure(1, weight=2, uniform="c")
        body.rowconfigure(1, weight=1)

        # -- source
        src = self._panel(body, "1  ·  Game files")
        src.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        row = ttk.Frame(src, style="Panel.TFrame"); row.pack(fill="x")
        self.var_content = tk.StringVar(value=self.s.get("content", ""))
        e = ttk.Entry(row, textvariable=self.var_content)
        e.pack(side="left", fill="x", expand=True)
        e.bind("<FocusOut>", lambda _e: self._refresh_tracks())
        e.bind("<Return>", lambda _e: self._refresh_tracks())
        ttk.Button(row, text="Browse…", command=self._browse_content).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Auto-detect", command=self._autodetect).pack(side="left", padx=(8, 0))
        self.lbl_status = tk.Label(src, text="", bg=PANEL, fg=MUTED, font=self.f_small, anchor="w")
        self.lbl_status.pack(fill="x", pady=(6, 0))

        # -- tracks
        trk = self._panel(body, "2  ·  Tracks")
        trk.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        lf = tk.Frame(trk, bg=FIELD, highlightthickness=1, highlightbackground=LINE)
        lf.pack(fill="both", expand=True)
        self.lst = tk.Listbox(lf, selectmode="extended", bg=FIELD, fg=FG, selectbackground=ACCENT, selectforeground="white",
                              activestyle="none", highlightthickness=0, bd=0, font=(self.f_base[0], 11), exportselection=False)
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.lst.yview)
        self.lst.configure(yscrollcommand=sb.set)
        self.lst.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        sb.pack(side="right", fill="y")
        self.lst.bind("<<ListboxSelect>>", lambda _e: self._update_buttons())
        tr = ttk.Frame(trk, style="Panel.TFrame"); tr.pack(fill="x", pady=(8, 0))
        ttk.Button(tr, text="Select all", command=lambda: (self.lst.select_set(0, "end"), self._update_buttons())).pack(side="left")
        ttk.Button(tr, text="None", command=lambda: (self.lst.select_clear(0, "end"), self._update_buttons())).pack(side="left", padx=(8, 0))
        ttk.Label(tr, text="Ctrl/Shift-click to pick several", style="Muted.TLabel").pack(side="right")

        # -- options + output
        right = ttk.Frame(body)
        right.grid(row=1, column=1, sticky="nsew")
        opt = self._panel(right, "3  ·  Export options")
        opt.pack(fill="x")
        self.var_tex = tk.BooleanVar(value=self.s.get("textures", True))
        self.var_size = tk.StringVar(value=self.s.get("max_texture", "2048"))
        self.var_lods = tk.BooleanVar(value=self.s.get("lods", True))
        self.var_preview = tk.BooleanVar(value=self.s.get("preview", True))
        self.var_events = tk.BooleanVar(value=self.s.get("events", False))
        self.var_skip = tk.BooleanVar(value=self.s.get("skip_existing", True))
        trow = ttk.Frame(opt, style="Panel.TFrame"); trow.pack(fill="x", anchor="w")
        ttk.Checkbutton(trow, text=" Textures, max size", variable=self.var_tex, command=self._update_buttons).pack(side="left")
        self.cmb_size = ttk.Combobox(trow, textvariable=self.var_size, values=["Full", "4096", "2048", "1024", "512"], width=6, state="readonly")
        self.cmb_size.pack(side="left", padx=(8, 0))
        grid = ttk.Frame(opt, style="Panel.TFrame"); grid.pack(fill="x")
        for i, (text, var) in enumerate((("LODs", self.var_lods), ("Preview .glb", self.var_preview),
                                         ("Event props", self.var_events), ("Skip decoded textures", self.var_skip))):
            ttk.Checkbutton(grid, text=" " + text, variable=var).grid(row=i // 2, column=i % 2, sticky="w", pady=(6, 0), padx=(0, 14))

        out = self._panel(right, "4  ·  Output folder")
        out.pack(fill="x", pady=(10, 0))
        orow = ttk.Frame(out, style="Panel.TFrame"); orow.pack(fill="x")
        self.var_out = tk.StringVar(value=self.s.get("out", os.path.join(os.path.expanduser("~"), "EVO_export")))
        ttk.Entry(orow, textvariable=self.var_out).pack(side="left", fill="x", expand=True)
        ttk.Button(orow, text="…", width=3, command=self._browse_out).pack(side="left", padx=(8, 0))
        ttk.Button(orow, text="Open", command=lambda: open_folder(self._last_out or self.var_out.get())).pack(side="left", padx=(8, 0))
        act = ttk.Frame(out, style="Panel.TFrame", padding=(0, 10, 0, 0))
        act.pack(fill="x")
        self.btn_go = ttk.Button(act, text="Export", style="Accent.TButton", command=self._start)
        self.btn_go.pack(side="left")
        self.btn_cancel = ttk.Button(act, text="Cancel", command=self._cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=(8, 0))
        self._last_out = None

        ue = self._panel(right, "5  ·  Unreal Engine")
        ue.pack(fill="x", pady=(10, 0))
        urow = ttk.Frame(ue, style="Panel.TFrame"); urow.pack(fill="x")
        self.lbl_ue = tk.Label(urow, text="●  Looking for Unreal…", bg=PANEL, fg=MUTED, font=self.f_base, anchor="w")
        self.lbl_ue.pack(side="left", fill="x", expand=True)
        ttk.Button(urow, text="↻", width=3, command=self._discover_async).pack(side="right")
        self.var_editor = tk.StringVar()
        self.cmb_editor = ttk.Combobox(ue, textvariable=self.var_editor, state="readonly", values=[])
        self.var_newlevel = tk.BooleanVar(value=self.s.get("ue_new_level", True))
        self.var_ue_inst = tk.BooleanVar(value=self.s.get("ue_instances", True))
        ttk.Checkbutton(ue, text=" Build in a new level (with sky + sun)", variable=self.var_newlevel).pack(anchor="w", pady=(6, 0))
        ttk.Checkbutton(ue, text=" Instanced props (tyres, crowds, trees)", variable=self.var_ue_inst).pack(anchor="w", pady=(6, 0))
        brow = ttk.Frame(ue, style="Panel.TFrame"); brow.pack(fill="x", pady=(10, 0))
        self.btn_send = ttk.Button(brow, text="Send to Unreal", style="Accent.TButton", command=self._send)
        self.btn_send.pack(side="left")
        self.btn_launch = ttk.Button(brow, text="Launch project…", command=self._launch)
        self.btn_launch.pack(side="left", padx=(8, 0))
        self.lbl_uehint = tk.Label(ue, text="", bg=PANEL, fg=MUTED, font=self.f_small, justify="left", anchor="w", wraplength=330)
        self.lbl_uehint.pack(fill="x", pady=(8, 0))

        # -- progress + log
        bottom = ttk.Frame(self, padding=(18, 10, 18, 16))
        bottom.pack(side="bottom", fill="x")
        body.pack_forget(); body.pack(fill="both", expand=True)      # body after bottom -> log never pushed off-screen
        prow = ttk.Frame(bottom); prow.pack(fill="x")
        self.lbl_phase = ttk.Label(prow, text="Ready", foreground=MUTED)
        self.lbl_phase.pack(side="left")
        self.lbl_eta = ttk.Label(prow, text="", foreground=MUTED)
        self.lbl_eta.pack(side="right")
        self.pb = ttk.Progressbar(bottom, mode="determinate", maximum=1000)
        self.pb.pack(fill="x", pady=(6, 8))
        lf2 = tk.Frame(bottom, bg=FIELD, highlightthickness=1, highlightbackground=LINE)
        lf2.pack(fill="both", expand=True)
        self.txt = tk.Text(lf2, height=7, bg=FIELD, fg=MUTED, insertbackground=FG, bd=0, highlightthickness=0,
                           font=self.f_mono, wrap="none", padx=8, pady=6)
        self.txt.pack(fill="both", expand=True)
        for tag, col in (("ok", OK), ("warn", WARN), ("err", ERR), ("head", FG)):
            self.txt.tag_configure(tag, foreground=col)
        self.txt.configure(state="disabled")
        self._log("Pick your unpacked EVO 'content' folder, select tracks, then Export.", "head")

    # ---------------- actions
    def _browse_content(self):
        d = filedialog.askdirectory(title="Assetto Corsa EVO 'content' folder", initialdir=self.var_content.get() or "/")
        if d:
            self.var_content.set(os.path.normpath(d)); self._refresh_tracks()

    def _browse_out(self):
        d = filedialog.askdirectory(title="Output folder", initialdir=self.var_out.get() or os.path.expanduser("~"))
        if d:
            self.var_out.set(os.path.normpath(d))

    def _autodetect(self):
        found = find_evo_content()
        if found:
            self.var_content.set(found); self._refresh_tracks()
            self._log("Found EVO at %s" % found, "ok")
        else:
            messagebox.showinfo(APP, "Couldn't find Assetto Corsa EVO in your Steam libraries.\nUse Browse… to pick the 'content' folder.")

    def _content_dir(self):
        p = self.var_content.get().strip().strip('"')
        if p and os.path.basename(os.path.normpath(p)).lower() != "content" and os.path.isdir(os.path.join(p, "content")):
            p = os.path.join(p, "content")
        return p

    def _refresh_tracks(self):
        state, msg, tracks = content_status(self._content_dir())
        self.lbl_status.configure(text=("✓  " if state == "ok" else "⚠  ") + msg, fg=OK if state == "ok" else (WARN if state == "packed" else ERR))
        keep = set(self.s.get("selected", []))
        self.tracks = tracks
        self.lst.delete(0, "end")
        for i, t in enumerate(tracks):
            self.lst.insert("end", "  %s" % pretty(t))
            if t in keep:
                self.lst.select_set(i)
        self._update_buttons()

    def _selected(self):
        return [self.tracks[i] for i in self.lst.curselection()]

    def _update_buttons(self):
        busy = self.worker is not None and self.worker.is_alive()
        self.btn_go.configure(state="disabled" if busy or not self._selected() else "normal",
                              text="Export %d tracks" % len(self._selected()) if len(self._selected()) > 1 else "Export")
        self.btn_cancel.configure(state="normal" if busy else "disabled")
        if hasattr(self, "btn_send"):
            ok = bool(self._selected()) and not busy and not self.ue_busy
            self.btn_send.configure(state="normal" if ok and self.editors else "disabled",
                                    text="Sending…" if self.ue_busy else "Send to Unreal")
            self.btn_launch.configure(state="normal" if ok else "disabled")
        self.cmb_size.configure(state="readonly" if self.var_tex.get() and not busy else "disabled")

    def _persist(self):
        self.s.update(content=self.var_content.get(), out=self.var_out.get(), textures=self.var_tex.get(),
                      max_texture=self.var_size.get(), lods=self.var_lods.get(), preview=self.var_preview.get(),
                      events=self.var_events.get(), skip_existing=self.var_skip.get(), selected=self._selected(),
                      ue_new_level=getattr(self, "var_newlevel", tk.BooleanVar(value=True)).get(),
                      ue_instances=getattr(self, "var_ue_inst", tk.BooleanVar(value=True)).get())
        save_settings(self.s)

    def _start(self):
        tracks = self._selected()
        out_root = self.var_out.get().strip().strip('"')
        if not tracks or not out_root:
            return
        try:
            os.makedirs(out_root, exist_ok=True)
        except OSError as e:
            messagebox.showerror(APP, "Can't create output folder:\n%s" % e); return
        self._persist()
        size = self.var_size.get()
        opts = dict(no_textures=not self.var_tex.get(), max_texture=0 if size == "Full" else int(size),
                    lods=self.var_lods.get(), preview=self.var_preview.get(), all_containers=self.var_events.get(),
                    skip_existing=self.var_skip.get())
        self.cancel_flag.clear()
        self.txt.configure(state="normal"); self.txt.delete("1.0", "end"); self.txt.configure(state="disabled")
        self.worker = threading.Thread(target=self._run, args=(self._content_dir(), tracks, out_root, opts), daemon=True)
        self.worker.start()
        self._t_start = time.time()
        self._update_buttons()

    def _cancel(self):
        self.cancel_flag.set()
        self.lbl_phase.configure(text="Cancelling…")

    # ---------------- worker (background thread -> queue)
    PHASES = {"Reading scene": (0.00, 0.05), "Meshes": (0.05, 0.40), "Textures": (0.40, 0.92), "Preview": (0.92, 0.99), "Done": (1, 1)}

    def _run(self, content, tracks, out_root, opts):
        results = []
        for ti, track in enumerate(tracks):
            out = os.path.join(out_root, track)
            self.q.put(("log", "── %s  (%d/%d) ──" % (pretty(track), ti + 1, len(tracks)), "head"))
            def progress(phase, done, total, ti=ti):
                a, b = self.PHASES.get(phase, (0, 0))
                frac = a + (b - a) * (done / float(total or 1))
                self.q.put(("progress", (ti + frac) / len(tracks), "%s · %s  %d/%d" % (pretty(track), phase, done, total)))
            try:
                args = EX.make_args(content=content, track=track, out=out, **opts)
                ex = EX.Exporter(args, progress=progress, logger=lambda l: self.q.put(("log", l, None)),
                                 cancel=self.cancel_flag.is_set)
                ex.run()
                results.append((track, out, ex.summary))
                sm = ex.summary
                self.q.put(("log", "✓ %s: %d meshes, %d materials, %d textures, %d instances in %.0fs"
                            % (pretty(track), sm['meshes'], sm['materials'], sm['textures'], sm['instances'], sm['seconds']), "ok"))
                if sm['warnings']:
                    self.q.put(("log", "  notes: %s" % ", ".join("%s ×%d" % kv for kv in sm['warnings'].items()), "warn"))
            except EX.Cancelled:
                self.q.put(("log", "Cancelled.", "warn")); break
            except Exception as e:
                self.q.put(("log", "✗ %s failed: %s" % (track, e), "err"))
                self.q.put(("log", traceback.format_exc(), "err"))
        self.q.put(("done", results, out_root))

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._log(msg[1], msg[2])
                elif kind == "progress":
                    self.pb["value"] = msg[1] * 1000
                    self.lbl_phase.configure(text=msg[2], foreground=FG)
                    el = time.time() - self._t_start
                    if msg[1] > 0.03:
                        rem = el / msg[1] - el
                        self.lbl_eta.configure(text="%s elapsed · ~%s left" % (self._fmt(el), self._fmt(rem)))
                elif kind == "done":
                    self._finished(msg[1], msg[2])
                elif kind == "editors":
                    self._set_editors(msg[1], msg[2])
                elif kind == "ue_done":
                    self._ue_finished(msg[1])
                elif kind == "waitpoll":
                    self._on_waitpoll(msg[1])
                elif kind == "ue_progress":
                    self.lbl_phase.configure(text=msg[1], foreground=FG)
        except queue.Empty:
            pass
        self.after(80, self._poll)

    @staticmethod
    def _fmt(sec):
        sec = int(max(sec, 0))
        return "%d:%02d" % (sec // 60, sec % 60)

    def _finished(self, results, out_root):
        self.worker = None
        self._update_buttons()
        self.lbl_eta.configure(text="")
        if not results:
            self.lbl_phase.configure(text="Stopped", foreground=WARN); return
        self.pb["value"] = 1000
        self.lbl_phase.configure(text="Finished %d track%s" % (len(results), "s" if len(results) > 1 else ""), foreground=OK)
        self._last_out = results[0][1] if len(results) == 1 else out_root
        self._log("", None)
        self._log("Next: in Unreal (5.3+, Python plugin on) → Tools → Execute Python Script → pick", "head")
        for t, out, sm in results:
            if sm.get("ue_script"):
                self._log("   " + sm["ue_script"], "ok")
        if results and results[0][2].get("ue_script"):
            cmd = 'py "%s"' % results[0][2]["ue_script"]
            self.clipboard_clear(); self.clipboard_append(cmd)
            self._log("(Copied  %s  to the clipboard - paste it into Unreal's console.)" % cmd, None)
        if getattr(self, "_send_after_export", False):
            self._send_after_export = False
            if self.editors:
                self.after(300, self._send)
            else:
                self.after(300, self._launch)
            return
        Done(self, results)

    # ---------------- Unreal
    def _discover_async(self):
        if not self._rx:
            self._set_editors([], "unreal_bridge.py missing"); return
        if self.ue_busy:
            return
        def job():
            try:
                with self._ue_lock:
                    eds = self._rx.discover(1.2)
                self.q.put(("editors", eds, None))
            except OSError as e:
                self.q.put(("editors", [], str(e)))
        threading.Thread(target=job, daemon=True).start()

    def _set_editors(self, eds, err):
        self.editors = eds
        if eds:
            labels = [e.label for e in eds]
            self.cmb_editor.configure(values=labels)
            if self.var_editor.get() not in labels:
                self.var_editor.set(labels[0])
            if len(eds) > 1:
                self.cmb_editor.pack(fill="x", pady=(6, 0), after=self.lbl_ue.master)
            else:
                self.cmb_editor.pack_forget()
            self.lbl_ue.configure(text="●  Connected: %s" % (eds[0].label if len(eds) == 1 else "%d editors open" % len(eds)), fg=OK)
            self.lbl_uehint.configure(text="Imports into the open project - keep Unreal open until it finishes.")
        else:
            self.cmb_editor.pack_forget()
            self.lbl_ue.configure(text="●  No open Unreal Editor found", fg=WARN)
            self.lbl_uehint.configure(text=(err + "\n" if err else "") +
                "Open your project, then in Edit → Project Settings → Plugins → Python tick "
                "'Enable Remote Execution' (and enable the Python Editor Script Plugin). "
                "Or click Launch project… - it turns that on for you, opens Unreal and imports.")
        self._update_buttons()
        if not self.ue_busy:
            self.after(6000, self._discover_async)

    def _ue_overrides(self):
        return {"NEW_LEVEL": bool(self.var_newlevel.get()), "PLACE_INSTANCES": bool(self.var_ue_inst.get())}

    def _exported(self, tracks):
        root = self.var_out.get().strip().strip('"')
        ok, missing = [], []
        for t in tracks:
            d = os.path.join(root, t)
            (ok if os.path.isfile(os.path.join(d, "track.json")) and os.path.isfile(os.path.join(d, "import_into_unreal.py")) else missing).append((t, d))
        return ok, missing

    def _check_ready(self):
        ok, missing = self._exported(self._selected())
        if missing:
            names = ", ".join(pretty(t) for t, _ in missing)
            if messagebox.askyesno(APP, "Not exported yet: %s\n\nExport now, then send to Unreal?" % names):
                self._send_after_export = True
                self._start()
            return None
        return ok

    def _send(self):
        ready = self._check_ready()
        if not ready or not self.editors:
            return
        labels = [e.label for e in self.editors]
        ed = self.editors[labels.index(self.var_editor.get())] if self.var_editor.get() in labels else self.editors[0]
        self._persist()
        self.ue_busy = True
        self._update_buttons()
        self.pb.configure(mode="indeterminate"); self.pb.start(12)
        self.lbl_eta.configure(text="")
        overrides = self._ue_overrides()
        self._log("── Sending %d track(s) to %s ──" % (len(ready), ed.label), "head")
        threading.Thread(target=self._send_worker, args=(ed, ready, overrides), daemon=True).start()

    def _send_worker(self, ed, ready, overrides):
        results = []
        for t, d in ready:
            self.q.put(("ue_progress", "Unreal · importing %s…" % pretty(t)))
            runner = UB.write_runner(d, overrides)
            stop = threading.Event()
            logf = UB.project_log(ed)
            if logf:
                threading.Thread(target=UB.tail_lines, daemon=True,
                                 args=(logf, stop, lambda l, t=t: self._ue_line(t, l))).start()
            try:
                with self._ue_lock:
                    res = self._rx.run(ed, UB.exec_file_statement(runner))
                outs = [o.get("output", "") for o in res.get("output", [])]
                ok = bool(res.get("success")) and any("EVO_IMPORT_OK" in o for o in outs)
                if not ok:
                    for o in outs[-12:]:
                        self.q.put(("log", "  " + o, "err" if "Error" in o or "Traceback" in o else None))
                results.append((t, ok))
            except Exception as e:
                self.q.put(("log", "✗ %s: %s" % (pretty(t), e), "err"))
                results.append((t, False))
            finally:
                time.sleep(0.5); stop.set()
        self.q.put(("ue_done", results))

    def _ue_line(self, track, line):
        txt = line.split("[EVO]", 1)[-1].strip()
        self.q.put(("log", "  UE ▸ " + txt, "err" if "failed" in txt else None))
        self.q.put(("ue_progress", "Unreal · %s · %s" % (pretty(track), txt[:70])))

    def _ue_finished(self, results):
        self.ue_busy = False
        self.pb.stop(); self.pb.configure(mode="determinate"); self.pb["value"] = 1000 if all(ok for _, ok in results) else 0
        good = [t for t, ok in results if ok]
        bad = [t for t, ok in results if not ok]
        if good:
            self._log("✓ In Unreal: %s  (Outliner → EVO/<track>)" % ", ".join(pretty(t) for t in good), "ok")
        if bad:
            self._log("✗ Import failed: %s - see the lines above / Unreal's Output Log" % ", ".join(pretty(t) for t in bad), "err")
        self.lbl_phase.configure(text="Unreal import finished" if not bad else "Unreal import had errors",
                                 foreground=OK if not bad else ERR)
        self._update_buttons()
        self.after(1000, self._discover_async)

    def _launch(self):
        ready = self._check_ready()
        if not ready:
            return
        up = filedialog.askopenfilename(title="Unreal project to import into", filetypes=[("Unreal project", "*.uproject")],
                                        initialdir=os.path.dirname(self.s.get("uproject", "")) or os.path.expanduser("~"))
        if not up:
            return
        up = os.path.normpath(up)
        self.s["uproject"] = up; self._persist()
        try:
            if UB.enable_remote_execution(up):
                self._log("Turned on Python Remote Execution in %s (Config/DefaultEngine.ini) - "
                          "Send to Unreal will work with this project from now on." % os.path.basename(up), "ok")
            what = UB.launch_editor(up)
        except Exception as e:
            messagebox.showerror(APP, str(e)); return
        name = os.path.splitext(os.path.basename(up))[0]
        self._log("Starting %s (%s). The import begins automatically once the editor has loaded…" % (name, what), "ok")
        self._wait_project = name.lower()
        self._wait_until = time.time() + 900
        self.ue_busy = True
        self._update_buttons()
        self.pb.configure(mode="indeterminate"); self.pb.start(12)
        self.lbl_phase.configure(text="Waiting for Unreal to open %s…" % name, foreground=FG)
        self._wait_for_editor()

    def _wait_for_editor(self):
        """Poll for the launched editor; when it shows up, send the import to it."""
        def job():
            try:
                with self._ue_lock:
                    eds = self._rx.discover(1.5)
            except OSError:
                eds = []
            self.q.put(("waitpoll", eds))
        threading.Thread(target=job, daemon=True).start()

    def _on_waitpoll(self, eds):
        match = [e for e in eds if e.project_name.lower() == getattr(self, "_wait_project", "")]
        if match:
            self.editors = eds
            labels = [e.label for e in eds]
            self.cmb_editor.configure(values=labels); self.var_editor.set(match[0].label)
            self.lbl_ue.configure(text="●  Connected: %s" % match[0].label, fg=OK)
            self._log("Unreal is up - sending the import.", "ok")
            self.ue_busy = False
            self.pb.stop(); self.pb.configure(mode="determinate")
            self.after(3000, self._send)          # give the editor a moment to finish loading the map
            return
        if time.time() > self._wait_until:
            self.ue_busy = False
            self.pb.stop(); self.pb.configure(mode="determinate")
            self._log("Unreal didn't answer within 15 minutes. If it's open, check Project Settings → Python → "
                      "Enable Remote Execution, then click Send to Unreal.", "err")
            self._update_buttons()
            self.after(1000, self._discover_async)
            return
        self.after(2000, self._wait_for_editor)

    def _log(self, line, tag=None):
        if tag is None and line:
            if "!" in line[:14] or "Warning" in line:
                tag = "warn"
        self.txt.configure(state="normal")
        self.txt.insert("end", line + "\n", tag or ())
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _close(self):
        if self.worker is not None and self.worker.is_alive():
            if not messagebox.askyesno(APP, "An export is running. Stop it and quit?"):
                return
            self.cancel_flag.set()
        self._persist()
        self.destroy()

class Done(tk.Toplevel):
    """Small summary window after an export."""
    def __init__(self, app, results):
        super().__init__(app)
        self.title("Export finished")
        self.configure(bg=PANEL)
        self.transient(app)
        self.resizable(False, False)
        f = tk.Frame(self, bg=PANEL, padx=22, pady=18); f.pack()
        tk.Label(f, text="✓  Export finished", bg=PANEL, fg=OK, font=(app.f_base[0], 14, "bold")).pack(anchor="w")
        for t, out, sm in results:
            tk.Label(f, text="%s — %d meshes · %d materials · %d textures · %s instances"
                     % (pretty(t), sm['meshes'], sm['materials'], sm['textures'], "{:,}".format(sm['instances'])),
                     bg=PANEL, fg=FG, font=app.f_base).pack(anchor="w", pady=(10 if t == results[0][0] else 2, 0))
        steps = ("Next: open your Unreal project (5.3+) and click  Send to Unreal  in panel 5.\n"
                 "No editor open? Use  Launch project…  - it starts Unreal and imports automatically.\n"
                 "Manual way: Tools → Execute Python Script → import_into_unreal.py in the track folder.\n"
                 "Or open <track>_preview.glb in Blender for a quick look.")
        tk.Label(f, text=steps, bg=PANEL, fg=MUTED, font=app.f_small, justify="left").pack(anchor="w", pady=(14, 0))
        b = tk.Frame(f, bg=PANEL); b.pack(fill="x", pady=(16, 0))
        ttk.Button(b, text="Open folder", command=lambda: open_folder(results[0][1] if len(results) == 1 else os.path.dirname(results[0][1]))).pack(side="left")
        ttk.Button(b, text="Close", command=self.destroy).pack(side="right")
        self.update_idletasks()
        x = app.winfo_rootx() + (app.winfo_width() - self.winfo_width()) // 2
        y = app.winfo_rooty() + (app.winfo_height() - self.winfo_height()) // 3
        self.geometry("+%d+%d" % (max(x, 0), max(y, 0)))

if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)      # crisp text on high-DPI screens
        except Exception:
            pass
    App().mainloop()
