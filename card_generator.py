from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import pytz
from PIL import Image, ImageDraw, ImageFont, ImageOps

W, H = 1600, 900

OUT_DIR = Path(os.environ.get("CARD_OUT_DIR", "/tmp/bamsignals_cards"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

ET_TZ = pytz.timezone("America/New_York")

GREEN = (53, 255, 146)
RED = (255, 72, 120)
WHITE = (240, 240, 240)
MUTED = (160, 160, 170)


def _font(size: int, bold: bool = False):
    here = Path(__file__).parent

    candidates = [
        str(here / ("SF-Pro-Display-Bold.otf" if bold else "SF-Pro-Display-Regular.otf")),
        str(here / "SF-Pro-Display-Bold.otf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]

    for path in candidates:
        try:
            if Path(path).exists():
                return ImageFont.truetype(path, size)
        except Exception:
            pass

    return ImageFont.load_default()


def _money(v: Any, default: str = "--") -> str:
    try:
        if v is None or v == "":
            return default
        return f"{float(v):.2f}"
    except Exception:
        return default


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def generate_trade_card(contract_data: Dict[str, Any], current_price=None):
    cd = contract_data or {}

    bg_path = Path(__file__).with_name("card_bg.png")

    if bg_path.exists():
        bg = Image.open(bg_path).convert("RGB")
        bg = ImageOps.fit(bg, (W, H), method=Image.Resampling.LANCZOS)
    else:
        bg = Image.new("RGB", (W, H), (5, 5, 5))

    img = bg.copy()
    d = ImageDraw.Draw(img)

    symbol = str(cd.get("symbol", "SPXW")).strip()

    mid = _to_float(cd.get("mid"), 0.0)
    open_p = _to_float(cd.get("open"), 0.0)
    shown = _to_float(current_price, mid)

    diff = shown - open_p
    pct = ((diff / open_p) * 100) if open_p else 0

    color = GREEN if diff >= 0 else RED
    sign = "+" if diff >= 0 else ""

    f_symbol = _font(44, True)
    f_price = _font(110, True)
    f_change = _font(34, True)
    f_meta = _font(24, False)
    f_label = _font(30, False)
    f_value = _font(32, True)

    d.text((55, 120), symbol, font=f_symbol, fill=WHITE)

    d.text((55, 250), _money(shown), font=f_price, fill=color)

    d.text(
        (300, 255),
        f"{sign}{diff:.2f}\n{sign}{pct:.2f}%",
        font=f_change,
        fill=color,
        spacing=0,
    )

    now_et = datetime.now(ET_TZ)
    d.text(
        (55, 430),
        f"Open {now_et.strftime('%m/%d %H:%M')} ET",
        font=f_meta,
        fill=MUTED,
    )

    left_x = 55
    right_x = 720

    rows_y = [560, 640, 720]

    left_labels = ["Open", "Open Int", "High"]
    right_labels = ["Mid", "Volume", "Low"]

    left_values = [
        _money(open_p),
        str(cd.get("open_interest", "--")),
        _money(cd.get("high", 0)),
    ]

    right_values = [
        _money(mid),
        str(cd.get("volume", "--")),
        _money(cd.get("low", 0)),
    ]

    for i, y in enumerate(rows_y):
        d.text((left_x, y), left_labels[i], font=f_label, fill=WHITE)
        d.text((left_x + 220, y), left_values[i], font=f_value,
               fill=GREEN if i != 1 else WHITE)

        d.text((right_x, y), right_labels[i], font=f_label, fill=WHITE)
        d.text((right_x + 220, y), right_values[i], font=f_value,
               fill=RED if i == 2 else WHITE)

    out = OUT_DIR / f"card_{uuid.uuid4().hex}.jpg"
    img.save(out, "JPEG", quality=95)

    return str(out)
