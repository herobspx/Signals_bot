import os
import json
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
    trade  = active_trades.get(trade_key)
    if not trade: return
    ticker = trade["polygon_ticker"]

    if is_market_open():
        try:
            async with websockets.connect("wss://socket.polygon.io/options", ping_interval=20) as ws:
                await ws.send(json.dumps({"action":"auth","params":POLYGON_KEY}))
                await ws.recv()
                await ws.send(json.dumps({"action":"subscribe","params":f"T.{ticker}"}))
                while trade_key in active_trades:
                    if not is_market_open():
                        asyncio.create_task(track_price(app, trade_key))
                        return
                    try:
                        msg  = await asyncio.wait_for(ws.recv(), timeout=30)
                        data = json.loads(msg)
                        for ev in data:
                            if ev.get("ev") == "T" and ev.get("sym") == ticker:
                                price = float(ev.get("p", 0))
                                if price <= 0: continue
                                t = active_trades.get(trade_key)
                                if not t: return
                                if abs(price - t.get("last_price", t["entry"])) >= 0.01:
                                    active_trades[trade_key]["last_price"] = price
                                    save_trades()
                                    await app.bot.send_message(chat_id=PRIVATE_GROUP, text=format_update(t, price), parse_mode="Markdown")
                    except asyncio.TimeoutError: continue
        except Exception as e:
            logger.error(f"WS error: {e}")
            if trade_key in active_trades:
                await asyncio.sleep(5)
                asyncio.create_task(track_price(app, trade_key))
    else:
        logger.info(f"REST polling: {trade_key}")
        while trade_key in active_trades:
            try:
                if is_market_open():
                    asyncio.create_task(track_price(app, trade_key))
                    return
                t2 = active_trades.get(trade_key)
                price = await get_price_rest(ticker, t2.get('symbol',''), t2.get('expiry',''), t2.get('type',''), t2.get('strike',0))
                logger.info(f"Price for {trade_key}: {price}")
                if price:
                    t = active_trades.get(trade_key)
                    if t:
                        active_trades[trade_key]["last_price"] = price
                        save_trades()
                        await app.bot.send_message(
                            chat_id=PRIVATE_GROUP,
                            text=format_update(t, price),
                            parse_mode="Markdown"
                        )
                else:
                    logger.info(f"No price available for {trade_key}")
            except Exception as e:
                logger.error(f"REST error for {trade_key}: {e}")
            finally:
                await asyncio.sleep(30)
        logger.info(f"REST polling stopped for {trade_key} — trade closed")

# ─── /start - Main Menu ────────────────────────────────────────────────────────
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text(
        "🤖 *لوحة التحكم*\n\nاختر من القائمة:",
        parse_mode="Markdown",
        reply_markup=main_menu_kb()
    )

# ─── Main Menu Handler ─────────────────────────────────────────────────────────
async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data
    if str(query.from_user.id) != str(ADMIN_ID):
        await query.answer("⛔ غير مصرح", show_alert=True)
        return

    # ── New Trade ──
    if data == "menu_trade":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔴 PUT",  callback_data="type_PUT"),
            InlineKeyboardButton("🟢 CALL", callback_data="type_CALL"),
        ],[
            InlineKeyboardButton("🔙 رجوع", callback_data="menu_back"),
        ]])
        await query.edit_message_text("اختر نوع الصفقة:", reply_markup=kb)
        return

    # ── Quick Signal ──
    if data == "menu_signal":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔴 PUT",  callback_data="signal_PUT"),
            InlineKeyboardButton("🟢 CALL", callback_data="signal_CALL"),
        ],[
            InlineKeyboardButton("🔙 رجوع", callback_data="menu_back"),
        ]])
        await query.edit_message_text("اختر نوع الإشارة:", reply_markup=kb)
        return

    # ── Active Trades ──
    if data == "menu_trades":
        if not active_trades:
            await query.edit_message_text(
                "📋 لا يوجد عقود نشطة حالياً.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]])
            )
            return
        lines = ["📊 *العقود النشطة:*\n"]
        for k, t in active_trades.items():
            diff  = t["last_price"] - t["entry"]
            pct   = (diff / t["entry"]) * 100
            sign  = "+" if diff >= 0 else ""
            color = "🟢" if diff > 0 else "🔴"
            lines.append(f"{color} `{t['symbol']}` {t['type']} | ${t['entry']:.2f} → ${t['last_price']:.2f} ({sign}{pct:.1f}%)")
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]])
        )
        return

    # ── Close Trade ──
    if data == "menu_close":
        if not active_trades:
            await query.edit_message_text(
                "لا يوجد عقود نشطة.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]])
            )
            return
        buttons = []
        for k, t in active_trades.items():
            label = f"❌ {t['symbol']} {t['type']} | ${t['last_price']:.2f}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"close_{k}")])
        buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")])
        await query.edit_message_text(
            "اختر العقد للإغلاق:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    # ── Close selected trade ──
    if data.startswith("close_"):
        trade_key = data.replace("close_", "")
        trade     = active_trades.get(trade_key)
        if not trade:
            await query.edit_message_text("⚠️ العقد غير موجود.")
            return
        context.user_data["closing_trade"] = trade_key
        await query.edit_message_text(
            f"📋 `{trade['symbol']} ${trade['strike']} {trade['type']}`\n\n"
            f"💵 السعر الحالي: ${trade['last_price']:.2f}\n\n"
            f"أرسل سعر الخروج أو اضغط للإغلاق بالسعر الحالي:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(f"✅ إغلاق بـ ${trade['last_price']:.2f}", callback_data=f"closeconfirm_{trade_key}_{trade['last_price']}"),
            ],[
                InlineKeyboardButton("🔙 رجوع", callback_data="menu_close"),
            ]])
        )
        return

    # ── Confirm close ──
    if data.startswith("closeconfirm_"):
        parts       = data.split("_")
        trade_key   = "_".join(parts[1:-1])
        close_price = float(parts[-1])
        trade = active_trades.pop(trade_key, None)
        if not trade:
            await query.edit_message_text("⚠️ العقد غير موجود.")
            return
        save_trades()
        await context.bot.send_message(chat_id=PRIVATE_GROUP, text=format_close(trade, close_price), parse_mode="Markdown")
        await query.edit_message_text(
            f"✅ تم إغلاق العقد بسعر ${close_price:.2f}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة", callback_data="menu_back")]])
        )
        return

    # ── Back to main menu ──
    if data == "menu_back":
        await query.edit_message_text("🤖 *لوحة التحكم*\n\nاختر من القائمة:", parse_mode="Markdown", reply_markup=main_menu_kb())
        return

    # ── Quick Signal PUT/CALL ──
    if data.startswith("signal_"):
        signal_type = data.replace("signal_", "")
        emoji  = "🔴" if signal_type == "PUT" else "🟢"
        msg    = f"⚡️ *تنبيه صفقة محتملة*\n━━━━━━━━━━━━━━━━\n{emoji} {signal_type}\n━━━━━━━━━━━━━━━━"
        sig_id = str(int(datetime.now().timestamp()))
        signals_store[sig_id] = {"type": signal_type, "msg": msg}
        await context.bot.send_message(chat_id=PRIVATE_GROUP, text=msg, parse_mode="Markdown")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📣 نشر في القناة العامة", callback_data=f"pub_{sig_id}"),
            InlineKeyboardButton("❌ تجاهل", callback_data=f"ign_{sig_id}"),
        ]])
        await query.edit_message_text(f"✅ تم إرسال إشارة {signal_type}\nنشر في القناة العامة؟", reply_markup=kb, parse_mode="Markdown")
        return

    # ── Publish to channel ──
    if data.startswith("pub_"):
        sig_id = data.replace("pub_", "")
        signal = signals_store.get(sig_id)
        if not signal:
            await query.answer("⚠️ انتهت صلاحية الإشارة", show_alert=True)
            return
        try:
            await context.bot.send_message(chat_id=PUBLIC_CHANNEL, text=signal["msg"], parse_mode="Markdown")
            await query.edit_message_text(f"✅ تم النشر في القناة العامة — {signal['type']}")
            signals_store.pop(sig_id, None)
        except Exception as e:
            await query.answer(f"⚠️ خطأ: {str(e)[:50]}", show_alert=True)
        return

    if data.startswith("ign_"):
        await query.edit_message_text("❌ تم التجاهل.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة", callback_data="menu_back")]]))
        return

# ─── Trade Conversation ────────────────────────────────────────────────────────
async def trade_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["type"] = query.data.replace("type_", "")
    await query.edit_message_text(
        f"✅ {context.user_data['type']}\n\nأرسل تفاصيل العقد:\n`SPXW 7050 24Apr26 3.90`\n\n_(الرمز، Strike، التاريخ، سعر الدخول)_",
        parse_mode="Markdown"
    )
    return CONTRACT

async def get_contract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        parts  = update.message.text.strip().split()
        symbol = parts[0].upper()
        strike = float(parts[1])
        expiry = parts[2]
        entry  = float(parts[3])
        context.user_data.update({"symbol": symbol, "strike": strike, "expiry": expiry, "entry": entry})
        await update.message.reply_text("🎯 أرسل الهدف (Target):\nمثال: `7070`", parse_mode="Markdown")
        return TARGET
    except:
        await update.message.reply_text("⚠️ صيغة خاطئة.\nمثال: `SPXW 7050 24Apr26 3.90`", parse_mode="Markdown")
        return CONTRACT

async def get_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["target"] = update.message.text.strip()
    await update.message.reply_text("❌ أرسل وقف الخسارة:\nمثال: `7129`", parse_mode="Markdown")
    return STOP_LOSS

async def get_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d   = context.user_data
    d["stop"] = update.message.text.strip()
    polygon_ticker = build_ticker(d["symbol"], d["expiry"], d["type"], d["strike"])
    trade_key = f"{d['symbol']}_{d['strike']}_{d['type']}_{d['expiry']}"
    trade = {
        "symbol": d["symbol"], "strike": d["strike"], "type": d["type"],
        "expiry": d["expiry"], "entry": d["entry"], "last_price": d["entry"],
        "target": d["target"], "stop": d["stop"],
        "polygon_ticker": polygon_ticker,
        "opened_at": datetime.now().isoformat(), "msg_id": None
    }
    active_trades[trade_key] = trade
    save_trades()
    sent = await context.bot.send_message(chat_id=PRIVATE_GROUP, text=format_entry(trade), parse_mode="Markdown")
    active_trades[trade_key]["msg_id"] = sent.message_id
    save_trades()
    asyncio.create_task(track_price(context.application, trade_key))
    status = "🟢 السوق مفتوح — تتبع لحظي" if is_market_open() else "🌙 السوق مغلق — تتبع كل 30 ثانية"
    await update.message.reply_text(
        f"✅ تم نشر العقد وبدأ التتبع!\n{status}\n\n"
        f"للعودة للقائمة: /start",
        parse_mode="Markdown"
    )
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ تم الإلغاء.\n\nللعودة: /start")
    return ConversationHandler.END

# ─── TradingView Webhook ──────────────────────────────────────────────────────
async def handle_webhook(request):
    try:
        data = await request.json()
        signal_type = data.get("signal", "").strip().upper()
        if signal_type not in ("PUT", "CALL"):
            return web.Response(text="Invalid", status=400)
        emoji  = "🔴" if signal_type == "PUT" else "🟢"
        msg    = f"⚡️ *تنبيه صفقة محتملة*\n━━━━━━━━━━━━━━━━\n{emoji} {signal_type}\n━━━━━━━━━━━━━━━━"
        sig_id = str(int(datetime.now().timestamp()))
        signals_store[sig_id] = {"type": signal_type, "msg": msg}
        bot_app = request.app["bot_app"]
        await bot_app.bot.send_message(chat_id=PRIVATE_GROUP, text=msg, parse_mode="Markdown")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📣 نشر في القناة العامة", callback_data=f"pub_{sig_id}"),
            InlineKeyboardButton("❌ تجاهل", callback_data=f"ign_{sig_id}"),
        ]])
        await bot_app.bot.send_message(chat_id=ADMIN_ID, text=f"⚡️ إشارة {signal_type} من المؤشر\nنشر في القناة؟", parse_mode="Markdown", reply_markup=kb)
        return web.Response(text="OK")
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(text="Error", status=500)

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

# ─── Main ──────────────────────────────────────────────────────────────────────
async def main():
    saved = load_trades()
    for k, t in saved.items():
        active_trades[k] = t

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).updater(None).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(trade_type, pattern="^type_")],
        states={
            CONTRACT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, get_contract)],
            TARGET:    [MessageHandler(filters.TEXT & ~filters.COMMAND, get_target)],
            STOP_LOSS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_stop)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(conv)
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
    web_app.router.add_post("/tg", tg_webhook)
    web_app.router.add_get("/", lambda r: web.Response(text="OK"))

    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    print(f"Bot running on port {PORT}")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
