from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Dict

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

# مقاس مناسب لتيليجرام كصورة عرضية واضحة
W, H = 1536, 864

OUT_DIR = Path(os.environ.get("CARD_OUT_DIR", "/tmp/bamsignals_cards"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

GREEN = (47, 214, 82)
RED = (255, 70, 83)
WHITE = (245, 245, 245)
MUTED = (205, 205, 205)
LINE = (155, 155, 155, 105)


def _font(size: int, bold: bool = False):
    """
    مهم:
    بعض السيرفرات مثل Render ما يكون فيها Arial أو DejaVu.
    لو ما لقينا خط نظام، نستخدم خط Pillow الافتراضي بحجم حقيقي
    عشان ما تطلع الصورة بخلفية فقط بدون كتابة.
    """
    candidates = []
    if bold:
        candidates += [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/Library/Fonts/Arial Bold.ttf",
        ]
    else:
        candidates += [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial.ttf",
        ]

    for p in candidates:
        try:
            if Path(p).exists():
                return ImageFont.truetype(p, size)
        except Exception:
            pass

    # fallback قابل للتكبير، عكس load_default القديم الصغير جدًا
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _money(v: Any, default: str = "--") -> str:
    try:
        if v is None or v == "":
            return default
        return f"{float(v):.2f}"
    except Exception:
        return str(v) if v is not None else default


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def _cover(img: Image.Image) -> Image.Image:
    return ImageOps.fit(
        img.convert("RGB"),
        (W, H),
        method=Image.Resampling.LANCZOS,
        centering=(0.50, 0.45),
    )


def _contract(trade: Dict[str, Any]) -> str:
    symbol = str(trade.get("symbol", "SPXW")).upper().replace("$", "").strip()
    strike_raw = trade.get("strike", "3900")
    try:
        strike = str(int(float(strike_raw)))
    except Exception:
        strike = str(strike_raw).replace(".0", "")

    expiry = str(trade.get("expiry", "08 Mar 24")).strip()
    typ = str(trade.get("type", "CALL")).upper().strip()
    typ = "Call" if typ == "CALL" else "Put" if typ == "PUT" else typ.title()

    # السطر المطلوب
    return f"{symbol} {strike} {expiry} {typ}"


def generate_trade_card(trade: Dict[str, Any], current_price: Any = None, status: str = "OPEN") -> str:
    """
    يولد صورة عقد بنفس الستايل المعتمد:
    - خلفية الروبوت
    - جدول يمين بدون إطار
    - بيانات العقد بسطر واحد
    - Watermark شفاف BAM | SPX
    """
    bg_path = Path(__file__).with_name("card_bg.png")

    if bg_path.exists():
        bg = _cover(Image.open(bg_path)).filter(ImageFilter.GaussianBlur(1.2))
    else:
        bg = Image.new("RGB", (W, H), "black")

    # تغميق الخلفية حتى تظهر الكتابة دائمًا
    img = Image.alpha_composite(bg.convert("RGBA"), Image.new("RGBA", (W, H), (0, 0, 0, 92)))

    # تظليل خفيف يسار ويمين
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    sd.rectangle((0, 0, 430, H), fill=(0, 0, 0, 70))
    sd.rectangle((1120, 0, W, H), fill=(0, 0, 0, 54))
    img = Image.alpha_composite(img, shade)

    d = ImageDraw.Draw(img)

    # الخطوط
    f_contract = _font(40, False)
    f_price = _font(118, True)
    f_change = _font(46, True)
    f_label = _font(34, False)
    f_val = _font(42, True)
    f_sub = _font(31, False)
    f_wm = _font(94, True)

    # سهم رجوع
    d.line([(95, 155), (60, 190), (95, 225)], fill=WHITE, width=7, joint="curve")

    # بيانات العقد - مقابل السهم
    d.text((145, 166), _contract(trade), font=f_contract, fill=WHITE)

    # السعر والربح
    entry = _to_float(trade.get("entry"), 0.0)
    cur = _to_float(current_price if current_price is not None else trade.get("last_price", entry), entry)

    diff = cur - entry if entry else 0.0
    pct = (diff / entry * 100) if entry else 0.0
    color = GREEN if diff >= 0 else RED
    sign = "+" if diff >= 0 else ""

    d.text((88, 355), _money(cur), font=f_price, fill=color)
    d.text((92, 520), f"▲ {sign}{diff:.2f}  {sign}{pct:.2f}%", font=f_change, fill=color)

    # Watermark شفاف في منتصف الصورة
    wm = "BAM | SPX"
    bbox = d.textbbox((0, 0), wm, font=f_wm)
    wm_x = (W - (bbox[2] - bbox[0])) // 2
    wm_y = 410
    d.text((wm_x, wm_y), wm, font=f_wm, fill=(255, 255, 255, 34))

    # بيانات الجدول يمين بدون إطار خارجي
    bid = trade.get("bid", max(cur - 0.05, 0))
    ask = trade.get("ask", cur + 0.05)
    openp = trade.get("open", entry or cur)
    high = trade.get("high", max(entry or cur, cur))
    mid = trade.get("mid", cur)
    vol = trade.get("volume", trade.get("vol", "--"))

    x0, y0 = 1190, 150
    col_gap, row_gap = 190, 185

    def cell(c: int, r: int, label: str, value: Any, fill=WHITE, sub: Any = None):
        x = x0 + c * col_gap
        y = y0 + r * row_gap
        d.text((x, y), label, font=f_label, fill=WHITE)
        d.text((x, y + 56), str(value), font=f_val, fill=fill)
        if sub is not None:
            d.text((x, y + 110), str(sub), font=f_sub, fill=WHITE)

    cell(0, 0, "Bid", _money(bid), GREEN, trade.get("bid_size", "33"))
    cell(1, 0, "Ask", _money(ask), RED, trade.get("ask_size", "40"))
    cell(0, 1, "Open", _money(openp))
    cell(1, 1, "High", _money(high))
    cell(0, 2, "Mid", _money(mid))
    cell(1, 2, "Volume", str(vol))

    # خطوط داخلية فقط - بدون إطار خارجي
    for yy in (y0 + 128, y0 + 128 + row_gap):
        d.line((x0 - 10, yy, x0 + col_gap * 2 + 95, yy), fill=LINE, width=2)

    for yy in (y0 - 10, y0 + row_gap - 10, y0 + row_gap * 2 - 10):
        d.line((x0 + col_gap - 45, yy, x0 + col_gap - 45, yy + 125), fill=LINE, width=2)

    out = OUT_DIR / f"card_{uuid.uuid4().hex}.jpg"
    img.convert("RGB").save(out, "JPEG", quality=95)
    return str(out)
