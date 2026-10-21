"""
StudyLock - focus sessions for Windows
--------------------------------------
Two separate modes:
  * Self Study : full-screen lock, optional study audio, doubts notepad
  * Lecture    : full-screen lock with a YouTube lecture (pause / resume / seek allowed)
Plus stopwatch, daily hours, streaks and a 14-day chart.

Needs only Python 3.9+ (Windows). No pip packages for the main app.
(The Lecture player uses 'pywebview', which the GitHub build installs for you.)
Run:  pythonw study_lock.py
"""
import sys, os, json, time, ctypes, re, queue, secrets, threading, subprocess
import http.server
import urllib.parse
import datetime as dt
import tkinter as tk
from tkinter import filedialog, messagebox

IS_WIN = sys.platform.startswith("win")
APP_NAME = "StudyLock"
DATA_FILE = os.path.join(os.path.expanduser("~"), ".study_lock_data.json")
PLAYER_LOG = os.path.join(os.path.expanduser("~"), ".study_lock_player.log")
STREAK_MIN_MINUTES = 30          # a day counts for the streak after this much study

# ----------------------------------------------------------------- theme
BG       = "#0d1117"
SIDEBAR  = "#010409"
SURFACE  = "#161b22"
SURFACE2 = "#21262d"
BORDER   = "#30363d"
TRACK    = "#262c36"
TEXT     = "#e6edf3"
MUTED    = "#8b949e"
ACC      = "#4f8cff"      # self study
ACC_H    = "#6ea0ff"
VIOLET   = "#a371f7"      # lecture
GOOD     = "#3fb950"
WARN     = "#d29922"
DANGER   = "#f85149"
LOCK_BG  = "#010409"
FONT     = "Segoe UI"


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

def vt(sec):
    sec = max(0, int(sec))
    h, m, x = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{x:02d}" if h else f"{m}:{x:02d}"

def today_key():
    return dt.date.today().isoformat()


# ----------------------------------------------------------------- data
def default_settings():
    return {
        "goal": 4,
        "self": {"study": 50, "break": 10, "cycles": 4, "audio": "", "volume": 70},
        "lecture": {"study": 50, "break": 10, "cycles": 2, "url": "", "pos": {}},
    }

def load_data():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            d = {}
    except Exception:
        d = {}
    d.setdefault("days", {})
    d.setdefault("days_lecture", {})
    d.setdefault("doubts", [])
    st = d.get("settings")
    if not isinstance(st, dict):
        st = {}
    old = dict(st)                               # settings from the first versions of the app
    new = default_settings()
    new["goal"] = old.get("goal", 4)
    for k in ("study", "break", "cycles"):
        if k in old:
            new["self"][k] = old[k]
            if k != "cycles":
                new["lecture"][k] = old[k]
    for k in ("audio", "volume"):
        if k in old:
            new["self"][k] = old[k]
    if old.get("lecture_url"):
        new["lecture"]["url"] = old["lecture_url"]
    if isinstance(old.get("lecture_pos"), dict):
        new["lecture"]["pos"] = old["lecture_pos"]
    for mode in ("self", "lecture"):
        if isinstance(old.get(mode), dict):
            new[mode].update(old[mode])
    d["settings"] = new
    return d

def save_data(d):
    try:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, DATA_FILE)
    except Exception as e:
        print("save error", e)


def dark_titlebar(win):
    """Dark window title bar on Windows 10/11 (ignored elsewhere)."""
    if not IS_WIN:
        return
    try:
        win.update_idletasks()
        user32 = ctypes.windll.user32
        user32.GetParent.restype = ctypes.c_void_p
        user32.GetParent.argtypes = [ctypes.c_void_p]
        hwnd = user32.GetParent(win.winfo_id())
        val = ctypes.c_int(1)
        for attr in (20, 19):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd), attr, ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        pass


# ----------------------------------------------------------------- audio (Windows MCI, mp3/wav)
class Audio:
    ALIAS = "studyaudio"

    def __init__(self):
        self.path = ""
        self.loaded = False
        self.playing = False
        self.started = False
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
        self.started = False
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
        self.started = self.started or self.playing

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

    def start(self):
        """Play from the beginning the first time, resume afterwards."""
        if not self.loaded:
            return
        if self.started:
            self.resume()
        else:
            self.play()

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
        self.started = False


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
<html lang="en"><head><meta charset="utf-8"><title>StudyLock Lecture</title>
<style>
:root{--bg:#0d1117;--bar:#161b22;--line:#30363d;--text:#e6edf3;--mut:#8b949e;--acc:#a371f7;--btn:#21262d;--btnh:#2d333b}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--text);font-family:"Segoe UI",system-ui,sans-serif;overflow:hidden;user-select:none;-webkit-user-select:none}
body{display:flex;flex-direction:column}
#top{flex:0 0 60px;display:flex;align-items:center;justify-content:space-between;padding:0 28px;background:var(--bar);border-bottom:1px solid var(--line)}
.brand{font-weight:700;font-size:16px;letter-spacing:.3px}
.chip{margin-left:14px;font-size:12px;font-weight:700;color:var(--acc);background:rgba(163,113,247,.15);padding:5px 11px;border-radius:999px}
#timerbox{text-align:right}
#timer{font-size:30px;font-weight:700;line-height:1;font-variant-numeric:tabular-nums}
#tl{font-size:10px;color:var(--mut);letter-spacing:1px;margin-top:4px}
#stage{flex:1;position:relative;background:#000;min-height:0}
#yt{position:absolute;top:0;left:0;width:100%;height:100%;border:0}
#shield{position:absolute;top:0;left:0;right:0;bottom:0;z-index:5;display:flex;align-items:center;justify-content:center;cursor:pointer}
#paused{display:none;position:absolute;top:0;left:0;right:0;bottom:0;background:rgba(13,17,23,.93);flex-direction:column;align-items:center;justify-content:center;gap:12px;text-align:center}
#paused .ic{width:84px;height:84px;border-radius:50%;background:var(--acc);display:flex;align-items:center;justify-content:center;font-size:34px;color:#fff;padding-left:6px}
#paused .t{font-size:24px;font-weight:700}
#paused .s{font-size:14px;color:var(--mut)}
#msg{display:none;position:relative;z-index:2;background:rgba(22,27,34,.97);border:1px solid var(--line);padding:26px 34px;border-radius:14px;max-width:70%;line-height:1.6;font-size:18px;text-align:center;cursor:default}
#ctl{flex:0 0 auto;padding:12px 28px 6px;background:var(--bar);border-top:1px solid var(--line)}
#seekrow{display:flex;align-items:center;gap:12px;color:var(--mut);font-size:13px;font-variant-numeric:tabular-nums}
#bar{flex:1;height:18px;display:flex;align-items:center;cursor:pointer}
#track{width:100%;height:6px;background:#30363d;border-radius:4px;overflow:hidden}
#fill{height:100%;width:0;background:var(--acc)}
#btns{display:flex;gap:10px;align-items:center;justify-content:center;padding:10px 0 6px}
.b{font-family:inherit;border:0;border-radius:9px;background:var(--btn);color:var(--text);font-size:14px;font-weight:600;padding:10px 16px;cursor:pointer}
.b:hover{background:var(--btnh)}
.b.main{background:var(--acc);color:#fff}
.b.main:hover{filter:brightness(1.1)}
#pp{min-width:110px}
#dbt{flex:0 0 auto;display:flex;gap:10px;align-items:center;padding:8px 28px 16px;background:var(--bar)}
#inp{flex:1;background:var(--btn);border:1px solid var(--line);border-radius:9px;color:var(--text);font-size:15px;padding:11px 14px;font-family:inherit;user-select:text;-webkit-user-select:text;outline:none}
#inp:focus{border-color:var(--acc)}
#cnt{color:var(--mut);min-width:130px;text-align:right;font-size:13px}
#note{color:var(--mut);font-size:12px;text-align:center;padding:0 0 8px;background:var(--bar)}
</style></head><body>
<div id="top"><div style="display:flex;align-items:center"><span class="brand">StudyLock &middot; Lecture</span><span class="chip" id="phase">Session</span></div>
<div id="timerbox"><div id="timer">--:--</div><div id="tl">FOCUS TIME LEFT</div></div></div>
<div id="stage"><div id="yt"></div>
<div id="shield" onclick="shieldClick(event)"><div id="paused"><div class="ic">&#9654;</div><div class="t">Paused</div><div class="s">The focus timer keeps running. Press Space or click to resume.</div></div>
<div id="msg"></div></div></div>
<div id="ctl">
<div id="seekrow"><span id="cur">0:00</span><div id="bar"><div id="track"><div id="fill"></div></div></div><span id="dur">0:00</span></div>
<div id="btns">
<button class="b" onclick="seek(-30)">&minus;30s</button><button class="b" onclick="seek(-10)">&minus;10s</button>
<button id="pp" class="b main" onclick="toggle()">Pause</button>
<button class="b" onclick="seek(10)">+10s</button><button class="b" onclick="seek(30)">+30s</button>
<button id="spd" class="b" onclick="cycleSpeed()">1x</button>
</div></div>
<div id="dbt"><input id="inp" placeholder="Write a doubt and press Enter - the video time is added automatically" autocomplete="off">
<button class="b main" onclick="saveDoubt()">Save doubt</button><span id="cnt"></span></div>
<div id="note">Shortcuts: Space = pause / resume &nbsp;&middot;&nbsp; Left / Right = &minus;10s / +10s</div>
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
function toggle(){
 if(!ready)return;
 if(ended){ended=false;hideMsg();player.seekTo(0,true);player.playVideo();return;}
 var st=player.getPlayerState();
 if(st===1||st===3){player.pauseVideo();}else{player.playVideo();}
}
function shieldClick(e){if(e.target.closest&&e.target.closest('#msg'))return;toggle();}
function seek(d){if(!ready)return;var t=player.getCurrentTime()+d,dur=player.getDuration();
 t=Math.max(0,dur>5?Math.min(t,dur-2):t);player.seekTo(t,true);}
function cycleSpeed(){si=(si+1)%SPEEDS.length;if(ready)player.setPlaybackRate(SPEEDS[si]);$('spd').textContent=SPEEDS[si]+'x';}
function saveDoubt(){var v=$('inp').value.trim();if(!v)return;var t=ready?player.getCurrentTime():0;
 api('/doubt',{text:v,t:t}).then(function(){count++;$('cnt').textContent=count+' doubt(s) saved';$('inp').value='';});}
$('inp').addEventListener('keydown',function(e){if(e.key==='Enter'){saveDoubt();}});
$('bar').addEventListener('click',function(e){
 if(!ready)return;var r=this.getBoundingClientRect(),f=Math.max(0,Math.min(1,(e.clientX-r.left)/r.width));
 player.seekTo(f*player.getDuration(),true);});
document.addEventListener('keydown',function(e){
 var inp=e.target&&e.target.id==='inp';
 if(!inp){
  if(e.key==='ArrowLeft')seek(-10);else if(e.key==='ArrowRight')seek(10);else if(e.key===' ')toggle();
  e.preventDefault();return;}
 if(e.ctrlKey||e.altKey||/^F\d+$/.test(e.key))e.preventDefault();
},true);
document.addEventListener('contextmenu',function(e){e.preventDefault();});
function onReady(){ready=true;player.playVideo();
 setTimeout(function(){if(!started)showMsg('<button class="b main" onclick="begin()">&#9654; Start lecture</button>');},4000);}
function onState(e){var st=e.data,pp=$('pp');
 if(st===1){started=true;ended=false;hideMsg();$('paused').style.display='none';pp.textContent='Pause';}
 else if(st===2){if(started)$('paused').style.display='flex';pp.textContent='Resume';}
 else if(st===0){ended=true;pp.textContent='Replay';$('paused').style.display='none';
  showMsg('Lecture finished.<br>The timer is still running - use the time to write down your doubts.');}}
function onError(e){showMsg('This video cannot be played here (error '+e.data+').<br>The owner may have disabled embedding, or the link is wrong.<br>Close this session after the timer and try another video.');}
window.onYouTubeIframeAPIReady=function(){
 player=new YT.Player('yt',{videoId:S.video_id,playerVars:{autoplay:1,controls:0,disablekb:1,fs:0,rel:0,modestbranding:1,playsinline:1,iv_load_policy:3,start:Math.floor(S.start||0)},
  events:{onReady:onReady,onStateChange:onState,onError:onError}});};
setInterval(function(){
 if(!S)return;
 $('timer').textContent=tm((S.phase_end-Date.now())/1000);
 if(ready){var t=player.getCurrentTime(),d=player.getDuration();
  $('cur').textContent=fmt(t);$('dur').textContent=fmt(d);$('fill').style.width=(d>0?Math.min(100,t/d*100):0)+'%';
  if(Date.now()-lastSave>4000){lastSave=Date.now();api('/pos',{t:t});}}
},400);
api('/state').then(function(s){S=s;$('phase').textContent='Session '+S.cycle+' of '+S.cycles;$('cnt').textContent=S.doubts+' doubt(s) in list';
 var sc=document.createElement('script');sc.src='https://www.youtube.com/iframe_api';
 sc.onerror=function(){showMsg('Could not load YouTube. Please check your internet connection.');};document.head.appendChild(sc);});
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


# ----------------------------------------------------------------- UI widgets
BTN_KINDS = {
    "primary":   (ACC, "#ffffff", ACC_H),
    "violet":    (VIOLET, "#ffffff", "#b68cff"),
    "secondary": (SURFACE2, TEXT, "#30363d"),
    "danger":    ("#3d1417", DANGER, "#59191d"),
    "success":   ("#10361c", GOOD, "#17502a"),
}
DIS_BG, DIS_FG = "#1b2028", "#5b626c"


class Btn(tk.Label):
    """Flat button built on a Label (hover effect, enable / disable)."""

    def __init__(self, parent, text, command=None, kind="secondary", size=10, padx=18, pady=9):
        self.c_bg, self.c_fg, self.c_hv = BTN_KINDS[kind]
        self.cmd = command
        self.enabled = True
        super().__init__(parent, text=text, bg=self.c_bg, fg=self.c_fg, font=(FONT, size, "bold"),
                         padx=padx, pady=pady, cursor="hand2")
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._click)

    def _enter(self, e):
        if self.enabled:
            self.config(bg=self.c_hv)

    def _leave(self, e):
        self.config(bg=self.c_bg if self.enabled else DIS_BG)

    def _click(self, e):
        if self.enabled and self.cmd:
            self.cmd()

    def enable(self, on=True):
        self.enabled = on
        self.config(bg=self.c_bg if on else DIS_BG, fg=self.c_fg if on else DIS_FG,
                    cursor="hand2" if on else "arrow")

    def set_text(self, text):
        self.config(text=text)


def card(parent, title=None, pad=16):
    """Bordered surface. Returns (outer, inner): pack/grid `outer`, put widgets in `inner`."""
    outer = tk.Frame(parent, bg=BORDER)
    inner = tk.Frame(outer, bg=SURFACE, padx=pad, pady=pad)
    inner.pack(fill="both", expand=True, padx=1, pady=1)
    if title:
        tk.Label(inner, text=title.upper(), bg=SURFACE, fg=MUTED,
                 font=(FONT, 9, "bold")).pack(anchor="w", pady=(0, 12))
    return outer, inner


def make_entry(parent, var, size=11, accent=ACC):
    return tk.Entry(parent, textvariable=var, bg=SURFACE2, fg=TEXT, insertbackground=TEXT,
                    relief="flat", font=(FONT, size), highlightthickness=1,
                    highlightbackground=BORDER, highlightcolor=accent)


def make_listbox(parent, height=5):
    return tk.Listbox(parent, height=height, bg=SURFACE2, fg=TEXT, bd=0, highlightthickness=1,
                      highlightbackground=BORDER, highlightcolor=BORDER, selectbackground=ACC,
                      selectforeground="#ffffff", font=(FONT, 10), activestyle="none")


def page_header(parent, title, subtitle):
    head = tk.Frame(parent, bg=BG)
    head.pack(fill="x", pady=(0, 18))
    tk.Label(head, text=title, bg=BG, fg=TEXT, font=(FONT, 22, "bold")).pack(anchor="w")
    tk.Label(head, text=subtitle, bg=BG, fg=MUTED, font=(FONT, 10)).pack(anchor="w", pady=(2, 0))


class Stepper(tk.Frame):
    """Label + [-] value [+] number input."""

    def __init__(self, parent, label, value, lo, hi, step=1, bg=SURFACE, command=None):
        super().__init__(parent, bg=bg)
        self.lo, self.hi, self.step, self.command = lo, hi, step, command
        self.var = tk.StringVar(value=str(value))
        tk.Label(self, text=label, bg=bg, fg=MUTED, font=(FONT, 9, "bold")).pack(anchor="w")
        row = tk.Frame(self, bg=bg)
        row.pack(pady=(6, 0))
        Btn(row, "\u2212", lambda: self.bump(-1), "secondary", size=12, padx=12, pady=4).pack(side="left")
        self.entry = tk.Entry(row, textvariable=self.var, width=4, justify="center", bg=SURFACE2, fg=TEXT,
                              insertbackground=TEXT, relief="flat", font=(FONT, 14, "bold"),
                              highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACC)
        self.entry.pack(side="left", padx=6, ipady=5)
        Btn(row, "+", lambda: self.bump(1), "secondary", size=12, padx=12, pady=4).pack(side="left")
        self.entry.bind("<FocusOut>", lambda e: self._commit())

    def peek(self):
        try:
            v = int(self.var.get())
        except ValueError:
            v = self.lo
        return max(self.lo, min(self.hi, v))

    def get(self):
        v = self.peek()
        self.var.set(str(v))
        return v

    def bump(self, d):
        self.var.set(str(self.peek() + d * self.step))
        self._commit()

    def _commit(self):
        self.get()
        if self.command:
            self.command()


class Ring(tk.Canvas):
    """Circular progress timer."""

    def __init__(self, parent, size=250, thick=12, bg=SURFACE, color=ACC):
        super().__init__(parent, width=size, height=size, bg=bg, highlightthickness=0)
        self.size = size
        p = thick / 2 + 3
        self.create_oval(p, p, size - p, size - p, outline=TRACK, width=thick)
        self.arc = self.create_arc(p, p, size - p, size - p, start=90, extent=0, style="arc",
                                   outline=color, width=thick)
        self.t = self.create_text(size / 2, size / 2 - size * 0.04, text="00:00", fill=TEXT,
                                  font=(FONT, int(size / 5.2), "bold"))
        self.sub = self.create_text(size / 2, size / 2 + size * 0.17, text="", fill=MUTED,
                                    font=(FONT, max(8, int(size / 24)), "bold"))

    def set(self, frac, text, sub="", color=ACC):
        frac = max(0.0, min(1.0, frac))
        self.itemconfigure(self.arc, extent=-359.9 * frac, outline=color)
        self.itemconfigure(self.t, text=text)
        self.itemconfigure(self.sub, text=sub)


class Bar(tk.Canvas):
    """Thin horizontal progress bar."""

    def __init__(self, parent, height=10, bg=SURFACE, color=GOOD):
        super().__init__(parent, height=height, bg=bg, highlightthickness=0)
        self.h, self.frac, self.color = height, 0.0, color
        self.bind("<Configure>", lambda e: self.redraw())

    def set(self, frac, color=None):
        self.frac = max(0.0, min(1.0, frac))
        if color:
            self.color = color
        self.redraw()

    def redraw(self):
        w = self.winfo_width()
        self.delete("all")
        self.create_rectangle(0, 0, w, self.h, fill=TRACK, outline="")
        if self.frac > 0:
            self.create_rectangle(0, 0, w * self.frac, self.h, fill=self.color, outline="")


# ----------------------------------------------------------------- pages
class SessionView(tk.Frame):
    """One page per mode ('self' or 'lecture'): timer, settings, source card, doubts."""

    def __init__(self, parent, app, mode):
        super().__init__(parent, bg=BG)
        self.app, self.mode = app, mode
        self.accent = ACC if mode == "self" else VIOLET
        self.kind = "primary" if mode == "self" else "violet"
        st = app.data["settings"][mode]
        self._last = None
        self._break_btns = False

        if mode == "self":
            page_header(self, "Self Study", "Lock your screen and study from your books. Distractions stay out.")
        else:
            page_header(self, "Lecture", "Watch a YouTube lecture on a locked screen. Pause, resume and seek freely.")

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # ---- left: timer
        lo, li = card(body, "Timer", pad=18)
        lo.grid(row=0, column=0, sticky="ns", padx=(0, 16))
        self.ring = Ring(li, size=250, thick=12, bg=SURFACE, color=self.accent)
        self.ring.pack()
        self.lbl_status = tk.Label(li, text="", bg=SURFACE, fg=MUTED, font=(FONT, 10), wraplength=250)
        self.lbl_status.pack(pady=(14, 14))
        self.btn_start = Btn(li, "Start session", lambda: app.start_session(mode), self.kind, size=11, pady=11)
        self.btn_start.pack(fill="x")
        self.btn_skip = Btn(li, "Skip break", app.skip_break, "secondary", pady=9)
        self.btn_end = Btn(li, "End session", app.stop_session, "danger", pady=9)

        # ---- right column
        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")

        so, si = card(right, "Session settings")
        so.pack(fill="x")
        row = tk.Frame(si, bg=SURFACE)
        row.pack(fill="x")
        self.s_study = Stepper(row, "FOCUS (MIN)", st["study"], 1, 240, step=5)
        self.s_study.pack(side="left", padx=(0, 30))
        self.s_break = Stepper(row, "BREAK (MIN)", st["break"], 1, 60, step=1)
        self.s_break.pack(side="left", padx=(0, 30))
        self.s_cycles = Stepper(row, "SESSIONS", st["cycles"], 1, 12, step=1)
        self.s_cycles.pack(side="left")

        if mode == "self":
            self._build_audio_card(right)
        else:
            self._build_lecture_card(right, st)

        do, di = card(right, "Doubts to clear in your break")
        do.pack(fill="both", expand=True, pady=(12, 0))
        self.lb = make_listbox(di, height=4)
        self.lb.pack(fill="both", expand=True)
        r = tk.Frame(di, bg=SURFACE)
        r.pack(fill="x", pady=(10, 0))
        self.v_doubt = tk.StringVar()
        e = make_entry(r, self.v_doubt, accent=self.accent)
        e.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 8))
        e.bind("<Return>", lambda ev: app.add_doubt(self.v_doubt))
        Btn(r, "Add", lambda: app.add_doubt(self.v_doubt), "secondary").pack(side="left", padx=(0, 6))
        Btn(r, "Mark resolved", lambda: app.resolve_doubt(self.lb), "secondary").pack(side="left")

    # ---- source cards
    def _build_audio_card(self, right):
        ao, ai = card(right, "Study audio (optional)")
        ao.pack(fill="x", pady=(12, 0))
        row = tk.Frame(ai, bg=SURFACE)
        row.pack(fill="x")
        self.audio_name = tk.StringVar(value="No file selected")
        tk.Label(row, textvariable=self.audio_name, bg=SURFACE, fg=TEXT, font=(FONT, 10),
                 anchor="w").pack(side="left", fill="x", expand=True)
        Btn(row, "Choose file", self.app.pick_audio, "secondary").pack(side="left", padx=6)
        self.btn_play = Btn(row, "Play", self.app.toggle_audio, "secondary")
        self.btn_play.pack(side="left")
        vr = tk.Frame(ai, bg=SURFACE)
        vr.pack(fill="x", pady=(10, 0))
        tk.Label(vr, text="Volume", bg=SURFACE, fg=MUTED, font=(FONT, 9, "bold")).pack(side="left", padx=(0, 10))
        self.vol = tk.Scale(vr, from_=0, to=100, orient="horizontal", showvalue=False, bg=SURFACE,
                            troughcolor=SURFACE2, highlightthickness=0, bd=0, sliderrelief="flat",
                            width=12, sliderlength=22, activebackground=ACC)
        self.vol.set(self.app.data["settings"]["self"]["volume"])
        self.vol.config(command=self.app.on_volume)
        self.vol.pack(side="left", fill="x", expand=True)
        tk.Label(ai, text="Plays automatically during focus sessions and pauses in breaks. "
                          "Audio plays only inside this app.",
                 bg=SURFACE, fg=MUTED, font=(FONT, 9), anchor="w", justify="left",
                 wraplength=520).pack(anchor="w", pady=(8, 0))

    def _build_lecture_card(self, right, st):
        lo, li = card(right, "YouTube lecture")
        lo.pack(fill="x", pady=(12, 0))
        row = tk.Frame(li, bg=SURFACE)
        row.pack(fill="x")
        self.v_url = tk.StringVar(value=st["url"])
        e = make_entry(row, self.v_url, accent=VIOLET)
        e.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 8))
        e.bind("<FocusOut>", lambda ev: self.update_lec_label())
        e.bind("<KeyRelease>", lambda ev: self.update_lec_label())
        Btn(row, "Paste", self.paste_url, "secondary").pack(side="left", padx=(0, 6))
        Btn(row, "Reset position", self.reset_pos, "secondary").pack(side="left")
        self.lbl_lec = tk.Label(li, text="", bg=SURFACE, fg=MUTED, font=(FONT, 9), anchor="w")
        self.lbl_lec.pack(anchor="w", pady=(8, 0))
        tk.Label(li, text="You can pause, resume, seek and change speed during the lecture. "
                          "The focus timer keeps running while paused. Playback resumes where you left off.",
                 bg=SURFACE, fg=MUTED, font=(FONT, 9), anchor="w", justify="left",
                 wraplength=520).pack(anchor="w", pady=(4, 0))

    def paste_url(self):
        try:
            self.v_url.set(self.app.root.clipboard_get().strip())
        except Exception:
            pass
        self.update_lec_label()

    def reset_pos(self):
        vid, _ = parse_youtube(self.v_url.get())
        if vid:
            self.app.data["settings"]["lecture"]["pos"].pop(vid, None)
            save_data(self.app.data)
        self.update_lec_label()

    def update_lec_label(self):
        if self.mode != "lecture":
            return
        url = self.v_url.get().strip()
        vid, t0 = parse_youtube(url)
        if not url:
            self.lbl_lec.config(text="Paste the link of a single YouTube video.", fg=MUTED)
        elif not vid:
            self.lbl_lec.config(text="This does not look like a single YouTube video link.", fg=WARN)
        else:
            saved = self.app.data["settings"]["lecture"]["pos"].get(vid)
            pos = saved if saved is not None else t0
            self.lbl_lec.config(text=f"Link OK  \u00b7  starts at {vt(pos)}", fg=GOOD)

    # ---- rendering (called every tick)
    def render(self):
        app = self.app
        mine = app.phase != "idle" and app.mode == self.mode
        if mine and app.phase == "study":
            rem = max(0.0, app.phase_end - time.time())
            frac = 1 - rem / max(1, app.phase_total)
            state = (ms(rem), "FOCUS", app.cycle, "study")
            vals = (frac, ms(rem), "FOCUS", self.accent,
                    f"Focus \u00b7 session {app.cycle} of {app.cycles}")
        elif mine and app.phase == "break":
            rem = max(0.0, app.phase_end - time.time())
            frac = 1 - rem / max(1, app.phase_total)
            state = (ms(rem), "BREAK", app.cycle, "break")
            vals = (frac, ms(rem), "BREAK", GOOD,
                    f"Break \u00b7 clear your doubts ({app.cycle} of {app.cycles} done)")
        else:
            mins = self.s_study.peek()
            state = (mins, "READY", 0, "idle", app.phase != "idle", app.end_msg if not mine else "")
            msg = app.end_msg if (app.end_msg and app.phase == "idle" and app.mode == self.mode) \
                else ("Another session is running." if app.phase != "idle" else "Ready when you are.")
            vals = (0.0, f"{mins:02d}:00", "READY", self.accent, msg)
        if state == self._last:
            return
        self._last = state
        frac, text, sub, color, status = vals
        self.ring.set(frac, text, sub, color)
        self.lbl_status.config(text=status, fg=GOOD if (mine and app.phase == "break") else MUTED)
        idle = app.phase == "idle"
        self.btn_start.enable(idle)
        self.btn_start.set_text("Start session" if idle else "Session in progress")
        show_break = mine and app.phase == "break"
        if show_break and not self._break_btns:
            self.btn_skip.pack(fill="x", pady=(8, 0))
            self.btn_end.pack(fill="x", pady=(8, 0))
            self._break_btns = True
        elif not show_break and self._break_btns:
            self.btn_skip.pack_forget()
            self.btn_end.pack_forget()
            self._break_btns = False


class StopwatchView(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, bg=BG)
        self.app = app
        self.running = False
        self.acc = 0.0
        self.t0 = 0.0
        self.laps = []
        page_header(self, "Stopwatch", "A free-running stopwatch. Add the time to your study log when you finish.")
        o, i = card(self, pad=24)
        o.pack(fill="x")
        self.lbl = tk.Label(i, text="00:00:00.0", bg=SURFACE, fg=TEXT, font=(FONT, 66, "bold"))
        self.lbl.pack(pady=(6, 16))
        row = tk.Frame(i, bg=SURFACE)
        row.pack()
        self.btn = Btn(row, "Start", self.toggle, "primary", size=11, padx=28, pady=10)
        self.btn.pack(side="left", padx=5)
        Btn(row, "Lap", self.lap, "secondary", size=11, padx=24, pady=10).pack(side="left", padx=5)
        Btn(row, "Reset", self.reset, "secondary", size=11, padx=24, pady=10).pack(side="left", padx=5)
        Btn(row, "Add to study log", self.save, "success", size=11, padx=24, pady=10).pack(side="left", padx=5)
        lo, li = card(self, "Laps")
        lo.pack(fill="both", expand=True, pady=(14, 0))
        self.lb = make_listbox(li, height=6)
        self.lb.pack(fill="both", expand=True)

    @staticmethod
    def fmt(e):
        return f"{hms(e)}.{int(e * 10) % 10}"

    def elapsed(self):
        return self.acc + (time.monotonic() - self.t0 if self.running else 0.0)

    def refresh(self):
        self.lbl.config(text=self.fmt(self.elapsed()))

    def toggle(self):
        if self.running:
            self.acc = self.elapsed()
            self.running = False
            self.btn.set_text("Start")
        else:
            self.t0 = time.monotonic()
            self.running = True
            self.btn.set_text("Pause")

    def lap(self):
        e = self.elapsed()
        if e <= 0:
            return
        self.laps.append(e)
        prev = self.laps[-2] if len(self.laps) > 1 else 0
        self.lb.insert(0, f"Lap {len(self.laps):<3} {self.fmt(e)}     +{self.fmt(e - prev)}")

    def reset(self):
        self.running = False
        self.acc = 0.0
        self.laps.clear()
        self.lb.delete(0, "end")
        self.btn.set_text("Start")
        self.refresh()

    def save(self):
        e = self.elapsed()
        if e < 1:
            return
        self.app.add_study(e, mode="self")
        save_data(self.app.data)
        self.reset()
        self.app.refresh_progress()
        messagebox.showinfo(APP_NAME, f"{hm(e)} added to today's study log.")


class ProgressView(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, bg=BG)
        self.app = app
        page_header(self, "Progress", "Your study hours, streaks and daily goal.")

        grid = tk.Frame(self, bg=BG)
        grid.pack(fill="x")
        self.stat = {}
        names = (("today", "Today"), ("week", "Last 7 days"), ("total", "All time"),
                 ("streak", "Current streak"), ("best", "Best streak"))
        for i, (k, name) in enumerate(names):
            grid.columnconfigure(i, weight=1, uniform="stat")
            o, inn = card(grid, pad=14)
            o.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0))
            self.stat[k] = tk.Label(inn, text="-", bg=SURFACE, fg=TEXT, font=(FONT, 19, "bold"))
            self.stat[k].pack(anchor="w")
            tk.Label(inn, text=name.upper(), bg=SURFACE, fg=MUTED, font=(FONT, 8, "bold")).pack(anchor="w", pady=(4, 0))

        go, gi = card(self, "Daily goal")
        go.pack(fill="x", pady=(12, 0))
        row = tk.Frame(gi, bg=SURFACE)
        row.pack(fill="x")
        self.goal = Stepper(row, "GOAL (HOURS)", app.data["settings"]["goal"], 1, 16, command=self.on_goal)
        self.goal.pack(side="left", padx=(0, 28))
        col = tk.Frame(row, bg=SURFACE)
        col.pack(side="left", fill="x", expand=True)
        self.lbl_goal = tk.Label(col, text="", bg=SURFACE, fg=TEXT, font=(FONT, 11, "bold"))
        self.lbl_goal.pack(anchor="w")
        self.lbl_split = tk.Label(col, text="", bg=SURFACE, fg=MUTED, font=(FONT, 9))
        self.lbl_split.pack(anchor="w", pady=(2, 8))
        self.bar = Bar(col, height=10)
        self.bar.pack(fill="x")

        co, ci = card(self, "Last 14 days")
        co.pack(fill="both", expand=True, pady=(12, 0))
        leg = tk.Frame(ci, bg=SURFACE)
        leg.place(relx=1.0, rely=0.0, anchor="ne")
        tk.Label(leg, text="\u25a0 Self study", bg=SURFACE, fg=ACC, font=(FONT, 9, "bold")).pack(side="left", padx=6)
        tk.Label(leg, text="\u25a0 Lecture", bg=SURFACE, fg=VIOLET, font=(FONT, 9, "bold")).pack(side="left")
        self.cv = tk.Canvas(ci, bg=SURFACE, highlightthickness=0, height=200)
        self.cv.pack(fill="both", expand=True)
        self.cv.bind("<Configure>", lambda e: self.draw_chart())

    def on_goal(self):
        self.app.data["settings"]["goal"] = self.goal.get()
        save_data(self.app.data)
        self.refresh()

    def refresh(self):
        app = self.app
        today = dt.date.today()
        t = app.secs(today)
        lec = min(app.secs_lec(today), t)
        wk = sum(app.secs(today - dt.timedelta(days=i)) for i in range(7))
        total = sum(app.data["days"].values())
        cur, best = app.streaks()
        self.stat["today"].config(text=hm(t))
        self.stat["week"].config(text=hm(wk))
        self.stat["total"].config(text=hm(total))
        self.stat["streak"].config(text=f"{cur} day{'s' if cur != 1 else ''}")
        self.stat["best"].config(text=f"{best} day{'s' if best != 1 else ''}")
        goal = self.goal.peek() * 3600
        done = t >= goal
        self.bar.set(t / goal, GOOD if done else ACC)
        self.lbl_goal.config(text=f"{hm(t)} of {hm(goal)}" + ("  \u00b7  goal reached" if done else ""))
        self.lbl_split.config(text=f"Self study {hm(t - lec)}   \u00b7   Lecture {hm(lec)}")
        self.draw_chart()

    def draw_chart(self):
        cv, app = self.cv, self.app
        cv.delete("all")
        w, h = cv.winfo_width(), cv.winfo_height()
        if w < 80 or h < 80:
            return
        today = dt.date.today()
        days = [today - dt.timedelta(days=13 - i) for i in range(14)]
        tot = [app.secs(d) / 3600 for d in days]
        lec = [min(app.secs_lec(d) / 3600, t) for d, t in zip(days, tot)]
        mx = max(max(tot), 1.0)
        left, pad_b, pad_t = 34, 40, 26
        plot_h = h - pad_b - pad_t
        y_base = h - pad_b
        step = 1 if mx <= 6 else 2
        for g in range(step, int(mx) + 1, step):
            y = y_base - plot_h * g / mx
            cv.create_line(left, y, w, y, fill=BORDER, dash=(2, 4))
            cv.create_text(left - 8, y, text=f"{g}h", fill=MUTED, font=(FONT, 8), anchor="e")
        cv.create_line(left, y_base, w, y_base, fill=BORDER)
        bw = (w - left - 6) / 14
        for i, d in enumerate(days):
            x0 = left + i * bw + bw * 0.2
            x1 = left + (i + 1) * bw - bw * 0.2
            t, l = tot[i], lec[i]
            if t > 0:
                y_mid = y_base - plot_h * (t - l) / mx
                y_top = y_base - plot_h * t / mx
                if t - l > 0:
                    cv.create_rectangle(x0, y_mid, x1, y_base, fill=ACC, outline="")
                if l > 0:
                    cv.create_rectangle(x0, y_top, x1, y_mid, fill=VIOLET, outline="")
                cv.create_text((x0 + x1) / 2, y_top - 9, text=f"{t:.1f}", fill=TEXT, font=(FONT, 8, "bold"))
            is_today = d == today
            cv.create_text((x0 + x1) / 2, h - 22, text=d.strftime("%a"), fill=TEXT if is_today else MUTED,
                           font=(FONT, 8, "bold" if is_today else "normal"))
            cv.create_text((x0 + x1) / 2, h - 9, text=str(d.day), fill=TEXT if is_today else MUTED,
                           font=(FONT, 8, "bold" if is_today else "normal"))


# ----------------------------------------------------------------- application
class App:
    def __init__(self, root):
        self.root = root
        self.data = load_data()
        self.audio = Audio()
        self.blocker = KeyBlocker()

        self.phase = "idle"       # idle | study | break
        self.mode = "self"        # self | lecture
        self.cycle = 0
        self.cycles = 1
        self.study_min = 50
        self.break_min = 10
        self.phase_total = 1
        self.phase_end = 0.0
        self.end_msg = ""
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

        root.title(APP_NAME)
        root.geometry("1040x720")
        root.minsize(960, 660)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.build_ui()

        s = self.data["settings"]["self"]
        self.audio.volume = s["volume"]
        if s["audio"] and self.audio.load(s["audio"]):
            self.views["self"].audio_name.set(os.path.basename(s["audio"]))

        self.refresh_doubts()
        self.refresh_progress()
        self.views["lecture"].update_lec_label()
        dark_titlebar(root)
        self.show_page("self")
        self.tick()

    # ---------- layout
    def build_ui(self):
        side = tk.Frame(self.root, bg=SIDEBAR, width=224)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        logo = tk.Frame(side, bg=SIDEBAR)
        logo.pack(fill="x", padx=22, pady=(28, 26))
        tk.Label(logo, text=APP_NAME, bg=SIDEBAR, fg=TEXT, font=(FONT, 18, "bold")).pack(anchor="w")
        tk.Label(logo, text="Deep focus sessions", bg=SIDEBAR, fg=MUTED, font=(FONT, 9)).pack(anchor="w", pady=(2, 0))

        self.nav = {}
        for key, label in (("self", "Self Study"), ("lecture", "Lecture"),
                           ("stopwatch", "Stopwatch"), ("progress", "Progress")):
            self._nav_item(side, key, label)

        box = tk.Frame(side, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER)
        box.pack(side="bottom", fill="x", padx=14, pady=16)
        inner = tk.Frame(box, bg=SURFACE, padx=14, pady=12)
        inner.pack(fill="x")
        tk.Label(inner, text="TODAY", bg=SURFACE, fg=MUTED, font=(FONT, 8, "bold")).pack(anchor="w")
        self.side_today = tk.Label(inner, text="0h 00m", bg=SURFACE, fg=TEXT, font=(FONT, 15, "bold"))
        self.side_today.pack(anchor="w")
        tk.Label(inner, text="STREAK", bg=SURFACE, fg=MUTED, font=(FONT, 8, "bold")).pack(anchor="w", pady=(8, 0))
        self.side_streak = tk.Label(inner, text="0 days", bg=SURFACE, fg=GOOD, font=(FONT, 12, "bold"))
        self.side_streak.pack(anchor="w")

        self.content = tk.Frame(self.root, bg=BG)
        self.content.pack(side="left", fill="both", expand=True, padx=30, pady=26)
        self.views = {"self": SessionView(self.content, self, "self"),
                      "lecture": SessionView(self.content, self, "lecture")}
        self.sw_view = StopwatchView(self.content, self)
        self.prog_view = ProgressView(self.content, self)
        self.pages = {"self": self.views["self"], "lecture": self.views["lecture"],
                      "stopwatch": self.sw_view, "progress": self.prog_view}
        self.current_page = None

    def _nav_item(self, parent, key, label):
        row = tk.Frame(parent, bg=SIDEBAR, cursor="hand2")
        row.pack(fill="x", pady=1)
        bar = tk.Frame(row, width=3, bg=SIDEBAR)
        bar.pack(side="left", fill="y")
        lab = tk.Label(row, text=label, bg=SIDEBAR, fg=MUTED, font=(FONT, 11, "bold"),
                       anchor="w", padx=19, pady=11)
        lab.pack(side="left", fill="x", expand=True)
        self.nav[key] = (row, bar, lab)
        for wdg in (row, bar, lab):
            wdg.bind("<Button-1>", lambda e, k=key: self.show_page(k))
            wdg.bind("<Enter>", lambda e, k=key: self._nav_hover(k, True))
            wdg.bind("<Leave>", lambda e, k=key: self._nav_hover(k, False))

    def _nav_hover(self, key, on):
        if key == self.current_page:
            return
        row, bar, lab = self.nav[key]
        c = "#0d1117" if on else SIDEBAR
        for wdg in (row, bar, lab):
            wdg.config(bg=c)

    def show_page(self, name):
        for k, page in self.pages.items():
            page.pack_forget()
        self.pages[name].pack(fill="both", expand=True)
        self.current_page = name
        for k, (row, bar, lab) in self.nav.items():
            active = k == name
            row.config(bg=SURFACE if active else SIDEBAR)
            lab.config(bg=SURFACE if active else SIDEBAR, fg=TEXT if active else MUTED)
            bar.config(bg=(ACC if k != "lecture" else VIOLET) if active else SIDEBAR)
        if name == "progress":
            self.prog_view.refresh()

    # ---------- data / progress
    def secs(self, d):
        return self.data["days"].get(d.isoformat(), 0)

    def secs_lec(self, d):
        return self.data["days_lecture"].get(d.isoformat(), 0)

    def add_study(self, sec, mode=None):
        mode = mode or self.mode
        k = today_key()
        self.data["days"][k] = self.data["days"].get(k, 0) + sec
        if mode == "lecture":
            self.data["days_lecture"][k] = self.data["days_lecture"].get(k, 0) + sec

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
        cur, _ = self.streaks()
        self.side_today.config(text=hm(self.secs(dt.date.today())))
        self.side_streak.config(text=f"{cur} day{'s' if cur != 1 else ''}")
        if self.current_page == "progress":
            self.prog_view.refresh()

    # ---------- audio
    def pick_audio(self):
        path = filedialog.askopenfilename(
            title="Choose study audio",
            filetypes=[("Audio files", "*.mp3 *.wav *.wma *.m4a"), ("All files", "*.*")])
        if not path:
            return
        if self.audio.load(path):
            self.views["self"].audio_name.set(os.path.basename(path))
            self.data["settings"]["self"]["audio"] = path
            save_data(self.data)
        else:
            messagebox.showerror(APP_NAME, "This file could not be loaded. Please try an MP3 or WAV file.")

    def toggle_audio(self):
        if not self.audio.loaded:
            if self.lock:
                return
            self.pick_audio()
            if not self.audio.loaded:
                return
        if self.audio.playing:
            self.audio.pause()
        else:
            self.audio.start()
        self.sync_audio_buttons()

    def on_volume(self, v):
        self.audio.set_volume(float(v))
        self.data["settings"]["self"]["volume"] = int(float(v))

    def sync_audio_buttons(self):
        txt = "Pause" if self.audio.playing else "Play"
        self.views["self"].btn_play.set_text(txt)
        if self.lock and hasattr(self, "l_play"):
            self.l_play.set_text(txt)

    # ---------- doubts
    def add_doubt(self, var):
        t = var.get().strip()
        if not t:
            return
        self.data["doubts"].append({"t": t, "at": dt.datetime.now().strftime("%H:%M")})
        var.set("")
        save_data(self.data)
        self.refresh_doubts()

    def resolve_doubt(self, listbox):
        sel = listbox.curselection()
        if not sel:
            messagebox.showinfo(APP_NAME, "Select a doubt in the list first.")
            return
        del self.data["doubts"][sel[0]]
        save_data(self.data)
        self.refresh_doubts()

    def refresh_doubts(self):
        for v in self.views.values():
            v.lb.delete(0, "end")
            for d in self.data["doubts"]:
                v.lb.insert("end", f"  {d['at']}    {d['t']}")
        if self.lock and hasattr(self, "l_lb"):
            self.l_lb.delete(0, "end")
            for d in self.data["doubts"][-5:]:
                self.l_lb.insert("end", f"  {d['at']}    {d['t']}")
            self.l_count.config(text=f"{len(self.data['doubts'])} doubt(s) saved")

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

    # ---------- session control
    def start_session(self, mode):
        if self.phase != "idle":
            return
        v = self.views[mode]
        study, brk, cyc = v.s_study.get(), v.s_break.get(), v.s_cycles.get()
        self.lec_active = False
        extra = ""
        if mode == "lecture":
            vid, t0 = parse_youtube(v.v_url.get())
            if not vid:
                messagebox.showerror(APP_NAME, "Please paste a valid link to a single YouTube video.")
                return
            saved = self.data["settings"]["lecture"]["pos"].get(vid)
            self.lec_vid = vid
            self.lec_start = float(saved if saved is not None else t0)
            self.lecture_pos = self.lec_start
            self.lec_active = True
            extra = "A YouTube lecture will play full screen. You can pause, resume and seek.\n\n"
        if not messagebox.askyesno(
                "Start focus session",
                f"Your screen will be locked for {study} minutes (strict mode: no exit before the timer ends).\n"
                f"Then a {brk} minute break. {cyc} session(s) in total.\n\n{extra}"
                "Emergency exit: Ctrl+Alt+Del or Ctrl+Shift+Esc, open Task Manager and end StudyLock.\n\n"
                "Start now?"):
            self.lec_active = False
            return
        s = self.data["settings"][mode]
        s.update(study=study, cycles=cyc, **{"break": brk})
        if mode == "lecture":
            s["url"] = v.v_url.get().strip()
        save_data(self.data)
        self.mode = mode
        self.study_min, self.break_min = study, brk
        self.cycles, self.cycle = cyc, 1
        self.end_msg = ""
        self.start_study()

    def start_study(self):
        self.phase = "study"
        self.phase_total = self.study_min * 60
        self.phase_end = time.time() + self.phase_total
        self.open_lock()
        if self.lec_active:
            self.audio.pause()
            self.sync_audio_buttons()
            self.player_fail = 0
            self.launch_player()
        elif self.audio.loaded and not self.audio.playing:
            self.audio.start()
            self.sync_audio_buttons()
        self.root.withdraw()
        self.beep()

    def finish_study(self):
        self.stop_player()
        self.close_lock()
        self.audio.pause()
        self.sync_audio_buttons()
        save_data(self.data)
        self.phase = "break"
        self.phase_total = self.break_min * 60
        self.phase_end = time.time() + self.phase_total
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(1500, lambda: self.root.attributes("-topmost", False))
        self.show_page(self.mode)
        self.views["lecture"].update_lec_label()
        self.beep()
        self.refresh_progress()

    def finish_break(self):
        if self.cycle < self.cycles:
            self.cycle += 1
            self.start_study()
        else:
            self.end_all("All sessions complete. Great work!")

    def skip_break(self):
        if self.phase == "break":
            self.finish_break()

    def stop_session(self):
        if self.phase == "break" and messagebox.askyesno(APP_NAME, "End the session now?"):
            self.end_all("Session ended.")

    def end_all(self, msg):
        self.phase = "idle"
        self.lec_active = False
        self.end_msg = msg
        self.beep()
        save_data(self.data)
        self.refresh_progress()

    def beep(self):
        try:
            import winsound
            winsound.MessageBeep()
        except Exception:
            self.root.bell()

    # ---------- lecture player process
    def lecture_state(self):          # called from the server thread
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
            self.data["settings"]["lecture"]["pos"][self.lec_vid] = max(0, self.lecture_pos - 3)
            save_data(self.data)

    def retry_player(self):
        self.player_fail = 0
        self.player_started = 0
        if self.lock and hasattr(self, "l_msg"):
            self.l_msg.config(text="Opening the lecture player\u2026 (2\u20135 seconds)")

    def lecture_tick(self, now):
        """True while the player window owns the screen (the lock screen stays behind it)."""
        if self.player is not None and self.player.poll() is not None:
            ran = now - self.player_started
            self.player = None
            self.player_fail = self.player_fail + 1 if ran < 10 else 0
        if self.player is None:
            if self.player_fail < 3:
                if now - self.player_started > 2:
                    self.launch_player()
            elif self.lock and hasattr(self, "l_msg"):
                self.l_msg.config(text="The lecture player could not start (Edge WebView2 missing?). "
                                       f"Press Retry. Log: {PLAYER_LOG}")
        return self.player is not None

    # ---------- lock screen
    def open_lock(self):
        w = tk.Toplevel(self.root)
        self.lock = w
        accent = ACC if self.mode == "self" else VIOLET
        w.configure(bg=LOCK_BG, cursor="arrow")
        w.attributes("-fullscreen", True)
        w.attributes("-topmost", True)
        w.protocol("WM_DELETE_WINDOW", lambda: None)
        for seq in ("<Alt-F4>", "<Escape>"):
            w.bind(seq, lambda e: "break")
        self.blocker.start()

        box = tk.Frame(w, bg=LOCK_BG)
        box.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(box, text="SELF STUDY" if self.mode == "self" else "LECTURE", bg=LOCK_BG, fg=accent,
                 font=(FONT, 13, "bold")).pack(pady=(0, 10))
        self.l_ring = Ring(box, size=300, thick=14, bg=LOCK_BG, color=accent)
        self.l_ring.pack()
        msg = ("Stay focused. Write doubts below and clear them in your break."
               if self.mode == "self" else "Opening the lecture player\u2026 (2\u20135 seconds)")
        self.l_msg = tk.Label(box, text=msg, bg=LOCK_BG, fg=MUTED, font=(FONT, 13), wraplength=620)
        self.l_msg.pack(pady=(14, 18))

        row = tk.Frame(box, bg=LOCK_BG)
        row.pack()
        self.lv_doubt = tk.StringVar()
        self.l_entry = tk.Entry(row, textvariable=self.lv_doubt, width=46, bg=SURFACE, fg=TEXT,
                                insertbackground=TEXT, relief="flat", font=(FONT, 13),
                                highlightthickness=1, highlightbackground=BORDER, highlightcolor=accent)
        self.l_entry.pack(side="left", ipady=8, padx=(0, 8))
        self.l_entry.bind("<Return>", lambda e: self.add_doubt(self.lv_doubt))
        Btn(row, "Save doubt", lambda: self.add_doubt(self.lv_doubt),
            "primary" if self.mode == "self" else "violet", size=11, pady=9).pack(side="left")
        self.l_count = tk.Label(box, bg=LOCK_BG, fg=MUTED, font=(FONT, 10))
        self.l_count.pack(pady=(10, 4))
        self.l_lb = make_listbox(box, height=4)
        self.l_lb.config(width=66, bg=SURFACE)
        self.l_lb.pack()

        if self.mode == "self":
            arow = tk.Frame(box, bg=LOCK_BG)
            arow.pack(pady=(20, 0))
            self.l_play = Btn(arow, "Play", self.toggle_audio, "secondary")
            self.l_play.pack(side="left")
            name = os.path.basename(self.audio.path) if self.audio.loaded else "No audio selected"
            tk.Label(arow, text=name, bg=LOCK_BG, fg=MUTED, font=(FONT, 10)).pack(side="left", padx=14)
            self.l_vol = tk.Scale(arow, from_=0, to=100, orient="horizontal", length=170, showvalue=False,
                                  bg=LOCK_BG, troughcolor=SURFACE2, highlightthickness=0, bd=0,
                                  sliderrelief="flat", width=12, sliderlength=20, activebackground=ACC)
            self.l_vol.set(self.audio.volume)
            self.l_vol.config(command=self.on_volume)
            self.l_vol.pack(side="left")
        else:
            Btn(box, "Retry lecture player", self.retry_player, "secondary").pack(pady=(18, 0))

        self.refresh_doubts()
        self.sync_audio_buttons()
        w.update_idletasks()
        w.focus_force()
        self.l_entry.focus_set()

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
        if d > 2:          # laptop slept / app hung: do not count that time as study
            d = 0
        self.drain_doubts()

        if self.phase == "study":
            self.add_study(d)
            rem = self.phase_end - now
            owns = self.lecture_tick(now) if self.lec_active else False
            if self.lock:
                frac = 1 - rem / max(1, self.phase_total)
                self.l_ring.set(frac, ms(rem), f"SESSION {self.cycle} OF {self.cycles}",
                                ACC if self.mode == "self" else VIOLET)
                if not owns:   # when the lecture player is on screen, do not cover it
                    self.lock.lift()
                    self.lock.attributes("-topmost", True)
                    if self.lock.focus_displayof() is None:
                        self.lock.focus_force()
                        self.l_entry.focus_set()
            if rem <= 0:
                self.finish_study()
            elif now - self.last_save > 20:
                save_data(self.data)
                self.last_save = now
                self.refresh_progress()
        elif self.phase == "break":
            if self.phase_end - now <= 0:
                self.finish_break()

        for v in self.views.values():
            v.render()
        self.sw_view.refresh()
        self.audio.keep_looping()
        self.root.after(200, self.tick)

    def on_close(self):
        if self.phase == "study":
            return
        if self.phase == "break" and not messagebox.askyesno(APP_NAME, "A break is running. Quit anyway?"):
            return
        self.stop_player()
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
    if "--player" in sys.argv:
        run_player(sys.argv[sys.argv.index("--player") + 1])
        return
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
