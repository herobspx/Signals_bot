from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import pytz
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

# Telegram-optimized landscape: the card fills the whole image.
W, H = 1536, 1024

CARD_X1, CARD_Y1 = 20, 20
CARD_X2, CARD_Y2 = 1580, 880
CARD_W = CARD_X2 - CARD_X1
CARD_H = CARD_Y2 - CARD_Y1

OUT_DIR = Path(os.environ.get("CARD_OUT_DIR", "/tmp/bamspx_cards"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

ET_TZ = pytz.timezone("America/New_York")

GREEN = (38, 255, 120)
RED = (255, 48, 96)
WHITE = (246, 248, 255)
MUTED = (178, 183, 196)
BORDER = (105, 110, 122)
LINE = (255, 255, 255, 40)


def _font(size: int, bold: bool = False):
    here = Path(__file__).parent
    candidates = [
        str(here / ("Cairo-Bold.ttf" if bold else "Cairo-Regular.ttf")),
        str(here / "Cairo-Bold.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for p in candidates:
        try:
            if Path(p).exists():
                return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def _money(v: Any, default: str = "--") -> str:
    try:
        if v is None or v == "":
            return default
        return f"{float(v):.2f}"
    except Exception:
        return str(v) if v is not None else default


def _fmt_volume(v: Any) -> str:
    try:
        n = float(v)
    except Exception:
        return str(v) if v not in (None, "") else "--"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.2f}K"
    return str(int(n))


def _mask(size, radius):
    m = Image.new("L", size, 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return m


def _load_card_bg() -> Image.Image:
    bg_path = Path(__file__).with_name("NEW_BG.png")

    if bg_path.exists():
        raw = Image.open(bg_path).convert("RGB")
    else:
        raw = Image.new("RGB", (CARD_W, CARD_H), (5, 6, 10))

    bg = ImageOps.fit(
        raw,
        (CARD_W, CARD_H),
        method=Image.Resampling.LANCZOS,
        centering=(0.55, 0.46),
    )

    return bg.filter(ImageFilter.GaussianBlur(0.05))


def generate_trade_card(contract_data: Dict[str, Any], current_price: Any = None, status: str = "OPEN") -> str:
    cd = contract_data or {}

    canvas = Image.new("RGBA", (W, H), (8, 8, 12, 255))
    bg = _load_card_bg().convert("RGBA")

    # Keep robot as watermark and make stats readable.
    card = Image.alpha_composite(bg, Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 128)))

    # approved-style left/bottom darkness
    vignette = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    vd = ImageDraw.Draw(vignette)

    for x in range(820):
        a = int(110 * (1 - x / 820))
        vd.line((x, 0, x, CARD_H), fill=(0, 0, 0, max(a, 0)))

    for y in range(CARD_H):
        if y > 430:
            a = min(120, int((y - 430) / (CARD_H - 430) * 120))
            vd.line((0, y, CARD_W, y), fill=(0, 0, 0, a))

    card = Image.alpha_composite(card, vignette)

    m = _mask((CARD_W, CARD_H), 52)
    canvas.paste(card, (CARD_X1, CARD_Y1), m)

    d = ImageDraw.Draw(canvas)

    # clean outer border only
    d.rounded_rectangle(
        (CARD_X1, CARD_Y1, CARD_X2, CARD_Y2),
        radius=52,
        outline=(*BORDER, 175),
        width=3,
    )

    symbol = str(cd.get("symbol", "SPXW")).strip()

    # If symbol is too long, reduce font slightly.
    f_contract = _font(52, True)
    if len(symbol) > 31:
        f_contract = _font(46, True)

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

    f_price = _font(170, True)
    f_change = _font(52, True)
    f_time = _font(43, False)
    f_label = _font(45, True)
    f_value = _font(48, True)
    f_icon = _font(46, True)

    ox, oy = CARD_X1, CARD_Y1

    # Header
    d.text((ox + 82, oy + 72), symbol, font=f_contract, fill=WHITE)

    # Header icons
    d.rounded_rectangle(
        (ox + 1200, oy + 64, ox + 1272, oy + 136),
        radius=14,
        outline=(190, 196, 210, 145),
        width=2,
        fill=(20, 22, 28, 90),
    )
    d.text((ox + 1236, oy + 98), "•••", font=f_icon, fill=WHITE, anchor="mm")

    d.rounded_rectangle(
        (ox + 1300, oy + 64, ox + 1372, oy + 136),
        radius=14,
        outline=(255, 190, 80, 175),
        width=2,
        fill=(155, 94, 22, 230),
    )
    d.text((ox + 1336, oy + 100), "⚡", font=_font(46, True), fill=(255, 238, 190), anchor="mm")

    # Price
    shown_text = _money(shown)
    d.text((ox + 82, oy + 185), shown_text, font=f_price, fill=color)

    price_w = d.textbbox((0, 0), shown_text, font=f_price)[2]
    change_x = ox + 82 + price_w + 42
    d.text((change_x, oy + 230), f"{sign}{diff:.2f}", font=f_change, fill=color)
    d.text((change_x, oy + 300), f"{sign}{pct:.2f}%", font=f_change, fill=color)

    now_et = datetime.now(ET_TZ)
    d.text((ox + 82, oy + 420), f"Open {now_et.strftime('%m/%d %H:%M')} ET", font=f_time, fill=MUTED)

    # Stats layout exactly like approved design: big readable rows, not compressed.
    left_label_x = ox + 82
    left_value_x = ox + 610

    right_label_x = ox + 735
    right_value_x = ox + 1370

    rows_y = [oy + 520, oy + 610, oy + 700]

    def row(y, ll, lv, lc, rl, rv, rc, draw_line=True):
        d.text((left_label_x, y), ll, font=f_label, fill=WHITE)
        d.text((left_value_x, y), str(lv), font=f_value, fill=lc, anchor="ra")

        d.text((right_label_x, y), rl, font=f_label, fill=WHITE)
        d.text((right_value_x, y), str(rv), font=f_value, fill=rc, anchor="ra")

        if draw_line:
            d.line((left_label_x, y + 62, left_value_x, y + 62), fill=LINE, width=2)
            d.line((right_label_x, y + 62, right_value_x, y + 62), fill=LINE, width=2)

    row(rows_y[0], "Open", _money(open_p), color, "Mid", _money(mid), WHITE)
    row(rows_y[1], "Open Int", str(oi), WHITE, "Volume", _fmt_volume(vol), WHITE)
    row(rows_y[2], "High", _money(high), color, "Low", _money(low), RED, draw_line=False)

    out = OUT_DIR / f"card_{uuid.uuid4().hex}.jpg"
    canvas.convert("RGB").save(out, "JPEG", quality=98)
    return str(out)
