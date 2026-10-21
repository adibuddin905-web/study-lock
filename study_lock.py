"""
STUDY LOCK  -  Windows study app
--------------------------------
* Study session me poori screen lock (fullscreen, always on top, Win/Alt+Tab/Alt+F4 blocked)
* Break me lock hat jata hai -> doubts clear karo
* Customizable study / break / cycles
* Stopwatch, daily hours, streak, 14-day chart
* Study audio (PC se file chuno) - sirf is app se play hota hai, lock ke andar bhi

Sirf Python 3.9+ (Windows) chahiye. Koi pip install nahi.
Run:  pythonw study_lock.py   (ya double-click run_study_app.bat)
"""
import sys, os, json, time, ctypes
import datetime as dt
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

IS_WIN = sys.platform.startswith("win")
DATA_FILE = os.path.join(os.path.expanduser("~"), ".study_lock_data.json")
STREAK_MIN_MINUTES = 30          # itni study kare to din "streak" me ginega

BG, CARD, FG, MUTED = "#12141c", "#1c2030", "#e8eaf2", "#8a90a8"
ACC, GOOD, WARN = "#6c8cff", "#43d9a0", "#ff8a65"
FONT = "Segoe UI"


# ----------------------------------------------------------------- helpers
def hms(s):
    s = max(0, int(s))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"

def ms(s):
    s = max(0, int(s + 0.999))
    return f"{s // 60:02d}:{s % 60:02d}"

def hm(s):
    s = int(s)
    return f"{s // 3600}h {s % 3600 // 60:02d}m"

def today_key():
    return dt.date.today().isoformat()


DEFAULTS = {
    "days": {},
    "doubts": [],
    "settings": {"study": 50, "break": 10, "cycles": 4,
                 "audio": "", "volume": 70, "goal": 4},
}

def load_data():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    for k, v in DEFAULTS.items():
        if k not in d:
            d[k] = json.loads(json.dumps(v))
    for k, v in DEFAULTS["settings"].items():
        d["settings"].setdefault(k, v)
    return d

def save_data(d):
    try:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, DATA_FILE)
    except Exception as e:
        print("save error", e)


# ----------------------------------------------------------------- audio (Windows MCI, mp3/wav)
class Audio:
    ALIAS = "studyaudio"

    def __init__(self):
        self.path = ""
        self.loaded = False
        self.playing = False
        self.volume = 70

    def _cmd(self, s):
        if not IS_WIN:
            return 1, ""
        buf = ctypes.create_unicode_buffer(256)
        err = ctypes.windll.winmm.mciSendStringW(s, buf, 255, 0)
        return err, buf.value

    def load(self, path):
        self.close()
        self.path = path
        if not path or not os.path.exists(path):
            return False
        kind = "waveaudio" if path.lower().endswith(".wav") else "mpegvideo"
        err, _ = self._cmd(f'open "{path}" type {kind} alias {self.ALIAS}')
        self.loaded = (err == 0)
        if self.loaded:
            self.set_volume(self.volume)
        return self.loaded

    def set_volume(self, v):
        self.volume = int(v)
        if self.loaded:
            self._cmd(f"setaudio {self.ALIAS} volume to {self.volume * 10}")

    def play(self):
        if not self.loaded:
            return
        err, _ = self._cmd(f"play {self.ALIAS}")
        self.playing = (err == 0)

    def pause(self):
        if self.loaded and self.playing:
            self._cmd(f"pause {self.ALIAS}")
        self.playing = False

    def resume(self):
        if self.loaded:
            err, _ = self._cmd(f"resume {self.ALIAS}")
            if err:
                self._cmd(f"play {self.ALIAS}")
            self.playing = True

    def toggle(self):
        if self.playing:
            self.pause()
        else:
            self.resume()

    def keep_looping(self):
        if self.loaded and self.playing:
            _, mode = self._cmd(f"status {self.ALIAS} mode")
            if mode == "stopped":
                self._cmd(f"play {self.ALIAS} from 0")

    def close(self):
        if self.loaded:
            self._cmd(f"close {self.ALIAS}")
        self.loaded = False
        self.playing = False


# ----------------------------------------------------------------- keyboard blocker (Windows)
class KeyBlocker:
    """Study ke time Win key, Alt+Tab, Alt+Esc, Alt+F4, Ctrl+Esc block karta hai.
    Ctrl+Alt+Del aur Ctrl+Shift+Esc Windows khud allow karta hai = emergency exit."""

    def __init__(self):
        self.hook = None
        self._cb = None

    def start(self):
        if not IS_WIN or self.hook:
            return
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32

        class KBDLL(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_size_t)]

        PROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                  wintypes.WPARAM, wintypes.LPARAM)
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                          wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, PROC, ctypes.c_void_p, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p

        def proc(n, w, l):
            try:
                if n == 0:
                    kb = ctypes.cast(l, ctypes.POINTER(KBDLL)).contents
                    vk, alt = kb.vkCode, bool(kb.flags & 0x20)
                    ctrl = bool(user32.GetAsyncKeyState(0x11) & 0x8000)
                    shift = bool(user32.GetAsyncKeyState(0x10) & 0x8000)
                    if (vk in (0x5B, 0x5C, 0x5D)                 # Win keys, Apps
                            or (alt and vk in (0x09, 0x1B, 0x73))  # Alt+Tab / Esc / F4
                            or (ctrl and not shift and vk == 0x1B)):  # Ctrl+Esc
                        return 1
            except Exception:
                pass
            return user32.CallNextHookEx(None, n, w, l)

        self._cb = PROC(proc)
        self.hook = user32.SetWindowsHookExW(13, self._cb, kernel32.GetModuleHandleW(None), 0)

    def stop(self):
        if IS_WIN and self.hook:
            ctypes.windll.user32.UnhookWindowsHookEx(self.hook)
        self.hook = None


# ----------------------------------------------------------------- app
class App:
    def __init__(self, root):
        self.root = root
        self.data = load_data()
        self.audio = Audio()
        self.blocker = KeyBlocker()

        self.phase = "idle"       # idle | study | break
        self.cycle = 0
        self.cycles = 1
        self.phase_total = 1
        self.phase_end = 0.0
        self.last_tick = time.time()
        self.last_save = time.time()
        self.lock = None

        # stopwatch
        self.sw_running = False
        self.sw_acc = 0.0
        self.sw_start = 0.0
        self.laps = []

        root.title("Study Lock")
        root.geometry("760x720")
        root.minsize(700, 660)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.style()
        self.build_ui()

        s = self.data["settings"]
        self.audio.volume = s["volume"]
        if s["audio"] and self.audio.load(s["audio"]):
            self.audio_name.set(os.path.basename(s["audio"]))

        self.refresh_doubts()
        self.refresh_progress()
        self.tick()

    # ---------- styling
    def style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", background=BG, foreground=FG, font=(FONT, 10))
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", background=CARD, foreground=MUTED,
                     padding=(18, 8), font=(FONT, 10, "bold"))
        st.map("TNotebook.Tab", background=[("selected", ACC)],
               foreground=[("selected", "#ffffff")])
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=CARD)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Card.TLabel", background=CARD, foreground=FG)
        st.configure("Muted.TLabel", background=CARD, foreground=MUTED)
        st.configure("TButton", background=CARD, foreground=FG, padding=8, borderwidth=0)
        st.map("TButton", background=[("active", "#2a3050")])
        st.configure("Acc.TButton", background=ACC, foreground="#ffffff",
                     font=(FONT, 11, "bold"), padding=10)
        st.map("Acc.TButton", background=[("active", "#8aa3ff")])
        st.configure("Horizontal.TProgressbar", troughcolor=CARD, background=GOOD,
                     borderwidth=0, thickness=10)
        st.configure("TSpinbox", fieldbackground=CARD, foreground=FG, background=CARD)
        st.configure("TScale", background=CARD, troughcolor=BG)

    # ---------- UI
    def build_ui(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=14, pady=14)
        self.nb = nb
        self.tab_session = ttk.Frame(nb)
        self.tab_sw = ttk.Frame(nb)
        self.tab_prog = ttk.Frame(nb)
        nb.add(self.tab_session, text="Study Session")
        nb.add(self.tab_sw, text="Stopwatch")
        nb.add(self.tab_prog, text="Progress")
        nb.bind("<<NotebookTabChanged>>", lambda e: self.refresh_progress())
        self.build_session()
        self.build_stopwatch()
        self.build_progress()

    def card(self, parent, **kw):
        f = ttk.Frame(parent, style="Card.TFrame", padding=14)
        f.pack(fill="x", pady=6, **kw)
        return f

    def build_session(self):
        p = self.tab_session
        s = self.data["settings"]

        # status
        c = self.card(p)
        self.lbl_phase = ttk.Label(c, text="Ready", style="Muted.TLabel", font=(FONT, 11))
        self.lbl_phase.pack()
        self.lbl_big = ttk.Label(c, text="00:00", style="Card.TLabel", font=(FONT, 54, "bold"))
        self.lbl_big.pack()
        self.pb = ttk.Progressbar(c, maximum=1000)
        self.pb.pack(fill="x", pady=(6, 0))
        self.lbl_today = ttk.Label(c, text="", style="Muted.TLabel")
        self.lbl_today.pack(pady=(8, 0))

        # settings
        c = self.card(p)
        row = ttk.Frame(c, style="Card.TFrame")
        row.pack()
        self.v_study = tk.IntVar(value=s["study"])
        self.v_break = tk.IntVar(value=s["break"])
        self.v_cycles = tk.IntVar(value=s["cycles"])
        for text, var, lo, hi in (("Study (min)", self.v_study, 1, 240),
                                  ("Break (min)", self.v_break, 1, 60),
                                  ("Cycles", self.v_cycles, 1, 12)):
            ttk.Label(row, text=text, style="Card.TLabel").pack(side="left", padx=(12, 4))
            ttk.Spinbox(row, from_=lo, to=hi, width=5, textvariable=var).pack(side="left")
        btns = ttk.Frame(c, style="Card.TFrame")
        btns.pack(pady=(12, 0))
        self.btn_start = ttk.Button(btns, text="START  (laptop lock)", style="Acc.TButton",
                                    command=self.start_session)
        self.btn_start.pack(side="left", padx=4)
        self.btn_skip = ttk.Button(btns, text="Break skip -> next study", command=self.skip_break)
        self.btn_stop = ttk.Button(btns, text="Session band karo", command=self.stop_session)

        # audio
        c = self.card(p)
        ttk.Label(c, text="Study audio", style="Card.TLabel",
                  font=(FONT, 10, "bold")).grid(row=0, column=0, sticky="w")
        self.audio_name = tk.StringVar(value="(koi file nahi chuni)")
        ttk.Label(c, textvariable=self.audio_name, style="Muted.TLabel").grid(
            row=0, column=1, sticky="w", padx=10)
        ttk.Button(c, text="File chuno", command=self.pick_audio).grid(row=0, column=2, padx=4)
        self.btn_play = ttk.Button(c, text="Play", command=self.toggle_audio)
        self.btn_play.grid(row=0, column=3, padx=4)
        ttk.Label(c, text="Vol", style="Muted.TLabel").grid(row=1, column=0, sticky="e", pady=(8, 0))
        self.v_vol = tk.IntVar(value=s["volume"])
        ttk.Scale(c, from_=0, to=100, variable=self.v_vol, command=self.on_volume).grid(
            row=1, column=1, columnspan=3, sticky="ew", pady=(8, 0))
        c.columnconfigure(1, weight=1)

        # doubts
        c = self.card(p)
        c.pack_configure(fill="both", expand=True)
        ttk.Label(c, text="Doubts (break me clear karne ke liye)", style="Card.TLabel",
                  font=(FONT, 10, "bold")).pack(anchor="w")
        self.lb = tk.Listbox(c, height=6, bg=BG, fg=FG, bd=0, highlightthickness=0,
                             selectbackground=ACC, font=(FONT, 11), activestyle="none")
        self.lb.pack(fill="both", expand=True, pady=6)
        row = ttk.Frame(c, style="Card.TFrame")
        row.pack(fill="x")
        self.v_doubt = tk.StringVar()
        e = tk.Entry(row, textvariable=self.v_doubt, bg=BG, fg=FG, insertbackground=FG,
                     relief="flat", font=(FONT, 11))
        e.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 6))
        e.bind("<Return>", lambda ev: self.add_doubt(self.v_doubt))
        ttk.Button(row, text="Add", command=lambda: self.add_doubt(self.v_doubt)).pack(side="left", padx=2)
        ttk.Button(row, text="Clear ho gaya", command=self.clear_doubt).pack(side="left", padx=2)

    def build_stopwatch(self):
        p = self.tab_sw
        c = self.card(p)
        self.lbl_sw = ttk.Label(c, text="00:00:00.0", style="Card.TLabel", font=(FONT, 56, "bold"))
        self.lbl_sw.pack(pady=10)
        row = ttk.Frame(c, style="Card.TFrame")
        row.pack()
        self.btn_sw = ttk.Button(row, text="Start", style="Acc.TButton", command=self.sw_toggle)
        self.btn_sw.pack(side="left", padx=4)
        ttk.Button(row, text="Lap", command=self.sw_lap).pack(side="left", padx=4)
        ttk.Button(row, text="Reset", command=self.sw_reset).pack(side="left", padx=4)
        ttk.Button(row, text="Study log me jodo", command=self.sw_save).pack(side="left", padx=4)
        c2 = self.card(p)
        c2.pack_configure(fill="both", expand=True)
        self.lb_laps = tk.Listbox(c2, bg=BG, fg=FG, bd=0, highlightthickness=0,
                                  font=(FONT, 11), activestyle="none")
        self.lb_laps.pack(fill="both", expand=True)

    def build_progress(self):
        p = self.tab_prog
        c = self.card(p)
        grid = ttk.Frame(c, style="Card.TFrame")
        grid.pack(fill="x")
        self.stat = {}
        for i, (k, name) in enumerate((("today", "Aaj"), ("week", "Is hafte"),
                                       ("total", "Total"), ("streak", "Streak"),
                                       ("best", "Best streak"))):
            f = ttk.Frame(grid, style="Card.TFrame")
            f.grid(row=0, column=i, padx=14)
            self.stat[k] = ttk.Label(f, text="-", style="Card.TLabel", font=(FONT, 18, "bold"))
            self.stat[k].pack()
            ttk.Label(f, text=name, style="Muted.TLabel").pack()
        grid.columnconfigure(tuple(range(5)), weight=1)

        c = self.card(p)
        row = ttk.Frame(c, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="Daily goal (hours)", style="Card.TLabel").pack(side="left")
        self.v_goal = tk.IntVar(value=self.data["settings"]["goal"])
        ttk.Spinbox(row, from_=1, to=16, width=4, textvariable=self.v_goal,
                    command=self.refresh_progress).pack(side="left", padx=8)
        self.pb_goal = ttk.Progressbar(c, maximum=1000)
        self.pb_goal.pack(fill="x", pady=(8, 0))
        self.lbl_goal = ttk.Label(c, text="", style="Muted.TLabel")
        self.lbl_goal.pack(anchor="w", pady=(4, 0))

        c = self.card(p)
        c.pack_configure(fill="both", expand=True)
        ttk.Label(c, text=f"Last 14 din (streak = roz kam se kam {STREAK_MIN_MINUTES} min)",
                  style="Muted.TLabel").pack(anchor="w")
        self.cv = tk.Canvas(c, bg=CARD, highlightthickness=0, height=230)
        self.cv.pack(fill="both", expand=True, pady=6)
        self.cv.bind("<Configure>", lambda e: self.draw_chart())

    # ---------- data / progress
    def secs(self, d):
        return self.data["days"].get(d.isoformat(), 0)

    def add_study(self, sec):
        k = today_key()
        self.data["days"][k] = self.data["days"].get(k, 0) + sec

    def streaks(self):
        thr = STREAK_MIN_MINUTES * 60
        ok = lambda d: self.secs(d) >= thr
        d = dt.date.today()
        if not ok(d):
            d -= dt.timedelta(days=1)
        cur = 0
        while ok(d):
            cur += 1
            d -= dt.timedelta(days=1)
        days = sorted(dt.date.fromisoformat(k) for k, v in self.data["days"].items() if v >= thr)
        best = run = 0
        prev = None
        for x in days:
            run = run + 1 if prev and (x - prev).days == 1 else 1
            best = max(best, run)
            prev = x
        return cur, best

    def refresh_progress(self):
        today = dt.date.today()
        t = self.secs(today)
        wk = sum(self.secs(today - dt.timedelta(days=i)) for i in range(7))
        total = sum(self.data["days"].values())
        cur, best = self.streaks()
        self.stat["today"].config(text=hm(t))
        self.stat["week"].config(text=hm(wk))
        self.stat["total"].config(text=hm(total))
        self.stat["streak"].config(text=f"{cur} din")
        self.stat["best"].config(text=f"{best} din")
        try:
            goal = max(1, int(self.v_goal.get())) * 3600
        except Exception:
            goal = 4 * 3600
        self.pb_goal["value"] = min(1000, t / goal * 1000)
        self.lbl_goal.config(text=f"{hm(t)} / {hm(goal)}")
        self.lbl_today.config(text=f"Aaj: {hm(t)}   |   Streak: {cur} din")
        self.draw_chart()

    def draw_chart(self):
        cv = self.cv
        cv.delete("all")
        w, h = cv.winfo_width(), cv.winfo_height()
        if w < 50 or h < 50:
            return
        today = dt.date.today()
        days = [today - dt.timedelta(days=13 - i) for i in range(14)]
        vals = [self.secs(d) / 3600 for d in days]
        mx = max(max(vals), 1)
        pad_b, pad_t = 28, 16
        bw = (w - 20) / 14
        thr = STREAK_MIN_MINUTES / 60
        for i, (d, v) in enumerate(zip(days, vals)):
            x0 = 10 + i * bw + 6
            x1 = 10 + (i + 1) * bw - 6
            bh = (h - pad_b - pad_t) * v / mx
            y1 = h - pad_b
            color = GOOD if v >= thr else MUTED
            cv.create_rectangle(x0, y1 - bh, x1, y1, fill=color, outline="")
            if v > 0:
                cv.create_text((x0 + x1) / 2, y1 - bh - 8, text=f"{v:.1f}", fill=FG, font=(FONT, 8))
            cv.create_text((x0 + x1) / 2, h - 12, text=str(d.day), fill=MUTED, font=(FONT, 9))

    # ---------- audio
    def pick_audio(self):
        path = filedialog.askopenfilename(
            title="Study audio chuno",
            filetypes=[("Audio", "*.mp3 *.wav *.wma *.m4a"), ("All files", "*.*")])
        if not path:
            return
        if self.audio.load(path):
            self.audio_name.set(os.path.basename(path))
            self.data["settings"]["audio"] = path
            save_data(self.data)
        else:
            messagebox.showerror("Audio", "Ye file load nahi hui. mp3 ya wav try karo.")

    def toggle_audio(self):
        if not self.audio.loaded:
            self.pick_audio()
            if not self.audio.loaded:
                return
        if self.audio.playing:
            self.audio.pause()
        elif self._started_once:
            self.audio.resume()
        else:
            self.audio.play()
            self._started_once = True
        self.sync_audio_buttons()

    _started_once = False

    def on_volume(self, v):
        self.audio.set_volume(float(v))
        self.data["settings"]["volume"] = int(float(v))

    def sync_audio_buttons(self):
        txt = "Pause" if self.audio.playing else "Play"
        self.btn_play.config(text=txt)
        if self.lock:
            self.l_play.config(text=txt)

    def audio_start_for_study(self):
        if self.audio.loaded and not self.audio.playing:
            if self._started_once:
                self.audio.resume()
            else:
                self.audio.play()
                self._started_once = True
        self.sync_audio_buttons()

    # ---------- doubts
    def add_doubt(self, var):
        t = var.get().strip()
        if not t:
            return
        self.data["doubts"].append({"t": t, "at": dt.datetime.now().strftime("%H:%M")})
        var.set("")
        save_data(self.data)
        self.refresh_doubts()

    def clear_doubt(self):
        sel = self.lb.curselection()
        if not sel:
            messagebox.showinfo("Doubts", "Pehle list me se doubt select karo.")
            return
        del self.data["doubts"][sel[0]]
        save_data(self.data)
        self.refresh_doubts()

    def refresh_doubts(self):
        self.lb.delete(0, "end")
        for d in self.data["doubts"]:
            self.lb.insert("end", f"{d['at']}   {d['t']}")
        if self.lock:
            self.l_lb.delete(0, "end")
            for d in self.data["doubts"][-5:]:
                self.l_lb.insert("end", f"{d['at']}   {d['t']}")
            self.l_count.config(text=f"{len(self.data['doubts'])} doubt(s) saved")

    # ---------- stopwatch
    def sw_elapsed(self):
        return self.sw_acc + (time.monotonic() - self.sw_start if self.sw_running else 0)

    def sw_toggle(self):
        if self.sw_running:
            self.sw_acc = self.sw_elapsed()
            self.sw_running = False
            self.btn_sw.config(text="Start")
        else:
            self.sw_start = time.monotonic()
            self.sw_running = True
            self.btn_sw.config(text="Pause")

    def sw_lap(self):
        e = self.sw_elapsed()
        if e <= 0:
            return
        self.laps.append(e)
        prev = self.laps[-2] if len(self.laps) > 1 else 0
        self.lb_laps.insert(0, f"Lap {len(self.laps)}    {self.sw_fmt(e)}    (+{self.sw_fmt(e - prev)})")

    def sw_reset(self):
        self.sw_running = False
        self.sw_acc = 0
        self.laps.clear()
        self.lb_laps.delete(0, "end")
        self.btn_sw.config(text="Start")

    def sw_save(self):
        e = self.sw_elapsed()
        if e < 1:
            return
        self.add_study(e)
        save_data(self.data)
        self.sw_reset()
        self.refresh_progress()
        messagebox.showinfo("Saved", f"{hm(e)} aaj ke study log me jud gaya.")

    @staticmethod
    def sw_fmt(e):
        return f"{hms(e)}.{int(e * 10) % 10}"

    # ---------- session control
    def start_session(self):
        if self.phase != "idle":
            return
        try:
            study, brk, cyc = int(self.v_study.get()), int(self.v_break.get()), int(self.v_cycles.get())
            assert 1 <= study <= 240 and 1 <= brk <= 60 and 1 <= cyc <= 12
        except Exception:
            messagebox.showerror("Settings", "Study 1-240, Break 1-60, Cycles 1-12 ke beech rakho.")
            return
        if not messagebox.askyesno(
                "Lock shuru?",
                f"{study} min ke liye poora laptop lock ho jayega (strict mode, beech me exit nahi).\n"
                f"Phir {brk} min break. Total {cyc} cycle.\n\nStart karein?"):
            return
        s = self.data["settings"]
        s.update(study=study, **{"break": brk}, cycles=cyc)
        save_data(self.data)
        self.cycles, self.cycle = cyc, 1
        self.start_study()

    def start_study(self):
        self.phase = "study"
        self.phase_total = int(self.v_study.get()) * 60
        self.phase_end = time.time() + self.phase_total
        self.btn_skip.pack_forget()
        self.btn_stop.pack_forget()
        self.btn_start.state(["disabled"])
        self.open_lock()
        self.audio_start_for_study()
        self.root.withdraw()
        self.beep()

    def finish_study(self):
        self.close_lock()
        self.audio.pause()
        self.sync_audio_buttons()
        save_data(self.data)
        self.phase = "break"
        self.phase_total = int(self.v_break.get()) * 60
        self.phase_end = time.time() + self.phase_total
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(1500, lambda: self.root.attributes("-topmost", False))
        self.btn_skip.pack(side="left", padx=4)
        self.btn_stop.pack(side="left", padx=4)
        self.nb.select(self.tab_session)
        self.beep()
        self.refresh_progress()

    def finish_break(self):
        if self.cycle < self.cycles:
            self.cycle += 1
            self.start_study()
        else:
            self.end_all("Sab cycles complete! Shabash.")

    def skip_break(self):
        if self.phase == "break":
            self.finish_break()

    def stop_session(self):
        if self.phase == "break" and messagebox.askyesno("Band karo", "Session yahin band kar dein?"):
            self.end_all("Session band.")

    def end_all(self, msg):
        self.phase = "idle"
        self.btn_skip.pack_forget()
        self.btn_stop.pack_forget()
        self.btn_start.state(["!disabled"])
        self.lbl_phase.config(text=msg, foreground=GOOD)
        self.lbl_big.config(text="00:00")
        self.pb["value"] = 0
        self.beep()
        save_data(self.data)
        self.refresh_progress()

    def beep(self):
        try:
            import winsound
            winsound.MessageBeep()
        except Exception:
            self.root.bell()

    # ---------- lock window
    def open_lock(self):
        w = tk.Toplevel(self.root)
        self.lock = w
        w.configure(bg="#0b0d14", cursor="arrow")
        w.attributes("-fullscreen", True)
        w.attributes("-topmost", True)
        w.protocol("WM_DELETE_WINDOW", lambda: None)
        for seq in ("<Alt-F4>", "<Escape>"):
            w.bind(seq, lambda e: "break")
        self.blocker.start()

        box = tk.Frame(w, bg="#0b0d14")
        box.place(relx=0.5, rely=0.5, anchor="center")
        self.l_phase = tk.Label(box, bg="#0b0d14", fg=MUTED, font=(FONT, 20))
        self.l_phase.pack()
        self.l_time = tk.Label(box, bg="#0b0d14", fg=FG, font=(FONT, 150, "bold"))
        self.l_time.pack()
        self.l_msg = tk.Label(box, bg="#0b0d14", fg=MUTED, font=(FONT, 16),
                              text="Focus. Doubts neeche likh lo - break me clear karna.")
        self.l_msg.pack(pady=(0, 24))

        row = tk.Frame(box, bg="#0b0d14")
        row.pack(pady=6)
        self.lv_doubt = tk.StringVar()
        self.l_entry = tk.Entry(row, textvariable=self.lv_doubt, width=50, bg=CARD, fg=FG,
                                insertbackground=FG, relief="flat", font=(FONT, 14))
        self.l_entry.pack(side="left", ipady=8, padx=(0, 8))
        self.l_entry.bind("<Return>", lambda e: self.add_doubt(self.lv_doubt))
        tk.Button(row, text="Doubt save", bg=ACC, fg="white", relief="flat", font=(FONT, 12, "bold"),
                  padx=14, pady=6, command=lambda: self.add_doubt(self.lv_doubt)).pack(side="left")
        self.l_count = tk.Label(box, bg="#0b0d14", fg=MUTED, font=(FONT, 12))
        self.l_count.pack()
        self.l_lb = tk.Listbox(box, height=5, width=70, bg=CARD, fg=FG, bd=0, highlightthickness=0,
                               font=(FONT, 12), activestyle="none")
        self.l_lb.pack(pady=8)

        arow = tk.Frame(box, bg="#0b0d14")
        arow.pack(pady=14)
        self.l_play = tk.Button(arow, text="Play", bg=CARD, fg=FG, relief="flat",
                                font=(FONT, 12, "bold"), padx=18, pady=6,
                                command=self.lock_toggle_audio)
        self.l_play.pack(side="left", padx=8)
        tk.Label(arow, text=os.path.basename(self.audio.path) if self.audio.loaded else "(audio nahi)",
                 bg="#0b0d14", fg=MUTED, font=(FONT, 11)).pack(side="left", padx=8)
        self.l_vol = tk.Scale(arow, from_=0, to=100, orient="horizontal", length=180,
                              bg="#0b0d14", fg=MUTED, troughcolor=CARD, highlightthickness=0,
                              command=self.on_volume)
        self.l_vol.set(self.audio.volume)
        self.l_vol.pack(side="left", padx=8)

        self.refresh_doubts()
        self.sync_audio_buttons()
        w.update_idletasks()
        w.focus_force()
        self.l_entry.focus_set()

    def lock_toggle_audio(self):
        if self.audio.loaded:
            if self.audio.playing:
                self.audio.pause()
            else:
                self.audio.resume()
            self.sync_audio_buttons()

    def close_lock(self):
        self.blocker.stop()
        if self.lock:
            try:
                self.lock.destroy()
            except Exception:
                pass
        self.lock = None

    # ---------- main loop
    def tick(self):
        now = time.time()
        d = now - self.last_tick
        self.last_tick = now
        if d > 2:          # laptop sleep / hang: us time ko study me mat gino
            d = 0

        if self.phase == "study":
            self.add_study(d)
            rem = self.phase_end - now
            elapsed = self.phase_total - rem
            label = f"Study {self.cycle}/{self.cycles}"
            self.lbl_phase.config(text=label, foreground=ACC)
            self.lbl_big.config(text=ms(rem))
            self.pb["value"] = max(0, min(1000, elapsed / self.phase_total * 1000))
            if self.lock:
                self.l_phase.config(text=f"STUDY  {self.cycle}/{self.cycles}")
                self.l_time.config(text=ms(rem))
                self.lock.lift()
                self.lock.attributes("-topmost", True)
                if self.lock.focus_displayof() is None:
                    self.lock.focus_force()
                    self.l_entry.focus_set()
            if rem <= 0:
                self.finish_study()
            if now - self.last_save > 20:
                save_data(self.data)
                self.last_save = now
                self.lbl_today.config(text=f"Aaj: {hm(self.secs(dt.date.today()))}   |   Streak: {self.streaks()[0]} din")

        elif self.phase == "break":
            rem = self.phase_end - now
            self.lbl_phase.config(text=f"BREAK - doubts clear karo ({self.cycle}/{self.cycles} done)",
                                  foreground=WARN)
            self.lbl_big.config(text=ms(rem))
            self.pb["value"] = max(0, min(1000, (self.phase_total - rem) / self.phase_total * 1000))
            if rem <= 0:
                self.finish_break()

        self.audio.keep_looping()

        if self.sw_running or self.lbl_sw.cget("text") == "":
            self.lbl_sw.config(text=self.sw_fmt(self.sw_elapsed()))
        elif not self.sw_running:
            self.lbl_sw.config(text=self.sw_fmt(self.sw_acc))

        self.root.after(200, self.tick)

    def on_close(self):
        if self.phase == "study":
            return
        if self.phase == "break" and not messagebox.askyesno("Exit", "Break chal raha hai. Band karein?"):
            return
        self.blocker.stop()
        self.audio.close()
        save_data(self.data)
        self.root.destroy()


def main():
    if IS_WIN:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
