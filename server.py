#!/usr/bin/env python3
"""השרת המקומי של התוכנה: מגיש את הממשק (ui/) ומריץ הורדות עם yt-dlp + ffmpeg.

  * התוכנה המותקנת / הניידת:  app.py מפעיל את השרת ומציג את הממשק בחלון.
  * מפתחים:                   python server.py   ← http://127.0.0.1:<פורט>

השרת מאזין ל-127.0.0.1 בלבד, ומקבל בקשות רק מהממשק שהוא עצמו מגיש (בדיקות Host/Origin).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

VERSION = "2.0.0"
APP_ID = "yt-playlist-zip"
SITE_URL = "https://hershale.github.io/download-youtube-playlists/"

FROZEN = bool(getattr(sys, "frozen", False))
IS_WIN = os.name == "nt"
EXE = ".exe" if IS_WIN else ""
NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW

APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
RES_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
WEB = RES_DIR / "ui"
BIN_DIR = Path(os.environ.get("YTZIP_BIN") or (APP_DIR / "bin"))
WORK = Path(tempfile.gettempdir()) / "yt-playlist-zip"


def _data_dir():
    env = os.environ.get("YTZIP_DATA")
    if env:
        return Path(env)
    if (APP_DIR / "portable.txt").is_file():  # גרסה ניידת: הכול נשמר ליד התוכנה
        return APP_DIR / "data"
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) if base else Path.home() / ".local" / "share") / "YTPlaylistDownloader"


DATA_DIR = _data_dir()
PORTABLE = (APP_DIR / "portable.txt").is_file()
LOG_FILE = None

YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
VIDEO_HEIGHTS = {"best", "2160", "1440", "1080", "720", "480", "360"}
AUDIO_QUALITY = {"best": "0", "320": "320K", "192": "192K", "128": "128K"}
MODES = {"both", "video", "audio"}
THUMB_PREFIX = "https://i.ytimg.com/"

DEFAULT_SETTINGS = {
    "theme": "system", "accent": "violet", "animations": True,
    "out_dir": "", "save_as_zip": True, "number_files": True, "open_when_done": False,
    "default_mode": "both", "default_video_quality": "1080", "default_audio_quality": "best",
    "audio_format": "mp3", "video_container": "mp4", "prefer_h264": True, "embed_meta": True,
    "speed_limit_kb": 0, "fragments": 4, "cookies_browser": "", "auto_update": True,
}
CHOICES = {
    "theme": {"system", "light", "dark"},
    "accent": {"violet", "blue", "teal", "rose", "amber"},
    "default_mode": MODES,
    "default_video_quality": VIDEO_HEIGHTS,
    "default_audio_quality": set(AUDIO_QUALITY),
    "audio_format": {"mp3", "m4a", "opus", "flac"},
    "video_container": {"mp4", "mkv"},
    "cookies_browser": {"", "chrome", "edge", "firefox", "brave"},
}
INT_RANGES = {"speed_limit_kb": (0, 1_000_000), "fragments": (1, 16)}

SETTINGS = dict(DEFAULT_SETTINGS)
HISTORY = []
JOBS = {}
LOCK = threading.Lock()
SETTINGS_LOCK = threading.Lock()
UPDATE_LOCK = threading.Lock()
JOB_SEM = threading.Semaphore(1)  # הורדה אחת בכל פעם; השאר ממתינות בתור
STATE = {"updating": False, "update_note": "", "ytdlp_version": None, "cleanup_started": False,
         "shell": "browser", "last_seen": time.time(), "quit": False}


# ---------------------------------------------------------------- כלים וסביבה

def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    try:
        if LOG_FILE:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        elif sys.stderr:
            print(line, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def find_tool(name):
    """מחפש כלי קודם בתיקיית bin של התוכנה, ורק אחר כך ב-PATH."""
    p = BIN_DIR / (name + EXE)
    if p.is_file():
        return str(p)
    return shutil.which(name)


def has_ytdlp():
    if find_tool("yt-dlp"):
        return True
    if FROZEN:
        return False
    import importlib.util
    return importlib.util.find_spec("yt_dlp") is not None


def ytdlp_cmd():
    exe = find_tool("yt-dlp")
    if exe:
        return [exe]
    if not FROZEN:
        return [sys.executable, "-m", "yt_dlp"]
    raise RuntimeError("רכיב yt-dlp לא נמצא בתיקיית ההתקנה. התקינו מחדש את התוכנה.")


def common_args():
    args = []
    if (BIN_DIR / ("ffmpeg" + EXE)).is_file():
        args += ["--ffmpeg-location", str(BIN_DIR)]
    deno = BIN_DIR / ("deno" + EXE)
    if deno.is_file():  # נדרש ע"י yt-dlp לפענוח אתגרי יוטיוב
        args += ["--js-runtimes", f"deno:{deno}"]
    return args


def tool_env():
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if BIN_DIR.is_dir():
        env["PATH"] = str(BIN_DIR) + os.pathsep + env.get("PATH", "")
    return env


def run_kw():
    kw = {"stdin": subprocess.DEVNULL, "env": tool_env()}
    if IS_WIN:
        kw["creationflags"] = NO_WINDOW
    return kw


def kill_tree(proc):
    """עוצר תהליך יחד עם כל תהליכי הבן שלו (למשל ffmpeg)."""
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=NO_WINDOW, timeout=15)
        else:
            proc.terminate()
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def downloads_dir():
    if IS_WIN:
        try:
            import ctypes

            class GUID(ctypes.Structure):
                _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort),
                            ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]

            guid = GUID(0x374DE290, 0x123F, 0x4565,
                        (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
            ptr = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(ptr)) == 0:
                path = ptr.value
                ctypes.windll.ole32.CoTaskMemFree(ptr)
                if path:
                    return Path(path)
        except Exception:  # noqa: BLE001
            pass
    return Path.home() / "Downloads"


def default_out_dir():
    return downloads_dir() / "YouTube Playlists"


def out_dir():
    env = os.environ.get("YTZIP_OUTDIR")
    if env:
        return Path(env)
    custom = SETTINGS.get("out_dir") or ""
    return Path(custom) if custom else default_out_dir()


def unique_path(folder, name):
    p = folder / name
    i = 2
    while p.exists():
        p = folder / f"{Path(name).stem} ({i}){Path(name).suffix}"
        i += 1
    return p


def open_folder(path):
    try:
        path.mkdir(parents=True, exist_ok=True)
        if IS_WIN:
            os.startfile(str(path))  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:  # noqa: BLE001
        log(f"open_folder failed: {e}")
        return False


def reveal(path):
    """פותח את התיקייה וממקד בקובץ (ב-Windows: Explorer /select)."""
    try:
        if path.is_dir():
            return open_folder(path)
        if IS_WIN:
            subprocess.Popen(["explorer", f"/select,{path}"])
            return True
        return open_folder(path.parent)
    except Exception as e:  # noqa: BLE001
        log(f"reveal failed: {e}")
        return False


def valid_url(url):
    try:
        p = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    return p.scheme in ("http", "https") and (p.hostname or "").lower() in YT_HOSTS


def safe_name(name, fallback="playlist"):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name or "").strip(" .")
    return name[:100] or fallback


def write_json_atomic(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- הגדרות והיסטוריה

def sanitize_settings(patch):
    """מחזיר (ערכים תקינים, הודעת שגיאה או None). שדות לא מוכרים מתעלמים מהם."""
    clean = {}
    for key, val in (patch or {}).items():
        if key not in DEFAULT_SETTINGS:
            continue
        default = DEFAULT_SETTINGS[key]
        if isinstance(default, bool):
            if not isinstance(val, bool):
                return None, f"ערך לא תקין עבור {key}"
        elif key in INT_RANGES:
            lo, hi = INT_RANGES[key]
            if isinstance(val, bool) or not isinstance(val, int) or not lo <= val <= hi:
                return None, f"{key} חייב להיות בין {lo} ל-{hi}"
        elif key == "out_dir":
            if not isinstance(val, str) or len(val) > 500:
                return None, "נתיב לא תקין"
            val = val.strip()
            if val:
                p = Path(val)
                if not p.is_absolute():
                    return None, "יש להזין נתיב מלא (למשל C:\\Users\\...\\Downloads)"
                try:
                    p.mkdir(parents=True, exist_ok=True)
                    probe = p / ".write-test"
                    probe.write_text("x")
                    probe.unlink()
                except OSError:
                    return None, "לא ניתן לכתוב לתיקייה הזאת. בחרו תיקייה אחרת."
        elif key in CHOICES:
            if val not in CHOICES[key]:
                return None, f"ערך לא תקין עבור {key}"
        clean[key] = val
    return clean, None


def load_state_files():
    global LOG_FILE
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        lp = DATA_DIR / "server.log"
        if lp.exists() and lp.stat().st_size > 1_000_000:
            lp.unlink()
        LOG_FILE = str(lp)
    except OSError:
        pass
    try:
        data = json.loads((DATA_DIR / "settings.json").read_text(encoding="utf-8"))
        for k, v in data.items():  # כל שדה נבדק בנפרד, כדי שערך פגום לא יפיל את השאר
            ok, err = sanitize_settings({k: v})
            if not err and ok:
                SETTINGS.update(ok)
    except (OSError, ValueError):
        pass
    try:
        data = json.loads((DATA_DIR / "history.json").read_text(encoding="utf-8"))
        if isinstance(data, list):
            HISTORY[:] = [h for h in data if isinstance(h, dict) and h.get("id") and h.get("path")][:100]
    except (OSError, ValueError):
        pass


def save_settings():
    try:
        write_json_atomic(DATA_DIR / "settings.json", SETTINGS)
    except OSError as e:
        log(f"cannot save settings: {e}")


def save_history():
    try:
        write_json_atomic(DATA_DIR / "history.json", HISTORY)
    except OSError as e:
        log(f"cannot save history: {e}")


def history_public():
    out = []
    for h in HISTORY:
        d = dict(h)
        d["exists"] = Path(h["path"]).exists()
        out.append(d)
    return out


# ---------------------------------------------------------------- yt-dlp

def ytdlp_version():
    if STATE["ytdlp_version"]:
        return STATE["ytdlp_version"]
    try:
        r = subprocess.run(ytdlp_cmd() + ["--version"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=25, **run_kw())
        STATE["ytdlp_version"] = r.stdout.strip() or None
    except Exception:  # noqa: BLE001
        STATE["ytdlp_version"] = None
    return STATE["ytdlp_version"]


def update_ytdlp():
    """מעדכן את yt-dlp המצורף (קובץ exe עצמאי). לא רץ בזמן הורדה."""
    exe = find_tool("yt-dlp")
    if not exe or Path(exe).resolve().parent != BIN_DIR.resolve():
        return {"ok": False, "message": "עדכון אוטומטי זמין רק בגרסה המותקנת או הניידת."}
    with LOCK:
        busy = any(j["status"] in ("queued", "running", "zipping") for j in JOBS.values())
    if busy:
        return {"ok": False, "message": "יש הורדה פעילה. נסו שוב בסיומה."}
    if not UPDATE_LOCK.acquire(blocking=False):
        return {"ok": False, "message": "עדכון כבר מתבצע."}
    STATE["updating"] = True
    try:
        before = ytdlp_version()
        r = subprocess.run([exe, "-U"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=240, **run_kw())
        text = ((r.stdout or "") + (r.stderr or "")).strip()
        STATE["ytdlp_version"] = None
        after = ytdlp_version()
        log(f"yt-dlp -U rc={r.returncode}: {text[-300:]}")
        if r.returncode != 0:
            return {"ok": False, "message": "העדכון נכשל. בדקו חיבור לאינטרנט.", "version": after}
        msg = f"הרכיב עודכן לגרסה {after}." if after and after != before else "הרכיב כבר מעודכן."
        STATE["update_note"] = msg if after != before else ""
        return {"ok": True, "message": msg, "version": after}
    except Exception as e:  # noqa: BLE001
        log(f"update failed: {e}")
        return {"ok": False, "message": f"העדכון נכשל: {e}"}
    finally:
        STATE["updating"] = False
        UPDATE_LOCK.release()


def get_info(url):
    cmd = ytdlp_cmd() + common_args() + ["--flat-playlist", "-J", "--no-warnings", "--yes-playlist"]
    if SETTINGS["cookies_browser"]:
        cmd += ["--cookies-from-browser", SETTINGS["cookies_browser"]]
    cmd += ["--", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120, **run_kw())
    except subprocess.TimeoutExpired:
        raise RuntimeError("הבדיקה ארכה יותר מדי. נסו שוב.") from None
    if r.returncode != 0:
        lines = (r.stderr or "").strip().splitlines()
        raise RuntimeError(lines[-1] if lines else "שגיאה לא ידועה")
    data = json.loads(r.stdout)
    entries = [e for e in (data.get("entries") or []) if e]
    if not entries:
        entries = [data]
    items, total = [], 0
    for n, e in enumerate(entries[:1000], 1):
        dur = e.get("duration") if isinstance(e.get("duration"), (int, float)) else None
        total += dur or 0
        vid = e.get("id") if isinstance(e.get("id"), str) else ""
        items.append({"i": n, "title": e.get("title") or "", "dur": dur,
                      "thumb": f"{THUMB_PREFIX}vi/{vid}/mqdefault.jpg" if re.fullmatch(r"[\w-]{6,20}", vid) else ""})
    return {
        "title": data.get("title") or (items[0]["title"] if items else "וידאו"),
        "uploader": data.get("uploader") or data.get("channel") or "",
        "count": len(entries), "duration": int(total),
        "thumb": next((i["thumb"] for i in items if i["thumb"]), ""),
        "items": items, "is_playlist": data.get("_type") == "playlist",
    }


def build_cmd(job, outdir):
    cfg, mode, quality = job["cfg"], job["mode"], job["quality"]
    tmpl = "%(playlist_index&{} - |)s%(title).100B.%(ext)s" if cfg["number_files"] else "%(title).100B.%(ext)s"
    cmd = ytdlp_cmd() + common_args() + [
        "--newline", "--no-colors", "--ignore-errors", "--yes-playlist",
        "--windows-filenames", "--no-mtime", "--concurrent-fragments", str(cfg["fragments"]),
        "-o", str(outdir / tmpl),
    ]
    if cfg["speed_limit_kb"]:
        cmd += ["--limit-rate", f"{cfg['speed_limit_kb']}K"]
    if cfg["cookies_browser"]:
        cmd += ["--cookies-from-browser", cfg["cookies_browser"]]
    if job["items"]:
        cmd += ["--playlist-items", job["items"]]
    if mode == "audio":
        cmd += ["-f", "ba/b", "-x", "--audio-format", cfg["audio_format"],
                "--audio-quality", AUDIO_QUALITY[quality]]
        if cfg["embed_meta"]:
            cmd += ["--embed-metadata", "--embed-thumbnail"]
    else:
        h = "" if quality == "best" else f"[height<={quality}]"
        if mode == "both":
            cmd += ["-f", f"bv*{h}+ba/b{h}/b", "--merge-output-format", cfg["video_container"]]
        else:
            cmd += ["-f", f"bv{h}/b{h}", "--remux-video", cfg["video_container"]]
        if cfg["prefer_h264"] and cfg["video_container"] == "mp4":
            cmd += ["-S", "res,vcodec:h264,acodec:m4a"]  # רזולוציה קודם, ואז תאימות רחבה
        if cfg["embed_meta"]:
            cmd += ["--embed-metadata"]
    return cmd + ["--", job["url"]]


def finish_files(job, files, workdir):
    """שומר את הקבצים שירדו: ZIP או תיקייה. מחזיר (נתיב, סוג, האם בתיקיית היעד)."""
    cfg = job["cfg"]
    base = safe_name(job["title"])
    try:
        folder = out_dir()
        folder.mkdir(parents=True, exist_ok=True)
        saved = True
    except OSError as e:
        log(f"output dir unavailable, using temp: {e}")
        folder, saved = workdir, False
    if cfg["save_as_zip"]:
        dest = unique_path(folder, base + ".zip")
        tmp = dest.with_name(dest.name + ".part")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            for f in files:
                z.write(f, f.name)
        os.replace(tmp, dest)
        return dest, "zip", saved
    dest = unique_path(folder, base)
    dest.mkdir(parents=True)
    for f in files:
        shutil.move(str(f), str(dest / f.name))
    return dest, "folder", saved


def run_job(job):
    workdir = WORK / job["id"]
    outdir = workdir / "files"
    keep_workdir = False
    JOB_SEM.acquire()
    try:
        if job["status"] == "cancelled":
            return
        job["status"] = "running"
        outdir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            build_cmd(job, outdir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            bufsize=1, encoding="utf-8", errors="replace", **run_kw(),
        )
        job["proc"] = proc
        nstreams = 2 if job["mode"] == "both" else 1
        stream = 0
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            job["log"].append(line)
            del job["log"][:-200]
            m = re.search(r"Downloading playlist: (.+)", line)
            if m and not job["title_fixed"]:
                job["title"] = m.group(1)
            m = re.search(r"Downloading item (\d+) of (\d+)", line)
            if m:
                job["index"], job["total"] = int(m.group(1)), int(m.group(2))
                stream, job["item_pct"] = 0, 0.0
            m = re.search(r"Destination: (.+)", line)
            if m:
                stream += 1
                job["current"] = re.sub(r"\.f\d+$", "", Path(m.group(1)).stem)
                job["item_pct"] = 0.0
            m = re.search(r"\[download\]\s+([\d.]+)%", line)
            if m:
                job["item_pct"] = float(m.group(1))
                m2 = re.search(r"at\s+([\d.]+\s*[KMG]?i?B/s)", line)
                job["speed"] = m2.group(1) if m2 else job["speed"]
                m2 = re.search(r"ETA\s+([\d:]+)", line)
                job["eta"] = m2.group(1) if m2 else job["eta"]
            if "ERROR" in line:
                job["errors"] += 1
            frac = min(((max(stream, 1) - 1) + job["item_pct"] / 100) / nstreams, 1.0)
            job["progress"] = min(((job["index"] - 1) + frac) / max(job["total"], 1), 0.99)
        proc.wait()
        job["speed"] = job["eta"] = ""
        if job["status"] == "cancelled":
            return
        files = sorted(p for p in outdir.iterdir()
                       if p.is_file() and not p.name.endswith((".part", ".ytdl", ".temp", ".webp", ".jpg", ".png")))
        if not files:
            raise RuntimeError("לא ירדו קבצים. בדקו את הקישור, אם הפלייליסט פרטי, או את הלוג.")
        job["status"] = "zipping"
        job["progress"] = 0.99
        dest, kind, saved = finish_files(job, files, workdir)
        keep_workdir = not saved
        size = sum(f.stat().st_size for f in (dest.iterdir() if kind == "folder" else [dest]))
        job.update(status="done", progress=1.0, files=len(files), kind=kind,
                   saved_path=str(dest) if saved else None, size=size)
        if saved:
            with LOCK:
                HISTORY.insert(0, {"id": job["id"], "title": job["title"], "files": len(files),
                                   "path": str(dest), "kind": kind, "mode": job["mode"],
                                   "quality": job["quality"], "size": size, "time": int(time.time()),
                                   "thumb": job["thumb"]})
                del HISTORY[100:]
                save_history()
            if job["cfg"]["open_when_done"]:
                reveal(dest)
    except Exception as e:  # noqa: BLE001
        if job["status"] != "cancelled":
            log(f"job {job['id']} failed: {e}")
            job.update(status="error", error=str(e))
    finally:
        job["proc"] = None
        JOB_SEM.release()
        shutil.rmtree(outdir, ignore_errors=True)
        if not keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def cleanup_loop():
    while True:
        time.sleep(600)
        cutoff = time.time() - 6 * 3600
        try:
            with LOCK:
                old = [k for k, j in JOBS.items()
                       if j["created"] < cutoff and j["status"] in ("done", "error", "cancelled")]
                for k in old:
                    JOBS.pop(k, None)
                    shutil.rmtree(WORK / k, ignore_errors=True)
        except Exception as e:  # noqa: BLE001
            log(f"cleanup error: {e}")


def public(job):
    keys = ("id", "status", "progress", "title", "index", "total", "current", "speed", "eta",
            "error", "errors", "files", "saved_path", "kind", "size", "mode", "quality", "thumb")
    d = {k: job.get(k) for k in keys}
    d["log"] = job["log"][-12:]
    return d


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = f"YTPlaylistZip/{VERSION}"

    def _port(self):
        return self.server.server_address[1]

    def _origin_allowed(self, origin):
        p = self._port()
        return origin in (f"http://127.0.0.1:{p}", f"http://localhost:{p}")

    def _guard(self, write):
        """Host (נגד DNS rebinding), Origin (נגד CSRF) וסוג תוכן."""
        p = self._port()
        host = (self.headers.get("Host") or "").lower()
        if host not in (f"127.0.0.1:{p}", f"localhost:{p}"):
            self.send_json({"error": "Host לא מורשה"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin is not None and not self._origin_allowed(origin):
            self.send_json({"error": "מקור לא מורשה"}, 403)
            return False
        if write:
            if origin is None:
                self.send_json({"error": "נדרש Origin"}, 403)
                return False
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self.send_json({"error": "Content-Type לא נתמך"}, 415)
                return False
        STATE["last_seen"] = time.time()
        return True

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            n = max(0, min(int(self.headers.get("Content-Length") or 0), 65536))
            data = json.loads(self.rfile.read(n) or b"{}")
            return data if isinstance(data, dict) else {}
        except (ValueError, json.JSONDecodeError):
            return {}

    def _dispatch(self, fn):
        try:
            fn()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            pass
        except Exception as e:  # noqa: BLE001  - שגיאה בבקשה אחת לא מפילה את השרת
            log(f"request error {self.command} {self.path}: {e!r}")
            try:
                self.send_json({"error": "שגיאה פנימית בשרת"}, 500)
            except Exception:  # noqa: BLE001
                pass

    def do_GET(self):
        self._dispatch(self._get)

    def do_POST(self):
        self._dispatch(self._post)

    def do_OPTIONS(self):  # אין CORS: הממשק והשרת באותו origin
        self._dispatch(lambda: self.send_json({"error": "לא נתמך"}, 405))

    def _get(self):
        if not self._guard(False):
            return
        path = urlparse(self.path).path
        if path == "/api/health":
            return self.send_json({
                "ok": True, "app": APP_ID, "version": VERSION, "shell": STATE["shell"],
                "portable": PORTABLE, "frozen": FROZEN, "data_dir": str(DATA_DIR),
                "ffmpeg": bool(find_tool("ffmpeg")), "ytdlp": has_ytdlp(),
                "ytdlp_version": ytdlp_version() if has_ytdlp() else None,
                "out_dir": str(out_dir()), "default_out_dir": str(default_out_dir()),
            })
        if path == "/api/settings":
            return self.send_json({"settings": SETTINGS, "defaults": DEFAULT_SETTINGS})
        if path == "/api/jobs":
            with LOCK:
                jobs = sorted(JOBS.values(), key=lambda j: j["created"], reverse=True)
                return self.send_json({"jobs": [public(j) for j in jobs], "updating": STATE["updating"],
                                       "note": STATE["update_note"]})
        m = re.fullmatch(r"/api/jobs/([0-9a-f]{12})", path)
        if m:
            job = JOBS.get(m.group(1))
            return self.send_json(public(job)) if job else self.send_json({"error": "משימה לא נמצאה"}, 404)
        if path == "/api/history":
            with LOCK:
                return self.send_json({"items": history_public()})
        self.serve_static(path)

    def serve_static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        f = (WEB / rel).resolve()
        if WEB.resolve() not in f.parents or not f.is_file():
            return self.send_json({"error": "לא נמצא"}, 404)
        ctype = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml",
                 ".png": "image/png", ".ico": "image/x-icon"}.get(f.suffix, "application/octet-stream")
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if f.suffix == ".html":
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data: https://i.ytimg.com; "
                             "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                             "connect-src 'self'; frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _post(self):
        if not self._guard(True):
            return
        path = urlparse(self.path).path
        body = self.read_json()
        if path == "/api/info":
            url = str(body.get("url", "")).strip()
            if not valid_url(url):
                return self.send_json({"error": "יש להזין קישור תקין של יוטיוב"}, 400)
            try:
                return self.send_json(get_info(url))
            except Exception as e:  # noqa: BLE001
                return self.send_json({"error": str(e)}, 400)
        if path == "/api/jobs":
            return self.create_job(body)
        m = re.fullmatch(r"/api/jobs/([0-9a-f]{12})/(cancel|dismiss)", path)
        if m and m.group(1) in JOBS:
            job = JOBS[m.group(1)]
            if m.group(2) == "cancel":
                if job["status"] in ("queued", "running", "zipping"):
                    job["status"] = "cancelled"
                    if job.get("proc"):
                        kill_tree(job["proc"])
            elif job["status"] in ("done", "error", "cancelled"):
                with LOCK:
                    JOBS.pop(job["id"], None)
            return self.send_json({"ok": True})
        if path == "/api/settings":
            clean, err = sanitize_settings(body)
            if err:
                return self.send_json({"error": err}, 400)
            with SETTINGS_LOCK:
                SETTINGS.update(clean)
                save_settings()
            return self.send_json({"settings": SETTINGS})
        if path == "/api/open-folder":
            return self.send_json({"ok": open_folder(out_dir()), "out_dir": str(out_dir())})
        if path == "/api/reveal":
            h = next((x for x in HISTORY if x["id"] == body.get("id")), None)
            ok = bool(h) and Path(h["path"]).exists() and reveal(Path(h["path"]))
            return self.send_json({"ok": ok})
        if path == "/api/history/remove":
            with LOCK:
                HISTORY[:] = [x for x in HISTORY if x["id"] != body.get("id")]
                save_history()
            return self.send_json({"ok": True})
        if path == "/api/history/clear":
            with LOCK:
                HISTORY.clear()
                save_history()
            return self.send_json({"ok": True})
        if path == "/api/update":
            return self.send_json(update_ytdlp())
        if path == "/api/quit":
            if STATE["shell"] != "browser":
                return self.send_json({"ok": False})
            STATE["quit"] = True
            return self.send_json({"ok": True})
        self.send_json({"error": "לא נמצא"}, 404)

    def create_job(self, body):
        url = str(body.get("url", "")).strip()
        mode, quality = body.get("mode", "both"), str(body.get("quality", "best"))
        allowed = AUDIO_QUALITY if mode == "audio" else VIDEO_HEIGHTS
        items = str(body.get("items", "") or "")
        if items and (len(items) > 4000 or not re.fullmatch(r"\d+(-\d+)?(,\d+(-\d+)?)*", items)):
            return self.send_json({"error": "בחירת פריטים לא תקינה"}, 400)
        if not valid_url(url) or mode not in MODES or quality not in allowed:
            return self.send_json({"error": "פרמטרים לא תקינים"}, 400)
        if STATE["updating"]:
            return self.send_json({"error": "מתבצע עדכון של רכיב ההורדה. נסו שוב בעוד רגע."}, 503)
        thumb = str(body.get("thumb", ""))
        title = str(body.get("title", ""))[:200].strip()
        job = {"id": uuid.uuid4().hex[:12], "url": url, "mode": mode, "quality": quality, "items": items,
               "cfg": dict(SETTINGS), "status": "queued", "progress": 0.0,
               "title": title or "playlist", "title_fixed": bool(title),
               "thumb": thumb if thumb.startswith(THUMB_PREFIX) else "",
               "index": 1, "total": 1, "item_pct": 0.0, "current": "", "speed": "", "eta": "",
               "errors": 0, "log": [], "created": time.time(), "proc": None}
        with LOCK:
            JOBS[job["id"]] = job
        threading.Thread(target=run_job, args=(job,), daemon=True).start()
        self.send_json({"id": job["id"]})

    def log_message(self, fmt, *args):
        pass


class _Server(ThreadingHTTPServer):
    # ב-Windows SO_REUSEADDR מאפשר ל-2 תהליכים להאזין לאותו פורט, לכן מכבים אותו.
    allow_reuse_address = not IS_WIN
    daemon_threads = True


class ServerApp:
    """עוטף את השרת: הפעלה, וכיבוי מסודר (עוצר הורדות ומנקה קבצים זמניים)."""

    def __init__(self, port=0):
        self.host = "127.0.0.1"
        self.port = port  # 0 = פורט פנוי שנבחר ע"י מערכת ההפעלה
        self.httpd = None
        self.thread = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/"

    def start(self):
        load_state_files()
        self.httpd = _Server((self.host, self.port), Handler)
        self.port = self.httpd.server_address[1]
        WORK.mkdir(parents=True, exist_ok=True)
        for child in WORK.iterdir():  # שאריות מריצה קודמת שקרסה
            shutil.rmtree(child, ignore_errors=True)
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={"poll_interval": 0.3}, daemon=True)
        self.thread.start()
        if not STATE["cleanup_started"]:
            STATE["cleanup_started"] = True
            threading.Thread(target=cleanup_loop, daemon=True).start()
            if FROZEN and SETTINGS["auto_update"] and find_tool("yt-dlp"):
                def auto_update():
                    time.sleep(6)
                    update_ytdlp()
                threading.Thread(target=auto_update, daemon=True).start()
        log(f"server listening on {self.host}:{self.port} (portable={PORTABLE}, data={DATA_DIR})")

    def active_jobs(self):
        with LOCK:
            return sum(1 for j in JOBS.values() if j["status"] in ("queued", "running", "zipping"))

    def stop(self):
        try:
            if self.httpd:
                self.httpd.shutdown()
                self.httpd.server_close()
        except Exception as e:  # noqa: BLE001
            log(f"shutdown error: {e}")
        with LOCK:
            jobs = list(JOBS.values())
        for job in jobs:
            if job["status"] in ("queued", "running", "zipping"):
                job["status"] = "cancelled"
            if job.get("proc"):
                kill_tree(job["proc"])
        time.sleep(0.3)
        shutil.rmtree(WORK, ignore_errors=True)
        log("server stopped")


def main():
    ap = argparse.ArgumentParser(description="השרת המקומי (למפתחים). משתמשים מפעילים את התוכנה.")
    ap.add_argument("--port", type=int, default=0)
    a = ap.parse_args()
    app = ServerApp(a.port)
    try:
        app.start()
    except OSError as e:
        print(f"לא ניתן להאזין לפורט {a.port}: {e}")
        return 1
    print(f"השרת רץ: {app.url}  (Ctrl+C לעצירה)")
    try:
        while not STATE["quit"]:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
