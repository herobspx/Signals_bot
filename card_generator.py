from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import pytz
from PIL import Image, ImageDraw, ImageFont, ImageOps

# BAM | SPX approved background card
# يستخدم الخلفية المعتمدة كما هي: card_bg.png
# بدون رسم أي إطار إضافي
# بدون رسم أي خطوط داخل الجدول
# فقط النصوص والأرقام فوق الخلفية

W, H = 1536, 1024

OUT_DIR = Path(os.environ.get("CARD_OUT_DIR", "/tmp/bamspx_cards"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

ET_TZ = pytz.timezone("America/New_York")

GREEN = (38, 255, 120)
RED = (255, 48, 96)
WHITE = (246, 248, 255)
MUTED = (175, 180, 192)


def _font(size: int, bold: bool = False):
    here = Path(__file__).parent

    candidates = [
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


def _load_background() -> Image.Image:
    bg_path = Path(__file__).with_name("card_bg.png")

    if bg_path.exists():
        bg = Image.open(bg_path).convert("RGB")
    else:
        bg = Image.new("RGB", (W, H), (0, 0, 0))

    # نفس أبعاد الخلفية المعتمدة بدون قص مزعج
    return ImageOps.fit(
        bg,
        (W, H),
        method=Image.Resampling.LANCZOS,
        centering=(0.50, 0.50),
    )


def generate_trade_card(
    contract_data: Dict[str, Any],
    current_price: Any = None,
    status: str = "OPEN",
) -> str:

    cd = contract_data or {}

    img = _load_background().convert("RGBA")

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

    # خطوط كبيرة وواضحة، بدون أي خطوط رسومية
    f_contract = _font(48, True)
    if len(symbol) > 31:
        f_contract = _font(42, True)

    f_price = _font(158, True)
    f_change = _font(50, True)
    f_time = _font(41, False)
    f_label = _font(42, True)
    f_value = _font(45, True)
    f_icon = _font(42, True)

    # نفس توزيع التصميم المعتمد
    # Header
    d.text((150, 205), symbol, font=f_contract, fill=WHITE)

    # أيقونات فقط — لا يوجد إطار للكارد ولا خطوط جدول
    d.text((1192, 206), "•••", font=f_icon, fill=WHITE, anchor="mm")
    d.text((1300, 208), "⚡", font=_font(43, True), fill=(255, 238, 190), anchor="mm")

    # السعر
    shown_text = _money(shown)
    d.text((150, 310), shown_text, font=f_price, fill=color)

    price_w = d.textbbox((0, 0), shown_text, font=f_price)[2]
    change_x = 150 + price_w + 42

    d.text((change_x, 350), f"{sign}{diff:.2f}", font=f_change, fill=color)
    d.text((change_x, 420), f"{sign}{pct:.2f}%", font=f_change, fill=color)

    # وقت الافتتاح
    now_et = datetime.now(ET_TZ)
    d.text(
        (150, 535),
        f"Open {now_et.strftime('%m/%d %H:%M')} ET",
        font=f_time,
        fill=MUTED,
    )

    # جدول بدون أي خطوط
    left_label_x = 150
    left_value_x = 600

    right_label_x = 750
    right_value_x = 1320

    rows_y = [645, 735, 825]

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
