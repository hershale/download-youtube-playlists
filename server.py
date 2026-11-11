#!/usr/bin/env python3
"""שרת להורדת פלייליסטים מיוטיוב כקובץ ZIP (מבוסס yt-dlp + ffmpeg).

הרצה:  python server.py  [--port 8000] [--host 127.0.0.1]
"""
import argparse
import json
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

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "docs"
WORK = Path(tempfile.gettempdir()) / "yt-playlist-zip"
WORK.mkdir(exist_ok=True)

YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
VIDEO_HEIGHTS = {"best", "2160", "1440", "1080", "720", "480", "360"}
AUDIO_QUALITY = {"best": "0", "320": "320K", "192": "192K", "128": "128K"}
MODES = {"both", "video", "audio"}

JOBS = {}
LOCK = threading.Lock()


def ytdlp_cmd():
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    return [sys.executable, "-m", "yt_dlp"]


def valid_url(url):
    try:
        p = urlparse(url)
    except Exception:
        return False
    return p.scheme in ("http", "https") and (p.hostname or "").lower() in YT_HOSTS


def safe_name(name, fallback="playlist"):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name or "").strip(" .")
    return (name[:100] or fallback)


def get_info(url):
    cmd = ytdlp_cmd() + ["--flat-playlist", "-J", "--no-warnings", "--yes-playlist", "--", url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "שגיאה לא ידועה").strip().splitlines()[-1])
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
    cmd = ytdlp_cmd() + [
        "--newline", "--no-colors", "--ignore-errors", "--yes-playlist",
        "--windows-filenames", "--no-mtime", "--concurrent-fragments", "4",
        "-o", str(outdir / "%(playlist_index&{} - |)s%(title).150B.%(ext)s"),
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
    outdir = WORK / job["id"] / "files"
    outdir.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(
            build_cmd(job["url"], job["mode"], job["quality"], outdir),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            encoding="utf-8", errors="replace",
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
        files = sorted(p for p in outdir.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl")))
        if not files:
            raise RuntimeError("לא ירדו קבצים. בדקו את הקישור (פלייליסט פרטי?) או את הלוג.")
        job["status"] = "zipping"
        job["progress"] = 0.99
        zpath = WORK / job["id"] / "result.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
            for f in files:
                z.write(f, f.name)
        shutil.rmtree(outdir, ignore_errors=True)
        job.update(status="done", progress=1.0, zip=str(zpath), files=len(files),
                   zip_name=safe_name(job["title"]) + ".zip")
    except Exception as e:  # noqa: BLE001
        if job["status"] != "cancelled":
            job.update(status="error", error=str(e))


def cleanup_loop():
    while True:
        time.sleep(600)
        cutoff = time.time() - 2 * 3600
        with LOCK:
            old = [k for k, j in JOBS.items() if j["created"] < cutoff]
            for k in old:
                JOBS.pop(k, None)
                shutil.rmtree(WORK / k, ignore_errors=True)


def public(job):
    keys = ("id", "status", "progress", "title", "index", "total", "current",
            "error", "errors", "files", "zip_name")
    d = {k: job.get(k) for k in keys}
    d["log"] = job["log"][-12:]
    return d


class Handler(BaseHTTPRequestHandler):
    server_version = "YTPlaylistZip/1.0"

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(min(n, 65536)) or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            return self.send_json({"ok": True, "ffmpeg": bool(shutil.which("ffmpeg"))})
        m = re.fullmatch(r"/api/jobs/([0-9a-f]{12})(/zip)?", path)
        if m:
            job = JOBS.get(m.group(1))
            if not job:
                return self.send_json({"error": "משימה לא נמצאה"}, 404)
            if not m.group(2):
                return self.send_json(public(job))
            if job["status"] != "done":
                return self.send_json({"error": "הקובץ עדיין לא מוכן"}, 409)
            zp = Path(job["zip"])
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
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
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
            job = {"id": uuid.uuid4().hex[:12], "url": url, "mode": mode, "quality": quality,
                   "status": "running", "progress": 0.0, "title": "playlist", "index": 1,
                   "total": 1, "item_pct": 0.0, "current": "", "errors": 0, "log": [],
                   "created": time.time()}
            with LOCK:
                JOBS[job["id"]] = job
            threading.Thread(target=run_job, args=(job,), daemon=True).start()
            return self.send_json({"id": job["id"]})
        m = re.fullmatch(r"/api/jobs/([0-9a-f]{12})/cancel", path)
        if m and m.group(1) in JOBS:
            job = JOBS[m.group(1)]
            job["status"] = "cancelled"
            if job.get("proc"):
                job["proc"].terminate()
            return self.send_json({"ok": True})
        self.send_json({"error": "לא נמצא"}, 404)

    def log_message(self, fmt, *args):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    if not shutil.which("ffmpeg"):
        print("אזהרה: ffmpeg לא נמצא. נדרש לחיבור וידאו+אודיו ולהמרה ל-MP3.")
    threading.Thread(target=cleanup_loop, daemon=True).start()
    print(f"השרת רץ בכתובת http://{a.host}:{a.port}  (Ctrl+C לעצירה)")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
