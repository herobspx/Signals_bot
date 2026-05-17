from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import pytz
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

W, H = 1536, 864

CARD_X1, CARD_Y1 = 74, 105
CARD_X2, CARD_Y2 = 1462, 805
CARD_W = CARD_X2 - CARD_X1
CARD_H = CARD_Y2 - CARD_Y1

OUT_DIR = Path(os.environ.get("CARD_OUT_DIR", "/tmp/bamsignals_cards"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

ET_TZ = pytz.timezone("America/New_York")

GREEN = (47, 214, 82)
RED = (255, 62, 110)
WHITE = (245, 247, 255)
MUTED = (174, 178, 190)
BORDER = (92, 96, 108)


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


def _rounded_mask(size, radius):
    mask = Image.new("L", size, 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return mask


def _fit_bg(path: Path) -> Image.Image:
    if path.exists():
        bg = Image.open(path).convert("RGB")
    else:
        bg = Image.new("RGB", (CARD_W, CARD_H), (6, 7, 10))

    bg = ImageOps.fit(
        bg,
        (CARD_W, CARD_H),
        method=Image.Resampling.LANCZOS,
        centering=(0.57, 0.46),
    )
    return bg.filter(ImageFilter.GaussianBlur(0.25))


def generate_trade_card(
    contract_data: Dict[str, Any],
    current_price: Any = None,
    status: str = "OPEN",
) -> str:

    cd = contract_data or {}

    bg_path = Path(__file__).with_name("card_bg.png")

    base = Image.new("RGBA", (W, H), (0, 0, 0, 255))

    card_bg = _fit_bg(bg_path).convert("RGBA")

    dark = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 138))
    card = Image.alpha_composite(card_bg, dark)

    shade = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    for i in range(760):
        a = int(125 * (1 - i / 760))
        sd.line((i, 0, i, CARD_H), fill=(0, 0, 0, max(a, 0)))
    card = Image.alpha_composite(card, shade)

    mask = _rounded_mask((CARD_W, CARD_H), 48)
    base.paste(card, (CARD_X1, CARD_Y1), mask)

    d = ImageDraw.Draw(base)

    d.rounded_rectangle(
        (CARD_X1, CARD_Y1, CARD_X2, CARD_Y2),
        radius=48,
        outline=(*BORDER, 210),
        width=3,
    )

    d.rounded_rectangle(
        (CARD_X1 + 2, CARD_Y1 + 2, CARD_X2 - 2, CARD_Y2 - 2),
        radius=46,
        outline=(255, 255, 255, 35),
        width=1,
    )

    f_contract = _font(45, True)
    f_price = _font(148, True)
    f_change = _font(48, True)
    f_open_ts = _font(38, False)
    f_label = _font(39, True)
    f_val = _font(42, True)
    f_icon = _font(42, True)

    symbol = str(cd.get("symbol", "SPXW")).strip()

    mid = _to_float(cd.get("mid"), 0.0)
    open_p = _to_float(cd.get("open"), 0.0)
    shown = _to_float(current_price, mid) if current_price is not None else mid

    diff = shown - open_p if open_p else 0.0
    pct = (diff / open_p * 100) if open_p else 0.0

    color = GREEN if diff >= 0 else RED
    sign = "+" if diff >= 0 else ""

    high = _to_float(cd.get("high"), 0.0)
    low = _to_float(cd.get("low"), 0.0)
    oi = cd.get("open_interest", cd.get("oi", "--"))
    vol = cd.get("volume", cd.get("vol", "--"))

    ox, oy = CARD_X1, CARD_Y1

    d.text((ox + 78, oy + 78), symbol, font=f_contract, fill=WHITE)

    d.rounded_rectangle((ox + 1132, oy + 80, ox + 1192, oy + 140), radius=12, outline=(170, 175, 190, 120), width=2)
    d.text((ox + 1162, oy + 107), "•••", font=f_icon, fill=WHITE, anchor="mm")

    d.rounded_rectangle((ox + 1228, oy + 80, ox + 1292, oy + 140), radius=12, fill=(145, 88, 24, 220), outline=(240, 185, 90, 180), width=2)
    d.text((ox + 1260, oy + 110), "⚡", font=_font(38, True), fill=(255, 236, 180), anchor="mm")

    d.text((ox + 76, oy + 185), _money(shown), font=f_price, fill=color)

    price_w = d.textbbox((0, 0), _money(shown), font=f_price)[2]
    change_x = ox + 76 + price_w + 35

    d.text((change_x, oy + 218), f"{sign}{diff:.2f}", font=f_change, fill=color)
    d.text((change_x, oy + 282), f"{sign}{pct:.2f}%", font=f_change, fill=color)

    now_et = datetime.now(ET_TZ)
    open_ts = f"Open {now_et.strftime('%m/%d %H:%M')} ET"
    d.text((ox + 80, oy + 380), open_ts, font=f_open_ts, fill=MUTED)

    LX_LBL = ox + 80
    LX_VAL = ox + 560
    RX_LBL = ox + 720
    RX_VAL = ox + 1290

    y1 = oy + 475
    gap = 82

    def draw_row(y, left_label, left_val, left_color, right_label, right_val, right_color):
        d.text((LX_LBL, y), left_label, font=f_label, fill=WHITE)
        d.text((LX_VAL, y), str(left_val), font=f_val, fill=left_color, anchor="ra")

        d.text((RX_LBL, y), right_label, font=f_label, fill=WHITE)
        d.text((RX_VAL, y), str(right_val), font=f_val, fill=right_color, anchor="ra")

    open_col = GREEN if shown >= open_p else RED

    draw_row(y1, "Open", _money(open_p), open_col, "Mid", _money(mid), WHITE)
    draw_row(y1 + gap, "Open Int", str(oi), WHITE, "Volume", _fmt_volume(vol), WHITE)
    draw_row(y1 + gap * 2, "High", _money(high), GREEN, "Low", _money(low), RED)

    out = OUT_DIR / f"card_{uuid.uuid4().hex}.jpg"
    base.convert("RGB").save(out, "JPEG", quality=96)
    return str(out)
