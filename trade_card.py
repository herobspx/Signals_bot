import os
import re
import uuid
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont, ImageFilter

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.environ.get("CARD_OUTPUT_DIR", "/tmp/bamsignals_cards")
os.makedirs(OUT_DIR, exist_ok=True)

CANVAS_W, CANVAS_H = 1600, 900
BG_PATH = os.path.join(BASE_DIR, "card_bg.jpg")

GREEN = (52, 210, 81)
RED = (255, 75, 86)
WHITE = (245, 245, 245)
MUTED = (185, 185, 190)
LINE = (82, 82, 86)
BLACK = (0, 0, 0)


def _font(size: int, bold: bool = False):
    candidates = []
    if bold:
        candidates += [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        ]
    candidates += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _fit_cover(img: Image.Image, size):
    w, h = img.size
    tw, th = size
    scale = max(tw / w, th / h)
    nw, nh = int(w * scale), int(h * scale)
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - tw) // 2
    top = (nh - th) // 2
    return img.crop((left, top, left + tw, top + th))


def _parse_expiry(expiry: str):
    if not expiry:
        return ""
    expiry = str(expiry).strip()
    formats = ["%d%b%y", "%d%b%Y", "%d%B%y", "%d%B%Y", "%d/%m/%y", "%d/%m/%Y", "%Y%m%d"]
    for fmt in formats:
        try:
            dt = datetime.strptime(expiry, fmt)
            return dt.strftime("%d %b %y")
        except Exception:
            pass
    m = re.match(r"(\d{1,2})([a-zA-Z]+)(\d{2,4})", expiry)
    if m:
        d, mon, y = m.groups()
        mon_map = {"jan":"Jan","feb":"Feb","mar":"Mar","apr":"Apr","may":"May","jun":"Jun","jul":"Jul","aug":"Aug","sep":"Sep","oct":"Oct","nov":"Nov","dec":"Dec"}
        mon = mon_map.get(mon.lower(), mon.capitalize())
        y = y[-2:]
        return f"{int(d):02d} {mon} {y}"
    return expiry


def _fmt_num(v, default="--"):
    try:
        if v is None or v == "":
            return default
        return f"{float(v):.2f}"
    except Exception:
        return str(v)


def _compact_volume(v):
    if v is None or v == "":
        return "--"
    try:
        n = float(str(v).replace(",", ""))
        if n >= 1_000_000:
            return f"{n/1_000_000:.2f}M"
        if n >= 1_000:
            return f"{n/1_000:.2f}K"
        return str(int(n))
    except Exception:
        return str(v)


def _text_center(draw, xy, text, font, fill):
    x, y = xy
    bbox = draw.textbbox((0, 0), str(text), font=font)
    tw = bbox[2] - bbox[0]
    draw.text((x - tw / 2, y), str(text), font=font, fill=fill)


def _make_bg():
    if os.path.exists(BG_PATH):
        bg = Image.open(BG_PATH).convert("RGB")
        bg = _fit_cover(bg, (CANVAS_W, CANVAS_H))
    else:
        bg = Image.new("RGB", (CANVAS_W, CANVAS_H), BLACK)

    # darken/soften so text stays readable
    bg = bg.filter(ImageFilter.GaussianBlur(1.0))
    shade = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 78))
    bg = Image.alpha_composite(bg.convert("RGBA"), shade)

    # extra left and right gradients
    grad = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    gp = grad.load()
    for x in range(CANVAS_W):
        # darker left edge and far right edge, transparent center
        a = 0
        if x < 520:
            a = int(105 * (1 - x / 520))
        elif x > 1180:
            a = int(78 * ((x - 1180) / (CANVAS_W - 1180)))
        for y in range(CANVAS_H):
            gp[x, y] = (0, 0, 0, max(0, min(150, a)))
    return Image.alpha_composite(bg, grad).convert("RGB")


def _draw_back_arrow(draw):
    # Webull-like simple chevron
    draw.line([(85, 205), (50, 240), (85, 275)], fill=WHITE, width=8, joint="curve")


def _draw_stats(draw, x=1218, y=146):
    # Always aligned. No outer border. Only internal separators.
    col_gap = 182
    row_gap = 178
    label_font = _font(34, False)
    value_font = _font(42, True)
    small_font = _font(28, False)
    line_w = 2

    def cell(col, row, label, value, sub=None, color=WHITE):
        cx = x + col * col_gap
        cy = y + row * row_gap
        draw.text((cx, cy), label, font=label_font, fill=WHITE)
        draw.text((cx, cy + 48), value, font=value_font, fill=color)
        if sub is not None:
            draw.text((cx, cy + 101), str(sub), font=small_font, fill=WHITE)

    # separators: tidy and consistent
    for r in range(3):
        y1 = y + r * row_gap - 4
        y2 = y1 + 122
        draw.line([(x + 122, y1), (x + 122, y2)], fill=LINE, width=line_w)
    for r in [1, 2]:
        yy = y + r * row_gap - 36
        draw.line([(x - 10, yy), (x + 335, yy)], fill=LINE, width=line_w)


def build_trade_card(trade: dict, current_price=None, bid=None, ask=None, open_price=None, high=None, mid=None, volume=None, title_symbol=None) -> str:
    """Build Telegram-ready trade card image. Returns jpg path."""
    img = _make_bg()
    draw = ImageDraw.Draw(img)

    opt_type = str(trade.get("type", "CALL")).upper()
    symbol = str(trade.get("symbol", "SPXW")).upper()
    strike = trade.get("strike", "")
    expiry = _parse_expiry(trade.get("expiry", ""))
    current = current_price if current_price is not None else trade.get("last_price", trade.get("entry", 0))
    entry = trade.get("entry", current)

    try:
        diff = float(current) - float(entry)
        pct = (diff / float(entry) * 100) if float(entry) else 0
    except Exception:
        diff, pct = 0, 0

    price_s = _fmt_num(current)
    bid_s = _fmt_num(bid if bid is not None else trade.get("bid", None), _fmt_num(current))
    ask_s = _fmt_num(ask if ask is not None else trade.get("ask", None), _fmt_num(current))
    open_s = _fmt_num(open_price if open_price is not None else trade.get("open", entry))
    high_s = _fmt_num(high if high is not None else trade.get("high", current))
    mid_s = _fmt_num(mid if mid is not None else trade.get("mid", current))
    vol_s = _compact_volume(volume if volume is not None else trade.get("volume", "--"))

    # header/title exactly like approved style
    title_base = title_symbol or ("SPX" if symbol.startswith("SPX") else symbol)
    main_title = f"{title_base} {int(float(strike)) if str(strike).replace('.','',1).isdigit() else strike}"
    subline = f"{expiry} (W) {opt_type.title()} 100  {symbol}" if expiry else f"{symbol} {strike} {opt_type.title()}"

    _draw_back_arrow(draw)
    draw.text((155, 145), main_title, font=_font(64, True), fill=WHITE)
    draw.text((158, 235), subline, font=_font(39, False), fill=MUTED)

    # left price block — same proportions as approved screenshot
    color = GREEN if diff >= 0 else RED
    sign = "+" if diff >= 0 else ""
    arrow = "▲" if diff >= 0 else "▼"
    draw.text((82, 405), price_s, font=_font(132, True), fill=color)
    draw.text((92, 570), f"{arrow} {sign}{diff:.2f}  {sign}{pct:.2f}%", font=_font(46, True), fill=color)

    # right clean table without outer frame
    rx, ry = 1215, 165
    col_gap = 190
    row_gap = 170
    lab = _font(33, False)
    val = _font(40, True)
    sub = _font(31, False)

    def draw_cell(col, row, label, value, sub_value=None, value_color=WHITE):
        cx = rx + col * col_gap
        cy = ry + row * row_gap
        draw.text((cx, cy), label, font=lab, fill=WHITE)
        draw.text((cx, cy + 48), str(value), font=val, fill=value_color)
        if sub_value is not None:
            draw.text((cx, cy + 102), str(sub_value), font=sub, fill=WHITE)

    for row in range(3):
        xx = rx + 120
        y1 = ry + row * row_gap + 4
        y2 = y1 + 118
        draw.line([(xx, y1), (xx, y2)], fill=LINE, width=2)
    for row in [1, 2]:
        yy = ry + row * row_gap - 32
        draw.line([(rx - 10, yy), (rx + 360, yy)], fill=LINE, width=2)

    draw_cell(0, 0, "Bid", bid_s, "33", GREEN)
    draw_cell(1, 0, "Ask", ask_s, "40", RED)
    draw_cell(0, 1, "Open", open_s)
    draw_cell(1, 1, "High", high_s)
    draw_cell(0, 2, "Mid", mid_s)
    draw_cell(1, 2, "Volume", vol_s)

    # save JPEG for Telegram preview
    out = os.path.join(OUT_DIR, f"card_{uuid.uuid4().hex}.jpg")
    img.save(out, "JPEG", quality=94, optimize=True)
    return out
