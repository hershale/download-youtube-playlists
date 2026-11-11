# ⬇️ מוריד פלייליסטים מיוטיוב (ZIP)

אתר פשוט להורדת פלייליסט שלם כקובץ ZIP אחד.

- וידאו + אודיו / וידאו בלבד / אודיו בלבד (MP3)
- בחירת איכות: עד 4K לווידאו, ו-128–320kbps לאודיו
- ממשק עברית, מודרני, תומך מצב כהה

## איך זה עובד

דף ווב סטטי לא יכול להוריד מיוטיוב בעצמו (חסימות דפדפן ואין שרת), לכן יש שני חלקים:

1. `docs/index.html` – הממשק. אפשר להריץ מקומית או לפרסם ב-GitHub Pages.
2. `server.py` – שרת קטן שמריץ `yt-dlp` ואורז ל-ZIP. רץ על המחשב שלכם.

## התקנה והרצה

דרישות: Python 3.9+, [yt-dlp](https://github.com/yt-dlp/yt-dlp), [ffmpeg](https://ffmpeg.org/).

```bash
pip install -U yt-dlp
# התקינו ffmpeg (Windows: winget install ffmpeg | macOS: brew install ffmpeg | Linux: apt install ffmpeg)
python server.py
```

פתחו http://localhost:8000

## פרסום ב-GitHub Pages

1. Settings → Pages → Source: `Deploy from a branch` → Branch: `main` → Folder: `/docs`
2. היכנסו לכתובת `https://<שם-משתמש>.github.io/<שם-הריפו>/`
3. הריצו `python server.py` על המחשב שלכם. הדף יתחבר אוטומטית ל-`http://localhost:8000`
   (אפשר לשנות בהגדרות שרת).

## הערות

- יש להוריד רק תכנים שיש לכם זכות להוריד, בהתאם לתנאי השימוש של יוטיוב.
- השרת מאזין ל-`127.0.0.1` בלבד כברירת מחדל. אל תחשפו אותו לאינטרנט ללא הגנה.
- אם ההורדה נכשלת, עדכנו את yt-dlp: `pip install -U yt-dlp`.
- קבצים זמניים נמחקים אוטומטית אחרי שעתיים.
