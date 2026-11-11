#!/usr/bin/env python3
"""התוכנה: מפעילה את השרת המקומי ומציגה את הממשק (ui/) בחלון משלה.

חלון: pywebview + WebView2 (קיים ב-Windows 10/11). אם אי אפשר, נפתח חלון אפליקציה של Edge/Chrome.
  YTPlaylistDownloader.exe                 - הפעלה רגילה
  YTPlaylistDownloader.exe --selftest OUT  - בדיקה עצמית (ל-CI), כותבת תוצאות לקובץ OUT
"""
import ctypes
import hashlib
import http.client
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
import webbrowser
from pathlib import Path

import server

TITLE = "מוריד פלייליסטים"  # גם כותרת החלון: משמש לאיתור חלון קיים בהפעלה כפולה
_window = None
_mutex = None


# ------------------------------------------------------------------ עזרים

def msgbox(text, error=False):
    if server.IS_WIN:
        ctypes.windll.user32.MessageBoxW(0, text, TITLE, 0x10 if error else 0x40)
    else:
        print(text, file=sys.stderr)


def already_running():
    """מנעול התוכנה: מונע הפעלה כפולה (נפרד לכל תיקיית נתונים, כך שניידת ומותקנת לא מתנגשות)."""
    global _mutex
    if not server.IS_WIN:
        return False
    name = "Local\\YTPlaylistDownloader-" + hashlib.sha1(str(server.DATA_DIR).lower().encode()).hexdigest()[:12]
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, name)
    return ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


def activate_existing_window():
    try:
        hwnd = ctypes.windll.user32.FindWindowW(None, TITLE)
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def write_runtime(app, shell):
    try:
        server.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (server.DATA_DIR / "runtime.json").write_text(
            json.dumps({"port": app.port, "shell": shell, "pid": os.getpid(), "version": server.VERSION}),
            encoding="utf-8")
    except OSError:
        pass


def webview2_version():
    if not server.IS_WIN:
        return None
    import winreg
    guid = r"\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for hive, base in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node"),
                       (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE"),
                       (winreg.HKEY_CURRENT_USER, r"Software")):
        try:
            with winreg.OpenKey(hive, base + guid) as k:
                return winreg.QueryValueEx(k, "pv")[0]
        except OSError:
            continue
    return None


# ------------------------------------------------------------------ גשר JS -> Python (חלון בלבד)

class Api:
    """השיטות הציבוריות כאן זמינות לממשק דרך window.pywebview.api."""

    def choose_folder(self):
        import webview
        kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or webview.FOLDER_DIALOG
        res = _window.create_file_dialog(kind, directory=str(server.out_dir()))
        return res[0] if res else None

    def open_external(self, url):
        if isinstance(url, str) and url.startswith("https://"):
            webbrowser.open(url)
        return True


# ------------------------------------------------------------------ חלון

def run_window(app):
    global _window
    import webview
    _window = webview.create_window(
        TITLE, app.url, js_api=Api(), width=1120, height=780, min_size=(940, 660),
        background_color="#0b0d14")

    def on_closing():
        if app.active_jobs():
            return bool(_window.create_confirmation_dialog(TITLE, "יש הורדות פעילות. לסגור ולבטל אותן?"))
        return True

    _window.events.closing += on_closing
    server.STATE["shell"] = "window"
    write_runtime(app, "window")
    # gui מפורש: בלי WebView2 נקבל שגיאה (ואז fallback), ולא מנוע IE ישן
    webview.start(gui="edgechromium" if server.IS_WIN else None, private_mode=False,
                  storage_path=str(server.DATA_DIR / "webview"))


def find_browser():
    pf, pf86 = os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")
    local = os.environ.get("LocalAppData", "")
    for p in (rf"{pf86}\Microsoft\Edge\Application\msedge.exe", rf"{pf}\Microsoft\Edge\Application\msedge.exe",
              rf"{pf}\Google\Chrome\Application\chrome.exe", rf"{pf86}\Google\Chrome\Application\chrome.exe",
              rf"{local}\Google\Chrome\Application\chrome.exe"):
        if p and Path(p).is_file():
            return p
    return None


def run_browser_fallback(app):
    """בלי WebView2: חלון אפליקציה של Edge/Chrome. אם אין, הדפדפן הרגיל (ואז יוצאים כשהממשק נסגר)."""
    server.STATE["shell"] = "browser"
    write_runtime(app, "browser")
    proc = None
    exe = find_browser()
    if exe:
        profile = server.DATA_DIR / "browser-profile"
        proc = subprocess.Popen([exe, f"--app={app.url}", f"--user-data-dir={profile}",
                                 "--no-first-run", "--window-size=1120,780"])
    else:
        webbrowser.open(app.url)
    server.STATE["last_seen"] = time.time()
    while not server.STATE["quit"]:
        time.sleep(1)
        if proc is not None and proc.poll() is not None and not app.active_jobs():
            break
        if proc is None and not app.active_jobs() and time.time() - server.STATE["last_seen"] > 300:
            break  # הדפדפן נסגר ואין הורדות: לא משאירים תהליך ברקע


# ------------------------------------------------------------------ בדיקה עצמית (CI)

def selftest(result_path):
    lines, failed = [], []

    def check(name, ok, detail=""):
        lines.append(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}".rstrip())
        if not ok:
            failed.append(name)

    def warn(name, detail=""):
        lines.append(f"WARN  {name}  {detail}".rstrip())

    def req(port, method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read()

    app = server.ServerApp(port=0)
    try:
        app.start()
        port = app.port
        me = f"http://127.0.0.1:{port}"
        check("frozen", server.FROZEN, f"bin={server.BIN_DIR}")
        check("נתיב נתונים", server.DATA_DIR.is_dir(), f"{server.DATA_DIR} (portable={server.PORTABLE})")
        if server.PORTABLE:
            check("גרסה ניידת שומרת נתונים ליד התוכנה", server.APP_DIR in server.DATA_DIR.parents,
                  str(server.DATA_DIR))
        for tool in ("yt-dlp", "ffmpeg", "ffprobe", "deno"):
            p = server.find_tool(tool)
            inside = bool(p) and Path(p).resolve().parent == server.BIN_DIR.resolve()
            check(f"{tool} נמצא בתיקיית ההתקנה", inside or (not server.FROZEN and bool(p)), p or "")
            if p:
                flag = "-version" if tool in ("ffmpeg", "ffprobe") else "--version"
                r = subprocess.run([p, flag], capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=90, **server.run_kw())
                first = (r.stdout or r.stderr).strip().splitlines()[:1]
                check(f"{tool} {flag}", r.returncode == 0, f"rc={r.returncode} " + (first[0] if first else ""))
        check("ממשק (ui) מצורף", (server.WEB / "index.html").is_file(), str(server.WEB))

        st, data = req(port, "GET", "/api/health", {"Origin": me})
        h = json.loads(data) if st == 200 else {}
        check("health", st == 200 and h.get("app") == server.APP_ID, f"status={st}")
        check("health: ytdlp+ffmpeg", bool(h.get("ytdlp")) and bool(h.get("ffmpeg")), str(h.get("ytdlp_version")))
        st, data = req(port, "GET", "/")
        check("הממשק מוגש", st == 200 and b"<html" in data[:300].lower(), f"status={st}")

        post = {"Content-Type": "application/json", "Origin": me}
        st, data = req(port, "POST", "/api/settings", post, json.dumps({"theme": "dark", "fragments": 6}))
        check("שמירת הגדרות", st == 200 and json.loads(data)["settings"]["theme"] == "dark", f"status={st}")
        st, _ = req(port, "POST", "/api/settings", post, json.dumps({"fragments": 999}))
        check("דחיית הגדרה לא תקינה", st == 400, f"status={st}")
        req(port, "POST", "/api/settings", post, json.dumps({"theme": "system", "fragments": 4}))
        st, _ = req(port, "GET", "/api/history", {"Origin": me})
        check("היסטוריה", st == 200, f"status={st}")

        body = json.dumps({"url": "https://www.youtube.com/playlist?list=x"})
        for name, hdr, want in (
            ("חסימת Origin זר", {**post, "Origin": "https://evil.example"}, 403),
            ("חסימת האתר הציבורי כ-Origin", {**post, "Origin": "https://hershale.github.io"}, 403),
            ("חסימת POST ללא Origin", {"Content-Type": "application/json"}, 403),
            ("חסימת Host זר (DNS rebinding)", {**post, "Host": "evil.example"}, 403),
            ("חסימת Content-Type לא JSON", {**post, "Content-Type": "text/plain"}, 415),
        ):
            st, _ = req(port, "POST", "/api/info", hdr, body)
            check(name, st == want, f"status={st}")

        ffmpeg, ffprobe = server.find_tool("ffmpeg"), server.find_tool("ffprobe")
        with tempfile.TemporaryDirectory() as td:
            mp3 = os.path.join(td, "t.mp3")
            r = subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                                "-c:a", "libmp3lame", "-b:a", "128k", "-y", mp3],
                               capture_output=True, text=True, timeout=60, **server.run_kw())
            check("המרת MP3 עם ffmpeg המצורף", r.returncode == 0 and os.path.getsize(mp3) > 1000, r.stderr.strip()[:200])
            r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                                "-of", "csv=p=0", mp3], capture_output=True, text=True, timeout=60, **server.run_kw())
            check("ffprobe קורא את הקובץ", r.returncode == 0 and r.stdout.strip() != "", r.stdout.strip())

        # חלון: לא נכשלים אם חסר (יש fallback), אבל מדווחים
        try:
            import webview
            detail = getattr(webview, "__version__", "")
            if server.IS_WIN:
                import clr  # noqa: F401
                from webview.platforms import edgechromium  # noqa: F401
            lines.append(f"PASS  רכיב החלון (pywebview) נטען  {detail}")
        except Exception as e:  # noqa: BLE001
            warn("רכיב החלון (pywebview) לא נטען - ייעשה שימוש ב-fallback", repr(e))
        v = webview2_version()
        lines.append(f"INFO  WebView2 runtime: {v or 'לא נמצא ברגיסטרי'}")
    except Exception:  # noqa: BLE001
        check("חריגה בבדיקה", False, traceback.format_exc())
    finally:
        app.stop()
    lines.append("RESULT: " + ("FAILED: " + ", ".join(failed) if failed else "OK"))
    try:
        Path(result_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass
    return 1 if failed else 0


# ------------------------------------------------------------------ main

def main():
    args = sys.argv[1:]
    if args and args[0] == "--selftest":
        out = args[1] if len(args) > 1 else str(Path(tempfile.gettempdir()) / "ytpd-selftest.txt")
        try:
            Path(out).write_text("STARTED\n", encoding="utf-8")
            return selftest(out)
        except Exception:  # noqa: BLE001
            Path(out).write_text("FAIL  crash\n" + traceback.format_exc() + "RESULT: FAILED\n", encoding="utf-8")
            return 1

    if already_running():
        if not activate_existing_window():
            msgbox("התוכנה כבר פועלת. חפשו אותה בשורת המשימות.")
        return 0
    app = server.ServerApp()
    try:
        app.start()
    except Exception:  # noqa: BLE001
        err = traceback.format_exc()
        server.log("fatal: " + err)
        msgbox("לא ניתן להפעיל את השרת המקומי.\nפרטים נשמרו ב:\n" + str(server.DATA_DIR / "server.log"), True)
        return 1
    try:
        try:
            run_window(app)
        except Exception:  # noqa: BLE001
            server.log("window failed, falling back: " + traceback.format_exc())
            run_browser_fallback(app)
    finally:
        app.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
