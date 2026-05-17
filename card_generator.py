from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import pytz
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

# مقاس مناسب لتيليجرام كصورة عرضية واضحة
W, H = 1536, 864

OUT_DIR = Path(os.environ.get("CARD_OUT_DIR", "/tmp/bamsignals_cards"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

ET_TZ = pytz.timezone("America/New_York")

GREEN = (47, 214, 82)
RED = (255, 70, 83)
WHITE = (245, 245, 245)
MUTED = (168, 171, 180)


def _font(size: int, bold: bool = False):
    """خط Cairo المرفوع مع الريبو أولاً، ثم خطوط النظام، ثم الافتراضي."""
    here = Path(__file__).parent
    candidates = []
    if bold:
        candidates += [
            str(here / "Cairo-Bold.ttf"),
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        ]
    else:
        candidates += [
            str(here / "Cairo-Bold.ttf"),
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        ]

    for p in candidates:
        try:
            if Path(p).exists():
                return ImageFont.truetype(p, size)
        except Exception:
            pass

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


def _fmt_volume(v: Any) -> str:
    """يحوّل 52200 -> 52.20K و 1840000 -> 1.84M"""
    try:
        n = float(v)
    except Exception:
        return str(v) if v not in (None, "") else "--"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.2f}K"
    return str(int(n))


def _cover(img: Image.Image) -> Image.Image:
    return ImageOps.fit(
        img.convert("RGB"),
        (W, H),
        method=Image.Resampling.LANCZOS,
        centering=(0.50, 0.42),
    )


def generate_trade_card(
    contract_data: Dict[str, Any],
    current_price: Any = None,
    status: str = "OPEN",
) -> str:
    """
    يولّد صورة العقد بالتصميم المعتمد.

    contract_data المتوقع:
    {
        "symbol": "SPXW 6700 17 May 26 Call",
        "bid": 5.30, "ask": 5.40, "mid": 5.35,
        "open": 3.90, "high": 5.70, "low": 1.90,
        "volume": 184, "open_interest": 920
    }

    - السعر الكبير = mid (أو current_price لو انمرّر للتتبع المباشر)
    - التغير ($ والنسبة) = mid مقابل open
    """
    cd = contract_data or {}

    # ── الخلفية ──
    bg_path = Path(__file__).with_name("card_bg.png")
    if bg_path.exists():
        bg = _cover(Image.open(bg_path)).filter(ImageFilter.GaussianBlur(0.6))
    else:
        bg = Image.new("RGB", (W, H), (8, 8, 10))

    img = Image.alpha_composite(
        bg.convert("RGBA"), Image.new("RGBA", (W, H), (0, 0, 0, 70))
    )

    # تظليل تدرّجي على اليسار (منطقة النص)
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    for i in range(820):
        a = int(150 * (1 - i / 820))
        sd.line((i, 0, i, H), fill=(0, 0, 0, max(a, 0)))
    img = Image.alpha_composite(img, shade)

    d = ImageDraw.Draw(img)

    # ── الخطوط ──
    f_contract = _font(46, True)
    f_price = _font(150, True)
    f_change = _font(50, True)
    f_open_ts = _font(40, False)
    f_label = _font(40, False)
    f_val = _font(46, True)

    # ── القيم ──
    symbol = str(cd.get("symbol", "SPXW")).strip()
    mid = _to_float(cd.get("mid"), 0.0)
    open_p = _to_float(cd.get("open"), 0.0)

    shown = _to_float(current_price, mid) if current_price is not None else mid

    diff = shown - open_p if open_p else 0.0
    pct = (diff / open_p * 100) if open_p else 0.0
    color = GREEN if diff >= 0 else RED
    sign = "+" if diff >= 0 else ""
    arrow = "\u25b2" if diff >= 0 else "\u25bc"

    # ── سطر العقد ──
    d.text((150, 230), symbol, font=f_contract, fill=WHITE)

    # ── السعر الكبير ──
    d.text((146, 320), _money(shown), font=f_price, fill=color)

    # ── التغير ($ ثم %) على يمين السعر ──
    price_w = d.textbbox((0, 0), _money(shown), font=f_price)[2]
    cx = 146 + price_w + 45
    d.text((cx, 345), f"{arrow} {sign}{diff:.2f}", font=f_change, fill=color)
    d.text((cx, 408), f"{sign}{pct:.2f}%", font=f_change, fill=color)

    # ── سطر Open + الوقت ──
    now_et = datetime.now(ET_TZ)
    open_ts = f"Open {now_et.strftime('%m/%d %H:%M')} ET"
    d.text((150, 520), open_ts, font=f_open_ts, fill=MUTED)

    # ── الجدول: عمودين × 3 صفوف ──
    bid = _to_float(cd.get("bid"), 0.0)
    ask = _to_float(cd.get("ask"), 0.0)
    high = _to_float(cd.get("high"), 0.0)
    low = _to_float(cd.get("low"), 0.0)
    oi = cd.get("open_interest", cd.get("oi", "--"))
    vol = cd.get("volume", cd.get("vol", "--"))

    LX_LBL, LX_VAL = 150, 560
    RX_LBL, RX_VAL = 800, 1380
    ROW_Y = [620, 700, 780]

    def row(y, l_lbl, l_val, l_col, r_lbl, r_val, r_col):
        d.text((LX_LBL, y), l_lbl, font=f_label, fill=WHITE)
        d.text((LX_VAL, y), str(l_val), font=f_val, fill=l_col, anchor="ra")
        d.text((RX_LBL, y), r_lbl, font=f_label, fill=WHITE)
        d.text((RX_VAL, y), str(r_val), font=f_val, fill=r_col, anchor="ra")

    open_col = GREEN if (open_p and shown >= open_p) else RED

    row(ROW_Y[0], "Open", _money(open_p), open_col, "Mid", _money(mid), WHITE)
    row(ROW_Y[1], "Open Int", oi, WHITE, "Volume", _fmt_volume(vol), WHITE)
    row(ROW_Y[2], "High", _money(high), GREEN, "Low", _money(low), RED)

    out = OUT_DIR / f"card_{uuid.uuid4().hex}.jpg"
    img.convert("RGB").save(out, "JPEG", quality=95)
    return str(out)
