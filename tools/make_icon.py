#!/usr/bin/env python3
"""יוצר את אייקון התוכנה (assets/icon.png ו-assets/icon.ico).

הרעיון: "רשימת השמעה שנסגרת ברוכסן" - דף לבן עם רוכסן אינדיגו שהמשיכה שלו היא משולש
Play. בכוונה בלי החץ-למטה הרגיל של תוכנות הורדה ובלי האדום של יוטיוב.
הגיאומטריה זהה לזו שב-assets/icon.svg (viewBox 256).
"""
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "assets"
S = 1024            # רזולוציית ציור (ואז מקטינים עם LANCZOS)
K = S / 256         # יחס בין מערכת ה-256 ל-1024


def sc(v):
    return v * K


def gradient(corners):
    """גרדיאנט בילינארי מארבע פינות (tl, tr, bl, br)."""
    tl, tr, bl, br = corners
    img = Image.new("RGB", (2, 2))
    img.putpixel((0, 0), tl)
    img.putpixel((1, 0), tr)
    img.putpixel((0, 1), bl)
    img.putpixel((1, 1), br)
    return img.resize((S, S), Image.BICUBIC)


def rrect_mask(box, r):
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).rounded_rectangle([sc(v) for v in box], radius=sc(r), fill=255)
    return m


def rounded_polygon_mask(points, radius):
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).polygon([(sc(x), sc(y)) for x, y in points], fill=255)
    m = m.filter(ImageFilter.GaussianBlur(sc(radius)))
    return m.point(lambda v: 255 if v > 127 else 0).filter(ImageFilter.GaussianBlur(1.2))


def build():
    # רקע: אינדיגו -> סגול -> ורוד
    bg = gradient([(79, 70, 229), (139, 92, 246), (99, 62, 235), (236, 72, 153)])
    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    canvas.paste(bg, (0, 0), rrect_mask((8, 8, 248, 248), 56))

    # ברק עדין בחלק העליון
    glow = Image.new("RGBA", (S, S), (255, 255, 255, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([sc(-40), sc(-120), sc(200), sc(90)], fill=(255, 255, 255, 46))
    glow = glow.filter(ImageFilter.GaussianBlur(sc(14)))
    clip = rrect_mask((8, 8, 248, 248), 56)
    glow.putalpha(ImageChops.multiply(glow.getchannel("A"), clip))
    canvas = Image.alpha_composite(canvas, glow)

    # צל לדף
    sheet_box = (68, 34, 188, 230)
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [sc(sheet_box[0]), sc(sheet_box[1] + 8), sc(sheet_box[2]), sc(sheet_box[3] + 8)],
        radius=sc(22), fill=(20, 12, 70, 120))
    canvas = Image.alpha_composite(canvas, shadow.filter(ImageFilter.GaussianBlur(sc(9))))

    # הדף
    sheet = gradient([(255, 255, 255), (248, 246, 255), (238, 234, 255), (226, 220, 252)])
    canvas.paste(sheet, (0, 0), rrect_mask(sheet_box, 22))

    d = ImageDraw.Draw(canvas)
    dark = (49, 46, 129, 255)

    # שיני הרוכסן (מדורגות): 9 זוגות
    for i in range(9):
        y = 52 + i * 11.5
        d.rounded_rectangle([sc(110), sc(y), sc(129), sc(y + 7)], radius=sc(2.5), fill=dark)
        d.rounded_rectangle([sc(127), sc(y + 5.7), sc(146), sc(y + 12.7)], radius=sc(2.5), fill=dark)

    # גוף המשיכה
    d.rounded_rectangle([sc(108), sc(150), sc(148), sc(184)], radius=sc(11), fill=(30, 27, 75, 255))
    d.rounded_rectangle([sc(120), sc(160), sc(136), sc(166)], radius=sc(3), fill=(255, 255, 255, 70))
    # צוואר
    d.rounded_rectangle([sc(122), sc(180), sc(134), sc(194)], radius=sc(4), fill=(30, 27, 75, 255))

    # ידית המשיכה: משולש Play מעוגל, בגרדיאנט ורוד-סגול
    tri = rounded_polygon_mask([(112, 192), (112, 230), (150, 211)], 3.2)
    pull = gradient([(244, 63, 154), (200, 70, 235), (236, 72, 153), (139, 92, 246)])
    canvas.paste(pull, (0, 0), tri)
    return canvas


def svg_text():
    teeth = []
    for i in range(9):
        y = 52 + i * 11.5
        teeth.append(f'<rect class="tl" x="110" y="{y}" width="19" height="7" rx="2.5"/>')
        teeth.append(f'<rect class="tr" x="127" y="{y + 5.7:.1f}" width="19" height="7" rx="2.5"/>')
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" role="img" aria-label="YouTube Playlist Downloader">
  <defs>
    <linearGradient id="ic-bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#4f46e5"/><stop offset=".5" stop-color="#8b5cf6"/><stop offset="1" stop-color="#ec4899"/>
    </linearGradient>
    <linearGradient id="ic-sheet" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#ffffff"/><stop offset="1" stop-color="#e2dcfc"/>
    </linearGradient>
    <linearGradient id="ic-pull" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#f43f9a"/><stop offset="1" stop-color="#8b5cf6"/>
    </linearGradient>
    <filter id="ic-blur" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="5"/></filter>
  </defs>
  <rect x="8" y="8" width="240" height="240" rx="56" fill="url(#ic-bg)"/>
  <rect x="68" y="42" width="120" height="196" rx="22" fill="#140c46" opacity=".47" filter="url(#ic-blur)"/>
  <rect x="68" y="34" width="120" height="196" rx="22" fill="url(#ic-sheet)"/>
  <g fill="#312e81">{''.join(teeth)}</g>
  <g class="pull">
    <rect x="108" y="150" width="40" height="34" rx="11" fill="#1e1b4b"/>
    <rect x="120" y="160" width="16" height="6" rx="3" fill="#fff" opacity=".28"/>
    <rect x="122" y="180" width="12" height="14" rx="4" fill="#1e1b4b"/>
    <path d="M114 195.5 L114 227 L146 211.2 Z" fill="url(#ic-pull)" stroke="url(#ic-pull)" stroke-width="5" stroke-linejoin="round"/>
  </g>
</svg>
"""


def main():
    OUT.mkdir(exist_ok=True)
    (OUT / "icon.svg").write_text(svg_text(), encoding="utf-8")
    big = build()
    big.resize((512, 512), Image.LANCZOS).save(OUT / "icon.png")
    sizes = [16, 24, 32, 48, 64, 128, 256]
    frames = [big.resize((s, s), Image.LANCZOS) for s in sizes]
    frames[-1].save(OUT / "icon.ico", format="ICO", sizes=[(s, s) for s in sizes], append_images=frames[:-1])
    print("wrote", OUT / "icon.png", OUT / "icon.ico")


if __name__ == "__main__":
    main()
