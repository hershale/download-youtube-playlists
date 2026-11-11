#!/usr/bin/env python3
"""שרת מקומי להורדת פלייליסטים מיוטיוב כקובץ ZIP (yt-dlp + ffmpeg).

שני אופני הרצה:
  * מפתחים:   python server.py [--port 8000]   (yt-dlp ו-ffmpeg מה-PATH או מתיקיית bin/)
  * משתמשים:  התוכנה המותקנת (app.py) - כל הכלים נמצאים בתיקיית ההתקנה, ב-bin/

השרת מאזין ל-127.0.0.1 בלבד, ומקבל בקשות רק מהאתר המורשה (או מהדף שהוא עצמו מגיש).
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
from urllib.parse import quote, urlparse

VERSION = "1.0.0"
APP_ID = "yt-playlist-zip"
SITE_URL = "https://hershale.github.io/download-youtube-playlists/"
SITE_ORIGIN = "https://hershale.github.io"
DEFAULT_PORT = 8000

FROZEN = bool(getattr(sys, "frozen", False))
IS_WIN = os.name == "nt"
EXE = ".exe" if IS_WIN else ""
NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW

APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
RES_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
WEB = RES_DIR / "docs"
BIN_DIR = Path(os.environ.get("YTZIP_BIN") or (APP_DIR / "bin"))
WORK = Path(tempfile.gettempdir()) / "yt-playlist-zip"
LOG_FILE = None  # app.py מגדיר נתיב

YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
VIDEO_HEIGHTS = {"best", "2160", "1440", "1080", "720", "480", "360"}
AUDIO_QUALITY = {"best": "0", "320": "320K", "192": "192K", "128": "128K"}
MODES = {"both", "video", "audio"}

JOBS = {}
LOCK = threading.Lock()
UPDATE_LOCK = threading.Lock()
STATE = {"updating": False, "ytdlp_version": None, "cleanup_started": False}


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


def out_dir():
    env = os.environ.get("YTZIP_OUTDIR")
    return Path(env) if env else downloads_dir() / "YouTube Playlists"


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


def valid_url(url):
    try:
        p = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    return p.scheme in ("http", "https") and (p.hostname or "").lower() in YT_HOSTS


def safe_name(name, fallback="playlist"):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name or "").strip(" .")
    return name[:100] or fallback


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
        return {"ok": False, "message": "עדכון אוטומטי זמין רק בגרסה המותקנת. עדכנו עם: pip install -U yt-dlp"}
    with LOCK:
        busy = any(j["status"] in ("running", "zipping") for j in JOBS.values())
    if busy:
        return {"ok": False, "message": "יש הורדה פעילה. נסו שוב בסיומה."}
    if not UPDATE_LOCK.acquire(blocking=False):
        return {"ok": False, "message": "עדכון כבר מתבצע."}
    STATE["updating"] = True
    try:
        r = subprocess.run([exe, "-U"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=240, **run_kw())
        text = ((r.stdout or "") + (r.stderr or "")).strip()
        STATE["ytdlp_version"] = None
        log(f"yt-dlp -U rc={r.returncode}: {text[-300:]}")
        tail = " ".join(text.splitlines()[-2:]) if text else ""
        return {"ok": r.returncode == 0, "message": tail or "העדכון הסתיים.", "version": ytdlp_version()}
    except Exception as e:  # noqa: BLE001
        log(f"update failed: {e}")
        return {"ok": False, "message": f"העדכון נכשל: {e}"}
    finally:
        STATE["updating"] = False
        UPDATE_LOCK.release()


# ---------------------------------------------------------------- הורדות

def get_info(url):
    cmd = ytdlp_cmd() + common_args() + ["--flat-playlist", "-J", "--no-warnings", "--yes-playlist", "--", url]
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
    return {
        "title": data.get("title") or "וידאו",
        "uploader": data.get("uploader") or data.get("channel") or "",
        "count": len(entries),
        "items": [e.get("title") or "" for e in entries[:6]],
        "is_playlist": data.get("_type") == "playlist",
    }


def build_cmd(url, mode, quality, outdir):
    cmd = ytdlp_cmd() + common_args() + [
        "--newline", "--no-colors", "--ignore-errors", "--yes-playlist",
        "--windows-filenames", "--no-mtime", "--concurrent-fragments", "4",
        "-o", str(outdir / "%(playlist_index&{} - |)s%(title).100B.%(ext)s"),
    ]
    if mode == "audio":
        cmd += ["-f", "ba/b", "-x", "--audio-format", "mp3",
                "--audio-quality", AUDIO_QUALITY[quality]]
    else:
        h = "" if quality == "best" else f"[height<={quality}]"
        if mode == "both":
            cmd += ["-f", f"bv*{h}+ba/b{h}/b", "--merge-output-format", "mp4"]
        else:
            cmd += ["-f", f"bv{h}/b{h}"]
    return cmd + ["--", url]


def run_job(job):
    workdir = WORK / job["id"]
    outdir = workdir / "files"
    outdir.mkdir(parents=True, exist_ok=True)
    keep_workdir = False
    try:
        proc = subprocess.Popen(
            build_cmd(job["url"], job["mode"], job["quality"], outdir),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            encoding="utf-8", errors="replace", **run_kw(),
        )
        job["proc"] = proc
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            job["log"].append(line)
            del job["log"][:-200]
            m = re.search(r"Downloading playlist: (.+)", line)
            if m:
                job["title"] = m.group(1)
            m = re.search(r"Downloading item (\d+) of (\d+)", line)
            if m:
                job["index"], job["total"] = int(m.group(1)), int(m.group(2))
            m = re.search(r"\[download\]\s+([\d.]+)%", line)
            if m:
                job["item_pct"] = float(m.group(1))
            m = re.search(r"Destination: (.+)", line)
            if m:
                job["current"] = Path(m.group(1)).stem
                job["item_pct"] = 0.0
            if "ERROR" in line:
                job["errors"] += 1
            total = max(job["total"], 1)
            job["progress"] = min(((job["index"] - 1) + job["item_pct"] / 100) / total, 0.99)
        proc.wait()
        if job["status"] == "cancelled":
            return
        files = sorted(p for p in outdir.iterdir()
                       if p.is_file() and not p.name.endswith((".part", ".ytdl", ".temp")))
        if not files:
            raise RuntimeError("לא ירדו קבצים. בדקו את הקישור (פלייליסט פרטי?) או את הלוג.")
        job["status"] = "zipping"
        job["progress"] = 0.99
        zip_name = safe_name(job["title"]) + ".zip"
        saved = True
        try:
            folder = out_dir()
            folder.mkdir(parents=True, exist_ok=True)
            dest = unique_path(folder, zip_name)
        except OSError as e:
            log(f"output dir unavailable, using temp: {e}")
            dest, saved, keep_workdir = workdir / zip_name, False, True
        tmp = dest.with_name(dest.name + ".part")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            for f in files:
                z.write(f, f.name)
        os.replace(tmp, dest)
        job.update(status="done", progress=1.0, zip=str(dest), files=len(files),
                   zip_name=dest.name, saved_path=str(dest) if saved else None)
    except Exception as e:  # noqa: BLE001
        if job["status"] != "cancelled":
            log(f"job {job['id']} failed: {e}")
            job.update(status="error", error=str(e))
    finally:
        job["proc"] = None
        shutil.rmtree(outdir, ignore_errors=True)
        if not keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def cleanup_loop():
    while True:
        time.sleep(600)
        cutoff = time.time() - 2 * 3600
        try:
            with LOCK:
                old = [k for k, j in JOBS.items() if j["created"] < cutoff and j["status"] != "running"]
                for k in old:
                    JOBS.pop(k, None)
                    shutil.rmtree(WORK / k, ignore_errors=True)
        except Exception as e:  # noqa: BLE001
            log(f"cleanup error: {e}")


def public(job):
    keys = ("id", "status", "progress", "title", "index", "total", "current",
            "error", "errors", "files", "zip_name", "saved_path")
    d = {k: job.get(k) for k in keys}
    d["log"] = job["log"][-12:]
    return d


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = f"YTPlaylistZip/{VERSION}"

    # -- אבטחה: Host (נגד DNS rebinding), Origin (נגד CSRF), סוג תוכן
    def _port(self):
        return self.server.server_address[1]

    def _origin_allowed(self, origin):
        p = self._port()
        return origin in (SITE_ORIGIN, f"http://127.0.0.1:{p}", f"http://localhost:{p}")

    def _guard(self, write):
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
        return True

    def cors(self):
        origin = self.headers.get("Origin")
        if origin and self._origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Max-Age", "600")

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            n = max(0, min(int(self.headers.get("Content-Length") or 0), 65536))
            return json.loads(self.rfile.read(n) or b"{}")
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

    def do_OPTIONS(self):
        self._dispatch(self._options)

    def do_GET(self):
        self._dispatch(self._get)

    def do_POST(self):
        self._dispatch(self._post)

    def _options(self):
        if not self._guard(False):
            return
        self.send_response(204)
        self.cors()
        self.end_headers()

    def _get(self):
        if not self._guard(False):
            return
        path = urlparse(self.path).path
        if path == "/api/health":
            return self.send_json({
                "ok": True, "app": APP_ID, "version": VERSION,
                "ffmpeg": bool(find_tool("ffmpeg")), "ytdlp": has_ytdlp(),
                "ytdlp_version": ytdlp_version() if has_ytdlp() else None,
                "out_dir": str(out_dir()), "updating": STATE["updating"],
            })
        m = re.fullmatch(r"/api/jobs/([0-9a-f]{12})(/zip)?", path)
        if m:
            job = JOBS.get(m.group(1))
            if not job:
                return self.send_json({"error": "משימה לא נמצאה"}, 404)
            if not m.group(2):
                return self.send_json(public(job))
            zp = Path(job.get("zip") or "")
            if job["status"] != "done" or not zp.is_file():
                return self.send_json({"error": "הקובץ לא זמין"}, 409)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(zp.stat().st_size))
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{quote(job['zip_name'])}")
            self.cors()
            self.end_headers()
            with open(zp, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1024 * 1024)
            return
        self.serve_static(path)

    def serve_static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        f = (WEB / rel).resolve()
        if WEB.resolve() not in f.parents or not f.is_file():
            return self.send_json({"error": "לא נמצא"}, 404)
        ctype = {".html": "text/html; charset=utf-8", ".css": "text/css",
                 ".js": "text/javascript", ".svg": "image/svg+xml"}.get(f.suffix, "application/octet-stream")
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
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
            url = str(body.get("url", "")).strip()
            mode, quality = body.get("mode", "both"), str(body.get("quality", "best"))
            allowed = AUDIO_QUALITY if mode == "audio" else VIDEO_HEIGHTS
            if not valid_url(url) or mode not in MODES or quality not in allowed:
                return self.send_json({"error": "פרמטרים לא תקינים"}, 400)
            if STATE["updating"]:
                return self.send_json({"error": "מתבצע עדכון של רכיב ההורדה. נסו שוב בעוד רגע."}, 503)
            job = {"id": uuid.uuid4().hex[:12], "url": url, "mode": mode, "quality": quality,
                   "status": "running", "progress": 0.0, "title": "playlist", "index": 1,
                   "total": 1, "item_pct": 0.0, "current": "", "errors": 0, "log": [],
                   "created": time.time(), "proc": None}
            with LOCK:
                JOBS[job["id"]] = job
            threading.Thread(target=run_job, args=(job,), daemon=True).start()
            return self.send_json({"id": job["id"]})
        m = re.fullmatch(r"/api/jobs/([0-9a-f]{12})/cancel", path)
        if m and m.group(1) in JOBS:
            job = JOBS[m.group(1)]
            job["status"] = "cancelled"
            if job.get("proc"):
                kill_tree(job["proc"])
            return self.send_json({"ok": True})
        if path == "/api/open-folder":
            return self.send_json({"ok": open_folder(out_dir()), "out_dir": str(out_dir())})
        if path == "/api/update":
            return self.send_json(update_ytdlp())
        self.send_json({"error": "לא נמצא"}, 404)

    def log_message(self, fmt, *args):
        pass


class _Server(ThreadingHTTPServer):
    # ב-Windows SO_REUSEADDR מאפשר ל-2 תהליכים להאזין לאותו פורט, לכן מכבים אותו.
    allow_reuse_address = not IS_WIN
    daemon_threads = True


class ServerApp:
    """עוטף את השרת: הפעלה, וכיבוי מסודר (עוצר הורדות ומנקה קבצים זמניים)."""

    def __init__(self, port=DEFAULT_PORT):
        self.host = "127.0.0.1"
        self.port = port
        self.httpd = None
        self.thread = None

    def start(self):
        self.httpd = _Server((self.host, self.port), Handler)  # OSError אם הפורט תפוס
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
        log(f"server listening on {self.host}:{self.port}")

    def active_jobs(self):
        with LOCK:
            return sum(1 for j in JOBS.values() if j["status"] in ("running", "zipping"))

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
            if job["status"] in ("running", "zipping"):
                job["status"] = "cancelled"
            if job.get("proc"):
                kill_tree(job["proc"])
        time.sleep(0.3)
        shutil.rmtree(WORK, ignore_errors=True)
        log("server stopped")


def main():
    ap = argparse.ArgumentParser(description="שרת הורדת פלייליסטים (מקומי בלבד)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    a = ap.parse_args()
    if not find_tool("ffmpeg"):
        print("אזהרה: ffmpeg לא נמצא. נדרש לחיבור וידאו+אודיו ולהמרה ל-MP3.")
    app = ServerApp(a.port)
    try:
        app.start()
    except OSError as e:
        print(f"לא ניתן להאזין לפורט {a.port}: {e}")
        return 1
    print(f"השרת רץ בכתובת http://127.0.0.1:{app.port}  (Ctrl+C לעצירה)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
