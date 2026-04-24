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

TELEGRAM_TOKEN = os.environ["SIGNALS_TOKEN"]
POLYGON_KEY    = os.environ["POLYGON_KEY"]
PRIVATE_GROUP  = -1003618409425
PUBLIC_CHANNEL = -1001934800979
ADMIN_ID       = int(os.environ.get("ADMIN_ID", "0"))
PORT           = int(os.environ.get("PORT", "8080"))
ET_TZ          = pytz.timezone("America/New_York")

# Conversation states
TYPE, CONTRACT, TARGET, STOP = range(4)

active_trades  = {}
signals_store  = {}
TRADES_FILE    = "trades.json"

# ─── DB ───────────────────────────────────────────────────────────────────────
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

# ─── Market Hours ─────────────────────────────────────────────────────────────
def is_market_open():
    now = datetime.now(ET_TZ)
    if now.weekday() >= 5:
        return False
    from datetime import time as dtime
    return dtime(9,30) <= now.time() <= dtime(16,0)

# ─── Format Messages ──────────────────────────────────────────────────────────
def format_entry(trade: dict) -> str:
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

def format_update(trade: dict, current: float) -> str:
    entry  = trade["entry"]
    diff   = current - entry
    pct    = (diff / entry) * 100
    sign   = "+" if diff >= 0 else ""
    emoji  = "📈" if diff > 0 else "📉"
    color  = "🟢" if diff > 0 else "🔴"
    profit = diff * 100  # per contract (100 multiplier)
    return (
        f"{emoji} *تحديث العقد*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📋 `{trade['symbol']} ${trade['strike']} {trade['expiry']} {trade['type'].upper()}`\n"
        f"💰 سعر الدخول: ${entry:.2f}\n"
        f"💵 السعر الآن: ${current:.2f}\n"
        f"{color} الربح: {sign}${diff:.2f} ({sign}{pct:.1f}%)\n"
        f"━━━━━━━━━━━━━━━━"
    )

def format_close(trade: dict, close: float) -> str:
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

# ─── Polygon ──────────────────────────────────────────────────────────────────
def parse_expiry(expiry: str) -> datetime | None:
    """Try multiple date formats"""
    formats = ["%d%b%y", "%d%B%y", "%d%b%Y", "%d%B%Y", "%d/%m/%y", "%d/%m/%Y", "%Y-%m-%d"]
    # Normalize: capitalize month
    expiry = expiry.strip()
    for fmt in formats:
        try:
            return datetime.strptime(expiry, fmt)
        except:
            continue
    # Try adding missing 'A' for common typo like '24pr26' -> '24Apr26'
    import re
    m = re.match(r"(\d{1,2})([a-zA-Z]+)(\d{2,4})", expiry)
    if m:
        day, mon, yr = m.groups()
        # Map common abbreviations
        mon_map = {"jan":"Jan","feb":"Feb","mar":"Mar","apr":"Apr","may":"May",
                   "jun":"Jun","jul":"Jul","aug":"Aug","sep":"Sep","oct":"Oct",
                   "nov":"Nov","dec":"Dec",
                   "pr":"Apr","ay":"May","un":"Jun","ul":"Jul","ug":"Aug",
                   "ep":"Sep","ct":"Oct","ov":"Nov","ec":"Dec"}
        mon_fixed = mon_map.get(mon.lower(), mon.capitalize())
        yr_fixed  = yr if len(yr) == 4 else f"20{yr}"
        try:
            return datetime.strptime(f"{day}{mon_fixed}{yr_fixed}", "%d%b%Y")
        except:
            pass
    return None

def build_ticker(symbol, expiry, opt_type, strike):
    try:
        dt = parse_expiry(expiry)
        if not dt:
            logger.error(f"Cannot parse date: {expiry}")
            return ""
        ds   = dt.strftime("%y%m%d")
        tc   = "P" if opt_type.upper() == "PUT" else "C"
        ss   = f"{int(float(strike)*1000):08d}"
        return f"O:{symbol.upper()}{ds}{tc}{ss}"
    except Exception as e:
        logger.error(f"Ticker error: {e}")
        return ""

async def get_price_rest(ticker):
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
                logger.info(f"WS subscribed: {ticker}")
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
                                    await app.bot.send_message(
                                        chat_id=PRIVATE_GROUP,
                                        text=format_update(t, price),
                                        parse_mode="Markdown"
                                    )
                    except asyncio.TimeoutError: continue
        except Exception as e:
            logger.error(f"WS error: {e}")
            if trade_key in active_trades:
                await asyncio.sleep(5)
                asyncio.create_task(track_price(app, trade_key))
    else:
        logger.info(f"Starting REST polling for {trade_key}")
        while trade_key in active_trades:
            try:
                if is_market_open():
                    asyncio.create_task(track_price(app, trade_key))
                    return
                price = await get_price_rest(ticker)
                logger.info(f"REST price for {trade_key}: {price}")
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
            except Exception as e:
                logger.error(f"REST poll error for {trade_key}: {e}")
            await asyncio.sleep(30)

# ─── Conversation ─────────────────────────────────────────────────────────────
async def trade_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔴 PUT",  callback_data="type_PUT"),
        InlineKeyboardButton("🟢 CALL", callback_data="type_CALL"),
    ]])
    await update.message.reply_text("اختر نوع الصفقة:", reply_markup=kb)
    return TYPE

async def choose_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["type"] = query.data.replace("type_", "")
    await query.edit_message_text(
        f"✅ {context.user_data['type']}\n\nأرسل تفاصيل العقد:\n`SPXW 7050 23Apr26 3.90`\n\n_(الرمز، السعر، التاريخ، سعر الدخول)_",
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
        await update.message.reply_text("⚠️ صيغة خاطئة. أرسل مثال:\n`SPXW 7050 23Apr26 3.90`", parse_mode="Markdown")
        return CONTRACT

async def get_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["target"] = update.message.text.strip()
    await update.message.reply_text("❌ أرسل وقف الخسارة (Stop Loss):\nمثال: `7129`", parse_mode="Markdown")
    return STOP

async def get_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data
    d["stop"] = update.message.text.strip()

    polygon_ticker = build_ticker(d["symbol"], d["expiry"], d["type"], d["strike"])
    trade_key = f"{d['symbol']}_{d['strike']}_{d['type']}_{d['expiry']}"

    trade = {
        "symbol":         d["symbol"],
        "strike":         d["strike"],
        "type":           d["type"],
        "expiry":         d["expiry"],
        "entry":          d["entry"],
        "last_price":     d["entry"],
        "target":         d["target"],
        "stop":           d["stop"],
        "polygon_ticker": polygon_ticker,
        "opened_at":      datetime.now().isoformat(),
        "msg_id":         None
    }

    active_trades[trade_key] = trade
    save_trades()

    # Post to group
    sent = await context.bot.send_message(
        chat_id=PRIVATE_GROUP,
        text=format_entry(trade),
        parse_mode="Markdown"
    )
    active_trades[trade_key]["msg_id"] = sent.message_id
    save_trades()

    # Start tracking
    asyncio.create_task(track_price(context.application, trade_key))

    status = "🟢 السوق مفتوح — تتبع لحظي" if is_market_open() else "🌙 السوق مغلق — تتبع كل 30 ثانية"
    await update.message.reply_text(
        f"✅ تم نشر العقد وبدأ التتبع!\n{status}",
        parse_mode="Markdown"
    )
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ تم الإلغاء.")
    return ConversationHandler.END

# ─── /stop command ────────────────────────────────────────────────────────────
async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    args = context.args
    if not args:
        if not active_trades:
            await update.message.reply_text("لا يوجد عقود نشطة.")
            return
        lines = ["📋 *العقود النشطة:*\n"]
        for k, t in active_trades.items():
            diff = t["last_price"] - t["entry"]
            pct  = (diff / t["entry"]) * 100
            sign = "+" if diff >= 0 else ""
            lines.append(f"• `{k}`\n  ${t['last_price']:.2f} ({sign}{pct:.1f}%)")
        lines.append("\n`/stop TRADE_KEY سعر_الخروج`")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    trade_key   = args[0]
    close_price = float(args[1]) if len(args) > 1 else active_trades.get(trade_key, {}).get("last_price", 0)
    trade = active_trades.pop(trade_key, None)
    if not trade:
        await update.message.reply_text(f"⚠️ ما لقيت: `{trade_key}`", parse_mode="Markdown")
        return
    save_trades()
    await context.bot.send_message(
        chat_id=PRIVATE_GROUP,
        text=format_close(trade, close_price),
        parse_mode="Markdown"
    )
    await update.message.reply_text("✅ تم إغلاق العقد.")

# ─── /signal command (manual PUT/CALL alert) ──────────────────────────────────
async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔴 PUT",  callback_data="manual_PUT"),
        InlineKeyboardButton("🟢 CALL", callback_data="manual_CALL"),
    ]])
    await update.message.reply_text("اختر نوع الإشارة:", reply_markup=kb)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data
    if str(query.from_user.id) != str(ADMIN_ID):
        await query.answer("⛔ غير مصرح", show_alert=True)
        return

    if data.startswith("manual_"):
        signal_type = data.replace("manual_", "")
        emoji = "🔴" if signal_type == "PUT" else "🟢"
        msg   = (
            f"⚡️ *تنبيه صفقة محتملة*\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"{emoji} {signal_type}\n"
            f"━━━━━━━━━━━━━━━━"
        )
        sig_id = str(int(datetime.now().timestamp()))
        signals_store[sig_id] = {"type": signal_type, "msg": msg}
        await context.bot.send_message(chat_id=PRIVATE_GROUP, text=msg, parse_mode="Markdown")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📣 نشر في القناة العامة", callback_data=f"pub_{sig_id}"),
            InlineKeyboardButton("❌ تجاهل", callback_data=f"ign_{sig_id}"),
        ]])
        await query.edit_message_text(f"✅ تم إرسال إشارة {signal_type}\nنشر في القناة العامة؟", reply_markup=kb, parse_mode="Markdown")
        return

    if data.startswith("ign_"):
        await query.edit_message_text("❌ تم التجاهل.")
        return

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

# ─── Main ─────────────────────────────────────────────────────────────────────
async def main():
    saved = load_trades()
    for k, t in saved.items():
        active_trades[k] = t

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).updater(None).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("trade", trade_start)],
        states={
            TYPE:     [CallbackQueryHandler(choose_type, pattern="^type_")],
            CONTRACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_contract)],
            TARGET:   [MessageHandler(filters.TEXT & ~filters.COMMAND, get_target)],
            STOP:     [MessageHandler(filters.TEXT & ~filters.COMMAND, get_stop)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("stop",   stop_cmd))
    app.add_handler(CommandHandler("signal", signal_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))

    await app.initialize()
    await app.start()

    # Resume tracking
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
