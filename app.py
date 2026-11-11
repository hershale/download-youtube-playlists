#!/usr/bin/env python3
"""מעטפת Windows לשרת: חלון סטטוס קטן, פתיחת האתר בדפדפן, וכיבוי מסודר.

נארז ל-exe עם PyInstaller (ראו .github/workflows/build-installer.yml).
  YTPlaylistDownloader.exe                 - הפעלה רגילה
  YTPlaylistDownloader.exe --selftest OUT  - בדיקה עצמית (ל-CI), כותבת תוצאות לקובץ OUT
"""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import traceback
import webbrowser
from pathlib import Path

import server

TITLE = "מוריד פלייליסטים מיוטיוב"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "YTPlaylistDownloader"


# ------------------------------------------------------------------ עזרים

def setup_logging():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        log_path = DATA_DIR / "server.log"
        if log_path.exists() and log_path.stat().st_size > 1_000_000:
            log_path.unlink()
        server.LOG_FILE = str(log_path)
    except OSError:
        pass


def probe(port):
    """בודק אם על הפורט כבר רץ השרת שלנו."""
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        c.request("GET", "/api/health")
        r = c.getresponse()
        return json.loads(r.read().decode("utf-8")) if r.status == 200 else None
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ בדיקה עצמית (CI)

def selftest(result_path):
    lines, failed = [], []

    def check(name, ok, detail=""):
        lines.append(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}".rstrip())
        if not ok:
            failed.append(name)

    def req(port, method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        return r.status, data

    app = server.ServerApp(port=0)
    try:
        app.start()
        port = app.port
        check("frozen", server.FROZEN, f"bin={server.BIN_DIR}")
        for tool in ("yt-dlp", "ffmpeg", "ffprobe", "deno"):
            p = server.find_tool(tool)
            inside = bool(p) and Path(p).resolve().parent == server.BIN_DIR.resolve()
            check(f"{tool} נמצא בתיקיית ההתקנה", inside or (not server.FROZEN and bool(p)), p or "")
            if p:
                r = subprocess.run([p, "--version"], capture_output=True, text=True,
                                   timeout=60, **server.run_kw())
                first = (r.stdout or r.stderr).strip().splitlines()[:1]
                check(f"{tool} --version", r.returncode == 0, first[0] if first else "")
        check("docs מצורפים", (server.WEB / "index.html").is_file(), str(server.WEB))

        st, data = req(port, "GET", "/api/health", {"Origin": server.SITE_ORIGIN})
        h = json.loads(data) if st == 200 else {}
        check("health", st == 200 and h.get("app") == server.APP_ID, f"status={st}")
        check("health: ytdlp+ffmpeg", bool(h.get("ytdlp")) and bool(h.get("ffmpeg")), str(h.get("ytdlp_version")))
        st, data = req(port, "GET", "/")
        check("דף הממשק מוגש מקומית", st == 200 and b"<html" in data[:200].lower(), f"status={st}")

        body = json.dumps({"url": "https://www.youtube.com/playlist?list=x"})
        post = {"Content-Type": "application/json"}
        st, _ = req(port, "POST", "/api/info", {**post, "Origin": "https://evil.example"}, body)
        check("חסימת Origin זר", st == 403, f"status={st}")
        st, _ = req(port, "POST", "/api/info", post, body)
        check("חסימת POST ללא Origin", st == 403, f"status={st}")
        st, _ = req(port, "POST", "/api/info", {**post, "Origin": server.SITE_ORIGIN, "Host": "evil.example"}, body)
        check("חסימת Host זר (DNS rebinding)", st == 403, f"status={st}")
        st, _ = req(port, "POST", "/api/info", {"Content-Type": "text/plain", "Origin": server.SITE_ORIGIN}, body)
        check("חסימת Content-Type לא JSON", st == 415, f"status={st}")

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


# ------------------------------------------------------------------ ממשק

def run_gui():
    import tkinter as tk
    from tkinter import messagebox

    try:  # חדות במסכי DPI גבוה
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass

    app = server.ServerApp()
    try:
        app.start()
    except OSError:
        root = tk.Tk()
        root.withdraw()
        info = probe(server.DEFAULT_PORT)
        if info and info.get("app") == server.APP_ID:
            webbrowser.open(server.SITE_URL)
            messagebox.showinfo(TITLE, "התוכנה כבר פועלת. פתחתי עבורך את האתר בדפדפן.")
            return 0
        messagebox.showerror(
            TITLE, f"לא ניתן להפעיל את השרת: פורט {server.DEFAULT_PORT} תפוס על ידי תוכנה אחרת.\n"
                   "סגרו אותה ונסו שוב.")
        return 1

    root = tk.Tk()
    root.title(TITLE)
    root.geometry("500x380")
    root.resizable(False, False)
    bg = "#f6f7fb"
    root.configure(bg=bg)
    font = "Segoe UI"
    root.report_callback_exception = lambda *a: server.log("gui error: " + "".join(traceback.format_exception(*a)))

    tk.Label(root, text="●  השרת פועל", fg="#16a34a", bg=bg, font=(font, 20, "bold")).pack(pady=(22, 2))
    tk.Label(root, text=f"http://127.0.0.1:{app.port}", fg="#6b7180", bg=bg, font=(font, 11)).pack()
    tk.Label(root, bg=bg, fg="#151822", font=(font, 11), justify="center", wraplength=440,
             text="האתר נפתח בדפדפן. השאירו חלון זה פתוח בזמן ההורדה.\n"
                  "סגירת החלון עוצרת את השרת.").pack(pady=(14, 8))

    status = tk.StringVar(value="")
    warn = []
    if not server.has_ytdlp():
        warn.append("רכיב yt-dlp חסר. התקינו מחדש את התוכנה.")
    if not server.find_tool("ffmpeg"):
        warn.append("רכיב FFmpeg חסר. התקינו מחדש את התוכנה.")
    status.set("\n".join(warn))
    tk.Label(root, textvariable=status, bg=bg, fg="#dc2626" if warn else "#6b7180", font=(font, 10),
             wraplength=440, justify="center").pack(pady=(0, 6))

    pending = {"msg": None}

    def poll():
        if pending["msg"]:
            status.set(pending["msg"])
            pending["msg"] = None
            btn_update.config(state="normal")
        root.after(500, poll)

    def do_update(auto=False):
        def work():
            if auto:
                pending["msg"] = "בודק עדכון לרכיב ההורדה…"
            res = server.update_ytdlp()
            if auto and not res.get("ok"):
                pending["msg"] = ""  # עדכון אוטומטי שנכשל (למשל ללא אינטרנט) לא מפריע למשתמש
            else:
                pending["msg"] = res.get("message", "")
        if not auto:
            btn_update.config(state="disabled")
            status.set("מעדכן…")
        threading.Thread(target=work, daemon=True).start()

    def quit_app():
        if app.active_jobs() and not messagebox.askyesno(TITLE, "יש הורדה פעילה. לסגור ולבטל אותה?"):
            return
        app.stop()
        root.destroy()

    def mk(parent, text, cmd, primary=False):
        return tk.Button(parent, text=text, command=cmd, font=(font, 11, "bold" if primary else "normal"),
                         bg="#5b5bf0" if primary else "#ffffff", fg="#ffffff" if primary else "#151822",
                         relief="flat", bd=1, padx=14, pady=8, cursor="hand2", width=18)

    row1 = tk.Frame(root, bg=bg)
    row1.pack(pady=4)
    mk(row1, "פתיחת האתר", lambda: webbrowser.open(server.SITE_URL), True).pack(side="left", padx=4)
    mk(row1, "תיקיית ההורדות", lambda: server.open_folder(server.out_dir())).pack(side="left", padx=4)
    row2 = tk.Frame(root, bg=bg)
    row2.pack(pady=4)
    btn_update = mk(row2, "עדכון רכיב ההורדה", do_update)
    btn_update.pack(side="left", padx=4)
    mk(row2, "סגירה ויציאה", quit_app).pack(side="left", padx=4)

    root.protocol("WM_DELETE_WINDOW", quit_app)
    root.after(500, poll)
    if server.FROZEN and server.has_ytdlp():
        root.after(8000, lambda: do_update(auto=True))
    webbrowser.open(server.SITE_URL)
    root.mainloop()
    return 0


def main():
    setup_logging()
    args = sys.argv[1:]
    if args and args[0] == "--selftest":
        out = args[1] if len(args) > 1 else str(Path(tempfile.gettempdir()) / "ytpd-selftest.txt")
        return selftest(out)
    try:
        return run_gui()
    except Exception:  # noqa: BLE001
        err = traceback.format_exc()
        server.log("fatal: " + err)
        try:
            import tkinter as tk
            from tkinter import messagebox
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror(TITLE, "אירעה שגיאה בהפעלת התוכנה.\nפרטים נשמרו ב:\n" + str(DATA_DIR / "server.log"))
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
