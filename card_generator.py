from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import pytz
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

# Final approved BAM | SPX card layout
# Dimensions match the approved mockup style.
W, H = 1536, 1024

CARD_X1, CARD_Y1 = 55, 95
CARD_X2, CARD_Y2 = 1481, 900
CARD_W = CARD_X2 - CARD_X1
CARD_H = CARD_Y2 - CARD_Y1

OUT_DIR = Path(os.environ.get("CARD_OUT_DIR", "/tmp/bamspx_cards"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

ET_TZ = pytz.timezone("America/New_York")

GREEN = (38, 255, 120)
RED = (255, 48, 96)
WHITE = (246, 248, 255)
MUTED = (175, 180, 192)
BORDER = (120, 124, 136)
LINE = (255, 255, 255, 42)
GOLD = (245, 168, 48)


def _font(size: int, bold: bool = False):
    here = Path(__file__).parent

    candidates = [
        str(here / ("Cairo-Bold.ttf" if bold else "Cairo-Regular.ttf")),
        str(here / "Cairo-Bold.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf" if bold else "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
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


def _rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return mask


def _load_background() -> Image.Image:
    """
    Required preferred asset:
        card_bg.png
    Put it in the same folder as card_generator.py.

    The code positions the robot exactly like the approved design:
    large watermark robot on the center/right, while numbers remain readable.
    """
    bg_path = Path(__file__).with_name("card_bg.png")

    if bg_path.exists():
        bg = Image.open(bg_path).convert("RGB")
    else:
        bg = Image.new("RGB", (CARD_W, CARD_H), (6, 7, 11))

    bg = ImageOps.fit(
        bg,
        (CARD_W, CARD_H),
        method=Image.Resampling.LANCZOS,
        centering=(0.57, 0.45),
    )

    # Very light blur only. Approved design is sharp, not foggy.
    return bg.filter(ImageFilter.GaussianBlur(0.10))


def _add_soft_reflection(canvas: Image.Image, card: Image.Image):
    try:
        reflection_h = 135
        reflection = card.crop((0, CARD_H - reflection_h, CARD_W, CARD_H))
        reflection = ImageOps.flip(reflection).resize((CARD_W, reflection_h), Image.Resampling.LANCZOS)
        reflection = reflection.filter(ImageFilter.GaussianBlur(3))

        fade = Image.new("L", (CARD_W, reflection_h), 0)
        fd = ImageDraw.Draw(fade)
        for y in range(reflection_h):
            alpha = max(0, int(48 * (1 - y / reflection_h)))
            fd.line((0, y, CARD_W, y), fill=alpha)

        canvas.paste(reflection, (CARD_X1, CARD_Y2 + 4), fade)
    except Exception:
        pass


def generate_trade_card(
    contract_data: Dict[str, Any],
    current_price: Any = None,
    status: str = "OPEN",
) -> str:
    cd = contract_data or {}

    # Canvas
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 255))

    # Card background
    bg = _load_background().convert("RGBA")

    # Contrast overlays: keep robot visible but put data above it.
    dark = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 112))
    card = Image.alpha_composite(bg, dark)

    # Strong left vignette for price area.
    left_vignette = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    vd = ImageDraw.Draw(left_vignette)
    for x in range(760):
        alpha = int(105 * (1 - x / 760))
        vd.line((x, 0, x, CARD_H), fill=(0, 0, 0, max(alpha, 0)))
    card = Image.alpha_composite(card, left_vignette)

    # Bottom vignette for table readability.
    bottom = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bottom)
    for y in range(CARD_H):
        if y > 430:
            alpha = min(120, int((y - 430) / (CARD_H - 430) * 120))
            bd.line((0, y, CARD_W, y), fill=(0, 0, 0, alpha))
    card = Image.alpha_composite(card, bottom)

    # Paste rounded card
    mask = _rounded_mask((CARD_W, CARD_H), 48)
    canvas.paste(card, (CARD_X1, CARD_Y1), mask)

    # Reflection from the exact card image
    _add_soft_reflection(canvas, card)

    d = ImageDraw.Draw(canvas)

    # Approved single soft outer border only
    d.rounded_rectangle(
        (CARD_X1, CARD_Y1, CARD_X2, CARD_Y2),
        radius=48,
        outline=(*BORDER, 165),
        width=3,
    )
    d.rounded_rectangle(
        (CARD_X1 + 2, CARD_Y1 + 2, CARD_X2 - 2, CARD_Y2 - 2),
        radius=46,
        outline=(255, 255, 255, 24),
        width=1,
    )

    # Data
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

    # Fonts tuned to the approved design
    f_contract = _font(48, True)
    f_price = _font(152, True)
    f_change = _font(48, True)
    f_time = _font(42, False)
    f_label = _font(42, True)
    f_value = _font(44, True)
    f_icon = _font(42, True)

    ox, oy = CARD_X1, CARD_Y1

    # Header
    d.text((ox + 82, oy + 72), symbol, font=f_contract, fill=WHITE)

    # Header icons, same position/style as mockup
    d.rounded_rectangle(
        (ox + 1168, oy + 64, ox + 1235, oy + 132),
        radius=12,
        outline=(190, 196, 210, 150),
        width=2,
        fill=(20, 22, 28, 95),
    )
    d.text((ox + 1202, oy + 95), "•••", font=f_icon, fill=WHITE, anchor="mm")

    d.rounded_rectangle(
        (ox + 1268, oy + 64, ox + 1337, oy + 132),
        radius=12,
        outline=(255, 190, 80, 180),
        width=2,
        fill=(155, 94, 22, 230),
    )
    d.text((ox + 1303, oy + 98), "⚡", font=_font(42, True), fill=(255, 238, 190), anchor="mm")

    # Price block
    shown_text = _money(shown)
    d.text((ox + 82, oy + 178), shown_text, font=f_price, fill=color)

    price_w = d.textbbox((0, 0), shown_text, font=f_price)[2]
    change_x = ox + 82 + price_w + 42

    d.text((change_x, oy + 218), f"{sign}{diff:.2f}", font=f_change, fill=color)
    d.text((change_x, oy + 285), f"{sign}{pct:.2f}%", font=f_change, fill=color)

    # Open timestamp
    now_et = datetime.now(ET_TZ)
    open_ts = f"Open {now_et.strftime('%m/%d %H:%M')} ET"
    d.text((ox + 82, oy + 390), open_ts, font=f_time, fill=MUTED)

    # Stats grid: exactly low-center like the approved design
    left_label_x = ox + 82
    left_value_x = ox + 585

    right_label_x = ox + 720
    right_value_x = ox + 1340

    row_y = [oy + 500, oy + 585, oy + 670]

    def draw_row(y, l_label, l_value, l_color, r_label, r_value, r_color):
        # labels
        d.text((left_label_x, y), l_label, font=f_label, fill=WHITE)
        d.text((right_label_x, y), r_label, font=f_label, fill=WHITE)

        # values
        d.text((left_value_x, y), str(l_value), font=f_value, fill=l_color, anchor="ra")
        d.text((right_value_x, y), str(r_value), font=f_value, fill=r_color, anchor="ra")

        # soft separator lines under the first two rows only
        if y != row_y[-1]:
            d.line((left_label_x, y + 58, left_value_x, y + 58), fill=LINE, width=2)
            d.line((right_label_x, y + 58, right_value_x, y + 58), fill=LINE, width=2)

    open_col = color

    draw_row(row_y[0], "Open", _money(open_p), open_col, "Mid", _money(mid), WHITE)
    draw_row(row_y[1], "Open Int", str(oi), WHITE, "Volume", _fmt_volume(vol), WHITE)
    draw_row(row_y[2], "High", _money(high), color, "Low", _money(low), RED)

    out = OUT_DIR / f"card_{uuid.uuid4().hex}.jpg"
    canvas.convert("RGB").save(out, "JPEG", quality=97)
    return str(out)
