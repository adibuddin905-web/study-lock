"""
StudyLock - focus sessions for Windows
--------------------------------------
Two separate modes:
  * Self Study : full-screen lock, PDF reader (scroll, highlight, draw, saved into the original file),
                 study audio, doubts
  * Lecture    : full-screen lock with a YouTube lecture (pause / resume / seek) and an optional PDF
                 beside it (split screen, adjustable)
Prayer break pauses everything (timer, video, audio); the screen stays locked.
Video speed 0.5x-3x with configurable shortcuts. The sidebar 'Update' button updates the exe in place.
Plus stopwatch, daily hours, streaks and a 14-day chart.

Needs Python 3.9+ (Windows). The study room uses 'pywebview' (Edge WebView2) and 'pymupdf'
(PDF engine); the GitHub build installs both for you.
Run:  pythonw study_lock.py
"""
import sys, os, json, time, ctypes, re, queue, secrets, threading, subprocess, collections
import http.server
import urllib.parse
import urllib.request
from collections import OrderedDict
import datetime as dt
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import font as tkfont

IS_WIN = sys.platform.startswith("win")
APP_NAME = "StudyLock"
DATA_FILE = os.path.join(os.path.expanduser("~"), ".study_lock_data.json")
PLAYER_LOG = os.path.join(os.path.expanduser("~"), ".study_lock_player.log")
GITHUB_REPO = "adibuddin905-web/study-lock"
UPDATE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/latest"
MIN_EXE_BYTES = 5_000_000
try:
    from build_id import BUILD_ID          # written by the GitHub build
except Exception:
    BUILD_ID = "dev"
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
        "keys": {},
        "self": {"study": 50, "break": 10, "cycles": 4, "audio": "", "volume": 70,
                 "pdfs": [], "pdf_state": {}, "pdf_night": True},
        "lecture": {"study": 50, "break": 10, "cycles": 2, "url": "", "pos": {},
                    "pdfs": [], "split": 55, "swap": False, "speed": 1.0},
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
    if isinstance(old.get("keys"), dict):
        new["keys"] = old["keys"]
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


ROOM_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>StudyLock Study Room</title>
<style>
:root{--bg:#0d1117;--sur:#161b22;--sur2:#21262d;--line:#30363d;--text:#e6edf3;--mut:#8b949e;--acc:#4f8cff;--vio:#a371f7;--good:#3fb950;--warn:#d29922;--bad:#f85149;--accent:var(--acc)}
body.lec{--accent:var(--vio)}
*{box-sizing:border-box}
html,body{height:100%;margin:0;background:var(--bg);color:var(--text);font-family:"Segoe UI",system-ui,sans-serif;overflow:hidden;user-select:none;-webkit-user-select:none}
#app{display:flex;flex-direction:column;height:100%}
button,select,input,textarea{font-family:inherit}
::-webkit-scrollbar{width:12px;height:12px}::-webkit-scrollbar-thumb{background:#3a414b;border-radius:8px;border:3px solid transparent;background-clip:content-box}::-webkit-scrollbar-track{background:transparent}
/* ---- top bar ---- */
#bar{flex:0 0 54px;display:flex;align-items:center;gap:10px;padding:0 12px;background:var(--sur);border-bottom:1px solid var(--line);position:relative;z-index:50}
#prog{position:absolute;left:0;bottom:-1px;height:3px;width:0;background:var(--accent)}
.brand{font-weight:700;font-size:15px}
.chip{font-size:11px;font-weight:700;color:var(--accent);background:rgba(140,140,255,.13);padding:5px 10px;border-radius:999px;white-space:nowrap}
#timer{font-size:24px;font-weight:700;font-variant-numeric:tabular-nums;min-width:74px}
.sp{flex:1}
.seg{display:flex;background:var(--sur2);border-radius:10px;padding:3px;gap:2px}
.seg button{border:0;background:transparent;color:var(--mut);font-size:12px;font-weight:700;padding:6px 11px;border-radius:8px;cursor:pointer}
.seg button.on{background:var(--accent);color:#fff}
.b{border:0;border-radius:9px;background:var(--sur2);color:var(--text);font-size:13px;font-weight:600;padding:8px 13px;cursor:pointer;white-space:nowrap}
.b:hover{filter:brightness(1.25)}
.b.acc{background:var(--accent);color:#fff}
.b.warn{background:#3b2a08;color:var(--warn)}
.b.good{background:#10361c;color:var(--good)}
#speedbox{display:none;align-items:center;gap:4px;background:var(--sur2);border-radius:10px;padding:3px}
#speedbox button{border:0;background:transparent;color:var(--text);font-size:14px;font-weight:700;width:28px;height:28px;border-radius:7px;cursor:pointer}
#speedbox button:hover{background:#30363d}
#speedbox #srs{width:auto;padding:0 8px;font-size:12px;color:var(--mut)}
#spv{min-width:46px;text-align:center;font-size:13px;font-weight:700;font-variant-numeric:tabular-nums}
/* ---- panes ---- */
#main{flex:1;display:flex;min-height:0;position:relative}
.pane{position:relative;min-width:0;min-height:0;display:flex;flex-direction:column;overflow:hidden}
.vsplit{flex:0 0 8px;cursor:col-resize;background:var(--line);position:relative}
.vsplit::after{content:"";position:absolute;left:2px;top:50%;width:4px;height:36px;margin-top:-18px;border-radius:3px;background:#5a6270}
.vsplit:hover,.vsplit.drag{background:var(--accent)}
#dragcover{display:none;position:fixed;left:0;top:0;right:0;bottom:0;z-index:200;cursor:col-resize}
/* ---- video ---- */
#pv{background:#000}
#yt{position:absolute;top:0;left:0;width:100%;height:100%;border:0}
#shield{position:absolute;top:0;left:0;right:0;bottom:0;z-index:5;cursor:pointer}
.hud{position:absolute;left:0;right:0;bottom:0;z-index:20;padding:26px 16px 10px;background:linear-gradient(rgba(0,0,0,0),rgba(0,0,0,.62));transition:opacity .25s}
.hud.hide{opacity:0;pointer-events:none}
#seekrow{display:flex;align-items:center;gap:10px;font-size:12px;font-variant-numeric:tabular-nums}
#bar2{flex:1;height:20px;display:flex;align-items:center;cursor:pointer}
#track{width:100%;height:5px;background:rgba(255,255,255,.28);border-radius:4px;overflow:hidden}
#fill{height:100%;width:0;background:var(--accent)}
#vrow{display:flex;gap:6px;align-items:center;justify-content:center;padding-top:6px;flex-wrap:wrap}
#vrow .b{background:rgba(48,54,61,.85);padding:7px 12px;font-size:12px}
#vrow .b.acc{background:var(--accent)}
.msg{display:none;position:absolute;z-index:40;top:50%;left:50%;transform:translate(-50%,-50%);background:rgba(22,27,34,.97);border:1px solid var(--line);padding:22px 28px;border-radius:14px;max-width:80%;line-height:1.6;font-size:16px;text-align:center}
/* ---- pdf group ---- */
#pdfgroup{display:none;flex-direction:column;min-width:0;min-height:0;background:var(--bg)}
#ptools{flex:0 0 auto;background:var(--sur);border-bottom:1px solid var(--line);padding:8px 10px;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.tool{border:0;background:var(--sur2);color:var(--text);font-size:12px;font-weight:700;padding:7px 11px;border-radius:8px;cursor:pointer;white-space:nowrap}
.tool:hover{filter:brightness(1.25)}
.tool.on{background:var(--accent);color:#fff}
.vsep{width:1px;height:22px;background:var(--line);margin:0 4px}
#sw{display:flex;gap:6px;align-items:center}
.dot{width:20px;height:20px;border-radius:50%;border:2px solid transparent;cursor:pointer;padding:0}
.dot.on{border-color:#fff}
#thick{width:90px;accent-color:var(--accent)}
#thv{font-size:12px;color:var(--mut);min-width:34px}
#q{width:130px;background:var(--sur2);color:var(--text);border:1px solid var(--line);border-radius:8px;font-size:12px;padding:6px 8px;outline:none;user-select:text}
#q:focus{border-color:var(--accent)}
#qc{font-size:11px;color:var(--mut);min-width:40px}
#saved{font-size:12px;font-weight:700;color:var(--good);min-width:70px;text-align:right}
#prow{flex:1;display:flex;min-height:0;min-width:0}
.pdfpane{background:var(--bg);flex:1 1 0}
.phead{flex:0 0 auto;display:flex;align-items:center;gap:6px;padding:6px 8px;background:var(--sur);border-bottom:1px solid var(--line)}
.dsel{background:var(--sur2);color:var(--text);border:1px solid var(--line);border-radius:7px;font-size:12px;padding:5px 6px;max-width:230px;min-width:0}
.pgi{width:44px;background:var(--sur2);color:var(--text);border:1px solid var(--line);border-radius:7px;text-align:center;font-size:12px;padding:4px;outline:none;user-select:text}
.pgn,.zl{font-size:12px;color:var(--mut)}
.scroll{flex:1;overflow:auto;background:#1f2328;position:relative}
.pages{display:flex;flex-direction:column;align-items:center;gap:12px;padding:14px 0}
.page{position:relative;background:#fff;box-shadow:0 2px 14px rgba(0,0,0,.5);flex:0 0 auto}
.page img{width:100%;height:100%;display:block;pointer-events:none;-webkit-user-drag:none}
.page canvas{position:absolute;left:0;top:0;width:100%;height:100%}
.page canvas.sr{pointer-events:none}
.page canvas.ov{touch-action:none;pointer-events:none}
.pages.night .page{background:#111}
.pages.night .page img{filter:invert(1) hue-rotate(180deg)}
.pmsg{position:absolute;top:40px;left:0;right:0;text-align:center;color:var(--mut);font-size:14px;padding:0 20px;display:none}
.tbox{position:absolute;z-index:10;background:rgba(255,255,255,.88);border:1px dashed #4f8cff;color:#000;resize:none;outline:none;font-family:Helvetica,Arial,sans-serif;line-height:1.2;padding:2px;overflow:hidden;user-select:text;-webkit-user-select:text}
/* ---- drawer / toast ---- */
#drawer{display:none;position:absolute;right:12px;top:60px;width:350px;max-height:calc(100% - 80px);overflow:auto;z-index:60;background:var(--sur);border:1px solid var(--line);border-radius:14px;padding:16px;box-shadow:0 12px 40px rgba(0,0,0,.55)}
.lbl{font-size:10px;font-weight:700;letter-spacing:1px;color:var(--mut);text-transform:uppercase;margin:0 0 8px}
#inp{width:100%;background:var(--sur2);border:1px solid var(--line);border-radius:9px;color:var(--text);font-size:14px;padding:10px 12px;outline:none;user-select:text;-webkit-user-select:text}
#inp:focus{border-color:var(--accent)}
#dlist{list-style:none;margin:10px 0 0;padding:0;font-size:12px;max-height:170px;overflow:auto}
#dlist li{padding:6px 8px;background:var(--sur2);border-radius:7px;margin-bottom:5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#aud{display:none;align-items:center;gap:10px;margin:0 0 14px}
#vol{flex:1}
.krow{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:6px;font-size:12px}
.keybtn{min-width:96px;border:1px solid var(--line);background:var(--sur2);color:var(--text);border-radius:8px;padding:6px 10px;font-size:12px;font-weight:700;cursor:pointer}
.keybtn.cap{border-color:var(--accent);color:var(--accent)}
#toast{display:none;position:fixed;left:50%;bottom:26px;transform:translateX(-50%);z-index:300;background:#1c2128;border:1px solid var(--line);color:var(--text);padding:10px 16px;border-radius:10px;font-size:13px;max-width:80%;text-align:center;box-shadow:0 8px 30px rgba(0,0,0,.5)}
</style></head><body>
<div id="app">
 <div id="bar">
  <span class="brand" id="brand">Study</span><span class="chip" id="phase">Session</span>
  <b id="timer">--:--</b>
  <div class="sp"></div>
  <div id="speedbox"><button id="sdn" title="Slower">&minus;</button><span id="spv">1.0x</span><button id="sup" title="Faster">+</button><button id="srs" title="Reset speed">1x</button></div>
  <div class="seg" id="views" style="display:none"><button id="v-video" onclick="setView('video')">Video</button><button id="v-split" class="on" onclick="setView('split')">Split</button><button id="v-pdf" onclick="setView('pdf')">PDF</button></div>
  <button class="b" id="swapb" style="display:none" onclick="doSwap()">Swap sides</button>
  <button class="b" id="vpp" style="display:none" onclick="toggle()">Pause video</button>
  <button class="b" id="dbtn" onclick="toggleDrawer()">Doubts</button>
  <button class="b warn" id="pbtn" onclick="prayerToggle()">Prayer break</button>
  <div id="prog"></div>
 </div>
 <div id="main">
  <div class="pane" id="pv" style="display:none">
   <div id="yt"></div><div id="shield" onclick="toggle()"></div>
   <div class="hud" id="hud">
    <div id="seekrow"><span id="cur">0:00</span><div id="bar2"><div id="track"><div id="fill"></div></div></div><span id="dur">0:00</span></div>
    <div id="vrow"><button class="b" onclick="seek(-30)">&minus;30s</button><button class="b" onclick="seek(-10)">&minus;10s</button>
     <button class="b acc" id="pp2" onclick="toggle()">Pause</button>
     <button class="b" onclick="seek(10)">+10s</button><button class="b" onclick="seek(30)">+30s</button></div>
   </div>
   <div class="msg" id="vmsg"></div>
  </div>
  <div class="vsplit" id="split" style="display:none"></div>
  <div id="pdfgroup">
   <div id="ptools">
    <button class="tool on" id="t-hand" onclick="setTool('hand')">Select</button>
    <button class="tool" id="t-hl" onclick="setTool('hl')">Highlighter</button>
    <button class="tool" id="t-pen" onclick="setTool('pen')">Pen</button>
    <button class="tool" id="t-text" onclick="setTool('text')">Text</button>
    <button class="tool" id="t-erase" onclick="setTool('erase')">Eraser</button>
    <span id="sw"></span>
    <input type="range" id="thick" style="display:none"><span id="thv"></span>
    <span class="vsep"></span>
    <button class="tool" onclick="undo()">Undo</button>
    <button class="tool" id="nightb" onclick="toggleNight()">Night</button>
    <button class="tool" id="addpane" onclick="addPane()">+ PDF pane</button>
    <span class="vsep"></span>
    <input id="q" placeholder="Search (Ctrl+F)"><button class="tool" onclick="searchStep(-1)">&lsaquo;</button><button class="tool" onclick="searchStep(1)">&rsaquo;</button><span id="qc"></span>
    <span class="sp"></span>
    <span id="saved">Saved</span><button class="tool" onclick="savePdf()">Save</button>
   </div>
   <div id="prow"></div>
  </div>
 </div>
 <div id="drawer">
  <div id="aud"><button class="b" id="abtn" onclick="audioToggle()">Play audio</button><input id="vol" type="range" min="0" max="100" value="70" oninput="audioVol(this.value)"></div>
  <div class="lbl">Doubts</div>
  <input id="inp" placeholder="Write a doubt and press Enter" autocomplete="off">
  <ul id="dlist"></ul>
  <div id="keys" style="display:none;margin-top:14px"><div class="lbl">Video shortcuts (click, then press a key)</div><div id="klist"></div><button class="b" style="margin-top:6px" onclick="resetKeys()">Reset to defaults</button></div>
 </div>
</div>
<div id="dragcover"></div><div id="toast"></div>
<script>
var K=new URLSearchParams(location.search).get('k');
function $(id){return document.getElementById(id);}
function pad(n){return String(n).padStart(2,'0');}
function fmt(s){s=Math.max(0,Math.floor(s||0));var h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return h?(h+':'+pad(m)+':'+pad(x)):(m+':'+pad(x));}
function tm(r){r=Math.max(0,Math.ceil(r));return pad(Math.floor(r/60))+':'+pad(r%60);}
function U(path,qs){return path+'?k='+encodeURIComponent(K)+(qs?'&'+qs:'');}
function api(path,body){return fetch(U(path),{method:body?'POST':'GET',body:body?JSON.stringify(body):undefined}).then(function(r){return r.json();});}
function toast(t,ms){var e=$('toast');e.textContent=t;e.style.display='block';clearTimeout(toast.t);toast.t=setTimeout(function(){e.style.display='none';},ms||4500);}
var S=null,hasVideo=false,hasPdf=false,view='split',ratio=55,swap=false,hoverPane='',prayerPaused=false,dragVol=false;

/* ================= layout ================= */
function applyLayout(){
 var showV=hasVideo&&view!=='pdf',showP=hasPdf&&view!=='video',both=showV&&showP;
 var pv=$('pv'),pg=$('pdfgroup'),sp=$('split');
 pv.style.display=showV?'flex':'none';pg.style.display=showP?'flex':'none';sp.style.display=both?'block':'none';
 if(both){pv.style.flex='0 0 '+ratio+'%';pg.style.flex='1 1 0';}else{pv.style.flex='1 1 0';pg.style.flex='1 1 0';}
 pv.style.order=swap?3:1;sp.style.order=2;pg.style.order=swap?1:3;
 ['video','split','pdf'].forEach(function(v){$('v-'+v).classList.toggle('on',v===view);});
}
function setView(v){view=v;applyLayout();}
function doSwap(){swap=!swap;applyLayout();api('/layout',{split:ratio,swap:swap});}
function dragH(onMove,onEnd,cls){
 var cover=$('dragcover');cover.style.display='block';
 function mv(e){onMove(e);}
 function up(){window.removeEventListener('mousemove',mv);window.removeEventListener('mouseup',up);cover.style.display='none';if(onEnd)onEnd();}
 window.addEventListener('mousemove',mv);window.addEventListener('mouseup',up);
}
$('split').addEventListener('mousedown',function(e){
 var sp=this;sp.classList.add('drag');e.preventDefault();
 dragH(function(ev){var r=$('main').getBoundingClientRect();var x=(ev.clientX-r.left)/r.width*100;ratio=swap?100-x:x;ratio=Math.max(20,Math.min(80,ratio));applyLayout();},
  function(){sp.classList.remove('drag');api('/layout',{split:ratio,swap:swap});});
});
function toggleDrawer(){var d=$('drawer');d.style.display=d.style.display==='block'?'none':'block';if(d.style.display==='block')$('inp').focus();}
$('pv').addEventListener('mouseenter',function(){hoverPane='pv';});$('pv').addEventListener('mouseleave',function(){hoverPane='';});

/* ================= doubts / audio / prayer ================= */
function prayerToggle(){api(S&&S.phase==='paused'?'/resume':'/pause',{});}
function audioToggle(){api('/audio',{cmd:'toggle'});}
function audioVol(v){api('/audio',{cmd:'volume',v:parseInt(v,10)});}
function saveDoubt(){var v=$('inp').value.trim();if(!v)return;var t=(hasVideo&&ready)?player.getCurrentTime():0;api('/doubt',{text:v,t:t});$('inp').value='';}
$('inp').addEventListener('keydown',function(e){if(e.key==='Enter'){saveDoubt();}});
$('vol').addEventListener('mousedown',function(){dragVol=true;});
document.addEventListener('mouseup',function(){dragVol=false;});

/* ================= shortcuts (configurable) ================= */
var DEFAULT_KEYS={speedUp:']',speedDown:'[',speedReset:'\\',playPause:'K',seekBack:'J',seekFwd:'L'};
var KEY_LABELS={speedUp:'Speed up (+0.1x)',speedDown:'Speed down (-0.1x)',speedReset:'Restore speed (1x)',playPause:'Play / pause video',seekBack:'Back 10 s',seekFwd:'Forward 10 s'};
var keys={},keyAct={},capturing=null;
function combo(e){
 var k=e.key;if(k==='Control'||k==='Shift'||k==='Alt'||k==='Meta')return '';
 if(k===' ')k='Space';else if(k.length===1)k=k.toUpperCase();
 var p=[];if(e.ctrlKey)p.push('Ctrl');if(e.altKey)p.push('Alt');if(e.shiftKey&&k.length>1)p.push('Shift');
 p.push(k);return p.join('+');
}
function rebuildKeyMap(){keyAct={};Object.keys(keys).forEach(function(a){if(keys[a])keyAct[keys[a]]=a;});}
function buildKeyUI(){
 var l=$('klist');l.innerHTML='';
 Object.keys(KEY_LABELS).forEach(function(a){
  var row=document.createElement('div');row.className='krow';
  var s=document.createElement('span');s.textContent=KEY_LABELS[a];
  var b=document.createElement('button');b.className='keybtn';b.textContent=keys[a]||'(none)';
  b.onclick=function(){capturing=a;b.textContent='Press a key...';b.classList.add('cap');};
  row.appendChild(s);row.appendChild(b);l.appendChild(row);});
}
function setKey(a,c){
 Object.keys(keys).forEach(function(o){if(o!==a&&keys[o]===c&&c)keys[o]='';});
 keys[a]=c;rebuildKeyMap();buildKeyUI();api('/keys',{keys:keys});
}
function resetKeys(){keys=JSON.parse(JSON.stringify(DEFAULT_KEYS));rebuildKeyMap();buildKeyUI();api('/keys',{keys:keys});}
function runAction(a){
 if(a==='speedUp')setSpeed(speed+0.1);else if(a==='speedDown')setSpeed(speed-0.1);else if(a==='speedReset')setSpeed(1);
 else if(a==='playPause')toggle();else if(a==='seekBack')seek(-10);else if(a==='seekFwd')seek(10);
}

/* ================= video (YouTube IFrame API) ================= */
var player=null,ready=false,started=false,ended=false,vpaused=false,lastSave=0,lastMove=Date.now(),vhover=false,wasPlaying=false,speed=1,speedDirect=false,warnedSpeed=false;
function vshow(h){var m=$('vmsg');m.innerHTML=h;m.style.display='block';}
function vhide(){$('vmsg').style.display='none';}
function begin(){vhide();try{player.playVideo();}catch(e){}}
function toggle(){
 if(!ready||prayerPaused)return;
 if(ended){ended=false;vhide();player.seekTo(0,true);player.playVideo();return;}
 var st=player.getPlayerState();
 if(st===1||st===3){player.pauseVideo();}else{player.playVideo();}
}
function seek(d){if(!ready||prayerPaused)return;var t=player.getCurrentTime()+d,dur=player.getDuration();
 t=Math.max(0,dur>5?Math.min(t,dur-2):t);player.seekTo(t,true);}
function videoEl(){try{return $('yt').contentDocument.querySelector('video');}catch(e){return null;}}
function applyRate(){
 if(!ready)return;
 var v=videoEl();
 if(v){speedDirect=true;if(Math.abs(v.playbackRate-speed)>0.001)v.playbackRate=speed;return;}
 speedDirect=false;
 var r=Math.min(2,speed);
 try{if(Math.abs(player.getPlaybackRate()-r)>0.001)player.setPlaybackRate(r);}catch(e){}
 if(speed>2&&!warnedSpeed){warnedSpeed=true;toast('Speeds above 2x are not available through the YouTube player on this system. Using 2x.',6000);}
}
function setSpeed(r,noSave){
 r=Math.round(r*10)/10;r=Math.max(0.5,Math.min(3,r));speed=r;
 $('spv').textContent=r.toFixed(1)+'x';applyRate();
 if(!noSave)api('/speed',{v:r});
}
$('sdn').onclick=function(){setSpeed(speed-0.1);};$('sup').onclick=function(){setSpeed(speed+0.1);};$('srs').onclick=function(){setSpeed(1);};
setInterval(applyRate,1000);
$('bar2').addEventListener('click',function(e){
 if(!ready||prayerPaused)return;var r=this.getBoundingClientRect(),f=Math.max(0,Math.min(1,(e.clientX-r.left)/r.width));
 player.seekTo(f*player.getDuration(),true);});
$('pv').addEventListener('mousemove',function(){lastMove=Date.now();});
$('hud').addEventListener('mouseenter',function(){vhover=true;});$('hud').addEventListener('mouseleave',function(){vhover=false;});
setInterval(function(){
 if(!hasVideo)return;
 var show=vpaused||prayerPaused||vhover||(Date.now()-lastMove<3000)||!started;
 $('hud').classList.toggle('hide',!show);
},250);
function onReady(){ready=true;player.playVideo();applyRate();
 setTimeout(function(){if(!started)vshow('<button class="b acc" style="font-size:15px;padding:12px 22px" onclick="begin()">&#9654; Start lecture</button>');},4000);}
function onState(e){var st=e.data;
 if(st===1){started=true;ended=false;vpaused=false;vhide();$('pp2').textContent='Pause';$('vpp').textContent='Pause video';applyRate();}
 else if(st===2){vpaused=started;$('pp2').textContent='Resume';$('vpp').textContent='Play video';}
 else if(st===0){ended=true;vpaused=true;$('pp2').textContent='Replay';vshow('Lecture finished.<br>The timer is still running - use the time for your doubts or notes.');}}
function onError(e){vshow('This video cannot be played here (error '+e.data+').<br>The owner may have disabled embedding, or the link is wrong.<br>Try another video after this session.');}
function loadYT(){
 window.onYouTubeIframeAPIReady=function(){
  player=new YT.Player('yt',{videoId:S.video_id,playerVars:{autoplay:1,controls:0,disablekb:1,fs:0,rel:0,modestbranding:1,playsinline:1,iv_load_policy:3,start:Math.floor(S.start||0)},
   events:{onReady:onReady,onStateChange:onState,onError:onError}});};
 var sc=document.createElement('script');sc.src='https://www.youtube.com/iframe_api';
 sc.onerror=function(){vshow('Could not load YouTube. Please check your internet connection.');};document.head.appendChild(sc);
}
setInterval(function(){
 if(!ready)return;
 var t=player.getCurrentTime(),d=player.getDuration();
 $('cur').textContent=fmt(t);$('dur').textContent=fmt(d);$('fill').style.width=(d>0?Math.min(100,t/d*100):0)+'%';
 if(!prayerPaused&&Date.now()-lastSave>4000){lastSave=Date.now();api('/pos',{t:t});}
},300);
function syncPause(){
 var p=(S.phase==='paused');
 if(p&&!prayerPaused){prayerPaused=true;
  if(ready){var st=player.getPlayerState();wasPlaying=(st===1||st===3);try{player.pauseVideo();}catch(e){}}
  $('pbtn').textContent='Resume';$('pbtn').className='b good';}
 else if(!p&&prayerPaused){prayerPaused=false;
  $('pbtn').textContent='Prayer break';$('pbtn').className='b warn';
  if(wasPlaying){try{player.playVideo();}catch(e){}}}
}

/* ================= PDF reader: tools ================= */
var tool='hand',panes=[],activePane=null,undoStack=[],tb=null,nightOn=false;
var PAL={hl:[[1,0.88,0.1],[0.45,0.9,0.4],[1,0.55,0.75],[0.45,0.75,1],[1,0.6,0.2]],
         pen:[[0.93,0.18,0.18],[0.2,0.4,0.95],[0.05,0.05,0.05],[0.1,0.65,0.3]],
         text:[[0,0,0],[0.85,0.1,0.1],[0.15,0.35,0.9],[0.1,0.55,0.25]]};
var OPT={hl:{c:0,w:14,min:4,max:40,step:1,alpha:0.35},pen:{c:0,w:3,min:0.5,max:12,step:0.5},text:{c:0,w:14,min:8,max:48,step:1}};
function rgb(c,a){return 'rgba('+Math.round(c[0]*255)+','+Math.round(c[1]*255)+','+Math.round(c[2]*255)+','+(a===undefined?1:a)+')';}
function setTool(t){
 commitText();tool=t;
 ['hand','hl','pen','text','erase'].forEach(function(x){$('t-'+x).classList.toggle('on',x===t);});
 panes.forEach(applyToolTo);buildOptions();
}
function buildOptions(){
 var sw=$('sw');sw.innerHTML='';var th=$('thick'),tv=$('thv');
 if(!OPT[tool]){th.style.display='none';tv.textContent='';return;}
 var o=OPT[tool];
 PAL[tool].forEach(function(c,i){var b=document.createElement('button');b.className='dot'+(i===o.c?' on':'');b.style.background=rgb(c);b.onclick=function(){o.c=i;buildOptions();};sw.appendChild(b);});
 th.style.display='';th.min=o.min;th.max=o.max;th.step=o.step;th.value=o.w;
 tv.textContent=o.w+(tool==='text'?' pt':' pt');
 th.oninput=function(){o.w=parseFloat(this.value);tv.textContent=o.w+' pt';};
}
function applyToolTo(P){P.pages.forEach(function(pg){var c=pg.ov;c.style.pointerEvents=(tool==='hand')?'none':'auto';c.style.cursor=tool==='erase'?'cell':(tool==='text'?'text':'crosshair');});}
function toggleNight(){nightOn=!nightOn;panes.forEach(function(P){P.pagesEl.classList.toggle('night',nightOn);});$('nightb').classList.toggle('on',nightOn);}
function setSaved(state,err){var s=$('saved');
 if(state==='saving'){s.textContent='Saving...';s.style.color='#d29922';}
 else if(state==='err'){s.textContent='Not saved';s.style.color='#f85149';s.title=err||'';if(err)toast('Could not save: '+err,7000);}
 else{s.textContent='Saved';s.style.color='#3fb950';s.title='';}}
function savePdf(){var P=activePane||panes[0];if(!P)return;setSaved('saving');api('/save',{doc:P.doc}).then(function(r){setSaved(r.err?'err':'ok',r.err);}).catch(function(){setSaved('err','server error');});}

/* ================= PDF reader: page loading queue ================= */
var Q={active:0,max:2,list:[]};
function imgW(d){return Math.min(2400,Math.max(400,Math.round(d.clientWidth*(window.devicePixelRatio||1)/100)*100));}
function reqPage(P,n){
 var d=P.pages[n].el,w=imgW(d),key=P.doc+'-'+w+'-'+P.ver[n];
 if(d.dataset.key===key||d.dataset.pending===key)return;
 d.dataset.pending=key;Q.list.push({P:P,n:n,key:key,w:w,doc:P.doc,ver:P.ver[n]});pump();
}
function pump(){
 while(Q.active<Q.max){
  Q.list=Q.list.filter(function(t){var pg=t.P.pages[t.n];return !t.P.dead&&pg&&pg.el.dataset.pending===t.key;});
  if(!Q.list.length)return;
  var bi=0,bd=1e18;
  Q.list.forEach(function(t,i){var d=t.P.pages[t.n].el,sc=t.P.scrollEl;var dd=Math.abs(d.offsetTop+d.offsetHeight/2-(sc.scrollTop+sc.clientHeight/2));if(dd<bd){bd=dd;bi=i;}});
  runTask(Q.list.splice(bi,1)[0]);
 }
}
function runTask(t){
 Q.active++;var pg=t.P.pages[t.n],d=pg.el,ctrl=new AbortController();pg.abort=ctrl;
 fetch(U('/page/'+t.doc+'/'+t.n,'w='+t.w+'&v='+t.ver),{signal:ctrl.signal}).then(function(r){if(!r.ok)throw new Error('x');return r.blob();}).then(function(b){
  var url=URL.createObjectURL(b),im=new Image();
  im.onload=function(){
   if(d.dataset.pending===t.key){
    if(pg.url)URL.revokeObjectURL(pg.url);
    pg.url=url;pg.img.src=url;d.dataset.key=t.key;d.dataset.pending='';clearOv(pg.ov);
   }else{URL.revokeObjectURL(url);}
  };
  im.onerror=function(){URL.revokeObjectURL(url);};
  im.src=url;
 }).catch(function(){}).then(function(){Q.active--;pump();});
}
function unloadPage(pg){
 var d=pg.el;if(!d.dataset.key&&!d.dataset.pending)return;
 d.dataset.key='';d.dataset.pending='';if(pg.abort){try{pg.abort.abort();}catch(e){}}
 if(pg.url){URL.revokeObjectURL(pg.url);pg.url=null;}pg.img.removeAttribute('src');
}
function clearOv(cv){cv.getContext('2d').clearRect(0,0,cv.width,cv.height);}
function updateVisible(P){
 var sc=P.scrollEl;if(!P.pages.length)return;
 var top=sc.scrollTop,vh=sc.clientHeight,lo=top-900,hi=top+vh+900,mid=top+vh*0.35,curP=0;
 for(var n=0;n<P.pages.length;n++){var pg=P.pages[n],d=pg.el,t=d.offsetTop,b=t+d.offsetHeight;
  if(b>=lo&&t<=hi)reqPage(P,n);else unloadPage(pg);
  if(t<=mid)curP=n;}
 if(document.activeElement!==P.pgi)P.pgi.value=String(curP+1);
}
function schedLoad(P){clearTimeout(P.lt);P.lt=setTimeout(function(){updateVisible(P);},120);}
function pageW(P){return Math.max(240,Math.round((P.scrollEl.clientWidth-36)*P.zoom));}
function relayout(P,force){
 if(!P.info||!P.pages.length)return;var sc=P.scrollEl;if(sc.clientWidth<80)return;
 var W=pageW(P);if(!force&&P.pages[0].el.clientWidth===W)return;
 var ry=sc.scrollTop/Math.max(1,sc.scrollHeight);
 P.pages.forEach(function(pg,n){var sz=P.info.pages[n],h=Math.round(W*sz[1]/sz[0]);
  pg.el.style.width=W+'px';pg.el.style.height=h+'px';pg.el.dataset.pending='';
  [pg.ov,pg.sr].forEach(function(c){c.width=W;c.height=h;});});
 sc.scrollTop=ry*sc.scrollHeight;schedLoad(P);drawSearch(P);
}
function refreshDocPage(doc,n){
 panes.forEach(function(P){if(P.doc!==doc||!P.pages[n])return;P.ver[n]++;P.pages[n].el.dataset.pending='';reqPage(P,n);});
}

/* ================= PDF reader: panes ================= */
function makePane(docIdx){
 var P={doc:docIdx,info:null,zoom:1,pages:[],ver:[],hits:[],hi:-1,query:'',dead:false,scrollPos:{}};
 var el=document.createElement('div');el.className='pane pdfpane';
 el.innerHTML='<div class="phead"><select class="dsel"></select><span class="sp"></span><input class="pgi" value="1"><span class="pgn">/ 1</span><button class="tool zm">&minus;</button><span class="zl">Fit</span><button class="tool zp">+</button><button class="tool zf">Fit</button><button class="tool pc" title="Close this pane">&times;</button></div><div class="scroll"><div class="pages"></div><div class="pmsg"></div></div>';
 P.el=el;P.scrollEl=el.querySelector('.scroll');P.pagesEl=el.querySelector('.pages');P.msgEl=el.querySelector('.pmsg');
 P.pgi=el.querySelector('.pgi');P.pgn=el.querySelector('.pgn');P.zl=el.querySelector('.zl');P.dsel=el.querySelector('.dsel');
 S.pdfs.forEach(function(n,i){var o=document.createElement('option');o.value=i;o.textContent=n;P.dsel.appendChild(o);});
 P.dsel.value=docIdx;P.dsel.onchange=function(){openDoc(P,parseInt(this.value,10));};
 P.dsel.style.display=S.pdfs.length>1?'':'none';
 el.querySelector('.zm').onclick=function(){zoomBy(P,1/1.15);};el.querySelector('.zp').onclick=function(){zoomBy(P,1.15);};
 el.querySelector('.zf').onclick=function(){P.zoom=1;P.zl.textContent='Fit';relayout(P,true);};
 el.querySelector('.pc').onclick=function(){closePane(P);};
 P.scrollEl.addEventListener('scroll',function(){clearTimeout(P.st);P.st=setTimeout(function(){updateVisible(P);},60);});
 new ResizeObserver(function(){clearTimeout(P.rt);P.rt=setTimeout(function(){relayout(P);},200);}).observe(P.scrollEl);
 P.pgi.addEventListener('keydown',function(e){if(e.key==='Enter'){var n=parseInt(this.value,10)-1;if(!isNaN(n)&&P.pages.length){n=Math.max(0,Math.min(P.pages.length-1,n));P.scrollEl.scrollTop=P.pages[n].el.offsetTop-10;}this.blur();}});
 el.addEventListener('mouseenter',function(){activePane=P;hoverPane='pp';});el.addEventListener('mouseleave',function(){if(hoverPane==='pp')hoverPane='';});
 el.addEventListener('mousedown',function(){activePane=P;});
 return P;
}
function zoomBy(P,f){P.zoom=Math.max(0.4,Math.min(4,P.zoom*f));P.zl.textContent=Math.round(P.zoom*100)+'%';relayout(P,true);}
function openDoc(P,i){
 if(P.info)P.scrollPos[P.doc]=P.scrollEl.scrollTop;
 P.doc=i;P.dsel.value=i;P.info=null;P.hits=[];P.hi=-1;
 P.pages.forEach(unloadPage);P.pages=[];P.ver=[];P.pagesEl.innerHTML='';
 showMsg(P,'Loading '+S.pdfs[i]+' ...');
 api('/pdfinfo/'+i).then(function(info){
  if(P.doc!==i||P.dead)return;
  if(info.error){showMsg(P,'Could not open this PDF: '+info.error);return;}
  P.info=info;P.msgEl.style.display='none';buildPages(P);
  P.scrollEl.scrollTop=P.scrollPos[i]||0;updateVisible(P);
 }).catch(function(){showMsg(P,'Could not load this PDF.');});
}
function showMsg(P,t){P.msgEl.textContent=t;P.msgEl.style.display='block';}
function buildPages(P){
 var W=pageW(P);P.pagesEl.innerHTML='';P.pages=[];P.ver=[];
 P.info.pages.forEach(function(sz,n){
  var h=Math.round(W*sz[1]/sz[0]),d=document.createElement('div');d.className='page';d.style.width=W+'px';d.style.height=h+'px';
  var img=document.createElement('img');img.draggable=false;d.appendChild(img);
  var sr=document.createElement('canvas');sr.className='sr';sr.width=W;sr.height=h;d.appendChild(sr);
  var ov=document.createElement('canvas');ov.className='ov';ov.width=W;ov.height=h;d.appendChild(ov);
  var pg={el:d,img:img,sr:sr,ov:ov,url:null,abort:null};
  attach(P,pg,n);P.pagesEl.appendChild(d);P.pages.push(pg);P.ver.push(0);
 });
 P.pgn.textContent='/ '+P.info.pages.length;
 P.pagesEl.classList.toggle('night',nightOn);
 applyToolTo(P);
}
function layoutPanes(){
 var row=$('prow');
 Array.prototype.slice.call(row.querySelectorAll('.vsplit')).forEach(function(s){s.remove();});
 panes.forEach(function(P,i){
  if(P.el.parentNode!==row)row.appendChild(P.el);
  P.el.style.order=String(i*2);P.el.style.flex=(P.w||1)+' 1 0';
  P.el.querySelector('.pc').style.display=panes.length>1?'':'none';
  if(i<panes.length-1){
   var sp=document.createElement('div');sp.className='vsplit';sp.style.order=String(i*2+1);
   sp.addEventListener('mousedown',function(e){innerSplit(e,sp,panes[i],panes[i+1]);});row.appendChild(sp);}
 });
 $('addpane').style.display=panes.length>=3?'none':'';
}
function innerSplit(e,sp,A,B){
 e.preventDefault();sp.classList.add('drag');
 var ra=A.el.getBoundingClientRect(),rb=B.el.getBoundingClientRect(),total=(A.w||1)+(B.w||1),span=ra.width+rb.width,left=ra.left;
 dragH(function(ev){var f=Math.max(0.15,Math.min(0.85,(ev.clientX-left)/span));A.w=total*f;B.w=total-A.w;A.el.style.flex=A.w+' 1 0';B.el.style.flex=B.w+' 1 0';},
  function(){sp.classList.remove('drag');});
}
function addPane(){
 if(panes.length>=3)return;
 var next=(panes.length?(panes[panes.length-1].doc+1):0)%S.pdfs.length;
 var P=makePane(next);P.w=1;panes.push(P);layoutPanes();openDoc(P,next);
}
function closePane(P){
 if(panes.length<=1)return;P.dead=true;P.pages.forEach(unloadPage);P.el.remove();panes.splice(panes.indexOf(P),1);
 if(activePane===P)activePane=panes[0];layoutPanes();
}
function initPdf(){
 var P=makePane(0);P.w=1;panes.push(P);layoutPanes();setTool('hand');openDoc(P,0);
 if(S.pdfs.length<2)$('addpane').title='Open a second view of the PDF';
}

/* ================= PDF reader: drawing / text / erase ================= */
function pt(P,pg,e,n){var r=pg.ov.getBoundingClientRect(),k=P.info.pages[n][0]/r.width;return [(e.clientX-r.left)*k,(e.clientY-r.top)*k];}
function simplify(pts){
 var out=[pts[0]];for(var i=1;i<pts.length;i++){var a=out[out.length-1],p=pts[i];if(Math.abs(p[0]-a[0])+Math.abs(p[1]-a[1])>0.8)out.push(p);}
 if(out.length<2)out.push(pts[pts.length-1]);if(out.length>3000)out=out.filter(function(_,i){return i%2===0;});return out;
}
var stroke=null;
function drawStroke(){
 var s=stroke,cv=s.pg.ov,c=cv.getContext('2d'),ppx=s.P.info.pages[s.n][0]/cv.width;
 c.clearRect(0,0,cv.width,cv.height);
 var o=s.kind==='hl'?OPT.hl:OPT.pen;
 c.strokeStyle=rgb(PAL[s.kind][o.c],s.kind==='hl'?o.alpha:1);c.lineWidth=o.w/ppx;c.lineCap='round';c.lineJoin='round';
 c.beginPath();s.css.forEach(function(p,i){if(i===0)c.moveTo(p[0],p[1]);else c.lineTo(p[0],p[1]);});
 if(s.css.length===1)c.lineTo(s.css[0][0]+0.1,s.css[0][1]);c.stroke();
}
function attach(P,pg,n){
 var cv=pg.ov;
 cv.addEventListener('pointerdown',function(e){
  if(tool==='hand'||e.button!==0)return;
  e.preventDefault();activePane=P;
  if(tool==='text'){startText(P,pg,n,e);return;}
  cv.setPointerCapture(e.pointerId);var p=pt(P,pg,e,n);
  if(tool==='hl'||tool==='pen'){stroke={P:P,pg:pg,n:n,kind:tool,pts:[p],css:[[e.offsetX,e.offsetY]]};drawStroke();}
  else if(tool==='erase'){eraseAt(P,n,p);}
 });
 cv.addEventListener('pointermove',function(e){
  if(stroke&&stroke.pg===pg){stroke.pts.push(pt(P,pg,e,n));stroke.css.push([e.offsetX,e.offsetY]);drawStroke();}
 });
 cv.addEventListener('pointerup',function(){
  if(stroke&&stroke.pg===pg){var s=stroke;stroke=null;var o=s.kind==='hl'?OPT.hl:OPT.pen;
   commit(P,n,{type:'ink',strokes:[simplify(s.pts)],color:PAL[s.kind][o.c],width:o.w,opacity:s.kind==='hl'?o.alpha:1});}
 });
 cv.addEventListener('pointercancel',function(){stroke=null;clearOv(cv);});
}
function commit(P,n,body){
 body.doc=P.doc;body.page=n;setSaved('saving');
 api('/annot',body).then(function(r){
  if(r.error){setSaved('err',r.error);clearOv(P.pages[n].ov);return;}
  if(r.xref)undoStack.push({doc:body.doc,page:n,xref:r.xref});
  setSaved(r.err?'err':'ok',r.err);refreshDocPage(body.doc,n);
 }).catch(function(){setSaved('err','server error');clearOv(P.pages[n].ov);});
}
function eraseAt(P,n,p){
 api('/annots/'+P.doc+'/'+n).then(function(list){
  var best=null,ba=1e18;
  (list||[]).forEach(function(a){var r=a.rect;
   if(p[0]>=r[0]-2&&p[0]<=r[2]+2&&p[1]>=r[1]-2&&p[1]<=r[3]+2){var ar=(r[2]-r[0])*(r[3]-r[1]);if(ar<ba){ba=ar;best=a;}}});
  if(best){setSaved('saving');api('/annot_del',{doc:P.doc,page:n,xref:best.xref}).then(function(r){setSaved(r.err?'err':'ok',r.err);refreshDocPage(P.doc,n);});}
 });
}
function undo(){
 var u=undoStack.pop();if(!u)return;setSaved('saving');
 api('/annot_del',{doc:u.doc,page:u.page,xref:u.xref}).then(function(r){setSaved(r.err?'err':'ok',r.err);refreshDocPage(u.doc,u.page);});
}
function startText(P,pg,n,e){
 commitText();
 var r=pg.ov.getBoundingClientRect(),x=e.clientX-r.left,y=e.clientY-r.top,ppx=P.info.pages[n][0]/r.width;
 var ta=document.createElement('textarea');ta.className='tbox';ta.style.left=x+'px';ta.style.top=y+'px';
 ta.style.fontSize=(OPT.text.w/ppx)+'px';ta.style.color=rgb(PAL.text[OPT.text.c]);ta.style.width=Math.min(260,Math.max(120,r.width-x-8))+'px';ta.rows=1;
 pg.el.appendChild(ta);
 tb={ta:ta,P:P,n:n,x:x,y:y,k:ppx,done:false};
 function grow(){ta.style.height='auto';ta.style.height=ta.scrollHeight+'px';}
 ta.addEventListener('input',grow);
 ta.addEventListener('keydown',function(ev){ev.stopPropagation();if(ev.key==='Enter'&&!ev.shiftKey){ev.preventDefault();commitText();}else if(ev.key==='Escape'){tb.done=true;ta.remove();tb=null;}});
 ta.addEventListener('blur',function(){setTimeout(commitText,60);});
 setTimeout(function(){ta.focus();grow();},0);
}
function commitText(){
 if(!tb||tb.done)return;var t=tb;t.done=true;tb=null;
 var val=t.ta.value.replace(/\s+$/,''),w=t.ta.offsetWidth,h=t.ta.scrollHeight;t.ta.remove();
 if(!val)return;
 if(/[^\u0000-\u00ff]/.test(val)){toast('The built-in PDF font can only draw Latin characters. Please remove other scripts.',6000);return;}
 commit(t.P,t.n,{type:'text',text:val,size:OPT.text.w,color:PAL.text[OPT.text.c],rect:[t.x*t.k,t.y*t.k,(t.x+w)*t.k+6,(t.y+h)*t.k+8]});
}

/* ================= PDF reader: search ================= */
function drawSearch(P){
 if(!P.pages.length)return;
 P.pages.forEach(function(pg){clearOv(pg.sr);});
 var flat=P.hits;
 flat.forEach(function(h,i){var pg=P.pages[h.page];if(!pg)return;var c=pg.sr.getContext('2d'),k=pg.sr.width/P.info.pages[h.page][0];
  c.fillStyle=(i===P.hi)?'rgba(255,140,0,.55)':'rgba(255,220,0,.35)';c.fillRect(h.r[0]*k,h.r[1]*k,(h.r[2]-h.r[0])*k,(h.r[3]-h.r[1])*k);});
}
function runSearch(P,q){
 P.query=q;
 return fetch(U('/search/'+P.doc,'q='+encodeURIComponent(q))).then(function(r){return r.json();}).then(function(res){
  P.hits=[];(res.hits||[]).forEach(function(h){h.rects.forEach(function(r){P.hits.push({page:h.page,r:r});});});
  P.hi=P.hits.length?0:-1;drawSearch(P);showHit(P);
  $('qc').textContent=P.hits.length?('1/'+P.hits.length):'0 found';
 });
}
function showHit(P){
 if(P.hi<0||!P.hits.length)return;var h=P.hits[P.hi],pg=P.pages[h.page];if(!pg)return;
 var k=pg.el.clientWidth/P.info.pages[h.page][0];P.scrollEl.scrollTop=pg.el.offsetTop+h.r[1]*k-80;
 drawSearch(P);$('qc').textContent=(P.hi+1)+'/'+P.hits.length;
}
function searchStep(d){
 var P=activePane||panes[0];if(!P)return;var q=$('q').value.trim();
 if(!q){P.hits=[];P.hi=-1;drawSearch(P);$('qc').textContent='';return;}
 if(P.query!==q||!P.hits.length){runSearch(P,q);return;}
 P.hi=(P.hi+d+P.hits.length)%P.hits.length;showHit(P);
}
$('q').addEventListener('keydown',function(e){e.stopPropagation();if(e.key==='Enter'){searchStep(e.shiftKey?-1:1);}else if(e.key==='Escape'){this.value='';searchStep(1);this.blur();}});

/* ================= keyboard / wheel ================= */
document.addEventListener('keydown',function(e){
 if(capturing){
  e.preventDefault();e.stopPropagation();
  if(e.key==='Escape'){capturing=null;buildKeyUI();return;}
  var c=combo(e);if(!c)return;setKey(capturing,e.key==='Backspace'?'':c);capturing=null;return;}
 var tg=e.target,typing=tg&&(tg.tagName==='INPUT'||tg.tagName==='SELECT'||tg.tagName==='TEXTAREA'),k=e.key||'';
 if(k==='F5'||k==='F11'||k==='F12'||(e.ctrlKey&&k.length===1&&'rpuownt'.indexOf(k.toLowerCase())>=0)){e.preventDefault();return;}
 if(e.ctrlKey&&k.toLowerCase()==='s'){e.preventDefault();savePdf();return;}
 if(e.ctrlKey&&k.toLowerCase()==='f'&&hasPdf){e.preventDefault();$('q').focus();$('q').select();return;}
 if(typing){if(e.altKey)e.preventDefault();return;}
 if(e.ctrlKey&&k.toLowerCase()==='z'){e.preventDefault();undo();return;}
 var act=keyAct[combo(e)];
 if(act&&hasVideo){e.preventDefault();runAction(act);return;}
 if(hoverPane==='pv'&&hasVideo){
  if(k==='ArrowLeft'){seek(-10);e.preventDefault();}else if(k==='ArrowRight'){seek(10);e.preventDefault();}else if(k===' '){toggle();e.preventDefault();}}
},true);
document.addEventListener('wheel',function(e){if(e.ctrlKey){e.preventDefault();if(hoverPane==='pp'&&activePane)zoomBy(activePane,e.deltaY<0?1.1:1/1.1);}},{passive:false});
document.addEventListener('contextmenu',function(e){e.preventDefault();});

/* ================= state sync / boot ================= */
function sync(s){
 S=s;syncPause();
 $('dbtn').textContent='Doubts ('+S.doubts+')';
 var ul=$('dlist');ul.innerHTML='';
 (S.doubt_list||[]).forEach(function(t){var li=document.createElement('li');li.textContent=t;ul.insertBefore(li,ul.firstChild);});
 if(S.audio&&S.audio.loaded){$('aud').style.display='flex';$('abtn').textContent=S.audio.playing?'Pause audio':'Play audio';if(!dragVol)$('vol').value=S.audio.volume;}
}
function renderTop(){
 if(!S)return;
 var paused=(S.phase==='paused'),rem=paused?S.paused_rem:(S.phase_end-Date.now())/1000;
 $('timer').textContent=tm(rem);
 $('phase').textContent=paused?'Paused':('Session '+S.cycle+' of '+S.cycles);
 var f=1-rem/Math.max(1,S.phase_total);$('prog').style.width=Math.max(0,Math.min(1,f))*100+'%';
 $('prog').style.background=paused?'#d29922':'';
}
api('/state').then(function(s){
 S=s;hasVideo=!!S.has_video;hasPdf=(S.pdfs||[]).length>0;ratio=S.split||55;swap=!!S.swap;
 document.body.classList.toggle('lec',S.mode==='lecture');
 $('brand').textContent=S.mode==='lecture'?'Lecture':'Self study';
 $('views').style.display=(hasVideo&&hasPdf)?'flex':'none';
 $('swapb').style.display=(hasVideo&&hasPdf)?'':'none';
 $('vpp').style.display=hasVideo?'':'none';
 $('speedbox').style.display=hasVideo?'flex':'none';
 keys=JSON.parse(JSON.stringify(DEFAULT_KEYS));Object.keys(S.keys||{}).forEach(function(a){if(a in keys)keys[a]=S.keys[a];});
 rebuildKeyMap();buildKeyUI();$('keys').style.display=hasVideo?'block':'none';
 speed=Math.max(0.5,Math.min(3,S.speed||1));$('spv').textContent=speed.toFixed(1)+'x';
 applyLayout();
 if(hasVideo)loadYT();
 if(hasPdf)initPdf();
 sync(S);
 setInterval(function(){api('/state').then(sync).catch(function(){});},500);
 setInterval(renderTop,250);renderTop();
});
</script></body></html>
"""


BACKUP_DIR = os.path.join(os.path.expanduser("~"), ".study_lock_backups")


def load_fitz():
    try:
        import pymupdf as fitz
        return fitz
    except Exception:
        pass
    try:
        import fitz
        return fitz
    except Exception:
        return None


class PdfStore:
    """PDF engine (PyMuPDF): renders pages, finds words, adds / removes highlights and ink,
    and saves the edits straight back into the ORIGINAL file (incremental save)."""

    def __init__(self):
        self.lock = threading.RLock()
        self.docs = {}
        self.backed = set()
        self.cache = OrderedDict()
        self.ver = {}
        self.fz = load_fitz()

    def _need(self):
        if self.fz is None:
            raise RuntimeError("PDF engine (PyMuPDF) is not installed in this build")

    def _doc(self, path):
        self._need()
        d = self.docs.get(path)
        if d is None:
            d = self.fz.open(path)
            self.docs[path] = d
        return d

    def info(self, path):
        with self.lock:
            d = self._doc(path)
            if d.needs_pass:
                raise RuntimeError("this PDF is password protected")
            pages = []
            for i in range(len(d)):
                r = d[i].rect
                pages.append([round(r.width, 2), round(r.height, 2)])
            return {"pages": pages, "writable": os.access(path, os.W_OK)}

    def _bump(self, path):
        self.ver[path] = self.ver.get(path, 0) + 1
        for k in [k for k in self.cache if k[0] == path]:
            self.cache.pop(k, None)

    def render(self, path, n, width):
        """Returns (bytes, content_type). Pages are cached; JPEG keeps rendering fast."""
        with self.lock:
            key = (path, n, int(width), self.ver.get(path, 0))
            hit = self.cache.get(key)
            if hit is not None:
                self.cache.move_to_end(key)
                return hit
            page = self._doc(path)[n]
            scale = max(0.3, min(6.0, float(width) / max(1.0, page.rect.width)))
            pix = page.get_pixmap(matrix=self.fz.Matrix(scale, scale), alpha=False)
            try:
                res = (pix.tobytes("jpeg", jpg_quality=88), "image/jpeg")
            except Exception:
                res = (pix.tobytes("png"), "image/png")
            self.cache[key] = res
            while len(self.cache) > 40:
                self.cache.popitem(last=False)
            return res

    def search(self, path, q):
        with self.lock:
            d = self._doc(path)
            hits, total = [], 0
            for i in range(len(d)):
                rects = d[i].search_for(q)
                if rects:
                    hits.append({"page": i, "rects": [[r.x0, r.y0, r.x1, r.y1] for r in rects]})
                    total += len(rects)
                    if total >= 500:
                        break
            return {"hits": hits}

    def words(self, path, n):
        with self.lock:
            page = self._doc(path)[n]
            return [[round(w[0], 2), round(w[1], 2), round(w[2], 2), round(w[3], 2)]
                    for w in page.get_text("words")]

    def annots(self, path, n):
        with self.lock:
            page = self._doc(path)[n]
            out = []
            for a in (page.annots() or []):
                r = a.rect
                out.append({"xref": a.xref, "type": a.type[1], "rect": [r.x0, r.y0, r.x1, r.y1]})
            return out

    def _backup(self, path):
        """One safety copy of the original, made right before the first edit of this run."""
        if path in self.backed:
            return
        self.backed.add(path)
        try:
            import shutil
            os.makedirs(BACKUP_DIR, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            shutil.copy2(path, os.path.join(BACKUP_DIR, f"{os.path.basename(path)}.{stamp}.bak"))
        except Exception as e:
            print("backup failed", e)

    def _persist(self, path):
        """Save into the original file. Returns None on success or an error string."""
        d = self.docs.get(path)
        if d is None:
            return None
        try:
            if d.can_save_incrementally():
                fn = getattr(d, "saveIncr", None) or getattr(d, "save_incr")
                fn()
                return None
        except Exception as e:
            first = str(e)
        else:
            first = ""
        try:                                   # fallback: full rewrite, then replace the original
            tmp = path + ".studylock.tmp"
            d.save(tmp, garbage=0, deflate=True)
            d.close()
            self.docs.pop(path, None)
            os.replace(tmp, path)
            return None
        except Exception as e:
            self.docs.pop(path, None)
            return (first + " / " if first else "") + str(e)

    def add(self, path, n, b):
        with self.lock:
            self._backup(path)
            d = self._doc(path)
            page = d[n]
            col = tuple(max(0.0, min(1.0, float(c))) for c in b.get("color", [1, 0.9, 0.2]))[:3]
            if b.get("type") == "highlight":
                quads = [self.fz.Rect(*[float(v) for v in r]) for r in b["rects"]]
                a = page.add_highlight_annot(quads=quads)
                if a is None:
                    raise RuntimeError("could not create the highlight")
                a.set_colors(stroke=col)
                a.update()
            elif b.get("type") == "ink":
                strokes = [[(float(x), float(y)) for x, y in s] for s in b["strokes"]]
                a = page.add_ink_annot(strokes)
                a.set_colors(stroke=col)
                a.set_border(width=float(b.get("width", 3)))
                op = float(b.get("opacity", 1))
                if op < 1:
                    a.set_opacity(op)
                a.update()
            elif b.get("type") == "text":
                size = float(b.get("size", 14))
                rect = self.fz.Rect(*[float(v) for v in b["rect"]])
                if rect.width < 40:
                    rect.x1 = rect.x0 + 40
                if rect.height < size * 1.6:
                    rect.y1 = rect.y0 + size * 1.6
                a = page.add_freetext_annot(rect, str(b["text"]), fontsize=size, fontname="helv",
                                            text_color=col, align=0)
                if a is None:
                    raise RuntimeError("the text did not fit in its box")
            else:
                raise RuntimeError("unknown annotation type")
            xref = a.xref
            self._bump(path)
            return {"xref": xref, "err": self._persist(path)}

    def delete(self, path, n, xref):
        with self.lock:
            self._backup(path)
            page = self._doc(path)[n]
            a = page.load_annot(int(xref))
            if a is not None:
                page.delete_annot(a)
            self._bump(path)
            return {"err": self._persist(path)}

    def save(self, path):
        with self.lock:
            return {"err": self._persist(path)} if path in self.docs else {"err": None}

    def close_all(self):
        with self.lock:
            for d in list(self.docs.values()):
                try:
                    d.close()
                except Exception:
                    pass
            self.docs.clear()


class LectureServer:
    """Tiny local server (127.0.0.1 only): serves the study-room page, renders PDF pages,
    saves PDF edits, and receives doubts / commands from the page."""

    def __init__(self, app):
        self.app = app
        self.store = PdfStore()
        self.token = secrets.token_urlsafe(12)
        srv = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _parse(self):
                q = urllib.parse.urlparse(self.path)
                qs = urllib.parse.parse_qs(q.query)
                return q.path, qs.get("k", [""])[0] == srv.token, qs

            def _send_bytes(self, code, data, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except Exception:
                    pass

            def _json(self, obj, code=200):
                self._send_bytes(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

            def _pdf_path(self, i):
                return srv.app.pdf_list[int(i)]

            def do_GET(self):
                path, ok, qs = self._parse()
                if not ok:
                    return self._json({}, 403)
                try:
                    if path == "/":
                        return self._send_bytes(200, ROOM_HTML.encode("utf-8"), "text/html; charset=utf-8")
                    if path == "/state":
                        return self._json(srv.app.lecture_state())
                    m = re.fullmatch(r"/pdfinfo/(\d+)", path)
                    if m:
                        return self._json(srv.store.info(self._pdf_path(m.group(1))))
                    m = re.fullmatch(r"/page/(\d+)/(\d+)", path)
                    if m:
                        w = int(qs.get("w", ["1000"])[0])
                        data, ctype = srv.store.render(self._pdf_path(m.group(1)), int(m.group(2)), w)
                        return self._send_bytes(200, data, ctype)
                    m = re.fullmatch(r"/search/(\d+)", path)
                    if m:
                        q = qs.get("q", [""])[0]
                        return self._json(srv.store.search(self._pdf_path(m.group(1)), q) if q else {"hits": []})
                    m = re.fullmatch(r"/words/(\d+)/(\d+)", path)
                    if m:
                        return self._json(srv.store.words(self._pdf_path(m.group(1)), int(m.group(2))))
                    m = re.fullmatch(r"/annots/(\d+)/(\d+)", path)
                    if m:
                        return self._json(srv.store.annots(self._pdf_path(m.group(1)), int(m.group(2))))
                except Exception as e:
                    return self._json({"error": str(e)}, 500)
                self._json({}, 404)

            def do_POST(self):
                path, ok, qs = self._parse()
                if not ok:
                    return self._json({}, 403)
                try:
                    n = int(self.headers.get("Content-Length", 0) or 0)
                    body = json.loads(self.rfile.read(n) or b"{}")
                except Exception:
                    body = {}
                a = srv.app
                try:
                    if path == "/doubt":
                        a.q_doubts.put(body)
                    elif path == "/pos":
                        a.lecture_pos = float(body.get("t", 0) or 0)
                    elif path == "/pause":
                        a.q_cmds.put("pause")
                    elif path == "/resume":
                        a.q_cmds.put("resume")
                    elif path == "/audio":
                        a.q_cmds.put(("audio", body.get("cmd"), body.get("v")))
                    elif path == "/speed":
                        a.q_cmds.put(("speed", body.get("v")))
                    elif path == "/keys":
                        a.q_cmds.put(("keys", body.get("keys")))
                    elif path == "/layout":
                        a.q_cmds.put(("layout", body.get("split"), body.get("swap")))
                    elif path == "/annot":
                        return self._json(srv.store.add(self._pdf_path(body["doc"]), int(body["page"]), body))
                    elif path == "/annot_del":
                        return self._json(srv.store.delete(self._pdf_path(body["doc"]), int(body["page"]),
                                                           body["xref"]))
                    elif path == "/save":
                        return self._json(srv.store.save(self._pdf_path(body["doc"])))
                except Exception as e:
                    return self._json({"error": str(e)})
                self._json({})

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path="/"):
        return f"http://127.0.0.1:{self.port}{path}?k={self.token}"


def run_player(url):
    """Alag process: fullscreen, always-on-top lecture window (Edge WebView2)."""
    try:
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
            "--autoplay-policy=no-user-gesture-required --disable-web-security "
            "--disable-site-isolation-trials --disable-features=IsolateOrigins,site-per-process")
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


# ----------------------------------------------------------------- in-app updater
class Updater:
    """Checks the 'latest' GitHub release and replaces the running exe (no manual download)."""

    def __init__(self):
        self.state = "idle"   # idle | checking | uptodate | available | downloading | ready | error | unavailable
        self.msg = ""
        self.pct = 0
        self.url = None
        self.size = 0
        self.dirty = True
        self.frozen = bool(getattr(sys, "frozen", False))
        self.exe = sys.executable
        self._busy = False

    def _set(self, state, msg="", pct=None):
        self.state, self.msg = state, msg
        if pct is not None:
            self.pct = pct
        self.dirty = True

    def cleanup(self):
        if self.frozen:
            try:
                if os.path.exists(self.exe + ".old"):
                    os.remove(self.exe + ".old")
            except Exception:
                pass

    def check(self):
        if self._busy:
            return
        if not self.frozen:
            self._set("unavailable", "Updates work in the .exe build only.")
            return
        self._busy = True
        self._set("checking", "Checking for updates...")
        threading.Thread(target=self._check, daemon=True).start()

    def _check(self):
        try:
            req = urllib.request.Request(UPDATE_API, headers={"User-Agent": APP_NAME,
                                                              "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.load(r)
            body = (data.get("body") or "").strip()
            sha = body.split()[0] if body else ""
            asset = next((a for a in data.get("assets", []) if a.get("name", "").lower().endswith(".exe")), None)
            if not asset:
                raise RuntimeError("no build found on GitHub yet")
            self.url = asset["browser_download_url"]
            self.size = int(asset.get("size", 0) or 0)
            if sha and BUILD_ID != "dev" and sha == BUILD_ID:
                self._set("uptodate", "You are up to date.")
            else:
                self._set("available", "A new version is available.")
        except Exception as e:
            self._set("error", f"Could not check for updates: {e}")
        finally:
            self._busy = False

    def install(self):
        if self._busy or self.state != "available":
            return
        self._busy = True
        self._set("downloading", "Downloading update...", 0)
        threading.Thread(target=self._install, daemon=True).start()

    def _install(self):
        exe = self.exe
        new = os.path.join(os.path.dirname(exe), "StudyLock.new.exe")
        old = exe + ".old"
        try:
            req = urllib.request.Request(self.url, headers={"User-Agent": APP_NAME})
            with urllib.request.urlopen(req, timeout=60) as r, open(new, "wb") as f:
                total = int(r.headers.get("Content-Length") or self.size or 0)
                got = 0
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        self._set("downloading", "Downloading update...", int(got * 100 / total))
            if os.path.getsize(new) < MIN_EXE_BYTES:
                raise RuntimeError("the downloaded file looks incomplete")
            with open(new, "rb") as f:
                if f.read(2) != b"MZ":
                    raise RuntimeError("the downloaded file is not a Windows program")
            if os.path.exists(old):
                try:
                    os.remove(old)
                except Exception:
                    pass
            os.replace(exe, old)              # a running exe can be renamed on Windows
            try:
                os.replace(new, exe)
            except Exception:
                os.replace(old, exe)
                raise
            self._set("ready", "Update installed. Restart to use it.")
        except Exception as e:
            try:
                if os.path.exists(new):
                    os.remove(new)
            except Exception:
                pass
            self._set("error", f"Update failed: {e}")
        finally:
            self._busy = False


# ----------------------------------------------------------------- UI widgets
BTN_KINDS = {
    "primary":   (ACC, "#ffffff", ACC_H),
    "violet":    (VIOLET, "#ffffff", "#b68cff"),
    "secondary": (SURFACE2, TEXT, "#30363d"),
    "danger":    ("#3d1417", DANGER, "#59191d"),
    "success":   ("#10361c", GOOD, "#17502a"),
    "warn":      ("#3b2a08", WARN, "#563d0c"),
}
DIS_BG, DIS_FG = "#1b2028", "#5b626c"


def round_rect(cv, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


class Btn(tk.Canvas):
    """Rounded flat button (hover effect, enable / disable, stretches with pack(fill='x'))."""

    def __init__(self, parent, text, command=None, kind="secondary", size=10, padx=18, pady=9, radius=9):
        self.cmd = command
        self.enabled = True
        self._hover = False
        self._text = text
        self._r = radius
        self._padx, self._pady = padx, pady
        self._font = tkfont.Font(family=FONT, size=size, weight="bold")
        self._colors = BTN_KINDS[kind]
        try:
            pbg = parent.cget("bg")
        except Exception:
            pbg = SURFACE
        w, h = self._min_size()
        super().__init__(parent, width=w, height=h, bg=pbg, highlightthickness=0, cursor="hand2")
        self.bind("<Configure>", self._draw)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._click)
        self._draw()

    def _min_size(self):
        return (self._font.measure(self._text) + 2 * self._padx,
                self._font.metrics("linespace") + 2 * self._pady)

    def _draw(self, event=None):
        mw, mh = self._min_size()
        w = event.width if event is not None else max(self.winfo_width(), mw)
        h = event.height if event is not None else max(self.winfo_height(), mh)
        c_bg, c_fg, c_hv = self._colors
        bg = (c_hv if self._hover else c_bg) if self.enabled else DIS_BG
        fg = c_fg if self.enabled else DIS_FG
        self.delete("all")
        round_rect(self, 1, 1, w - 1, h - 1, self._r, fill=bg, outline="")
        self.create_text(w / 2, h / 2, text=self._text, fill=fg, font=self._font)

    def _enter(self, e):
        self._hover = True
        self._draw()

    def _leave(self, e):
        self._hover = False
        self._draw()

    def _click(self, e):
        if self.enabled and self.cmd:
            self.cmd()

    def enable(self, on=True):
        if on != self.enabled:
            self.enabled = on
            self.config(cursor="hand2" if on else "arrow")
            self._draw()

    def set_text(self, text):
        if text != self._text:
            self._text = text
            self.config(width=self._min_size()[0])
            self._draw()

    def set_kind(self, kind):
        self._colors = BTN_KINDS[kind]
        self._draw()


def card(parent, title=None, pad=16):
    """Bordered surface. Returns (outer, inner): pack/grid `outer`, put widgets in `inner`."""
    outer = tk.Frame(parent, bg=BORDER)
    inner = tk.Frame(outer, bg=SURFACE, padx=pad, pady=pad)
    inner.pack(fill="both", expand=True, padx=1, pady=1)
    if title:
        tk.Label(inner, text=title.upper(), bg=SURFACE, fg=MUTED,
                 font=(FONT, 9, "bold")).pack(anchor="w", pady=(0, 10))
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
    head.pack(fill="x", pady=(0, 16))
    left = tk.Frame(head, bg=BG)
    left.pack(side="left")
    tk.Label(left, text=title, bg=BG, fg=TEXT, font=(FONT, 22, "bold")).pack(anchor="w")
    tk.Label(left, text=subtitle, bg=BG, fg=MUTED, font=(FONT, 10)).pack(anchor="w", pady=(2, 0))
    tk.Label(head, text=dt.date.today().strftime("%A, %d %B %Y"), bg=BG, fg=MUTED,
             font=(FONT, 10)).pack(side="right", anchor="s")


class Stepper(tk.Frame):
    """Label + [-] value [+] number input."""

    def __init__(self, parent, label, value, lo, hi, step=1, bg=SURFACE, command=None):
        super().__init__(parent, bg=bg)
        self.lo, self.hi, self.step, self.command = lo, hi, step, command
        self.var = tk.StringVar(value=str(value))
        tk.Label(self, text=label, bg=bg, fg=MUTED, font=(FONT, 9, "bold")).pack(anchor="w")
        row = tk.Frame(self, bg=bg)
        row.pack(pady=(6, 0))
        Btn(row, "\u2212", lambda: self.bump(-1), "secondary", size=12, padx=12, pady=3).pack(side="left")
        self.entry = tk.Entry(row, textvariable=self.var, width=4, justify="center", bg=SURFACE2, fg=TEXT,
                              insertbackground=TEXT, relief="flat", font=(FONT, 14, "bold"),
                              highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACC)
        self.entry.pack(side="left", padx=6, ipady=5)
        Btn(row, "+", lambda: self.bump(1), "secondary", size=12, padx=12, pady=3).pack(side="left")
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
    """One page per mode ('self' or 'lecture'): timer, settings, source cards, doubts."""

    def __init__(self, parent, app, mode):
        super().__init__(parent, bg=BG)
        self.app, self.mode = app, mode
        self.accent = ACC if mode == "self" else VIOLET
        self.kind = "primary" if mode == "self" else "violet"
        st = app.data["settings"][mode]
        self._last = None
        self._btnkey = None

        if mode == "self":
            page_header(self, "Self Study", "Lock your screen and study from your books or PDFs.")
        else:
            page_header(self, "Lecture", "Watch a YouTube lecture on a locked screen. Pause and seek freely.")

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # ---- left: timer
        lo, li = card(body, "Timer", pad=18)
        lo.grid(row=0, column=0, sticky="ns", padx=(0, 16))
        self.ring = Ring(li, size=250, thick=12, bg=SURFACE, color=self.accent)
        self.ring.pack()
        self.lbl_status = tk.Label(li, text="", bg=SURFACE, fg=MUTED, font=(FONT, 10), wraplength=250,
                                   justify="center")
        self.lbl_status.pack(pady=(14, 14))
        self.btn_start = Btn(li, "Start session", lambda: app.start_session(mode), self.kind, size=11, pady=11)
        self.btn_start.pack(fill="x")
        self.btn_resume = Btn(li, "Resume", app.resume_session, "success", size=11, pady=11)
        self.btn_skip = Btn(li, "Skip break", app.skip_break, "secondary")
        self.btn_prayer = Btn(li, "Prayer break", app.pause_session, "warn")
        self.btn_end = Btn(li, "End session", app.stop_session, "danger")

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
            self._build_pdf_card(right)
        else:
            self._build_lecture_card(right, st)
            self._build_pdf_card(right)

        do, di = card(right, "Doubts to clear in your break")
        do.pack(fill="both", expand=True, pady=(12, 0))
        self.lb = make_listbox(di, height=3)
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
        self.vol = tk.Scale(row, from_=0, to=100, orient="horizontal", showvalue=False, bg=SURFACE,
                            troughcolor=SURFACE2, highlightthickness=0, bd=0, sliderrelief="flat",
                            width=12, sliderlength=22, activebackground=ACC, length=120)
        self.vol.set(self.app.data["settings"]["self"]["volume"])
        self.vol.config(command=self.app.on_volume)
        self.vol.pack(side="right", padx=(10, 0))
        self.btn_play = Btn(row, "Play", self.app.toggle_audio, "secondary")
        self.btn_play.pack(side="right", padx=6)
        Btn(row, "Choose file", self.app.pick_audio, "secondary").pack(side="right")

    def _build_pdf_card(self, right):
        title = "Study material (PDF, optional)" if self.mode == "self" else "PDF beside the video (optional)"
        po, pi = card(right, title)
        po.pack(fill="x", pady=(12, 0))
        row = tk.Frame(pi, bg=SURFACE)
        row.pack(fill="x")
        self.lb_pdf = make_listbox(row, height=2 if self.mode == "lecture" else 3)
        self.lb_pdf.pack(side="left", fill="x", expand=True)
        col = tk.Frame(row, bg=SURFACE)
        col.pack(side="left", padx=(10, 0))
        Btn(col, "Add PDF", lambda: self.app.add_pdfs(self.mode), "secondary").pack(fill="x")
        Btn(col, "Remove", lambda: self.app.remove_pdf(self.mode, self.lb_pdf), "secondary").pack(fill="x", pady=(6, 0))
        hint = ("Opens in a full-screen reader: scroll, highlight, draw. Edits are saved into the original file."
                if self.mode == "self" else
                "Opens next to the video in a split screen you can resize. Edits are saved into the original file.")
        tk.Label(pi, text=hint, bg=SURFACE, fg=MUTED, font=(FONT, 9), anchor="w").pack(anchor="w", pady=(8, 0))

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
        tk.Label(li, text="Pause, resume, seek and change speed any time. The focus timer keeps running while "
                          "paused. Prayer break pauses everything.",
                 bg=SURFACE, fg=MUTED, font=(FONT, 9), anchor="w", justify="left",
                 wraplength=560).pack(anchor="w", pady=(2, 0))

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
    def _layout_buttons(self, key):
        if key == self._btnkey:
            return
        self._btnkey = key
        for b in (self.btn_resume, self.btn_skip, self.btn_prayer, self.btn_end):
            b.pack_forget()
        if key == "break":
            self.btn_skip.pack(fill="x", pady=(8, 0))
            self.btn_prayer.pack(fill="x", pady=(8, 0))
            self.btn_end.pack(fill="x", pady=(8, 0))
        elif key == "paused":
            self.btn_resume.pack(fill="x", pady=(8, 0))
            self.btn_end.pack(fill="x", pady=(8, 0))

    def render(self):
        app = self.app
        mine = app.phase != "idle" and app.mode == self.mode
        key = "idle"
        status_fg = MUTED
        if mine and app.phase == "study":
            rem = max(0.0, app.phase_end - time.time())
            frac = 1 - rem / max(1, app.phase_total)
            vals = (frac, ms(rem), "FOCUS", self.accent,
                    f"Focus \u00b7 session {app.cycle} of {app.cycles}")
        elif mine and app.phase == "break":
            rem = max(0.0, app.phase_end - time.time())
            frac = 1 - rem / max(1, app.phase_total)
            vals = (frac, ms(rem), "BREAK", GOOD,
                    f"Break \u00b7 clear your doubts ({app.cycle} of {app.cycles} done)")
            key, status_fg = "break", GOOD
        elif mine and app.phase == "paused":
            frac = 1 - app.paused_rem / max(1, app.phase_total)
            away = ms(time.time() - app.paused_at)
            vals = (frac, ms(app.paused_rem), "PAUSED", WARN,
                    f"Prayer break \u00b7 everything is paused ({away}).\nPress Resume when you are back.")
            key, status_fg = "paused", WARN
        else:
            mins = self.s_study.peek()
            if app.phase != "idle":
                msg = "Another session is running."
            elif app.end_msg and app.mode == self.mode:
                msg = app.end_msg
            else:
                msg = "Ready when you are."
            vals = (0.0, f"{mins:02d}:00", "READY", self.accent, msg)
        if vals == self._last:
            return
        self._last = vals
        frac, text, sub, color, status = vals
        self.ring.set(frac, text, sub, color)
        self.lbl_status.config(text=status, fg=status_fg)
        idle = app.phase == "idle"
        self.btn_start.enable(idle)
        self.btn_start.set_text("Start session" if idle else "Session in progress")
        self._layout_buttons(key)


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
        self.lb.insert(0, f"  Lap {len(self.laps):<3} {self.fmt(e)}     +{self.fmt(e - prev)}")

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
LOCK_ATTRS = ("l_ring", "l_msg", "l_entry", "l_lb", "l_count", "l_play", "l_vol", "lv_doubt", "l_prayer")


class App:
    def __init__(self, root):
        self.root = root
        self.data = load_data()
        self.audio = Audio()
        self.blocker = KeyBlocker()

        self.phase = "idle"       # idle | study | break | paused
        self.mode = "self"        # self | lecture
        self.cycle = 0
        self.cycles = 1
        self.study_min = 50
        self.break_min = 10
        self.phase_total = 1
        self.phase_end = 0.0
        self.end_msg = ""
        self.resume_phase = "study"
        self.paused_rem = 0.0
        self.paused_at = 0.0
        self.last_tick = time.time()
        self.last_save = time.time()
        self.lock = None

        # lecture
        self.q_cmds = queue.Queue()
        self.q_doubts = queue.Queue()
        self.server = None
        self.player = None
        self.player_started = 0.0
        self.player_fail = 0
        self.lec_active = False
        self.player_kind = None      # None | 'lecture' | 'pdf'
        self.pdf_list = []
        self.audio_was_playing = False
        self.lec_vid = None
        self.lec_start = 0.0
        self.lecture_pos = 0.0

        root.title(APP_NAME)
        root.geometry("1060x740")
        root.minsize(980, 680)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.updater = Updater()
        self.updater.cleanup()
        self.build_ui()

        s = self.data["settings"]["self"]
        self.audio.volume = s["volume"]
        if s["audio"] and self.audio.load(s["audio"]):
            self.views["self"].audio_name.set(os.path.basename(s["audio"]))

        self.refresh_doubts()
        self.refresh_pdf_list()
        self.refresh_progress()
        self.views["lecture"].update_lec_label()
        dark_titlebar(root)
        self.show_page("self")
        if self.updater.frozen:
            root.after(5000, self.updater.check)       # quiet check a few seconds after start
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

        box = tk.Frame(side, bg=BORDER)
        box.pack(side="bottom", fill="x", padx=14, pady=16)
        self.upd_btn = Btn(side, "Check for updates", self.on_update_click, "secondary", size=9, padx=12, pady=7)
        self.upd_btn.pack(side="bottom", fill="x", padx=14)
        self.upd_lbl = tk.Label(side, text="", bg=SIDEBAR, fg=MUTED, font=(FONT, 8), wraplength=190,
                                justify="left", anchor="w")
        self.upd_lbl.pack(side="bottom", fill="x", padx=16, pady=(0, 6))
        inner = tk.Frame(box, bg=SURFACE, padx=14, pady=12)
        inner.pack(fill="x", padx=1, pady=1)
        tk.Label(inner, text="TODAY", bg=SURFACE, fg=MUTED, font=(FONT, 8, "bold")).pack(anchor="w")
        self.side_today = tk.Label(inner, text="0h 00m", bg=SURFACE, fg=TEXT, font=(FONT, 15, "bold"))
        self.side_today.pack(anchor="w")
        tk.Label(inner, text="STREAK", bg=SURFACE, fg=MUTED, font=(FONT, 8, "bold")).pack(anchor="w", pady=(8, 0))
        self.side_streak = tk.Label(inner, text="0 days", bg=SURFACE, fg=GOOD, font=(FONT, 12, "bold"))
        self.side_streak.pack(anchor="w")

        self.content = tk.Frame(self.root, bg=BG)
        self.content.pack(side="left", fill="both", expand=True, padx=30, pady=24)
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
        for page in self.pages.values():
            page.pack_forget()
        self.pages[name].pack(fill="both", expand=True)
        self.current_page = name
        for k, (row, bar, lab) in self.nav.items():
            active = k == name
            row.config(bg=SURFACE if active else SIDEBAR)
            lab.config(bg=SURFACE if active else SIDEBAR, fg=TEXT if active else MUTED)
            bar.config(bg=(VIOLET if k == "lecture" else ACC) if active else SIDEBAR)
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

    # ---------- PDFs (separate list for each mode)
    def pdf_paths(self, mode=None):
        mode = mode or self.mode
        return [p for p in self.data["settings"][mode]["pdfs"] if os.path.exists(p)]

    def add_pdfs(self, mode):
        paths = filedialog.askopenfilenames(title="Choose PDF files", filetypes=[("PDF files", "*.pdf")])
        if not paths:
            return
        lst = self.data["settings"][mode]["pdfs"]
        for p in paths:
            p = os.path.normpath(p)
            if p not in lst:
                lst.append(p)
        save_data(self.data)
        self.refresh_pdf_list()

    def remove_pdf(self, mode, listbox):
        sel = listbox.curselection()
        if not sel:
            messagebox.showinfo(APP_NAME, "Select a PDF in the list first.")
            return
        del self.data["settings"][mode]["pdfs"][sel[0]]
        save_data(self.data)
        self.refresh_pdf_list()

    def refresh_pdf_list(self):
        for mode, view in self.views.items():
            lb = view.lb_pdf
            lb.delete(0, "end")
            for p in self.data["settings"][mode]["pdfs"]:
                flag = "" if os.path.exists(p) else "   (file not found)"
                lb.insert("end", f"  {os.path.basename(p)}{flag}")

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
            for d in self.data["doubts"][-6:]:
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

    def on_update_click(self):
        u = self.updater
        if u.state == "available":
            if self.phase != "idle":
                messagebox.showinfo(APP_NAME, "Please finish or end the current session before updating.")
                return
            if messagebox.askyesno("Update StudyLock", "Download and install the new version now?\n"
                                   "Your data and settings are kept."):
                u.install()
        elif u.state == "ready":
            self.restart_app()
        elif u.state in ("idle", "uptodate", "error", "unavailable"):
            u.check()

    def update_ui(self):
        u = self.updater
        ver = BUILD_ID[:7] if BUILD_ID != "dev" else "dev"
        lbl, btn, kind, en = f"Version {ver}", "Check for updates", "secondary", True
        if u.state == "checking":
            lbl, btn, en = "Checking for updates...", "Please wait", False
        elif u.state == "uptodate":
            lbl, btn = f"Up to date ({ver})", "Check again"
        elif u.state == "available":
            lbl, btn, kind = "A new version is available.", "Update now", "success"
        elif u.state == "downloading":
            lbl, btn, en = f"Downloading update... {u.pct}%", "Downloading", False
        elif u.state == "ready":
            lbl, btn, kind = "Update installed.", "Restart now", "primary"
        elif u.state in ("error", "unavailable"):
            lbl, btn = u.msg[:70], "Retry" if u.state == "error" else "OK"
            en = u.state == "error"
        self.upd_lbl.config(text=lbl)
        self.upd_btn.set_text(btn)
        self.upd_btn.set_kind(kind)
        self.upd_btn.enable(en)
        u.dirty = False

    def restart_app(self):
        exe = sys.executable
        self.stop_player()
        self.blocker.stop()
        self.audio.close()
        save_data(self.data)
        env = dict(os.environ)
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        try:
            subprocess.Popen([exe], env=env, close_fds=True,
                             creationflags=(0x00000008 | 0x00000200) if IS_WIN else 0)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Could not restart automatically: {e}\nPlease start StudyLock again.")
        self.root.destroy()

    def drain_cmds(self):
        while True:
            try:
                c = self.q_cmds.get_nowait()
            except queue.Empty:
                break
            if c == "pause":
                self.pause_session()
            elif c == "resume":
                self.resume_session()
            elif isinstance(c, tuple) and c[0] == "audio":
                if c[1] == "toggle" and self.audio.loaded:
                    if self.audio.playing:
                        self.audio.pause()
                    else:
                        self.audio.start()
                    self.sync_audio_buttons()
                elif c[1] == "volume" and c[2] is not None:
                    self.on_volume(c[2])
            elif isinstance(c, tuple) and c[0] == "speed":
                try:
                    self.data["settings"]["lecture"]["speed"] = max(0.5, min(3.0, round(float(c[1]), 1)))
                    save_data(self.data)
                except Exception:
                    pass
            elif isinstance(c, tuple) and c[0] == "keys":
                if isinstance(c[1], dict):
                    self.data["settings"]["keys"] = {str(k): str(v) for k, v in c[1].items()}
                    save_data(self.data)
            elif isinstance(c, tuple) and c[0] == "layout":
                try:
                    lec = self.data["settings"]["lecture"]
                    lec["split"] = max(20.0, min(80.0, float(c[1])))
                    lec["swap"] = bool(c[2])
                    save_data(self.data)
                except Exception:
                    pass

    # ---------- session control
    def start_session(self, mode):
        if self.phase != "idle":
            return
        v = self.views[mode]
        study, brk, cyc = v.s_study.get(), v.s_break.get(), v.s_cycles.get()
        self.lec_active = False
        self.player_kind = None
        self.pdf_list = self.pdf_paths(mode)
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
            self.player_kind = "room"
            extra = "A YouTube lecture will play full screen. You can pause, resume and seek.\n"
            if self.pdf_list:
                extra += f"{len(self.pdf_list)} PDF file(s) will open beside the video (split screen).\n"
            extra += "\n"
        elif self.pdf_list:
            self.player_kind = "room"
            extra = (f"{len(self.pdf_list)} PDF file(s) will open in a full-screen reader "
                     "(scroll, highlight, draw). Edits are saved into the original files.\n\n")
        if not messagebox.askyesno(
                "Start focus session",
                f"Your screen will be locked for {study} minutes (strict mode: no exit before the timer ends).\n"
                f"Then a {brk} minute break. {cyc} session(s) in total.\n\n{extra}"
                "Prayer break pauses the timer, video and audio at any time (the screen stays locked).\n"
                "Emergency exit: Ctrl+Alt+Del or Ctrl+Shift+Esc, open Task Manager and end StudyLock.\n\n"
                "Start now?"):
            self.lec_active = False
            self.player_kind = None
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

    def enter_study_screen(self):
        self.open_lock()
        if self.player_kind:
            self.player_fail = 0
            self.launch_player()
        if self.lec_active:
            self.audio.pause()
            self.sync_audio_buttons()
        elif self.mode == "self" and self.audio.loaded and not self.audio.playing:
            self.audio.start()
            self.sync_audio_buttons()
        self.root.withdraw()

    def start_study(self):
        self.phase = "study"
        self.phase_total = self.study_min * 60
        self.phase_end = time.time() + self.phase_total
        self.enter_study_screen()
        self.beep()

    def show_main(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(1500, lambda: self.root.attributes("-topmost", False))
        self.show_page(self.mode)

    def finish_study(self):
        self.leave_study_screen()
        save_data(self.data)
        self.phase = "break"
        self.phase_total = self.break_min * 60
        self.phase_end = time.time() + self.phase_total
        self.show_main()
        self.views["lecture"].update_lec_label()
        self.beep()
        self.refresh_progress()

    def leave_study_screen(self):
        self.stop_player()
        self.close_lock()
        self.audio.pause()
        self.sync_audio_buttons()

    def pause_session(self):
        """Prayer break: freeze timer, study counting, audio and video. The screen stays locked."""
        if self.phase not in ("study", "break"):
            return
        now = time.time()
        self.paused_rem = max(1.0, self.phase_end - now)
        self.paused_at = now
        self.resume_phase = self.phase
        self.phase = "paused"
        self.audio_was_playing = self.audio.playing
        if self.audio.playing:
            self.audio.pause()
            self.sync_audio_buttons()
        self.sync_prayer_btn()
        save_data(self.data)

    def resume_session(self):
        if self.phase != "paused":
            return
        now = time.time()
        self.phase = self.resume_phase
        self.phase_end = now + self.paused_rem
        self.last_tick = now
        if self.audio_was_playing and self.audio.loaded and not self.lec_active:
            self.audio.start()
        self.audio_was_playing = False
        self.sync_audio_buttons()
        self.sync_prayer_btn()
        self.beep()

    def toggle_pause(self):
        if self.phase == "paused":
            self.resume_session()
        else:
            self.pause_session()

    def sync_prayer_btn(self):
        if self.lock and hasattr(self, "l_prayer"):
            paused = self.phase == "paused"
            self.l_prayer.set_text("Resume" if paused else "Prayer break")
            self.l_prayer.set_kind("success" if paused else "warn")

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
        if self.phase in ("break", "paused") and messagebox.askyesno(APP_NAME, "End the session now?"):
            self.end_all("Session ended.")

    def end_all(self, msg):
        self.phase = "idle"
        self.lec_active = False
        self.player_kind = None
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
        a = self.audio
        lec = self.data["settings"]["lecture"]
        return {"video_id": self.lec_vid, "has_video": bool(self.lec_active), "mode": self.mode,
                "start": self.lec_start, "split": lec.get("split", 55), "swap": lec.get("swap", False),
                "speed": lec.get("speed", 1.0), "keys": self.data["settings"].get("keys", {}),
                "phase": self.phase, "phase_end": self.phase_end * 1000,
                "paused_rem": self.paused_rem, "phase_total": self.phase_total,
                "cycle": self.cycle, "cycles": self.cycles,
                "doubts": len(self.data["doubts"]),
                "doubt_list": [d["t"] for d in self.data["doubts"][-8:]],
                "pdfs": [os.path.basename(p) for p in self.pdf_list],
                "audio": {"loaded": bool(a.loaded and self.mode == "self"), "playing": bool(a.playing),
                          "volume": int(a.volume)}}

    def launch_player(self):
        if self.server is None:
            self.server = LectureServer(self)
        if self.lec_active:
            self.lec_start = self.lecture_pos
        cmd = [sys.executable]
        if not getattr(sys, "frozen", False):
            cmd.append(os.path.abspath(__file__))
        cmd += ["--player", self.server.url("/")]
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
        if self.server is not None:
            self.server.store.close_all()       # release the PDF files between sessions
        if self.lec_active and self.lec_vid and self.lecture_pos > 0:
            self.data["settings"]["lecture"]["pos"][self.lec_vid] = max(0, self.lecture_pos - 3)
            save_data(self.data)

    def retry_player(self):
        self.player_fail = 0
        self.player_started = 0
        if self.lock and hasattr(self, "l_msg"):
            self.l_msg.config(text="Opening the player\u2026 (2\u20135 seconds)")

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
                self.l_msg.config(text="The player could not start (Edge WebView2 missing?). "
                                       f"Press Retry. Log: {PLAYER_LOG}")
        return self.player is not None

    # ---------- lock screen
    def _prayer_btn(self, parent):
        paused = self.phase == "paused"
        self.l_prayer = Btn(parent, "Resume" if paused else "Prayer break",
                            lambda: self.root.after(30, self.toggle_pause), "success" if paused else "warn")
        return self.l_prayer

    def _lock_doubt_box(self, parent, accent, bg, entry_width, list_height, expand=False):
        self.lv_doubt = tk.StringVar()
        self.l_entry = tk.Entry(parent, textvariable=self.lv_doubt, width=entry_width, bg=SURFACE2 if bg == SURFACE else SURFACE,
                                fg=TEXT, insertbackground=TEXT, relief="flat", font=(FONT, 12),
                                highlightthickness=1, highlightbackground=BORDER, highlightcolor=accent)
        self.l_entry.bind("<Return>", lambda e: self.add_doubt(self.lv_doubt))
        return self.l_entry

    def _lock_audio_row(self, parent, bg):
        arow = tk.Frame(parent, bg=bg)
        self.l_play = Btn(arow, "Play", self.toggle_audio, "secondary", size=9, padx=14, pady=6)
        self.l_play.pack(side="left")
        self.l_vol = tk.Scale(arow, from_=0, to=100, orient="horizontal", showvalue=False,
                              bg=bg, troughcolor=SURFACE2, highlightthickness=0, bd=0,
                              sliderrelief="flat", width=12, sliderlength=20, activebackground=ACC, length=150)
        self.l_vol.set(self.audio.volume)
        self.l_vol.config(command=self.on_volume)
        self.l_vol.pack(side="left", padx=(10, 0), fill="x", expand=True)
        return arow

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

        self._lock_layout_center(w, accent)

        self.refresh_doubts()
        self.sync_audio_buttons()
        w.update_idletasks()
        w.focus_force()
        self.l_entry.focus_set()

    def _lock_layout_center(self, w, accent):
        box = tk.Frame(w, bg=LOCK_BG)
        box.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(box, text="SELF STUDY" if self.mode == "self" else "LECTURE", bg=LOCK_BG, fg=accent,
                 font=(FONT, 13, "bold")).pack(pady=(0, 10))
        self.l_ring = Ring(box, size=300, thick=14, bg=LOCK_BG, color=accent)
        self.l_ring.pack()
        if self.player_kind:
            msg = "Opening your study room\u2026 (2\u20135 seconds)"
        else:
            msg = "Stay focused. Write doubts below and clear them in your break."
        self.l_msg = tk.Label(box, text=msg, bg=LOCK_BG, fg=MUTED, font=(FONT, 13), wraplength=620)
        self.l_msg.pack(pady=(14, 18))

        row = tk.Frame(box, bg=LOCK_BG)
        row.pack()
        self._lock_doubt_box(row, accent, LOCK_BG, 46, 4)
        self.l_entry.config(font=(FONT, 13))
        self.l_entry.pack(side="left", ipady=8, padx=(0, 8))
        Btn(row, "Save doubt", lambda: self.add_doubt(self.lv_doubt),
            "primary" if self.mode == "self" else "violet", size=11, pady=9).pack(side="left")
        self.l_count = tk.Label(box, bg=LOCK_BG, fg=MUTED, font=(FONT, 10))
        self.l_count.pack(pady=(10, 4))
        self.l_lb = make_listbox(box, height=4)
        self.l_lb.config(width=66, bg=SURFACE)
        self.l_lb.pack()

        bottom = tk.Frame(box, bg=LOCK_BG)
        bottom.pack(pady=(20, 0))
        if self.mode == "self":
            arow = self._lock_audio_row(bottom, LOCK_BG)
            arow.pack(side="left", padx=(0, 20))
            name = os.path.basename(self.audio.path) if self.audio.loaded else "No audio selected"
            tk.Label(bottom, text=name, bg=LOCK_BG, fg=MUTED, font=(FONT, 10)).pack(side="left", padx=(0, 20))
        if self.player_kind:
            Btn(bottom, "Retry player", self.retry_player, "secondary").pack(side="left", padx=(0, 12))
        self._prayer_btn(bottom).pack(side="left")

    def close_lock(self):
        self.blocker.stop()
        if self.lock:
            try:
                self.lock.destroy()
            except Exception:
                pass
        self.lock = None
        for a in LOCK_ATTRS:
            self.__dict__.pop(a, None)

    # ---------- main loop
    def maintain_lock(self, now, rem, paused):
        owns = self.lecture_tick(now) if self.player_kind else False
        if not self.lock:
            return
        frac = 1 - rem / max(1, self.phase_total)
        color = WARN if paused else (ACC if self.mode == "self" else VIOLET)
        self.l_ring.set(frac, ms(rem), "PAUSED" if paused else f"SESSION {self.cycle} OF {self.cycles}", color)
        if not owns:   # when the player window is on screen, do not cover it
            self.lock.lift()
            self.lock.attributes("-topmost", True)
            if self.lock.focus_displayof() is None:
                self.lock.focus_force()
                self.l_entry.focus_set()

    def tick(self):
        now = time.time()
        d = now - self.last_tick
        self.last_tick = now
        if d > 2:          # laptop slept / app hung: do not count that time as study
            d = 0
        self.drain_doubts()
        self.drain_cmds()
        if self.updater.dirty:
            self.update_ui()

        if self.phase == "study":
            self.add_study(d)
            rem = self.phase_end - now
            self.maintain_lock(now, rem, False)
            if rem <= 0:
                self.finish_study()
            elif now - self.last_save > 20:
                save_data(self.data)
                self.last_save = now
                self.refresh_progress()
        elif self.phase == "paused":
            if self.resume_phase == "study":
                self.maintain_lock(now, self.paused_rem, True)
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
        if self.phase in ("break", "paused") and not messagebox.askyesno(APP_NAME, "A session is running. Quit anyway?"):
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
