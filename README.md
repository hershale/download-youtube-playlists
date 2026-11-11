# ⬇️ מוריד פלייליסטים מיוטיוב (ZIP) - ל-Windows

מורידים פלייליסט שלם מיוטיוב לקובץ ZIP אחד, ישירות למחשב שלכם.

- וידאו + אודיו / וידאו בלבד / אודיו בלבד (MP3)
- בחירת איכות: עד 4K לווידאו, ו-128–320kbps לאודיו
- בלי להתקין Python, yt-dlp או FFmpeg, ובלי להקליד פקודות

**האתר:** https://hershale.github.io/download-youtube-playlists/

---

## למשתמשים: התקנה והפעלה

1. **הורידו את [Setup.exe](https://github.com/hershale/download-youtube-playlists/releases/latest/download/Setup.exe)** והריצו אותו.
   - ההתקנה אישית ולא דורשת הרשאות מנהל.
   - אם Windows מציג "Windows protected your PC": לחצו **More info** ← **Run anyway**. ההודעה מופיעה כי הקובץ לא חתום דיגיטלית.
2. בסיום ההתקנה התוכנה נפתחת, ומיד נפתח האתר בדפדפן. בפעמים הבאות הפעילו אותה מתפריט **התחל** ← *YouTube Playlist Downloader* (או מהקיצור בשולחן העבודה).
3. באתר: הדביקו קישור לפלייליסט, בחרו מה להוריד ובאיזו איכות, ולחצו **הורדה כ-ZIP**.
4. הקובץ נשמר בתיקייה **Downloads\YouTube Playlists**. אפשר לפתוח אותה מהכפתור באתר או מחלון התוכנה.

**חשוב:**
- השאירו את חלון התוכנה ("● השרת פועל") פתוח בזמן ההורדה. הוא פועל בצד המחשב שלכם והאתר מתחבר אליו.
- לסיום לחצו **סגירה ויציאה**. התוכנה עוצרת הורדות פעילות ומנקה קבצים זמניים.
- בפעם הראשונה Chrome או Edge עשויים לשאול אם לאפשר לאתר גישה להתקנים ברשת המקומית. יש ללחוץ **אפשר**.
- אם יוטיוב משנה משהו וההורדה נכשלת, לחצו בחלון התוכנה **עדכון רכיב ההורדה**. התוכנה גם מנסה להתעדכן אוטומטית בכל הפעלה.
- הסרה: הגדרות Windows ← אפליקציות ← YouTube Playlist Downloader.

### פרטיות ואבטחה
- אין שרת מרכזי. כל הורדה מתבצעת ישירות מהמחשב שלכם, ושום מידע לא נשלח אלינו.
- השרת מאזין ל-`127.0.0.1` בלבד (לא נגיש מהרשת), ומקבל בקשות רק מהאתר המורשה. בקשות מאתרים אחרים נחסמות (הגנת Origin/Host/CSRF).
- יומן תקלות: `%LOCALAPPDATA%\YTPlaylistDownloader\server.log`

### הערה משפטית
יש להוריד רק תכנים שיש לכם זכות להוריד. השימוש באחריותכם ובהתאם לתנאי השימוש של יוטיוב ולדיני זכויות היוצרים.

---

## למפתחים

### מבנה
| קובץ | תפקיד |
|---|---|
| `docs/index.html` | הממשק (מתפרסם ב-GitHub Pages, ומוגש גם מהשרת המקומי) |
| `server.py` | השרת המקומי: API, הורדה עם yt-dlp, אריזה ל-ZIP, הגנות |
| `app.py` | מעטפת Windows: חלון סטטוס, פתיחת האתר, כיבוי מסודר, `--selftest` |
| `installer/setup.iss` | סקריפט Inno Setup ל-`Setup.exe` |
| `.github/workflows/build-installer.yml` | בנייה, בדיקות ופרסום אוטומטיים |

בתוך ההתקנה, `yt-dlp.exe`, `ffmpeg.exe`, `ffprobe.exe` ו-`deno.exe` נמצאים בתיקייה `bin\` ליד התוכנה, והשרת מוצא אותם משם בלי תלות ב-PATH.
(Deno נדרש ע"י yt-dlp לפענוח אתגרי יוטיוב.)

### הרצה ללא התקנה (פיתוח)
```bash
pip install -U yt-dlp      # וגם ffmpeg ב-PATH, או בתיקייה bin\ ליד server.py
python server.py           # http://127.0.0.1:8000
```
משתני סביבה לבדיקות: `YTZIP_BIN` (תיקיית כלים), `YTZIP_OUTDIR` (תיקיית פלט).

### בניית ה-Installer
- **אוטומטית:** דחיפת תג `v*` (למשל `git tag v1.0.1 && git push origin v1.0.1`) מריצה את ה-Workflow. הוא מוריד את הכלים, בונה עם PyInstaller ו-Inno Setup, מריץ בדיקה עצמית (גם אחרי התקנה והסרה בשקט, עם PATH ללא Python), ומפרסם את `Setup.exe` ב-Releases. אפשר גם להריץ ידנית מלשונית Actions.
- **ידנית ב-Windows:** ראו את שלבי ה-Workflow. בקצרה: `pyinstaller --noconfirm --clean --windowed --name YTPlaylistDownloader --add-data "docs;docs" app.py`, העתקת הכלים ל-`dist\YTPlaylistDownloader\bin`, ואז `ISCC.exe installer\setup.iss`.

### רישיונות
רכיבי צד שלישי המצורפים למתקין (yt-dlp, FFmpeg, Deno, Python) מפורטים ב-[THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt).
