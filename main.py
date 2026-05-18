import os
import io
import json
import logging
import asyncio
import math
import httpx
from datetime import datetime
from aiohttp import web
import pytz
from card_generator import generate_trade_card
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN  = os.environ["SIGNALS_TOKEN"]
IBKR_HOST       = os.environ.get("IBKR_HOST", "194.163.143.252")
IBKR_PORT       = int(os.environ.get("IBKR_PORT", "4003"))
POLYGON_KEY     = os.environ["POLYGON_KEY"]
PRIVATE_GROUP   = -1003618409425
PUBLIC_CHANNEL  = -1001934800979
ADMIN_ID        = int(os.environ.get("ADMIN_ID", "0"))
PORT            = int(os.environ.get("PORT", "8080"))
ET_TZ           = pytz.timezone("America/New_York")
KSA_TZ          = pytz.timezone("Asia/Riyadh")
WEBHOOK_SECRET  = "ai-candle-123"

# نطاق سعر العقد المقبول
MIN_PRICE = 3.50
MAX_PRICE = 3.90

TYPE, CONTRACT, TARGET, STOP_LOSS, CLOSE_PRICE, GET_STOP = range(6)

active_trades       = {}
closed_trades_today = []
closed_trades_all   = []
signals_store       = {}
public_tracks       = {}

HISTORY_FILE = "trades_history.json"
TRADES_FILE  = "trades.json"

OUTLOOK_SENT_DATE = None
latest_market_outlook = {}

# ─────────────────────────────────────────
# حفظ وتحميل
# ─────────────────────────────────────────
def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return []

def save_history():
    with open(HISTORY_FILE, "w") as f:
        json.dump(closed_trades_all, f)

def load_trades():
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, "r") as f:
            return json.load(f)
    return {}

def save_trades():
    clean = {}
    for k, v in active_trades.items():
        clean[k] = {x: v[x] for x in (
            "symbol","strike","type","expiry","entry","last_price",
            "target","stop","polygon_ticker","opened_at","msg_id","bid","ask","mid","open","high","low","volume","oi","max_price"
        ) if x in v}
    with open(TRADES_FILE, "w") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)

# ─────────────────────────────────────────
# تحليل التاريخ
# ─────────────────────────────────────────
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

# ─────────────────────────────────────────
# Daily BAMSPX Outlook
# ─────────────────────────────────────────
async def get_polygon_spx_price():
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/indices/tickers/I:SPX?apiKey={POLYGON_KEY}"
            r = await c.get(url)
            if r.status_code == 200:
                data = r.json().get("ticker", {})
                price = data.get("value") or data.get("day", {}).get("c")
                if price:
                    return float(price)
        except Exception as e:
            logger.warning(f"SPX snapshot failed: {e}")

        try:
            url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/SPY?apiKey={POLYGON_KEY}"
            r = await c.get(url)
            if r.status_code == 200:
                data = r.json().get("ticker", {})
                price = data.get("day", {}).get("c") or data.get("lastTrade", {}).get("p")
                if price:
                    return float(price) * 10
        except Exception as e:
            logger.warning(f"SPY snapshot failed: {e}")

    return None

def _fmt_level(value):
    try:
        value = float(value)
        return str(int(value)) if value.is_integer() else f"{value:.2f}"
    except:
        return str(value if value not in (None, "") else "--")

def build_daily_outlook_text(outlook=None, call_flow=0, put_flow=0):
    outlook = outlook or latest_market_outlook
    if not outlook:
        return None

    support_low     = _fmt_level(outlook.get("support_low", "--"))
    support_high    = _fmt_level(outlook.get("support_high", "--"))
    resistance_low  = _fmt_level(outlook.get("resistance_low", "--"))
    resistance_high = _fmt_level(outlook.get("resistance_high", "--"))

    indicator_bias = str(outlook.get("bias", "NEUTRAL")).upper()

    if call_flow > put_flow:
        bias = "CALL"
    elif put_flow > call_flow:
        bias = "PUT"
    else:
        bias = indicator_bias if indicator_bias in ("CALL", "PUT") else "CALL"

    if bias == "CALL":
        flow_line = "تدفق عقود CALL يميل للإيجابية"
        power_line = "مع استمرار تمركز القوة الشرائية قرب مناطق الدعم."
        scenario_line = "📈 الثبات أعلى الدعم الحالي قد يدعم استمرار الزخم الصاعد خلال الجلسة."
        target = _fmt_level(outlook.get("call_target") or outlook.get("target", "--"))
    else:
        flow_line = "تدفق عقود PUT يميل للسلبية"
        power_line = "مع استمرار تمركز الضغط البيعي قرب مناطق المقاومة."
        scenario_line = "📉 الثبات أسفل المقاومة الحالية قد يدعم استمرار الهبوط خلال الجلسة."
        target = _fmt_level(outlook.get("put_target") or outlook.get("target", "--"))

    return (
        "📊 | نظرة BAMSPX لجلسة اليوم\n\n"
        "━━━━━━━━━━━━\n\n"
        f"🔹 الدعم الرئيسي:\n{support_low} - {support_high}\n\n"
        f"🔹 المقاومة الرئيسية:\n{resistance_low} - {resistance_high}\n\n"
        "━━━━━━━━━━━━\n\n"
        f"🧠 {flow_line}\n"
        f"{power_line}\n\n"
        f"{scenario_line}\n\n"
        "━━━━━━━━━━━━\n\n"
        f"🎯 استهداف BAMSPX اليوم:\n{target}\n\n"
        "━━━━━━━━━━━━\n\n"
        "⚠️ يتم اعتماد الدخول فقط بعد ظهور إشارة مؤكدة من مؤشر BAMSPX."
    )

async def get_ibkr_flow_bias(price=None):
    try:
        from ib_insync import IB, Option

        if price is None:
            price = latest_market_outlook.get("price")

        price = float(price)
        today = datetime.now(ET_TZ).strftime("%Y%m%d")
        base = round(price / 5) * 5
        strikes = [base + i * 5 for i in range(-10, 11)]

        ib = IB()
        await ib.connectAsync(IBKR_HOST, IBKR_PORT, clientId=55, timeout=20)

        call_flow = 0
        put_flow = 0

        for strike in strikes:
            for right in ("C", "P"):
                try:
                    contract = Option(
                        symbol="SPX",
                        lastTradeDateOrContractMonth=today,
                        strike=float(strike),
                        right=right,
                        exchange="SMART",
                        currency="USD",
                        tradingClass="SPXW",
                        multiplier="100",
                    )

                    qualified = await ib.qualifyContractsAsync(contract)
                    if not qualified:
                        continue

                    ticker = ib.reqMktData(qualified[0], "100,101", False, False)
                    await asyncio.sleep(0.35)

                    vol = int(ticker.volume) if ticker.volume and ticker.volume > 0 else 0
                    oi_raw = ticker.callOpenInterest if right == "C" else ticker.putOpenInterest
                    oi = int(oi_raw) if oi_raw and oi_raw > 0 else 0
                    score = (vol * 3) + oi

                    if right == "C":
                        call_flow += score
                    else:
                        put_flow += score

                except Exception as e:
                    logger.debug(f"IBKR flow strike={strike} right={right}: {e}")
                    continue

        ib.disconnect()

        if call_flow > put_flow:
            bias = "CALL"
        elif put_flow > call_flow:
            bias = "PUT"
        else:
            bias = str(latest_market_outlook.get("bias", "NEUTRAL")).upper()

        logger.info(f"✅ IBKR flow bias={bias} call_flow={call_flow} put_flow={put_flow}")
        return bias, call_flow, put_flow

    except Exception as e:
        logger.error(f"get_ibkr_flow_bias error: {e}")
        bias = str(latest_market_outlook.get("bias", "NEUTRAL")).upper()
        return bias, 0, 0


async def send_daily_outlook(app, force=False):
    global OUTLOOK_SENT_DATE

    now_ksa = datetime.now(KSA_TZ)
    today_key = now_ksa.strftime("%Y-%m-%d")

    if not force and OUTLOOK_SENT_DATE == today_key:
        return False

    if not latest_market_outlook:
        logger.warning("⚠️ Daily outlook skipped: no MARKET_OUTLOOK from indicator")
        return False

    flow_bias, call_flow, put_flow = await get_ibkr_flow_bias(
        price=latest_market_outlook.get("price")
    )

    outlook_for_msg = dict(latest_market_outlook)
    if flow_bias in ("CALL", "PUT"):
        outlook_for_msg["bias"] = flow_bias

    msg = build_daily_outlook_text(
        outlook=outlook_for_msg,
        call_flow=call_flow,
        put_flow=put_flow
    )

    if not msg:
        logger.warning("⚠️ Daily outlook skipped: empty message")
        return False

    await app.bot.send_message(chat_id=PUBLIC_CHANNEL, text=msg)
    OUTLOOK_SENT_DATE = today_key
    logger.info(f"✅ Daily outlook sent | bias={flow_bias}")
    return True

async def daily_outlook_loop(app):
    while True:
        try:
            now_ksa = datetime.now(KSA_TZ)
            if now_ksa.hour == 16 and now_ksa.minute == 0:
                await send_daily_outlook(app)
        except Exception as e:
            logger.error(f"Daily outlook error: {e}")
        await asyncio.sleep(60)

async def outlook_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    if not latest_market_outlook:
        await update.message.reply_text("⚠️ لم تصل بيانات النظرة من المؤشر حتى الآن.")
        return

    sent = await send_daily_outlook(context.application, force=True)
    if sent:
        await update.message.reply_text("✅ تم إرسال نظرة BAMSPX للقناة العامة.")
    else:
        await update.message.reply_text("⚠️ لم يتم الإرسال. راجع Logs.")

# ─────────────────────────────────────────
# IBKR — اختيار أفضل عقد
# ─────────────────────────────────────────
async def find_best_contract(opt_type: str, spx_price: float):
    """
    يبحث في عقود SPXW 0DTE ويختار أفضل عقد:
    - سعر Ask بين MIN_PRICE و MAX_PRICE
    - أعلى OI + Volume
    - يجرب SPXW أولاً ثم SPX كـ fallback
    """
    try:
        from ib_insync import IB, Option
        today = datetime.now(ET_TZ).strftime("%Y%m%d")
        right = "C" if opt_type.upper() == "CALL" else "P"

        ib = IB()
        await ib.connectAsync(IBKR_HOST, IBKR_PORT, clientId=21, timeout=20)

        real_price = float(spx_price)
        logger.info(f"=== Search: {opt_type} SPX={real_price} Date={today} Range=${MIN_PRICE}-${MAX_PRICE} ===")

        base    = round(real_price / 5) * 5
        strikes = [base + i * 5 for i in range(-60, 61)]

        candidates = []
        for strike in strikes:
            try:
                # ── التعديل 1: نجرب SPXW أولاً ثم SPX كـ fallback ──
                qualified = []
                used_symbol = None
                for sym in ["SPXW", "SPX"]:
                    contract = Option(
                        symbol=sym,
                        lastTradeDateOrContractMonth=today,
                        strike=float(strike),
                        right=right,
                        exchange='CBOE',
                        currency='USD',
                        tradingClass='SPXW'
                    )
                    qualified = await ib.qualifyContractsAsync(contract)
                    if qualified:
                        used_symbol = sym
                        break

                if not qualified:
                    continue

                ticker = ib.reqMktData(qualified[0], "100,101,165", False, False)
                await asyncio.sleep(1.5)

                bid  = ticker.bid  if ticker.bid  and ticker.bid  > 0 else 0
                ask  = ticker.ask  if ticker.ask  and ticker.ask  > 0 else 0
                last = ticker.last if ticker.last and ticker.last > 0 else 0
                oi   = ticker.callOpenInterest if right == "C" else ticker.putOpenInterest
                vol  = ticker.volume if ticker.volume and ticker.volume > 0 else 0
                oi   = int(oi) if oi else 0

                def _px(v):
                    if not v or v <= 0:
                        return 0.0
                    return round(v / 100, 2) if v > 10 else round(v, 2)

                open_p = _px(ticker.open)
                high_p = _px(ticker.high)
                low_p  = _px(ticker.low)

                raw_price = ask if ask > 0 else last
                if raw_price <= 0:
                    continue
                price = round(raw_price / 100, 2) if raw_price > 10 else round(raw_price, 2)

                logger.info(f"Strike {strike} ({used_symbol}): price={price} OI={oi} Vol={vol}")

                if MIN_PRICE <= price <= MAX_PRICE:
                    exp_dt = datetime.strptime(today, "%Y%m%d")
                    candidates.append({
                        "strike": strike,
                        "expiry": exp_dt.strftime("%d%b%y"),
                        "mid":    round((bid + ask) / 2 / 100, 2) if bid and ask and bid > 10 else price,
                        "bid":    round(bid / 100, 2) if bid > 10 else round(bid, 2),
                        "ask":    round(ask / 100, 2) if ask > 10 else round(ask, 2),
                        "price":  price,
                        "open":   open_p if open_p > 0 else price,
                        "high":   high_p if high_p > 0 else price,
                        "low":    low_p  if low_p  > 0 else price,
                        "oi":     oi,
                        "vol":    vol,
                        "score":  oi + vol,
                        "symbol": used_symbol,
                    })
                    logger.info(f"✅ Candidate: {strike} ({used_symbol}) price={price} OI={oi} Vol={vol}")

            except Exception as e:
                logger.debug(f"Strike {strike}: {e}")
                continue

        ib.disconnect()

        if not candidates:
            logger.warning(f"No contract found between ${MIN_PRICE} and ${MAX_PRICE}")

            # ── التعديل 2: إشعار الأدمن لما ما يلقى عقد ──
            try:
                bot = Bot(token=os.environ["SIGNALS_TOKEN"])
                await bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        f"⚠️ *لم يُعثر على عقد مناسب*\n\n"
                        f"النوع: `{opt_type}`\n"
                        f"SPX: `{spx_price}`\n"
                        f"النطاق المطلوب: `${MIN_PRICE} – ${MAX_PRICE}`\n"
                        f"عدد الـ Strikes المفحوصة: `{len(strikes)}`\n\n"
                        f"السبب المحتمل: لا يوجد عقد SPXW 0DTE بهذا النطاق السعري الآن."
                    ),
                    parse_mode="Markdown"
                )
            except Exception as notify_err:
                logger.error(f"Admin notify error: {notify_err}")

            return None

        best = max(candidates, key=lambda x: x["score"])
        logger.info(f"✅ Best: strike={best['strike']} symbol={best['symbol']} price={best['price']} score={best['score']}")
        return best

    except Exception as e:
        logger.error(f"find_best_contract error: {e}")
        return None


# ─────────────────────────────────────────
# IBKR — بيانات العقد المباشرة للكارد
# ─────────────────────────────────────────
async def get_ibkr_snapshot(symbol, expiry_str, opt_type, strike):
    try:
        from ib_insync import IB, Option

        dt = parse_expiry(expiry_str)
        if not dt:
            return None

        ib = IB()
        await ib.connectAsync(IBKR_HOST, IBKR_PORT, clientId=77, timeout=15)

        contracts = []
        for sym in ["SPXW", "SPX"]:
            contract = Option(
                symbol=sym,
                lastTradeDateOrContractMonth=dt.strftime("%Y%m%d"),
                strike=float(strike),
                right=("P" if opt_type.upper() == "PUT" else "C"),
                exchange="CBOE",
                currency="USD",
                tradingClass="SPXW",
                multiplier="100",
            )
            contracts = await ib.qualifyContractsAsync(contract)
            if contracts:
                break

        if not contracts:
            ib.disconnect()
            return None

        ticker = ib.reqMktData(contracts[0], "100,101,104,165", False, False)
        await asyncio.sleep(2)

        def is_bad(v):
            try:
                if v is None or v == "":
                    return True
                v = float(v)
                return math.isnan(v) or math.isinf(v) or v <= 0
            except Exception:
                return True

        def px(v):
            if is_bad(v):
                return None
            v = float(v)
            # بعض مزودي البيانات يرجعون السعر مضروب في 100
            return round(v / 100, 2) if v > 100 else round(v, 2)

        def to_int(v):
            if is_bad(v):
                return None
            try:
                return int(float(v))
            except Exception:
                return None

        bid = px(ticker.bid)
        ask = px(ticker.ask)
        last = px(ticker.last)

        if bid is not None and ask is not None:
            mid = round((bid + ask) / 2, 2)
        else:
            mid = ask if ask is not None else bid if bid is not None else last

        oi_raw = ticker.callOpenInterest if opt_type.upper() == "CALL" else ticker.putOpenInterest

        snapshot = {
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "price": mid if mid is not None else ask if ask is not None else bid if bid is not None else last,
            "open": px(ticker.open),
            "high": px(ticker.high),
            "low": px(ticker.low),
            "volume": to_int(ticker.volume),
            "oi": to_int(oi_raw),
        }

        ib.disconnect()
        return snapshot

    except Exception as e:
        logger.error(f"IBKR snapshot error: {e}")
        return None


# ─────────────────────────────────────────
# IBKR — سعر عقد محدد
# ─────────────────────────────────────────
async def get_ibkr_price(symbol, expiry_str, opt_type, strike):
    snap = await get_ibkr_snapshot(symbol, expiry_str, opt_type, strike)
    if not snap:
        return None
    price = snap.get("price")
    return float(price) if price else None

# ─────────────────────────────────────────
# Polygon fallback
# ─────────────────────────────────────────
async def get_price_rest(ticker):
    try:
        url = f"https://api.polygon.io/v3/snapshot/options/{ticker}?apiKey={POLYGON_KEY}"
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url)
            if r.status_code == 200:
                data = r.json().get("results", {})
                if data:
                    day   = data.get("day", {})
                    price = (data.get("last_quote", {}).get("ask") or
                             data.get("last_quote", {}).get("bid") or
                             day.get("close") or day.get("last") or 0)
                    if price and float(price) > 0:
                        return float(price)
    except: pass
    return None

# ─────────────────────────────────────────
# تحويل trade إلى contract_data للكارد
# ─────────────────────────────────────────
def _to_contract_data(trade: dict) -> dict:
    return {
        "symbol":        trade.get("symbol_line")
                          or f"{trade.get('symbol','SPXW')} {trade.get('strike','')} "
                             f"{trade.get('expiry','')} "
                             f"{'Call' if str(trade.get('type','')).upper()=='CALL' else 'Put'}",
        "bid":           trade.get("bid", 0),
        "ask":           trade.get("ask", 0),
        "mid":           trade.get("mid", trade.get("entry", 0)),
        "open":          trade.get("open", trade.get("entry", 0)),
        "high":          trade.get("high", trade.get("entry", 0)),
        "low":           trade.get("low", trade.get("entry", 0)),
        "volume":        trade.get("volume", trade.get("vol", "--")),
        "open_interest": trade.get("oi", "--"),
    }

# ─────────────────────────────────────────
# إرسال كارد
# ─────────────────────────────────────────
async def send_trade_card(bot, chat_id, trade, current_price=None, caption=None, reply_markup=None):
    try:
        path = generate_trade_card(_to_contract_data(trade), current_price=current_price)
        with open(path, "rb") as photo:
            return await bot.send_photo(
                chat_id=chat_id, photo=photo,
                caption=caption, parse_mode="Markdown",
                reply_markup=reply_markup
            )
    except Exception as e:
        logger.error(f"Card error: {e}")
        if caption:
            return await bot.send_message(
                chat_id=chat_id, text=caption,
                parse_mode="Markdown", reply_markup=reply_markup
            )
        return None

# ─────────────────────────────────────────
# نص الرسالة
# ─────────────────────────────────────────
def format_signal_caption(opt_type, strike, expiry, entry_price, target, stop_loss):
    emoji = "🟢" if opt_type.upper() == "CALL" else "🔴"
    return (
        f"{emoji} *دخول {opt_type.upper()}*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📋 العقد: `SPXW ${strike} {expiry} {opt_type.upper()}`\n"
        f"💰 سعر التنفيذ المقترح: ${entry_price:.2f}\n"
        f"🎯 الهدف المتوقع: {target}\n"
        f"❌ وقف الخسارة: {stop_loss}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⚠️ حسب حركة السوق قد يتحقق الهدف\n"
        f"وقد يتم الخروج ببعضه والإلتزام بوقف الخسارة"
    )

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

# ─────────────────────────────────────────
# تتبع سعر القناة الخاصة
# ─────────────────────────────────────────
async def track_price(app, trade_key):
    logger.info(f"Tracking started: {trade_key}")
    while trade_key in active_trades:
        try:
            trade = active_trades.get(trade_key)
            if not trade or not trade.get("auto_update", True):
                await asyncio.sleep(30)
                continue
            price = await get_ibkr_price(
                trade.get("symbol",""), trade.get("expiry",""),
                trade.get("type",""), trade.get("strike",0)
            )
            if not price:
                price = await get_price_rest(trade.get("polygon_ticker",""))
            if price:
                snapshot = await get_ibkr_snapshot(
                    trade.get("symbol",""),
                    trade.get("expiry",""),
                    trade.get("type",""),
                    trade.get("strike",0)
                )

                if snapshot:
                    for field in ["bid", "ask", "mid", "open", "high", "low", "volume", "oi"]:
                        value = snapshot.get(field)
                        if value not in (None, "", "--"):
                            active_trades[trade_key][field] = value

                active_trades[trade_key]["last_price"] = price

                if price > active_trades[trade_key].get("max_price", trade["entry"]):
                    active_trades[trade_key]["max_price"] = price

                save_trades()

                await send_trade_card(app.bot, PRIVATE_GROUP, active_trades[trade_key], current_price=price)
        except Exception as e:
            logger.error(f"Track error {trade_key}: {e}")
        await asyncio.sleep(10)
    logger.info(f"Tracking stopped: {trade_key}")

# ─────────────────────────────────────────
# تتبع سعر القناة العامة
# ─────────────────────────────────────────
async def track_price_public(app, sig_id):
    logger.info(f"Public tracking started: {sig_id}")
    signal = signals_store.get(sig_id, {})
    trade  = signal.get("trade")
    if not trade:
        return
    while sig_id in public_tracks:
        try:
            price = await get_ibkr_price(
                trade.get("symbol",""), trade.get("expiry",""),
                trade.get("type",""), trade.get("strike",0)
            )
            if not price:
                price = await get_price_rest(trade.get("polygon_ticker",""))
            if price:
                snapshot = await get_ibkr_snapshot(
                    trade.get("symbol",""),
                    trade.get("expiry",""),
                    trade.get("type",""),
                    trade.get("strike",0)
                )

                if snapshot:
                    for field in ["bid", "ask", "mid", "open", "high", "low", "volume", "oi"]:
                        value = snapshot.get(field)
                        if value not in (None, "", "--"):
                            trade[field] = value

                public_tracks[sig_id]["last_price"] = price

                await send_trade_card(app.bot, PUBLIC_CHANNEL, trade, current_price=price)
                logger.info(f"Public update sent: {sig_id} @ ${price:.2f}")
        except Exception as e:
            logger.error(f"Public track error {sig_id}: {e}")
        await asyncio.sleep(10)
    logger.info(f"Public tracking stopped: {sig_id}")

# ─────────────────────────────────────────
# WEBHOOK — استقبال إشارة المؤشر
# ─────────────────────────────────────────
async def handle_webhook(request):
    try:
        data = await request.json()

        if data.get("secret") != WEBHOOK_SECRET:
            return web.Response(text="Unauthorized", status=401)

        msg_type = data.get("type", "").upper()
        route = data.get("route", "").upper()
        logger.info(f"Webhook received: type={msg_type} route={route}")

        if msg_type in ("MARKET_OUTLOOK", "MARKET_DATA") or route in ("MARKET_OUTLOOK", "MARKET_DATA"):
            global latest_market_outlook
            latest_market_outlook = {
                "support_low":     data.get("support_low", "--"),
                "support_high":    data.get("support_high", "--"),
                "resistance_low":  data.get("resistance_low", "--"),
                "resistance_high": data.get("resistance_high", "--"),
                "call_target":     data.get("call_target", "--"),
                "put_target":      data.get("put_target", "--"),
                "bias":            data.get("bias", "NEUTRAL"),
                "target":          data.get("target", "--"),
                "range_status":    data.get("range_status", "--"),
                "price":           data.get("price", "--"),
                "symbol":          data.get("symbol", "--"),
                "received_at":     datetime.now(KSA_TZ).isoformat(),
            }
            logger.info(f"✅ MARKET_OUTLOOK saved: {latest_market_outlook}")
            return web.Response(text="OK - market outlook saved")

        opt_type  = msg_type
        spx_price = float(data.get("entry", 0))
        target    = data.get("target", "--")
        stop_loss = data.get("stop_loss", "--")

        if opt_type not in ("CALL", "PUT") or spx_price <= 0:
            return web.Response(text="Invalid data", status=400)

        bot_app = request.app["bot_app"]
        logger.info(f"Signal received: {opt_type} SPX={spx_price}")

        best = await find_best_contract(opt_type, spx_price)

        if not best:
            logger.warning(f"No contract found for {opt_type} SPX={spx_price}")
            return web.Response(text="OK - no contract found")

        strike      = best["strike"]
        expiry      = best["expiry"]
        entry_price = best["mid"]
        used_symbol = best.get("symbol", "SPXW")
        polygon_ticker = build_ticker(used_symbol, expiry, opt_type, strike)

        typ_disp = "Call" if opt_type == "CALL" else "Put"
        symbol_line = f"{used_symbol} {int(float(strike))} {expiry} {typ_disp}"

        trade = {
            "symbol":         used_symbol,
            "symbol_line":    symbol_line,
            "strike":         strike,
            "type":           opt_type,
            "expiry":         expiry,
            "entry":          entry_price,
            "last_price":     entry_price,
            "target":         str(round(float(target), 2)) if target != "--" else "--",
            "stop":           str(round(float(stop_loss), 2)) if stop_loss != "--" else "--",
            "polygon_ticker": polygon_ticker,
            "opened_at":      datetime.now().isoformat(),
            "msg_id":         None,
            "auto_update":    True,
            "bid":            best["bid"],
            "ask":            best["ask"],
            "mid":            best["mid"],
            "open":           best.get("open", entry_price),
            "high":           best.get("high", entry_price),
            "low":            best.get("low", entry_price),
            "volume":         best["vol"],
            "oi":             best["oi"],
        }

        caption = format_signal_caption(opt_type, strike, expiry, entry_price, trade["target"], trade["stop"])

        sig_id = str(int(datetime.now().timestamp()))
        signals_store[sig_id] = {"trade": trade, "type": opt_type}

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📣 نشر في القناة العامة", callback_data=f"pubsig_{sig_id}"),
            InlineKeyboardButton("❌ تجاهل", callback_data=f"ignsig_{sig_id}")
        ]])

        sent = await send_trade_card(
            bot_app.bot, PRIVATE_GROUP, trade,
            current_price=entry_price,
            caption=caption,
            reply_markup=kb
        )

        logger.info(f"Signal card sent: {opt_type} {used_symbol} {strike} @ {entry_price}")
        return web.Response(text="OK")

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(text="Error", status=500)

# ─────────────────────────────────────────
# معالج أزرار البوت
# ─────────────────────────────────────────
async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if str(query.from_user.id) != str(ADMIN_ID):
        await query.answer("⛔ غير مصرح", show_alert=True)
        return

    if data.startswith("pubsig_"):
        sig_id = data.replace("pubsig_", "")
        signal = signals_store.get(sig_id)
        if not signal:
            await query.answer("⚠️ انتهت صلاحية الإشارة", show_alert=True)
            return
        trade   = signal["trade"]
        caption = format_signal_caption(
            trade["type"], trade["strike"], trade["expiry"],
            trade["entry"], trade["target"], trade["stop"]
        )
        await send_trade_card(context.bot, PUBLIC_CHANNEL, trade,
                              current_price=trade["entry"], caption=caption)
        public_tracks[sig_id] = {"last_price": trade["entry"]}
        asyncio.create_task(track_price_public(context.application, sig_id))
        await query.edit_message_reply_markup(reply_markup=None)
        await query.answer("✅ تم النشر في القناة العامة", show_alert=True)
        return

    if data.startswith("ignsig_"):
        sig_id = data.replace("ignsig_", "")
        signals_store.pop(sig_id, None)
        await query.edit_message_reply_markup(reply_markup=None)
        return

    if data == "menu_outlook":
        if not latest_market_outlook:
            await query.answer("⚠️ لم تصل بيانات المؤشر بعد", show_alert=True)
            await query.edit_message_text(
                "⚠️ لم تصل بيانات النظرة من المؤشر حتى الآن.\n\n"
                "تأكد أن تنبيه MARKET_OUTLOOK من TradingView يعمل.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]])
            )
            return

        await query.answer("⏳ جاري تجهيز النظرة العامة...", show_alert=False)
        sent = await send_daily_outlook(context.application, force=True)

        if sent:
            await query.edit_message_text(
                "✅ تم إرسال النظرة العامة للقناة العامة.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]])
            )
        else:
            await query.edit_message_text(
                "⚠️ لم يتم إرسال النظرة العامة. راجع Logs.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]])
            )
        return

    if data == "menu_trade":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔴 PUT",  callback_data="type_PUT"),
            InlineKeyboardButton("🟢 CALL", callback_data="type_CALL")
        ],[InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]])
        await query.edit_message_text("اختر نوع الصفقة:", reply_markup=kb)
        return

    if data == "menu_signal":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔴 PUT",  callback_data="signal_PUT"),
            InlineKeyboardButton("🟢 CALL", callback_data="signal_CALL")
        ],[InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]])
        await query.edit_message_text("اختر نوع الإشارة:", reply_markup=kb)
        return

    if data == "menu_trades":
        if not active_trades:
            await query.edit_message_text("📋 لا يوجد عقود نشطة.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]]))
            return
        lines = ["📊 *العقود النشطة:*\n"]
        for k, t in active_trades.items():
            diff  = t["last_price"] - t["entry"]
            pct   = (diff / t["entry"]) * 100
            sign  = "+" if diff >= 0 else ""
            color = "🟢" if diff > 0 else "🔴"
            lines.append(f"{color} `{t['symbol']}` {t['type']} | ${t['entry']:.2f} → ${t['last_price']:.2f} ({sign}{pct:.1f}%)")
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]]))
        return

    if data == "menu_close":
        if not active_trades:
            await query.edit_message_text("لا يوجد عقود نشطة.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]]))
            return
        buttons = [[InlineKeyboardButton(
            f"❌ {t['symbol']} {t['type']} | ${t['last_price']:.2f}",
            callback_data=f"close_{k}"
        )] for k, t in active_trades.items()]
        buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")])
        await query.edit_message_text("اختر العقد للإغلاق:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("close_"):
        trade_key = data.replace("close_", "")
        trade = active_trades.get(trade_key)
        if not trade:
            await query.edit_message_text("⚠️ العقد غير موجود.")
            return
        await query.edit_message_text(
            f"📋 `{trade['symbol']} ${trade['strike']} {trade['type']}`\n\n💵 السعر الحالي: ${trade['last_price']:.2f}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(f"✅ إغلاق بـ ${trade['last_price']:.2f}",
                    callback_data=f"closeconfirm_{trade_key}_{trade['last_price']}")
            ],[InlineKeyboardButton("🔙 رجوع", callback_data="menu_close")]])
        )
        return

    if data.startswith("closeconfirm_"):
        parts       = data.split("_")
        trade_key   = "_".join(parts[1:-1])
        close_price = float(parts[-1])
        trade = active_trades.pop(trade_key, None)
        if not trade:
            await query.edit_message_text("⚠️ العقد غير موجود.")
            return
        trade["close_price"] = close_price
        trade["max_price"]   = trade.get("max_price", trade["entry"])
        trade["closed_at"]   = datetime.now(ET_TZ).isoformat()
        closed_trades_today.append(trade)
        closed_trades_all.append(trade)
        save_history()
        save_trades()
        await query.edit_message_text(f"✅ تم إغلاق العقد بسعر ${close_price:.2f}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة", callback_data="menu_back")]]))
        return

    if data in ("menu_pause", "menu_resume"):
        pausing = data == "menu_pause"
        for k in active_trades: active_trades[k]["auto_update"] = not pausing
        save_trades()
        status     = "⏸ تم إيقاف التحديث" if pausing else "▶️ تم تفعيل التحديث"
        toggle_lbl = "▶️ متابعة" if pausing else "⏸ إيقاف"
        toggle_cb  = "menu_resume" if pausing else "menu_pause"
        await query.edit_message_text(status, reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(toggle_lbl, callback_data=toggle_cb),
            InlineKeyboardButton("✏️ تحديث يدوي", callback_data="menu_manual")
        ],[InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]]))
        return

    if data == "menu_manual":
        if not active_trades:
            await query.edit_message_text("لا يوجد عقود نشطة.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]]))
            return
        context.user_data["awaiting_manual_price"] = True
        await query.edit_message_text("✏️ أرسل السعر الجديد:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]]))
        return

    if data in ("menu_add5", "menu_add10"):
        if not active_trades:
            await query.edit_message_text("لا يوجد عقود نشطة.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]]))
            return
        add = 0.05 if data == "menu_add5" else 0.10
        for k, t in list(active_trades.items()):
            new_price = round(t.get("last_price", t["entry"]) + add, 2)
            active_trades[k]["last_price"] = new_price
            if new_price > active_trades[k].get("max_price", t["entry"]):
                active_trades[k]["max_price"] = new_price
            save_trades()
            await send_trade_card(context.bot, PRIVATE_GROUP, active_trades[k], current_price=new_price)
        await query.answer(f"✅ +{int(add*100)}¢", show_alert=False)
        return

    if data == "menu_report":
        await query.edit_message_text("📊 اختر نوع التقرير:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📅 يومي",   callback_data="report_daily")],
            [InlineKeyboardButton("📆 أسبوعي", callback_data="report_weekly")],
            [InlineKeyboardButton("🗓 شهري",   callback_data="report_monthly")],
            [InlineKeyboardButton("🔙 رجوع",   callback_data="menu_back")]
        ]))
        return

    if data in ("report_daily", "report_weekly", "report_monthly"):
        from datetime import timedelta
        now = datetime.now(ET_TZ)
        if data == "report_daily":    since = now - timedelta(days=1);  label = "اليومي"
        elif data == "report_weekly": since = now - timedelta(weeks=1); label = "الأسبوعي"
        else:                         since = now - timedelta(days=30); label = "الشهري"
        trades = [t for t in closed_trades_all
                  if datetime.fromisoformat(t.get("closed_at","1970-01-01")).replace(tzinfo=ET_TZ) >= since]
        if not trades:
            await query.edit_message_text("لا توجد صفقات.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]]))
            return
        img = make_stats_image(trades, label)
        await context.bot.send_photo(chat_id=PUBLIC_CHANNEL, photo=img, caption=f"📊 التقرير {label}")
        await query.answer(f"✅ تم إرسال التقرير {label}", show_alert=True)
        return

    if data == "menu_back":
        await query.edit_message_text("🤖 *لوحة التحكم*\n\nاختر من القائمة:",
            parse_mode="Markdown", reply_markup=main_menu_kb())
        return

    if data.startswith("signal_"):
        signal_type = data.replace("signal_", "")
        emoji = "🔴" if signal_type == "PUT" else "🟢"
        msg = f"⚡️ *تنبيه صفقة محتملة*\n━━━━━━━━━━━━━━━━\n{emoji} {signal_type}\n━━━━━━━━━━━━━━━━"
        sig_id = str(int(datetime.now().timestamp()))
        signals_store[sig_id] = {"type": signal_type, "msg": msg, "trade": None}
        await context.bot.send_message(chat_id=PRIVATE_GROUP, text=msg, parse_mode="Markdown")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📣 نشر في القناة العامة", callback_data=f"pub_{sig_id}"),
            InlineKeyboardButton("❌ تجاهل", callback_data=f"ign_{sig_id}")
        ]])
        await query.edit_message_text(f"✅ إشارة {signal_type} — نشر في القناة؟", reply_markup=kb)
        return

    if data.startswith("pub_"):
        sig_id = data.replace("pub_", "")
        signal = signals_store.get(sig_id)
        if not signal:
            await query.answer("⚠️ انتهت صلاحية الإشارة", show_alert=True)
            return
        await context.bot.send_message(chat_id=PUBLIC_CHANNEL, text=signal.get("msg",""), parse_mode="Markdown")
        await query.edit_message_text(f"✅ تم النشر — {signal.get('type','')}")
        signals_store.pop(sig_id, None)
        return

    if data.startswith("ign_"):
        await query.edit_message_reply_markup(reply_markup=None)
        return

# ─────────────────────────────────────────
# لوحة التحكم
# ─────────────────────────────────────────
def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 صفقة جديدة",    callback_data="menu_trade"),
         InlineKeyboardButton("⚡️ إشارة سريعة",  callback_data="menu_signal")],
        [InlineKeyboardButton("📋 العقود النشطة", callback_data="menu_trades"),
         InlineKeyboardButton("❌ إغلاق عقد",     callback_data="menu_close")],
        [InlineKeyboardButton("⏸ إيقاف التحديث", callback_data="menu_pause"),
         InlineKeyboardButton("✏️ تحديث يدوي",   callback_data="menu_manual")],
        [InlineKeyboardButton("📈 +5",  callback_data="menu_add5"),
         InlineKeyboardButton("📈 +10", callback_data="menu_add10")],
        [InlineKeyboardButton("📊 النظرة العامة", callback_data="menu_outlook")],
        [InlineKeyboardButton("📊 إرسال تقرير",  callback_data="menu_report")],
    ])

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text("🤖 *لوحة التحكم*\n\nاختر من القائمة:",
        parse_mode="Markdown", reply_markup=main_menu_kb())

async def manual_price_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data or not context.user_data.get("awaiting_manual_price"):
        return
    try:
        price = float(update.message.text.strip().replace("$","").replace(",",""))
        context.user_data["awaiting_manual_price"] = False
        if not active_trades:
            await update.message.reply_text("لا توجد عقود نشطة.")
            return
        for k, t in active_trades.items():
            active_trades[k]["last_price"] = price
            if price > active_trades[k].get("max_price", active_trades[k]["entry"]):
                active_trades[k]["max_price"] = price
            save_trades()
            await send_trade_card(update.get_bot(), PRIVATE_GROUP, active_trades[k], current_price=price)
        await update.message.reply_text(f"✅ تم تحديث السعر إلى *${price:.2f}*", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ سعر غير صحيح")

# ─────────────────────────────────────────
# Conversation صفقة يدوية
# ─────────────────────────────────────────
async def trade_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["type"]   = query.data.replace("type_", "")
    context.user_data["symbol"] = "SPXW"
    today = datetime.now().strftime("%d%b%y")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"📅 اليوم ({today})", callback_data="expiry_today")
    ],[InlineKeyboardButton("✏️ تاريخ آخر", callback_data="expiry_other")],
    [InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]])
    await query.edit_message_text(f"✅ {context.user_data['type']}\n\nاختر تاريخ انتهاء العقد:", reply_markup=kb)
    return CONTRACT

async def pick_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "expiry_today":
        context.user_data["expiry"] = datetime.now().strftime("%d%b%y")
        await query.edit_message_text("🔢 أرسل رقم العقد (Strike):\nمثال: `5700`", parse_mode="Markdown")
        return TARGET
    else:
        await query.edit_message_text("📅 أرسل التاريخ:\nمثال: `15May26`", parse_mode="Markdown")
        return CONTRACT

async def get_expiry_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["expiry"] = update.message.text.strip()
    await update.message.reply_text("🔢 أرسل رقم العقد (Strike):\nمثال: `5700`", parse_mode="Markdown")
    return TARGET

async def get_strike(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["strike"] = float(update.message.text.strip().replace(",",""))
        await update.message.reply_text("💵 أرسل سعر الدخول:\nمثال: `3.90`", parse_mode="Markdown")
        return STOP_LOSS
    except:
        await update.message.reply_text("⚠️ رقم غير صحيح، مثال: `5700`", parse_mode="Markdown")
        return TARGET

async def get_entry_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["entry"] = float(update.message.text.strip().replace("$","").replace(",",""))
        await update.message.reply_text("🎯 أرسل الهدف:\nمثال: `7.00`", parse_mode="Markdown")
        return CLOSE_PRICE
    except:
        await update.message.reply_text("⚠️ سعر غير صحيح، مثال: `3.90`", parse_mode="Markdown")
        return STOP_LOSS

async def get_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["target"] = update.message.text.strip()
    await update.message.reply_text("❌ أرسل وقف الخسارة:\nمثال: `2.00`", parse_mode="Markdown")
    return GET_STOP

async def get_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data
    d["stop"] = update.message.text.strip()
    polygon_ticker = build_ticker(d["symbol"], d["expiry"], d["type"], d["strike"])
    trade_key = f"{d['symbol']}_{d['strike']}_{d['type']}_{d['expiry']}"
    trade = {
        "symbol": d["symbol"], "strike": d["strike"], "type": d["type"],
        "expiry": d["expiry"], "entry": d["entry"], "last_price": d["entry"],
        "target": d["target"], "stop": d["stop"],
        "polygon_ticker": polygon_ticker,
        "opened_at": datetime.now().isoformat(), "msg_id": None, "auto_update": True
    }
    active_trades[trade_key] = trade
    save_trades()
    sent = await send_trade_card(context.bot, PRIVATE_GROUP, trade,
                                  current_price=trade["entry"], caption=format_entry(trade))
    if sent: active_trades[trade_key]["msg_id"] = sent.message_id
    save_trades()
    asyncio.create_task(track_price(context.application, trade_key))
    await update.message.reply_text("✅ تم نشر العقد!\n🟢 التتبع التلقائي شغال\n\nللعودة: /start", parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ تم الإلغاء.\n\nللعودة: /start")
    return ConversationHandler.END

# ─────────────────────────────────────────
# API endpoints
# ─────────────────────────────────────────
async def get_active_trades(request):
    import json as _json
    if active_trades:
        return web.Response(text=_json.dumps(active_trades, ensure_ascii=False), content_type="application/json")
    if closed_trades_today:
        return web.Response(text=_json.dumps({"last_closed": closed_trades_today[-1]}, ensure_ascii=False), content_type="application/json")
    return web.Response(text="{}", content_type="application/json")

async def get_closed_trades(request):
    import json as _json
    return web.Response(text=_json.dumps(closed_trades_today, ensure_ascii=False), content_type="application/json")

async def test_ibkr(request):
    try:
        from ib_insync import IB
        ib = IB()
        await ib.connectAsync(IBKR_HOST, IBKR_PORT, clientId=99, timeout=10)
        accounts = ib.managedAccounts()
        ib.disconnect()
        return web.Response(text=f"IBKR Connected | {IBKR_HOST}:{IBKR_PORT} | Accounts: {accounts}")
    except Exception as e:
        return web.Response(text=f"IBKR Failed: {e}")

async def test_outlook(request):
    try:
        bot_app = request.app["bot_app"]
        await send_daily_outlook(bot_app, force=True)
        return web.Response(text="OK - Daily outlook sent")
    except Exception as e:
        logger.error(f"test_outlook error: {e}")
        return web.Response(text=f"Error: {e}", status=500)

async def tg_webhook(request):
    try:
        bot_app = request.app["bot_app"]
        data    = await request.json()
        update  = Update.de_json(data, bot_app.bot)
        await bot_app.process_update(update)
        return web.Response(text="OK")
    except Exception as e:
        logger.error(f"TG error: {e}")
        return web.Response(text="OK")

# ─────────────────────────────────────────
# صورة التقرير
# ─────────────────────────────────────────
def make_stats_image(trades: list, label: str = "اليومي") -> io.BytesIO:
    from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter
    from datetime import datetime as dt

    BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    REG  = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    MONO = "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf"

    def f(size, style="reg"):
        try:
            p = {"bold":BOLD,"reg":REG,"mono":MONO}[style]
            return ImageFont.truetype(p, size)
        except: return ImageFont.load_default()

    PAD=35; ROW_H=52; HEAD_H=90; COL_H=42; FOOT_H=90
    COLS=[140,85,115,115,130]
    W=sum(COLS)+PAD*2
    H=HEAD_H+COL_H+ROW_H*max(len(trades),1)+FOOT_H

    WHITE=(255,255,255); GRAY1=(200,203,210); GRAY2=(130,133,145)
    GREEN=(0,218,102); RED=(232,48,48); GOLD=(222,178,0)

    try:
        bg = Image.open("card_bg.png")
        bg = ImageOps.fit(bg.convert("RGB"),(W,H),method=Image.Resampling.LANCZOS)
        bg = bg.filter(ImageFilter.GaussianBlur(3))
        img = Image.alpha_composite(bg.convert("RGBA"), Image.new("RGBA",(W,H),(0,0,0,195)))
    except:
        img = Image.new("RGBA",(W,H),(10,10,15,255))

    def ovr(base,xy,col,r=0):
        ov=Image.new("RGBA",base.size,(0,0,0,0))
        d2=ImageDraw.Draw(ov)
        if r: d2.rounded_rectangle(xy,radius=r,fill=col)
        else: d2.rectangle(xy,fill=col)
        return Image.alpha_composite(base,ov)

    img=ovr(img,[0,0,W,5],(*GOLD,255))
    img=ovr(img,[0,H-5,W,H],(*GOLD,255))
    img=ovr(img,[0,0,6,H],(*GOLD,220))
    img=ovr(img,[0,0,W,HEAD_H],(0,0,0,150))
    d=ImageDraw.Draw(img)
    d.text((W//2,30),"SPX Options Report",fill=GOLD,font=f(22,"bold"),anchor="mm")
    d.text((W//2,62),dt.now().strftime("%A  ·  %d %b %Y"),fill=GRAY1,font=f(14,"reg"),anchor="mm")
    img=ovr(img,[0,HEAD_H,W,HEAD_H+COL_H],(10,10,15,230))
    d=ImageDraw.Draw(img)
    d.line([0,HEAD_H,W,HEAD_H],fill=(*GOLD,100),width=1)
    d.line([0,HEAD_H+COL_H,W,HEAD_H+COL_H],fill=(*GOLD,200),width=2)
    col_xs=[PAD+sum(COLS[:i])+COLS[i]//2 for i in range(len(COLS))]
    for cx,h in zip(col_xs,["Strike","Type","Entry","High","P&L"]):
        d.text((cx,HEAD_H+COL_H//2),h,fill=WHITE,font=f(14,"bold"),anchor="mm")

    total_profit=total_loss=0
    for ri,t in enumerate(trades):
        y=HEAD_H+COL_H+ri*ROW_H
        is_put=t.get("type","").upper()=="PUT"
        entry=float(t.get("entry",0)); maxp=float(t.get("max_price",entry))
        is_win=maxp>entry
        pnl=(maxp-entry)*100 if is_win else -(entry*100)
        if is_win: total_profit+=pnl
        else: total_loss+=pnl
        row_bg=(70,8,8,140) if is_put else (8,55,22,140)
        img=ovr(img,[6,y,W,y+ROW_H],row_bg)
        d=ImageDraw.Draw(img)
        cy=y+ROW_H//2
        tc=RED if is_put else GREEN
        pc=GREEN if is_win else RED
        sign="+" if is_win else "-"
        vals=[(str(t.get("strike","")),WHITE,"bold"),(t.get("type",""),tc,"bold"),
              (f"${entry:.2f}",GRAY1,"mono"),(f"${maxp:.2f}",WHITE,"mono"),
              (f"{sign}${abs(pnl):.0f}",pc,"bold")]
        for cx,(val,col,style) in zip(col_xs,vals):
            d.text((cx,cy),val,fill=col,font=f(15,style),anchor="mm")
        d.line([6,y+ROW_H,W,y+ROW_H],fill=(50,53,62,180),width=1)

    fy=HEAD_H+COL_H+len(trades)*ROW_H
    img=ovr(img,[0,fy,W,H-5],(0,0,0,210))
    d=ImageDraw.Draw(img)
    d.line([0,fy,W,fy],fill=(*GOLD,180),width=2)
    net=total_profit+total_loss
    third=(W-PAD*2)//3
    for i,(lbl,val,col,bgc) in enumerate([
        ("Total Profit",f"+${total_profit:.0f}",GREEN,(8,50,20,220)),
        ("Total Loss",f"-${abs(total_loss):.0f}",RED,(55,8,8,220)),
        ("Net P&L",f"{'+'if net>=0 else''}{net:.0f}$",GREEN if net>=0 else RED,
         (8,50,20,220) if net>=0 else (55,8,8,220)),
    ]):
        cx=PAD+i*third+third//2
        img=ovr(img,[PAD+i*third+10,fy+14,PAD+(i+1)*third-10,fy+FOOT_H-14],bgc,r=14)
        d=ImageDraw.Draw(img)
        d.text((cx,fy+36),lbl,fill=GRAY2,font=f(12,"reg"),anchor="mm")
        d.text((cx,fy+62),val,fill=col,font=f(18,"bold"),anchor="mm")

    buf=io.BytesIO()
    img.convert("RGB").save(buf,format="PNG")
    buf.seek(0)
    return buf

# ─────────────────────────────────────────
# main
# ─────────────────────────────────────────
async def main():
    saved = load_trades()
    for k, t in saved.items():
        active_trades[k] = t
    closed_trades_all.extend(load_history())

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).updater(None).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(trade_type, pattern="^type_")],
        states={
            CONTRACT:    [CallbackQueryHandler(pick_expiry, pattern="^expiry_"),
                          MessageHandler(filters.TEXT & ~filters.COMMAND, get_expiry_text)],
            TARGET:      [MessageHandler(filters.TEXT & ~filters.COMMAND, get_strike)],
            STOP_LOSS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, get_entry_price)],
            CLOSE_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_target)],
            GET_STOP:    [MessageHandler(filters.TEXT & ~filters.COMMAND, get_stop)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("outlook", outlook_cmd))
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manual_price_handler))
    app.add_handler(CallbackQueryHandler(menu_handler))

    await app.initialize()
    await app.start()

    for trade_key in list(active_trades.keys()):
        asyncio.create_task(track_price(app, trade_key))

    asyncio.create_task(daily_outlook_loop(app))

    base_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if base_url:
        await app.bot.set_webhook(f"https://{base_url}/tg")

    web_app = web.Application()
    web_app["bot_app"] = app
    web_app.router.add_post("/webhook", handle_webhook)
    web_app.router.add_post("/tg",      tg_webhook)
    web_app.router.add_get("/",                 lambda r: web.Response(text="OK"))
    web_app.router.add_get("/active_trades",    get_active_trades)
    web_app.router.add_get("/closed_trades",    get_closed_trades)
    web_app.router.add_get("/test_ibkr",        test_ibkr)
    web_app.router.add_get("/test_outlook",     test_outlook)

    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    print(f"✅ Bot running on port {PORT}")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
