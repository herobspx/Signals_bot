import os
import io
import json
import logging
import asyncio
import httpx
from datetime import datetime
from aiohttp import web
import pytz
from card_generator import generate_trade_card
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
WEBHOOK_SECRET  = "ai-candle-123"

# نطاق سعر العقد المقبول
MIN_PRICE = float(os.environ.get("MIN_CONTRACT_PRICE", "0.10"))
MAX_PRICE = float(os.environ.get("MAX_CONTRACT_PRICE", "3.90"))

TYPE, CONTRACT, TARGET, STOP_LOSS, CLOSE_PRICE, GET_STOP = range(6)

# إذا وصل من TradingView رقم entry غير منطقي أثناء اختبار التنبيه،
# نستخدم منتصف الهدف والوقف كقيمة SPX تقريبية حتى لا يبحث البوت حول سترايكات خاطئة.
def normalize_spx_price(raw_entry, target, stop_loss):
    try:
        spx = float(raw_entry or 0)
    except Exception:
        spx = 0.0
    try:
        t = float(target)
        s_stop = float(stop_loss)
        low = min(t, s_stop)
        high = max(t, s_stop)
        mid = (t + s_stop) / 2.0
        # إذا entry خارج نطاق الهدف/الوقف بشكل واضح، فالغالب أنه قيمة اختبار أو قديمة.
        if spx <= 0 or spx < low - 150 or spx > high + 150:
            logger.warning(f"SPX entry normalized from {spx} to {mid} using target/stop")
            return mid
    except Exception:
        pass
    return spx

active_trades       = {}
closed_trades_today = []
closed_trades_all   = []
signals_store       = {}  # يخزن إشارات المؤشر بانتظار قرار النشر للعامة
public_tracks       = {}  # عقود تُتابع في القناة العامة

HISTORY_FILE = "trades_history.json"
TRADES_FILE  = "trades.json"

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
            "target","stop","polygon_ticker","opened_at","msg_id"
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
# IBKR — اختيار أفضل عقد
# ─────────────────────────────────────────
def _next_weekday_yyyymmdd_et():
    """يرجع أقرب يوم تداول تقريبيًا بتوقيت نيويورك. لا يعالج العطل الرسمية."""
    from datetime import timedelta
    d = datetime.now(ET_TZ)
    while d.weekday() >= 5:  # السبت/الأحد
        d = d + timedelta(days=1)
    return d.strftime("%Y%m%d")

async def find_best_contract(opt_type: str, spx_price: float):
    """
    V42 — اختيار عقد SPXW بطريقة أوسع وأوضح:
    - يبحث عن SPXW على CBOE و SMART.
    - يستخدم تاريخ أقرب يوم تداول بدل اليوم فقط إذا كان Weekend.
    - يجرّب أسعار Live ثم Delayed ثم Delayed Frozen.
    - يبحث على سترايكات كل 5 نقاط حتى 1000 نقطة.
    - يرشح فقط عقود سعرها <= MAX_PRICE.
    - يختار أعلى سعر تحت 3.90، لأنه غالبًا أقوى Delta من العقود الأرخص.
    """
    try:
        from ib_insync import IB, Option

        expiry = _next_weekday_yyyymmdd_et()
        right = "C" if opt_type.upper() == "CALL" else "P"

        ib = IB()
        await ib.connectAsync(IBKR_HOST, IBKR_PORT, clientId=42, timeout=20)

        # جرّب live، ثم delayed، ثم delayed frozen. بعض حسابات IBKR لا تعطي live للخيارات.
        market_data_modes = [1, 2, 4]

        base5 = round(spx_price / 5) * 5
        max_distance = int(os.environ.get("MAX_STRIKE_DISTANCE", "1000"))

        if opt_type.upper() == "CALL":
            strikes = [base5 + i * 5 for i in range(0, int(max_distance / 5) + 1)]
        else:
            strikes = [base5 - i * 5 for i in range(0, int(max_distance / 5) + 1) if base5 - i * 5 > 0]

        best = None
        checked = 0
        qualified_count = 0
        priced_count = 0
        under_cap_count = 0
        sample_prices = []

        logger.info(
            f"=== IBKR V42 Search: {opt_type} | SPX={spx_price} | Expiry={expiry} | "
            f"Allowed price={MIN_PRICE}-{MAX_PRICE} | Strikes={len(strikes)} | base5={base5} ==="
        )

        for md_mode in market_data_modes:
            try:
                ib.reqMarketDataType(md_mode)
                logger.info(f"IBKR marketDataType={md_mode}")
            except Exception as e:
                logger.info(f"marketDataType {md_mode} failed: {e}")

            # نعيد البحث في كل وضع بيانات فقط إذا لم نجد عقدًا مناسبًا.
            for strike in strikes:
                checked += 1
                try:
                    # IBKR يعرّف عقود SPXW اليومية بهذه الطريقة:
                    # symbol يجب أن يكون SPX وليس SPXW
                    # tradingClass يجب أن يكون SPXW
                    # exchange نستخدم SMART لتفادي Unknown contract
                    variants = [
                        Option("SPX", expiry, float(strike), right, "SMART", currency="USD", tradingClass="SPXW", multiplier="100"),
                    ]

                    used_contract = None
                    for contract in variants:
                        try:
                            q = await ib.qualifyContractsAsync(contract)
                            if q:
                                used_contract = q[0]
                                break
                        except Exception as qe:
                            logger.info(f"Qualify failed strike={strike} {getattr(contract, 'symbol', '')}/{getattr(contract, 'exchange', '')}: {qe}")

                    if not used_contract:
                        continue

                    qualified_count += 1

                    # snapshot=False غالبًا يحتاج وقت. نحاول ننتظر عدة مرات حتى تظهر الأسعار.
                    ticker = ib.reqMktData(used_contract, "", False, False)
                    await asyncio.sleep(1.2)

                    bid = ticker.bid if ticker.bid and ticker.bid > 0 else 0
                    ask = ticker.ask if ticker.ask and ticker.ask > 0 else 0
                    last = ticker.last if ticker.last and ticker.last > 0 else 0
                    close_px = ticker.close if ticker.close and ticker.close > 0 else 0
                    market_price = ticker.marketPrice() if ticker.marketPrice() and ticker.marketPrice() > 0 else 0
                    mid = round((bid + ask) / 2, 2) if bid and ask else 0

                    # للشراء نفضّل ask، ثم marketPrice، ثم mid، ثم last/close.
                    raw_price = ask or market_price or mid or last or close_px
                    price = round(float(raw_price), 2) if raw_price and raw_price > 0 else 0

                    ib.cancelMktData(used_contract)

                    if price <= 0:
                        logger.info(f"Strike {strike}: no price | mode={md_mode} localSymbol={getattr(used_contract, 'localSymbol', '')}")
                        continue

                    priced_count += 1
                    sample_prices.append((strike, price, bid, ask, last, close_px, getattr(used_contract, 'localSymbol', '')))

                    logger.info(
                        f"Strike {strike}: price={price} bid={bid} ask={ask} last={last} close={close_px} "
                        f"mode={md_mode} localSymbol={getattr(used_contract, 'localSymbol', '')}"
                    )

                    if price > MAX_PRICE or price < MIN_PRICE:
                        continue

                    under_cap_count += 1
                    candidate = {
                        "symbol": "SPXW",
                        "strike": float(strike),
                        "expiry": datetime.strptime(expiry, "%Y%m%d").strftime("%d%b%y"),
                        "mid": price,
                        "bid": round(float(bid), 2) if bid else 0,
                        "ask": round(float(ask), 2) if ask else 0,
                        "last": round(float(last), 2) if last else 0,
                        "oi": 0,
                        "vol": 0,
                        "localSymbol": getattr(used_contract, "localSymbol", ""),
                        "conId": getattr(used_contract, "conId", None),
                    }

                    if best is None:
                        best = candidate
                    else:
                        # أعلى سعر تحت الحد، ثم الأقرب للـ SPX عند التعادل.
                        cur_p = float(candidate["mid"])
                        best_p = float(best["mid"])
                        cur_dist = abs(float(candidate["strike"]) - float(spx_price))
                        best_dist = abs(float(best["strike"]) - float(spx_price))
                        if cur_p > best_p or (cur_p == best_p and cur_dist < best_dist):
                            best = candidate

                    # إذا وصلنا سعر قريب جدًا من 3.90 نوقف لتسريع الاختيار.
                    if best and float(best["mid"]) >= MAX_PRICE - 0.05:
                        break

                except Exception as e:
                    logger.info(f"Strike {strike} error: {e}")
                    continue

            if best:
                break

        ib.disconnect()

        logger.info(
            f"=== V42 Search done. checked={checked} qualified={qualified_count} "
            f"priced={priced_count} under_cap={under_cap_count} best={best} "
            f"sample_prices={sample_prices[:10]} ==="
        )

        return best

    except Exception as e:
        logger.error(f"find_best_contract V42 error: {e}")
        return None

# ─────────────────────────────────────────
# IBKR — سعر عقد محدد
# ─────────────────────────────────────────
async def get_ibkr_price(symbol, expiry_str, opt_type, strike):
    try:
        from ib_insync import IB, Option
        dt = parse_expiry(expiry_str)
        if not dt: return None
        ib = IB()
        await ib.connectAsync(IBKR_HOST, IBKR_PORT, clientId=10, timeout=15)
        contract = Option(
            symbol="SPX",
            lastTradeDateOrContractMonth=dt.strftime("%Y%m%d"),
            strike=float(strike),
            right=("P" if opt_type.upper() == "PUT" else "C"),
            exchange="SMART",
            currency="USD",
            tradingClass="SPXW",
            multiplier="100",
        )
        contracts = await ib.qualifyContractsAsync(contract)
        if not contracts:
            ib.disconnect()
            return None
        ticker = ib.reqMktData(contracts[0], "", False, False)
        await asyncio.sleep(2)
        price = None
        if ticker.ask and ticker.ask > 0:   price = ticker.ask
        elif ticker.bid and ticker.bid > 0: price = ticker.bid
        elif ticker.last and ticker.last > 0: price = ticker.last
        ib.disconnect()
        return float(price) if price else None
    except Exception as e:
        logger.error(f"IBKR price error: {e}")
        return None

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
# إرسال كارد
# ─────────────────────────────────────────
async def send_trade_card(bot, chat_id, trade, current_price=None, caption=None, reply_markup=None):
    try:
        path = generate_trade_card(trade, current_price=current_price)
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
            if price and price > active_trades[trade_key].get("last_price", trade["entry"]):
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
            if price and price > public_tracks[sig_id].get("last_price", trade["entry"]):
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

        opt_type   = data.get("type", "").upper()
        target     = data.get("target", "--")
        stop_loss  = data.get("stop_loss", "--")
        spx_price  = normalize_spx_price(data.get("entry", 0), target, stop_loss)

        if opt_type not in ("CALL", "PUT") or spx_price <= 0:
            return web.Response(text="Invalid data", status=400)

        bot_app = request.app["bot_app"]
        logger.info(f"Signal received: {opt_type} SPX={spx_price}")

        # ابحث عن أفضل عقد من IBKR
        best = await find_best_contract(opt_type, spx_price)

        if not best:
            # لو ما لقينا عقد، أرسل رسالة نصية فقط
            msg = (
                f"{'🟢' if opt_type == 'CALL' else '🔴'} *إشارة {opt_type}*\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"⚠️ لم يُعثر على عقد مناسب تحت أو يساوي 3.90\n"
                f"🎯 الهدف: {target}\n"
                f"❌ الوقف: {stop_loss}"
            )
            await bot_app.bot.send_message(chat_id=PRIVATE_GROUP, text=msg, parse_mode="Markdown")
            return web.Response(text="OK - no contract found")

        strike     = best["strike"]
        expiry     = best["expiry"]
        entry_price = best["mid"]
        polygon_ticker = build_ticker("SPXW", expiry, opt_type, strike)

        trade = {
            "symbol":          "SPXW",
            "strike":          strike,
            "type":            opt_type,
            "expiry":          expiry,
            "entry":           entry_price,
            "last_price":      entry_price,
            "target":          str(round(float(target), 2)) if target != "--" else "--",
            "stop":            str(round(float(stop_loss), 2)) if stop_loss != "--" else "--",
            "polygon_ticker":  polygon_ticker,
            "opened_at":       datetime.now().isoformat(),
            "msg_id":          None,
            "auto_update":     True,
            "bid":             best["bid"],
            "ask":             best["ask"],
            "volume":          best["vol"],
            "oi":              best["oi"],
        }

        caption = format_signal_caption(opt_type, strike, expiry, entry_price, trade["target"], trade["stop"])

        # زر النشر في القناة العامة
        sig_id = str(int(datetime.now().timestamp()))
        signals_store[sig_id] = {"trade": trade, "type": opt_type}

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📣 نشر في القناة العامة", callback_data=f"pubsig_{sig_id}"),
            InlineKeyboardButton("❌ تجاهل", callback_data=f"ignsig_{sig_id}")
        ]])

        # أرسل الكارد للقناة الخاصة
        sent = await send_trade_card(
            bot_app.bot, PRIVATE_GROUP, trade,
            current_price=entry_price,
            caption=caption,
            reply_markup=kb
        )

        logger.info(f"Signal card sent: {opt_type} SPXW {strike} @ {entry_price}")
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

    # ── نشر إشارة المؤشر في القناة العامة ──
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
        await query.answer("✅ تم النشر في القناة العامة — التحديث شغال", show_alert=True)
        return

    if data.startswith("ignsig_"):
        sig_id = data.replace("ignsig_", "")
        signals_store.pop(sig_id, None)
        await query.edit_message_reply_markup(reply_markup=None)
        return

    # ── القائمة الرئيسية ──
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
        parts      = data.split("_")
        trade_key  = "_".join(parts[1:-1])
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
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manual_price_handler))
    app.add_handler(CallbackQueryHandler(menu_handler))

    await app.initialize()
    await app.start()

    for trade_key in list(active_trades.keys()):
        asyncio.create_task(track_price(app, trade_key))

    base_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if base_url:
        await app.bot.set_webhook(f"https://{base_url}/tg")

    web_app = web.Application()
    web_app["bot_app"] = app
    web_app.router.add_post("/webhook", handle_webhook)
    web_app.router.add_post("/tg",      tg_webhook)
    web_app.router.add_get("/",                  lambda r: web.Response(text="OK"))
    web_app.router.add_get("/active_trades",     get_active_trades)
    web_app.router.add_get("/closed_trades",     get_closed_trades)
    web_app.router.add_get("/test_ibkr",         test_ibkr)

    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    print(f"✅ Bot v37 running on port {PORT}")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
