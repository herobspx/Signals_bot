from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import pytz
from PIL import Image, ImageDraw, ImageFont, ImageOps

# BAM | SPX card generator
# - يستخدم card_bg.png بنفس أبعادها الأصلية
# - يمنع خروج النص خارج الخلفية
# - بدون خطوط جدول إضافية
# - بدون إطار إضافي من الكود
# - يدعم SF Pro Display إذا أضفت ملفات الخط داخل نفس المجلد

OUT_DIR = Path(os.environ.get("CARD_OUT_DIR", "/tmp/bamspx_cards"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

ET_TZ = pytz.timezone("America/New_York")

GREEN = (38, 255, 120)
RED = (255, 48, 96)
WHITE = (246, 248, 255)
MUTED = (170, 175, 188)


def _font(size: int, bold: bool = False):
    here = Path(__file__).parent

    candidates = [
        str(here / ("SF-Pro-Display-Bold.otf" if bold else "SF-Pro-Display-Regular.otf")),
        str(here / "SF-Pro-Display-Bold.otf"),
        str(here / ("Cairo-Bold.ttf" if bold else "Cairo-Regular.ttf")),
        str(here / "Cairo-Bold.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
        if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]

    for path in candidates:
        try:
            if Path(path).exists():
                return ImageFont.truetype(path, size)
        except Exception:
            pass

    return ImageFont.load_default()


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _money(value: Any, default: str = "--") -> str:
    try:
        if value is None or value == "":
            return default
        return f"{float(value):.2f}"
    except Exception:
        return str(value) if value is not None else default


def _fmt_volume(value: Any) -> str:
    try:
        n = float(value)
    except Exception:
        return str(value) if value not in (None, "") else "--"

    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.2f}K"
    return str(int(n))


def _fit_font_to_width(draw: ImageDraw.ImageDraw, text: str, max_width: int, start_size: int, bold: bool = True):
    size = start_size
    while size >= 22:
        font = _font(size, bold)
        bbox = draw.textbbox((0, 0), text, font=font)
        if (bbox[2] - bbox[0]) <= max_width:
            return font
        size -= 2
    return _font(22, bold)


def _load_background() -> Image.Image:
    bg_path = Path(__file__).with_name("card_bg.png")

    if bg_path.exists():
        return Image.open(bg_path).convert("RGB")

    # fallback فقط لو الخلفية غير موجودة
    return Image.new("RGB", (1400, 788), (0, 0, 0))


def generate_trade_card(
    contract_data: Dict[str, Any],
    current_price: Any = None,
    status: str = "OPEN",
) -> str:

    cd = contract_data or {}

    bg = _load_background()
    W, H = bg.size

    # نحافظ على أبعاد الخلفية المعتمدة بدون قص أو تمديد
    img = bg.convert("RGBA")
    d = ImageDraw.Draw(img)

    symbol = str(cd.get("symbol", "SPXW")).strip()

    mid = _to_float(cd.get("mid"), 0.0)
    open_p = _to_float(cd.get("open"), 0.0)
    shown = _to_float(current_price, mid) if current_price is not None else mid

    diff = shown - open_p if open_p else 0.0
    pct = (diff / open_p * 100) if open_p else 0.0

    color = GREEN if diff >= 0 else RED
    sign = "+" if diff >= 0 else ""

    high = cd.get("high", "--")
    low = cd.get("low", "--")
    oi = cd.get("open_interest", cd.get("oi", "--"))
    vol = cd.get("volume", cd.get("vol", "--"))

    # الإحداثيات نسبية حسب أبعاد الخلفية، حتى لا يطلع النص خارج الصورة
    x0 = int(W * 0.040)
    y0 = int(H * 0.125)

    max_symbol_w = int(W * 0.56)
    f_symbol = _fit_font_to_width(d, symbol, max_symbol_w, int(H * 0.048), True)

    f_price = _font(int(H * 0.145), True)
    f_change = _font(int(H * 0.040), True)
    f_time = _font(int(H * 0.026), False)
    f_label = _font(int(H * 0.032), False)
    f_value = _font(int(H * 0.034), True)
    f_icon = _font(int(H * 0.034), True)

    # Header
    d.text((x0, y0), symbol, font=f_symbol, fill=WHITE)

    # Icons فقط
    icon_y = int(H * 0.116)
    more_x = int(W * 0.845)
    bolt_x = int(W * 0.920)

    d.text((more_x, icon_y), "•••", font=f_icon, fill=WHITE, anchor="mm")
    d.text((bolt_x, icon_y), "⚡", font=f_icon, fill=(255, 218, 150), anchor="mm")

    # Price
    shown_text = _money(shown)
    price_x = x0
    price_y = int(H * 0.255)
    d.text((price_x, price_y), shown_text, font=f_price, fill=color)

    price_bbox = d.textbbox((0, 0), shown_text, font=f_price)
    price_w = price_bbox[2] - price_bbox[0]

    change_x = price_x + price_w + int(W * 0.030)
    d.text((change_x, int(H * 0.285)), f"{sign}{diff:.2f}", font=f_change, fill=color)
    d.text((change_x, int(H * 0.345)), f"{sign}{pct:.2f}%", font=f_change, fill=color)

    # Open time
    now_et = datetime.now(ET_TZ)
    d.text(
        (x0, int(H * 0.475)),
        f"Open {now_et.strftime('%m/%d %H:%M')} ET",
        font=f_time,
        fill=MUTED,
    )

    # جدول بدون خطوط نهائياً
    left_label_x = x0
    left_value_x = int(W * 0.410)

    right_label_x = int(W * 0.515)
    right_value_x = int(W * 0.900)

    rows_y = [
        int(H * 0.610),
        int(H * 0.705),
        int(H * 0.800),
    ]

    def row(y, left_label, left_value, left_color, right_label, right_value, right_color):
        d.text((left_label_x, y), left_label, font=f_label, fill=WHITE)
        d.text((left_value_x, y), str(left_value), font=f_value, fill=left_color, anchor="ra")

        d.text((right_label_x, y), right_label, font=f_label, fill=WHITE)
        d.text((right_value_x, y), str(right_value), font=f_value, fill=right_color, anchor="ra")

    row(rows_y[0], "Open", _money(open_p), color, "Mid", _money(mid), WHITE)
    row(rows_y[1], "Open Int", str(oi), WHITE, "Volume", _fmt_volume(vol), WHITE)
    row(rows_y[2], "High", _money(high), color, "Low", _money(low), RED)

    out = OUT_DIR / f"card_{uuid.uuid4().hex}.jpg"
    img.convert("RGB").save(out, "JPEG", quality=98)

    return str(out)
