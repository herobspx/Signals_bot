import os
import json
import io
import logging
import asyncio
import httpx
import websockets
from datetime import datetime
from aiohttp import web
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

import sys
import traceback

def handle_exception(exc_type, exc_value, exc_tb):
    logger.error("FATAL ERROR:", exc_info=(exc_type, exc_value, exc_tb))
    traceback.print_exception(exc_type, exc_value, exc_tb)
    sys.exit(1)

sys.excepthook = handle_exception
logger.info("=== BOT STARTING ===")

TELEGRAM_TOKEN  = os.environ["SIGNALS_TOKEN"]
IBKR_HOST       = os.environ.get("IBKR_HOST", "ibkr-gateway.railway.internal")
IBKR_PORT       = int(os.environ.get("IBKR_PORT", "4002"))
POLYGON_KEY     = os.environ["POLYGON_KEY"]
WEBULL_EMAIL    = os.environ.get("WEBULL_EMAIL", "")
WEBULL_PASSWORD = os.environ.get("WEBULL_PASSWORD", "")
PRIVATE_GROUP  = -1003618409425
PUBLIC_CHANNEL = -1001934800979
ADMIN_ID       = int(os.environ.get("ADMIN_ID", "0"))
PORT           = int(os.environ.get("PORT", "8080"))
ET_TZ          = pytz.timezone("America/New_York")

TYPE, CONTRACT, TARGET, STOP_LOSS, CLOSE_PRICE = range(5)

active_trades = {}
signals_store = {}
TRADES_FILE   = "trades.json"

def load_trades():
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, "r") as f:
            return json.load(f)
    return {}

def save_trades():
    clean = {}
    for k, v in active_trades.items():
        clean[k] = {x: v[x] for x in ("symbol","strike","type","expiry","entry","last_price","target","stop","polygon_ticker","opened_at","msg_id") if x in v}
    with open(TRADES_FILE, "w") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)

def is_market_open():
    now = datetime.now(ET_TZ)
    if now.weekday() >= 5: return False
    from datetime import time as dtime
    return dtime(9,30) <= now.time() <= dtime(16,0)

def format_entry(trade):
    emoji = "🔴" if trade["type"].upper() == "PUT" else "🟢"
    return (
        f"{emoji} *دخول {trade['type'].upper()}*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📋 العقد: `{trade['symbol']} ${trade['strike']} {trade['expiry']} {trade['type'].upper()}`\n"
        f"💰 سعر الدخول: ${trade['entry']:.2f}\n"
        f"🎯 الهدف المتوقع: {trade['target']}\n"
        f"❌ وقف الخسارة: {trade['stop']}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⚠️ حسب حركة السوق قد يتحقق الهدف\n"
        f"وقد يتم الخروج ببعضه والإلتزام بوقف الخسارة"
    )

def format_update(trade, current):
    entry = trade["entry"]
    diff  = current - entry
    pct   = (diff / entry) * 100
    sign  = "+" if diff >= 0 else ""
    emoji = "📈" if diff > 0 else "📉"
    color = "🟢" if diff > 0 else "🔴"
    return (
        f"{emoji} *تحديث العقد*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📋 `{trade['symbol']} ${trade['strike']} {trade['expiry']} {trade['type'].upper()}`\n"
        f"💰 سعر الدخول: ${entry:.2f}\n"
        f"💵 السعر الآن: ${current:.2f}\n"
        f"{color} الربح: {sign}${diff:.2f} ({sign}{pct:.1f}%)\n"
        f"━━━━━━━━━━━━━━━━"
    )

def format_close(trade, close):
    entry  = trade["entry"]
    diff   = close - entry
    pct    = (diff / entry) * 100
    sign   = "+" if diff >= 0 else ""
    result = "✅ تم الإغلاق بربح" if diff > 0 else "❌ تم الإغلاق بخسارة"
    return (
        f"{result}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📋 `{trade['symbol']} ${trade['strike']} {trade['expiry']} {trade['type'].upper()}`\n"
        f"💰 الدخول: ${entry:.2f}\n"
        f"🏁 الخروج: ${close:.2f}\n"
        f"📊 {sign}${diff:.2f} ({sign}{pct:.1f}%)\n"
        f"━━━━━━━━━━━━━━━━"
    )

def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 صفقة جديدة", callback_data="menu_trade"),
         InlineKeyboardButton("⚡️ إشارة سريعة", callback_data="menu_signal")],
        [InlineKeyboardButton("📋 العقود النشطة", callback_data="menu_trades"),
         InlineKeyboardButton("❌ إغلاق عقد", callback_data="menu_close")],
    ])

def parse_expiry(expiry):
    import re
    formats = ["%d%b%y", "%d%b%Y", "%d%B%y", "%d%B%Y", "%d/%m/%y", "%d/%m/%Y"]
    expiry = expiry.strip()
    for fmt in formats:
        try: return datetime.strptime(expiry, fmt)
        except: pass
    m = re.match(r"(\d{1,2})([a-zA-Z]+)(\d{2,4})", expiry)
    if m:
        day, mon, yr = m.groups()
        mon_map = {"jan":"Jan","feb":"Feb","mar":"Mar","apr":"Apr","may":"May",
                   "jun":"Jun","jul":"Jul","aug":"Aug","sep":"Sep","oct":"Oct",
                   "nov":"Nov","dec":"Dec","pr":"Apr","ay":"May"}
        mon_fixed = mon_map.get(mon.lower(), mon.capitalize())
        yr_fixed  = yr if len(yr) == 4 else f"20{yr}"
        try: return datetime.strptime(f"{day}{mon_fixed}{yr_fixed}", "%d%b%Y")
        except: pass
    return None

def build_ticker(symbol, expiry, opt_type, strike):
    try:
        dt = parse_expiry(expiry)
        if not dt: return ""
        ds = dt.strftime("%y%m%d")
        tc = "P" if opt_type.upper() == "PUT" else "C"
        ss = f"{int(float(strike)*1000):08d}"
        return f"O:{symbol.upper()}{ds}{tc}{ss}"
    except Exception as e:
        logger.error(f"Ticker error: {e}")
        return ""


def make_card(trade: dict, current_price: float, card_type: str = "update") -> io.BytesIO:
    """Generate professional trade card image using Pillow"""
    from PIL import Image, ImageDraw, ImageFont
    import textwrap

    W, H = 800, 480
    BG      = (13, 17, 23)
    CARD_BG = (22, 27, 34)
    GREEN   = (0, 210, 110)
    RED     = (255, 75, 75)
    GOLD    = (255, 200, 50)
    WHITE   = (255, 255, 255)
    GRAY    = (140, 148, 160)
    BORDER  = (48, 54, 61)

    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Try to load fonts, fallback to default
    try:
        font_big   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
        font_med   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 26)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
        font_tiny  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
    except:
        font_big = font_med = font_small = font_tiny = ImageFont.load_default()

    # Card background
    draw.rounded_rectangle([20, 20, W-20, H-20], radius=16, fill=CARD_BG, outline=BORDER, width=1)

    # Header stripe
    is_put  = trade["type"].upper() == "PUT"
    h_color = RED if is_put else GREEN
    draw.rounded_rectangle([20, 20, W-20, 75], radius=16, fill=h_color)
    draw.rectangle([20, 55, W-20, 75], fill=h_color)

    # Header text
    opt_emoji = "🔴 PUT" if is_put else "🟢 CALL"
    if card_type == "entry":
        header = f"دخول جديد  {opt_emoji}"
    elif card_type == "close":
        header = f"إغلاق العقد  {opt_emoji}"
    else:
        header = f"تحديث  {opt_emoji}"

    draw.text((W//2, 47), header, fill=WHITE, font=font_med, anchor="mm")

    # Contract name
    contract_str = f"{trade['symbol']}  ${trade['strike']}  {trade['expiry']}  {trade['type'].upper()}"
    draw.text((W//2, 105), contract_str, fill=GOLD, font=font_med, anchor="mm")

    # Divider
    draw.line([40, 128, W-40, 128], fill=BORDER, width=1)

    # Price data
    entry   = float(trade["entry"])
    diff    = current_price - entry
    pct     = (diff / entry) * 100 if entry else 0
    sign    = "+" if diff >= 0 else ""
    p_color = GREEN if diff >= 0 else RED

    # Left column
    draw.text((60, 150), "سعر الدخول", fill=GRAY, font=font_tiny)
    draw.text((60, 172), f"${entry:.2f}", fill=WHITE, font=font_med)

    draw.text((60, 220), "السعر الحالي", fill=GRAY, font=font_tiny)
    draw.text((60, 242), f"${current_price:.2f}", fill=WHITE, font=font_med)

    # Right column
    draw.text((W//2 + 20, 150), "الربح / الخسارة", fill=GRAY, font=font_tiny)
    draw.text((W//2 + 20, 172), f"{sign}${diff:.2f}", fill=p_color, font=font_med)

    draw.text((W//2 + 20, 220), "النسبة", fill=GRAY, font=font_tiny)
    draw.text((W//2 + 20, 242), f"{sign}{pct:.1f}%", fill=p_color, font=font_med)

    # Divider
    draw.line([40, 285, W-40, 285], fill=BORDER, width=1)

    # Target & Stop
    draw.text((60, 300), "🎯 الهدف", fill=GRAY, font=font_small)
    draw.text((60, 323), str(trade.get("target", "-")), fill=GREEN, font=font_small)

    draw.text((W//2 + 20, 300), "❌ وقف الخسارة", fill=GRAY, font=font_small)
    draw.text((W//2 + 20, 323), str(trade.get("stop", "-")), fill=RED, font=font_small)

    # Divider
    draw.line([40, 360, W-40, 360], fill=BORDER, width=1)

    # Footer timestamp
    now_et = datetime.now(ET_TZ).strftime("%Y-%m-%d  %H:%M ET")
    draw.text((W//2, 390), now_et, fill=GRAY, font=font_tiny, anchor="mm")

    # BAM watermark
    draw.text((W//2, 420), "BAM Signals", fill=BORDER, font=font_small, anchor="mm")

    buf = io.BytesIO()
    img.save(buf, format="PNG", quality=95)
    buf.seek(0)
    return buf


async def get_ibkr_price(symbol: str, expiry_str: str, opt_type: str, strike: float):
    """Get real-time option price using ib_insync"""
    try:
        from ib_insync import IB, Option
        dt = parse_expiry(expiry_str)
        if not dt:
            return None

        ib = IB()
        await ib.connectAsync(IBKR_HOST, IBKR_PORT, clientId=10, timeout=10)

        # Try CBOE first, then SMART
        for exchange in ["CBOE", "SMART"]:
            contract = Option(
                symbol=symbol,
                lastTradeDateOrContractMonth=dt.strftime("%Y%m%d"),
                strike=strike,
                right=("P" if opt_type.upper() == "PUT" else "C"),
                exchange=exchange,
                currency="USD",
                multiplier="100"
            )
            contracts = await ib.qualifyContractsAsync(contract)
            if contracts:
                break

        if not contracts:
            ib.disconnect()
            return None

        tickers = await ib.reqTickersAsync(contracts[0])
        ib.disconnect()

        if tickers:
            ticker = tickers[0]
            price  = ticker.last or ticker.bid or ticker.ask or 0
            if price and float(price) > 0:
                logger.info(f"IBKR ib_insync price: ${price}")
                return float(price)
    except Exception as e:
        logger.error(f"IBKR ib_insync error: {e}")
    return None

async def get_cboe_price(symbol: str, expiry_str: str, opt_type: str, strike: float):
    """Get option price from CBOE - works 24/7 for SPXW"""
    try:
        dt = parse_expiry(expiry_str)
        if not dt:
            return None
        # CBOE option symbol format: SPXW240424P07045000
        date_str  = dt.strftime("%y%m%d")
        type_char = "P" if opt_type.upper() == "PUT" else "C"
        strike_str = f"{int(strike * 1000):08d}"
        cboe_sym  = f"{symbol.upper()}{date_str}{type_char}{strike_str}"

        # Try CBOE delayed quotes API
        url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{cboe_sym}.json"
        headers = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"}
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(url, headers=headers)
            if r.status_code == 200:
                data = r.json()
                price = data.get("data", {}).get("last", 0) or data.get("data", {}).get("bid", 0)
                if price and float(price) > 0:
                    logger.info(f"CBOE price for {cboe_sym}: ${price}")
                    return float(price)

        # Try CBOE options chain
        chain_url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/_{symbol.upper()}.json"
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(chain_url, headers=headers)
            if r.status_code == 200:
                data  = r.json()
                opts  = data.get("data", {}).get("options", [])
                target = f"{symbol.upper()}{date_str}{type_char}{strike_str}"
                for opt in opts:
                    if opt.get("option", "") == target:
                        price = opt.get("last", 0) or opt.get("bid", 0)
                        if price and float(price) > 0:
                            logger.info(f"CBOE chain price: ${price}")
                            return float(price)
    except Exception as e:
        logger.error(f"CBOE price error: {e}")
    return None

async def get_price_rest(ticker, symbol="", expiry="", opt_type="", strike=0):
    if symbol and expiry and opt_type and strike:
        # Try IBKR first (real-time, 24/7)
        price = await get_ibkr_price(symbol, expiry, opt_type, strike)
        if price:
            return price
        logger.info("IBKR failed, trying CBOE...")
        price = await get_cboe_price(symbol, expiry, opt_type, strike)
        if price:
            return price
        logger.info("CBOE failed, trying Polygon...")
    try:
        url = f"https://api.polygon.io/v2/last/trade/{ticker}?apiKey={POLYGON_KEY}"
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url)
            if r.status_code == 200:
                return float(r.json()["results"]["p"])
    except: pass
    try:
        url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/prev?apiKey={POLYGON_KEY}"
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url)
            if r.status_code == 200:
                res = r.json().get("results", [])
                if res: return float(res[0]["c"])
    except: pass
    return None



async def track_price(app, trade_key):
    """Track price using REST polling every 30s"""
    trade = active_trades.get(trade_key)
    if not trade:
        return
    logger.info(f"REST polling started: {trade_key}")

    while trade_key in active_trades:
        try:
            t = active_trades.get(trade_key)
            if not t:
                break

            price = await get_price_rest(
                t["polygon_ticker"],
                symbol=t["symbol"],
                expiry=t["expiry"],
                opt_type=t["type"],
                strike=t["strike"]
            )

            if price and abs(price - t.get("last_price", t["entry"])) >= 0.01:
                active_trades[trade_key]["last_price"] = price
                save_trades()
                try:
                    card = make_card(t, price, "update")
                    await app.bot.send_photo(
                        chat_id=PRIVATE_GROUP,
                        photo=card,
                        caption=format_update(t, price),
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error(f"Card error: {e}")
                    await app.bot.send_message(
                        chat_id=PRIVATE_GROUP,
                        text=format_update(t, price),
                        parse_mode="Markdown"
                    )

        except Exception as e:
            logger.error(f"track_price error: {e}")

        await asyncio.sleep(30)

    logger.info(f"REST polling stopped: {trade_key}")


