"""
STUDY LOCK  -  Windows study app
--------------------------------
* Study session me poori screen lock (fullscreen, always on top, Win/Alt+Tab/Alt+F4 blocked)
* Break me lock hat jata hai -> doubts clear karo
* Customizable study / break / cycles
* Stopwatch, daily hours, streak, 14-day chart
* Study audio (PC se file chuno) - sirf is app se play hota hai, lock ke andar bhi
* Lecture mode: YouTube lecture lock ke andar chalta hai - PAUSE nahi hota, sirf aage/peeche

Sirf Python 3.9+ (Windows) chahiye. Koi pip install nahi.
Run:  pythonw study_lock.py   (ya double-click run_study_app.bat)
"""
import sys, os, json, time, ctypes, re, queue, secrets, threading, subprocess
import http.server
import urllib.parse
import datetime as dt
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

IS_WIN = sys.platform.startswith("win")
DATA_FILE = os.path.join(os.path.expanduser("~"), ".study_lock_data.json")
PLAYER_LOG = os.path.join(os.path.expanduser("~"), ".study_lock_player.log")
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
                 "audio": "", "volume": 70, "goal": 4,
                 "lecture_on": False, "lecture_url": "", "lecture_pos": {}},
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
        d["settings"].setdefault(k, json.loads(json.dumps(v)))
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


# ----------------------------------------------------------------- lecture (YouTube) support
def vt(sec):
    sec = max(0, int(sec))
    h, m, x = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{x:02d}" if h else f"{m}:{x:02d}"


def parse_youtube(url):
    """-> (video_id, start_seconds) ya (None, 0)"""
    url = (url or "").strip()
    m = re.search(r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:[^#]*&)?v=|embed/|shorts/|live/|v/))"
                  r"([A-Za-z0-9_-]{11})", url)
    if m:
        vid = m.group(1)
    elif re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        vid = url
    else:
        return None, 0
    start = 0
    mt = re.search(r"[?&#](?:t|start)=([0-9hms]+)", url)
    if mt:
        v = mt.group(1)
        if v.isdigit():
            start = int(v)
        else:
            for n, u in re.findall(r"(\d+)([hms])", v):
                start += int(n) * {"h": 3600, "m": 60, "s": 1}[u]
    return vid, start


PLAYER_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Lecture</title>
<style>
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:#0b0d14;color:#e8eaf2;font-family:"Segoe UI",sans-serif;overflow:hidden;user-select:none;-webkit-user-select:none}
body{display:flex;flex-direction:column}
#top{flex:0 0 64px;display:flex;align-items:center;justify-content:space-between;padding:0 28px;background:#12141c}
#phase{font-size:18px;color:#8a90a8}
#timer{font-size:40px;font-weight:700;letter-spacing:1px}
#stage{flex:1;position:relative;background:#000;min-height:0}
#yt{position:absolute;top:0;left:0;width:100%;height:100%;border:0}
#shield{position:absolute;top:0;left:0;right:0;bottom:0;z-index:5;display:flex;align-items:center;justify-content:center;text-align:center;font-size:22px}
#msg{display:none;background:rgba(11,13,20,.93);padding:28px 36px;border-radius:14px;max-width:70%;line-height:1.6}
button{font-family:inherit;border:0;border-radius:10px;background:#1c2030;color:#e8eaf2;font-size:16px;font-weight:600;padding:12px 18px;cursor:pointer}
button:hover{background:#2a3050}
button.acc{background:#6c8cff;color:#fff}
#ctl{flex:0 0 auto;display:flex;gap:10px;align-items:center;justify-content:center;padding:10px;background:#12141c}
#pos{min-width:160px;text-align:center;color:#8a90a8}
#dbt{flex:0 0 auto;display:flex;gap:10px;align-items:center;padding:10px 28px 14px;background:#12141c}
#inp{flex:1;background:#1c2030;border:0;border-radius:10px;color:#e8eaf2;font-size:16px;padding:12px 14px;font-family:inherit;user-select:text;-webkit-user-select:text;outline:none}
#cnt{color:#8a90a8;min-width:140px;text-align:right}
</style></head><body>
<div id="top"><div id="phase">Lecture</div><div id="timer">--:--</div></div>
<div id="stage"><div id="yt"></div><div id="shield"><div id="msg"></div></div></div>
<div id="ctl">
<button onclick="seek(-30)">-30s</button><button onclick="seek(-10)">-10s</button>
<span id="pos">0:00 / 0:00</span>
<button onclick="seek(10)">+10s</button><button onclick="seek(30)">+30s</button>
<button id="spd" onclick="cycleSpeed()">1x</button>
</div>
<div id="dbt"><input id="inp" placeholder="Doubt likho (video ka time apne aap jud jayega) - Enter dabao" autocomplete="off">
<button class="acc" onclick="saveDoubt()">Doubt save</button><span id="cnt"></span></div>
<script>
var K=new URLSearchParams(location.search).get('k');
var S=null,player=null,ready=false,started=false,ended=false,lastSave=0,count=0;
var SPEEDS=[1,1.25,1.5,1.75,2],si=0;
function $(id){return document.getElementById(id);}
function pad(n){return String(n).padStart(2,'0');}
function fmt(s){s=Math.max(0,Math.floor(s||0));var h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return h?(h+':'+pad(m)+':'+pad(x)):(m+':'+pad(x));}
function tm(r){r=Math.max(0,Math.ceil(r));return pad(Math.floor(r/60))+':'+pad(r%60);}
function api(path,body){return fetch(path+'?k='+encodeURIComponent(K),{method:body?'POST':'GET',body:body?JSON.stringify(body):undefined}).then(function(r){return r.json();});}
function showMsg(h){var m=$('msg');m.innerHTML=h;m.style.display='block';}
function hideMsg(){$('msg').style.display='none';}
function begin(){hideMsg();try{player.playVideo();}catch(e){}}
function seek(d){if(!ready)return;var t=player.getCurrentTime()+d,dur=player.getDuration();
 t=Math.max(0,dur>5?Math.min(t,dur-2):t);player.seekTo(t,true);if(started&&!ended)player.playVideo();}
function cycleSpeed(){si=(si+1)%SPEEDS.length;if(ready)player.setPlaybackRate(SPEEDS[si]);$('spd').textContent=SPEEDS[si]+'x';}
function saveDoubt(){var v=$('inp').value.trim();if(!v)return;var t=ready?player.getCurrentTime():0;
 api('/doubt',{text:v,t:t}).then(function(){count++;$('cnt').textContent=count+' doubt saved';$('inp').value='';});}
$('inp').addEventListener('keydown',function(e){if(e.key==='Enter'){saveDoubt();}});
document.addEventListener('keydown',function(e){
 var inp=e.target&&e.target.id==='inp';
 if(!inp){if(e.key==='ArrowLeft')seek(-10);else if(e.key==='ArrowRight')seek(10);e.preventDefault();return;}
 if(e.ctrlKey||e.altKey||/^F\d+$/.test(e.key))e.preventDefault();
},true);
document.addEventListener('contextmenu',function(e){e.preventDefault();});
function onReady(){ready=true;player.playVideo();
 setTimeout(function(){if(!started)showMsg('<button class="acc" onclick="begin()">&#9654; Lecture start karo</button>');},4000);}
function onState(e){var st=e.data;
 if(st===1){started=true;ended=false;hideMsg();}
 else if(st===2&&started&&!ended){player.playVideo();}
 else if(st===0){ended=true;showMsg('Video khatam ho gaya.<br>Timer chalne do, doubts likh lo.');}}
function onError(e){showMsg('Ye video yahan chal nahi sakta (error '+e.data+').<br>Video owner embed allow nahi karta ya link galat hai.<br>Koi aur video ka link do.');}
window.onYouTubeIframeAPIReady=function(){
 player=new YT.Player('yt',{videoId:S.video_id,playerVars:{autoplay:1,controls:0,disablekb:1,fs:0,rel:0,modestbranding:1,playsinline:1,iv_load_policy:3,start:Math.floor(S.start||0)},
  events:{onReady:onReady,onStateChange:onState,onError:onError}});};
setInterval(function(){
 if(!S)return;
 $('timer').textContent=tm((S.phase_end-Date.now())/1000);
 if(ready){var t=player.getCurrentTime(),d=player.getDuration();$('pos').textContent=fmt(t)+' / '+fmt(d);
  if(Date.now()-lastSave>4000){lastSave=Date.now();api('/pos',{t:t});}}
},500);
api('/state').then(function(s){S=s;$('phase').textContent='Study '+S.cycle+'/'+S.cycles;$('cnt').textContent=S.doubts+' doubt(s)';
 var sc=document.createElement('script');sc.src='https://www.youtube.com/iframe_api';
 sc.onerror=function(){showMsg('YouTube load nahi hua. Internet check karo.');};document.head.appendChild(sc);});
</script></body></html>
"""


class LectureServer:
    """Chhota local server (sirf 127.0.0.1): player page deta hai, doubts/position leta hai."""

    def __init__(self, app):
        self.app = app
        self.token = secrets.token_urlsafe(12)
        srv = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _parse(self):
                q = urllib.parse.urlparse(self.path)
                ok = urllib.parse.parse_qs(q.query).get("k", [""])[0] == srv.token
                return q.path, ok

            def _send(self, code, body, ctype="application/json"):
                b = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype + "; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                path, ok = self._parse()
                if not ok:
                    return self._send(403, "{}")
                if path == "/":
                    return self._send(200, PLAYER_HTML, "text/html")
                if path == "/state":
                    return self._send(200, json.dumps(srv.app.lecture_state()))
                self._send(404, "{}")

            def do_POST(self):
                path, ok = self._parse()
                if not ok:
                    return self._send(403, "{}")
                try:
                    n = int(self.headers.get("Content-Length", 0) or 0)
                    body = json.loads(self.rfile.read(n) or b"{}")
                except Exception:
                    body = {}
                try:
                    if path == "/doubt":
                        srv.app.q_doubts.put(body)
                    elif path == "/pos":
                        srv.app.lecture_pos = float(body.get("t", 0) or 0)
                except Exception:
                    pass
                self._send(200, "{}")

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self):
        return f"http://127.0.0.1:{self.port}/?k={self.token}"


def run_player(url):
    """Alag process: fullscreen, always-on-top lecture window (Edge WebView2)."""
    try:
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--autoplay-policy=no-user-gesture-required"
        import webview
        webview.create_window("Study Lecture", url, fullscreen=True, on_top=True,
                              background_color="#0b0d14", easy_drag=False, text_select=False)
        webview.start(gui="edgechromium" if IS_WIN else None)
    except Exception:
        import traceback
        try:
            with open(PLAYER_LOG, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + traceback.format_exc() + "\n")
        except Exception:
            pass
        sys.exit(1)


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

        # lecture
        self.q_doubts = queue.Queue()
        self.server = None
        self.player = None
        self.player_started = 0.0
        self.player_fail = 0
        self.lec_active = False
        self.lec_vid = None
        self.lec_start = 0.0
        self.lecture_pos = 0.0

        # stopwatch
        self.sw_running = False
        self.sw_acc = 0.0
        self.sw_start = 0.0
        self.laps = []

        root.title("Study Lock")
        root.geometry("760x730")
        root.minsize(700, 640)
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
        self.update_lec_label()
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
        st.configure("Card.TCheckbutton", background=CARD, foreground=FG)
        st.map("Card.TCheckbutton", background=[("active", CARD)])
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
        f = ttk.Frame(parent, style="Card.TFrame", padding=10)
        f.pack(fill="x", pady=4, **kw)
        return f

    def build_session(self):
        p = self.tab_session
        s = self.data["settings"]

        # status
        c = self.card(p)
        self.lbl_phase = ttk.Label(c, text="Ready", style="Muted.TLabel", font=(FONT, 11))
        self.lbl_phase.pack()
        self.lbl_big = ttk.Label(c, text="00:00", style="Card.TLabel", font=(FONT, 44, "bold"))
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

        # lecture
        c = self.card(p)
        self.v_lec_on = tk.BooleanVar(value=s["lecture_on"])
        ttk.Checkbutton(c, text="Lecture mode: study time me YouTube lecture chalao (pause nahi hoga, sirf aage/peeche)",
                        variable=self.v_lec_on, style="Card.TCheckbutton").pack(anchor="w")
        row = ttk.Frame(c, style="Card.TFrame")
        row.pack(fill="x", pady=(8, 0))
        self.v_lec_url = tk.StringVar(value=s["lecture_url"])
        le = tk.Entry(row, textvariable=self.v_lec_url, bg=BG, fg=FG, insertbackground=FG,
                      relief="flat", font=(FONT, 11))
        le.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 6))
        le.bind("<FocusOut>", lambda ev: self.update_lec_label())
        ttk.Button(row, text="Position reset", command=self.reset_lec_pos).pack(side="left")
        self.lbl_lec = ttk.Label(c, text="", style="Muted.TLabel")
        self.lbl_lec.pack(anchor="w", pady=(4, 0))

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
        self.lb = tk.Listbox(c, height=4, bg=BG, fg=FG, bd=0, highlightthickness=0,
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
        if not self.prepare_lecture():
            return
        if not messagebox.askyesno(
                "Lock shuru?",
                f"{study} min ke liye poora laptop lock ho jayega (strict mode, beech me exit nahi).\n"
                f"Phir {brk} min break. Total {cyc} cycle.\n"
                f"{'Lecture mode ON: video pause nahi hoga.' if self.lec_active else ''}\n\nStart karein?"):
            return
        s = self.data["settings"]
        s.update(study=study, **{"break": brk}, cycles=cyc,
                 lecture_on=bool(self.v_lec_on.get()), lecture_url=self.v_lec_url.get().strip())
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
        if self.lec_active:
            self.audio.pause()
            self.sync_audio_buttons()
            self.player_fail = 0
            self.launch_player()
        else:
            self.audio_start_for_study()
        self.root.withdraw()
        self.beep()

    def finish_study(self):
        self.stop_player()
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
        self.update_lec_label()
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

    # ---------- lecture
    def update_lec_label(self):
        vid, t0 = parse_youtube(self.v_lec_url.get())
        if not self.v_lec_url.get().strip():
            self.lbl_lec.config(text="YouTube video ka link yahan paste karo.")
        elif not vid:
            self.lbl_lec.config(text="Link samajh nahi aaya (ek video ka link do, playlist ka nahi).")
        else:
            saved = self.data["settings"]["lecture_pos"].get(vid)
            pos = saved if saved is not None else t0
            self.lbl_lec.config(text=f"Video OK  |  Yahan se resume hoga: {vt(pos)}")

    def reset_lec_pos(self):
        vid, _ = parse_youtube(self.v_lec_url.get())
        if vid:
            self.data["settings"]["lecture_pos"].pop(vid, None)
            save_data(self.data)
        self.update_lec_label()

    def prepare_lecture(self):
        self.lec_active = False
        if not self.v_lec_on.get():
            return True
        vid, t0 = parse_youtube(self.v_lec_url.get())
        if not vid:
            messagebox.showerror("Lecture", "YouTube video ka sahi link daalo (ek video ka, playlist ka nahi).")
            return False
        saved = self.data["settings"]["lecture_pos"].get(vid)
        self.lec_vid = vid
        self.lec_start = float(saved if saved is not None else t0)
        self.lecture_pos = self.lec_start
        self.lec_active = True
        return True

    def lecture_state(self):          # server thread se bulaya jata hai
        return {"video_id": self.lec_vid, "start": self.lec_start,
                "phase_end": self.phase_end * 1000, "cycle": self.cycle,
                "cycles": self.cycles, "doubts": len(self.data["doubts"])}

    def launch_player(self):
        if self.server is None:
            self.server = LectureServer(self)
        self.lec_start = self.lecture_pos
        cmd = [sys.executable]
        if not getattr(sys, "frozen", False):
            cmd.append(os.path.abspath(__file__))
        cmd += ["--player", self.server.url()]
        self.player_started = time.time()
        try:
            self.player = subprocess.Popen(cmd, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            print("player launch error", e)
            self.player = None
            self.player_fail += 1

    def stop_player(self):
        if self.player is not None:
            try:
                if self.player.poll() is None:
                    self.player.terminate()
            except Exception:
                pass
        self.player = None
        if self.lec_active and self.lec_vid and self.lecture_pos > 0:
            self.data["settings"]["lecture_pos"][self.lec_vid] = max(0, self.lecture_pos - 3)
            save_data(self.data)

    def retry_player(self):
        self.player_fail = 0
        self.player_started = 0
        if self.lock and hasattr(self, "l_msg"):
            self.l_msg.config(text="Lecture player khul raha hai... (2-5 sec)")

    def lecture_tick(self, now):
        """True = player screen par hai (Tk lock ko peeche rehna hai)."""
        if self.player is not None and self.player.poll() is not None:
            ran = now - self.player_started
            self.player = None
            self.player_fail = self.player_fail + 1 if ran < 10 else 0
        if self.player is None:
            if self.player_fail < 3:
                if now - self.player_started > 2:
                    self.launch_player()
            elif self.lock:
                self.l_msg.config(text="Lecture player start nahi hua (Edge WebView2?). Retry dabao. "
                                       f"Log: {PLAYER_LOG}")
        return self.player is not None

    def drain_doubts(self):
        changed = False
        while True:
            try:
                b = self.q_doubts.get_nowait()
            except queue.Empty:
                break
            text = str(b.get("text", "")).strip()
            if not text:
                continue
            try:
                t = float(b.get("t", 0) or 0)
            except Exception:
                t = 0
            if t > 0:
                text += f"   [video {vt(t)}]"
            self.data["doubts"].append({"t": text, "at": dt.datetime.now().strftime("%H:%M")})
            changed = True
        if changed:
            save_data(self.data)
            self.refresh_doubts()

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
        if self.lec_active:
            self.l_msg.config(text="Lecture player khul raha hai... (2-5 sec). Na khule to Retry dabao.")
            tk.Button(box, text="Lecture dobara kholo (Retry)", bg=CARD, fg=FG, relief="flat",
                      font=(FONT, 11, "bold"), command=self.retry_player).pack(pady=(0, 14))

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
        self.drain_doubts()

        if self.phase == "study":
            self.add_study(d)
            rem = self.phase_end - now
            elapsed = self.phase_total - rem
            label = f"Study {self.cycle}/{self.cycles}"
            self.lbl_phase.config(text=label, foreground=ACC)
            self.lbl_big.config(text=ms(rem))
            self.pb["value"] = max(0, min(1000, elapsed / self.phase_total * 1000))
            owns = self.lecture_tick(now) if self.lec_active else False
            if self.lock:
                self.l_phase.config(text=f"STUDY  {self.cycle}/{self.cycles}")
                self.l_time.config(text=ms(rem))
                if not owns:   # lecture player screen par ho to usse upar mat aao
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
        self.stop_player()
        self.blocker.stop()
        self.audio.close()
        save_data(self.data)
        self.root.destroy()


def main():
    if "--player" in sys.argv:
        if IS_WIN:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
        run_player(sys.argv[sys.argv.index("--player") + 1])
        return
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
