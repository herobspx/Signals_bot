import os
import math
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

BASE_DIR = Path(__file__).resolve().parent
ASSET_BG = BASE_DIR / "assets" / "card_bg.png"
OUT_DIR = BASE_DIR / "tmp" / "bamsignals_cards"
OUT_DIR.mkdir(parents=True, exist_ok=True)

W, H = 1792, 1024
GREEN = (57, 214, 91)
RED = (255, 76, 91)
WHITE = (245, 245, 245)
MUTED = (186, 186, 190)
LINE = (64, 64, 68)
BLACK = (0, 0, 0)


def _font(size: int, bold: bool = False):
    candidates = []
    if bold:
        candidates += [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/Library/Fonts/Arial Bold.ttf",
        ]
    candidates += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return ImageFont.truetype(p, size=size)
    return ImageFont.load_default()


def _fmt_price(x, default="--"):
    try:
        if x is None:
            return default
        return f"{float(x):.2f}"
    except Exception:
        s = str(x).strip()
        return s if s else default


def _fmt_num(x, default="--"):
    try:
        if x is None:
            return default
        v = float(x)
        if abs(v) >= 1_000_000:
            return f"{v/1_000_000:.2f}M"
        if abs(v) >= 1_000:
            return f"{v/1_000:.2f}K"
        if v.is_integer():
            return str(int(v))
        return f"{v:.2f}"
    except Exception:
        s = str(x).strip()
        return s if s else default


def _parse_expiry(expiry: str):
    expiry = str(expiry or "").strip()
    for fmt in ("%d%b%y", "%d%b%Y", "%d%B%y", "%d%B%Y", "%d/%m/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(expiry, fmt)
        except Exception:
            pass
    return None


def _expiry_display(expiry: str) -> str:
    dt = _parse_expiry(expiry)
    if not dt:
        return str(expiry or "")
    # نفس الستايل: 08 Mar 24 (W)
    return dt.strftime("%d %b %y") + " (W)"


def _cover_bg(path: Path):
    if path.exists():
        bg = Image.open(path).convert("RGB")
    else:
        bg = Image.new("RGB", (W, H), BLACK)
    bw, bh = bg.size
    scale = max(W / bw, H / bh)
    nw, nh = int(bw * scale), int(bh * scale)
    bg = bg.resize((nw, nh), Image.LANCZOS)
    left = (nw - W) // 2
    top = (nh - H) // 2
    bg = bg.crop((left, top, left + W, top + H))

    # تغميق احترافي مع ترك الروبوت ظاهر
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 88))
    bg = Image.alpha_composite(bg.convert("RGBA"), overlay)

    # vignette خفيف من الأطراف
    vig = Image.new("L", (W, H), 0)
    vd = ImageDraw.Draw(vig)
    vd.ellipse((-130, -350, W + 280, H + 380), fill=120)
    vig = vig.filter(ImageFilter.GaussianBlur(120))
    dark = Image.new("RGBA", (W, H), (0, 0, 0, 130))
    clear = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bg = Image.alpha_composite(bg, Image.composite(clear, dark, vig))
    return bg


def _draw_chevron(draw: ImageDraw.ImageDraw, x=70, y=205, size=44):
    draw.line([(x + size, y - size), (x, y), (x + size, y + size)], fill=WHITE, width=8, joint="curve")


def _draw_stat_grid(draw, x, y, data):
    # نفس جدول الصورة: إطار خارجي ناعم + فواصل داخلية متناسقة
    box_w, box_h = 440, 650
    draw.rounded_rectangle((x, y, x + box_w, y + box_h), radius=22, outline=(75, 75, 78), width=3, fill=(0, 0, 0, 78))

    # خطوط أفقية
    row_h = box_h // 3
    for i in (1, 2):
        yy = y + i * row_h
        draw.line((x + 38, yy, x + box_w - 38, yy), fill=LINE, width=3)
    # خطوط عمودية داخل كل صف
    midx = x + box_w // 2
    for i in range(3):
        yy1 = y + i * row_h + 45
        yy2 = y + (i + 1) * row_h - 45
        draw.line((midx, yy1, midx, yy2), fill=LINE, width=3)

    label_f = _font(39)
    value_f = _font(42, True)
    small_f = _font(35)

    cells = [
        ("Bid", data.get("bid"), GREEN, data.get("bid_size", "33")),
        ("Ask", data.get("ask"), RED, data.get("ask_size", "40")),
        ("Open", data.get("open"), WHITE, None),
        ("High", data.get("high"), WHITE, None),
        ("Mid", data.get("mid"), WHITE, None),
        ("Volume", data.get("volume"), WHITE, None),
    ]
    for idx, (label, value, color, size_val) in enumerate(cells):
        row = idx // 2
        col = idx % 2
        cx = x + 55 + col * (box_w // 2)
        cy = y + 47 + row * row_h
        draw.text((cx, cy), label, font=label_f, fill=MUTED)
        draw.text((cx, cy + 58), _fmt_price(value) if label != "Volume" else _fmt_num(value), font=value_f, fill=color)
        if size_val is not None:
            draw.text((cx, cy + 116), str(size_val), font=small_f, fill=WHITE)


def build_trade_card(trade: dict, current_price=None, bid=None, ask=None, output_path=None) -> str:
    """Create the exact landscape card used by BAMSignals."""
    img = _cover_bg(ASSET_BG)
    draw = ImageDraw.Draw(img)

    symbol = str(trade.get("symbol", "SPXW")).upper()
    title_symbol = "SPX" if symbol == "SPXW" else symbol
    strike = trade.get("strike", "")
    try:
        strike_title = str(int(float(strike)))
    except Exception:
        strike_title = str(strike)

    opt_type = str(trade.get("type", "CALL")).upper()
    expiry = trade.get("expiry", "")
    entry = float(trade.get("entry", 0) or 0)
    last = float(current_price if current_price is not None else trade.get("last_price", entry) or entry)

    diff = last - entry if entry else 0.0
    pct = (diff / entry * 100) if entry else 0.0
    up = diff >= 0
    price_color = GREEN if up else RED
    sign = "+" if up else ""

    # Bid/Ask fallback
    if bid is None:
        bid = last - 0.05 if last else None
    if ask is None:
        ask = last + 0.05 if last else None
    bid = max(0, float(bid)) if bid is not None else None
    ask = max(0, float(ask)) if ask is not None else None

    header_f = _font(66, True)
    sub_f = _font(42)
    price_f = _font(150, True)
    chg_f = _font(54, True)

    _draw_chevron(draw, 70, 210, 44)

    # رأس الصورة مثل النموذج
    draw.text((168, 165), f"{title_symbol} {strike_title}", font=header_f, fill=WHITE)
    subline = f"{_expiry_display(expiry)} {opt_type.title()} 100  {symbol}"
    draw.text((168, 260), subline, font=sub_f, fill=MUTED)

    # السعر الحالي — يسار وبارز
    draw.text((74, 460), _fmt_price(last), font=price_f, fill=price_color)
    draw.text((82, 645), f"▲ {sign}{diff:.2f}  {sign}{pct:.2f}%", font=chg_f, fill=price_color)

    # جدول اليمين بالضبط تقريبًا
    stats = {
        "bid": bid,
        "ask": ask,
        "bid_size": trade.get("bid_size", "33"),
        "ask_size": trade.get("ask_size", "40"),
        "open": trade.get("open", entry or last),
        "high": trade.get("high", max(entry, last) if entry else last),
        "mid": trade.get("mid", last),
        "volume": trade.get("volume", "498.68K" if not trade.get("volume") else trade.get("volume")),
    }
    _draw_stat_grid(draw, 1264, 145, stats)

    if output_path is None:
        safe_key = f"{symbol}_{strike_title}_{opt_type}_{int(datetime.now().timestamp()*1000)}.jpg"
        output_path = OUT_DIR / safe_key
    else:
        output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(output_path, quality=94, optimize=True)
    return str(output_path)
