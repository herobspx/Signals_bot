import os
import io
import json
import logging
import asyncio
import httpx
import websockets
from datetime import datetime
from aiohttp import web
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from card_generator import generate_trade_card
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
closed_trades_today  = []
closed_trades_all    = []  # كل الصفقات المغلقة

HISTORY_FILE = "trades_history.json"

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return []

def save_history():
    with open(HISTORY_FILE, "w") as f:
        json.dump(closed_trades_all, f)
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


async def send_trade_card(bot, chat_id, trade, current_price=None, caption=None):
    try:
        path = generate_trade_card(trade, current_price=current_price)
        with open(path, "rb") as photo:
            return await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Card generation/send error: {e}")
        if caption:
            return await bot.send_message(chat_id=chat_id, text=caption, parse_mode="Markdown")
        return None

def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 صفقة جديدة", callback_data="menu_trade"),
         InlineKeyboardButton("⚡️ إشارة سريعة", callback_data="menu_signal")],
        [InlineKeyboardButton("📋 العقود النشطة", callback_data="menu_trades"),
         InlineKeyboardButton("❌ إغلاق عقد", callback_data="menu_close")],
        [InlineKeyboardButton("⏸ إيقاف التحديث", callback_data="menu_pause"),
         InlineKeyboardButton("✏️ تحديث يدوي", callback_data="menu_manual")],
        [InlineKeyboardButton("📈 +5", callback_data="menu_add5"),
         InlineKeyboardButton("📈 +10", callback_data="menu_add10")],
        [InlineKeyboardButton("📊 إرسال تقرير", callback_data="menu_report")],
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
    """Price tracking disabled — manual updates only"""
    logger.info(f"Manual mode: {trade_key} — no auto polling")
    return


# ─── /start - Main Menu ────────────────────────────────────────────────────────
async def manual_price_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle manual price input"""
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
            # Send card to channel
            await send_trade_card(
                update.get_bot(),
                PRIVATE_GROUP,
                active_trades[k],
                current_price=price,
                caption=None
            )
        await update.message.reply_text(
            f"✅ تم تحديث السعر إلى *${price:.2f}* وإرساله للقناة",
            parse_mode="Markdown"
        )
    except ValueError:
        await update.message.reply_text("❌ سعر غير صحيح، أرسل رقماً فقط مثل: 36.80")

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
        trade["close_price"] = close_price
        trade["max_price"]   = trade.get("max_price", trade["entry"])
        trade["closed_at"]   = datetime.now(ET_TZ).isoformat()
        closed_trades_today.append(trade)
        closed_trades_all.append(trade)
        save_history()
        save_trades()
        await query.edit_message_text(
            f"✅ تم إغلاق العقد بسعر ${close_price:.2f}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة", callback_data="menu_back")]])
        )
        return

    # ── Pause / Resume ──
    if data in ("menu_pause", "menu_resume"):
        pausing = data == "menu_pause"
        for k in active_trades:
            active_trades[k]["auto_update"] = not pausing
        save_trades()
        status     = "⏸ تم إيقاف التحديث التلقائي" if pausing else "▶️ تم تفعيل التحديث التلقائي"
        toggle_lbl = "▶️ متابعة التحديث" if pausing else "⏸ إيقاف التحديث"
        toggle_cb  = "menu_resume"        if pausing else "menu_pause"
        await query.edit_message_text(
            status,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(toggle_lbl,        callback_data=toggle_cb),
                 InlineKeyboardButton("✏️ تحديث يدوي",  callback_data="menu_manual")],
                [InlineKeyboardButton("🔙 رجوع",         callback_data="menu_back")]
            ])
        )
        return

    # ── Manual Price ──
    if data == "menu_manual":
        if not active_trades:
            await query.edit_message_text(
                "لا يوجد عقود نشطة.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]])
            )
            return
        context.user_data["awaiting_manual_price"] = True
        await query.edit_message_text(
            "✏️ أرسل السعر الجديد (مثال: 36.80):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]])
        )
        return

    # ── +5 / +10 quick update ──
    if data in ("menu_add5", "menu_add10"):
        if not active_trades:
            await query.edit_message_text(
                "لا يوجد عقود نشطة.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]])
            )
            return
        add = 0.05 if data == "menu_add5" else 0.10
        for k, t in list(active_trades.items()):
            new_price = round(t.get("last_price", t["entry"]) + add, 2)
            active_trades[k]["last_price"] = new_price
            if new_price > active_trades[k].get("max_price", t["entry"]):
                active_trades[k]["max_price"] = new_price
            save_trades()
            await send_trade_card(
                context.bot,
                PRIVATE_GROUP,
                active_trades[k],
                current_price=new_price,
                caption=None
            )
        await query.answer(f"✅ تم إرسال تحديث +{int(add)}", show_alert=False)
        return

    # ── Report ──
    if data == "menu_report":
        await query.edit_message_text(
            "📊 اختر نوع التقرير:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📅 يومي",   callback_data="report_daily")],
                [InlineKeyboardButton("📆 أسبوعي", callback_data="report_weekly")],
                [InlineKeyboardButton("🗓 شهري",   callback_data="report_monthly")],
                [InlineKeyboardButton("🔙 رجوع",   callback_data="menu_back")],
            ])
        )
        return

    if data in ("report_daily", "report_weekly", "report_monthly"):
        from datetime import timedelta
        now = datetime.now(ET_TZ)
        if data == "report_daily":
            since = now - timedelta(days=1)
            label = "اليومي"
        elif data == "report_weekly":
            since = now - timedelta(weeks=1)
            label = "الأسبوعي"
        else:
            since = now - timedelta(days=30)
            label = "الشهري"

        trades = [t for t in closed_trades_all
                  if datetime.fromisoformat(t.get("closed_at","1970-01-01")).replace(tzinfo=ET_TZ) >= since]

        if not trades:
            await query.edit_message_text(
                f"لا توجد صفقات في الفترة المحددة.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="menu_back")]])
            )
            return

        img = make_stats_image(trades, label)
        await context.bot.send_photo(chat_id=PUBLIC_CHANNEL, photo=img, caption=f"📊 التقرير {label}")
        await query.answer(f"✅ تم إرسال التقرير {label} للقناة", show_alert=True)
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
    sent = await send_trade_card(context.bot, PRIVATE_GROUP, trade, current_price=trade["entry"], caption=format_entry(trade))
    active_trades[trade_key]["msg_id"] = sent.message_id
    active_trades[trade_key]["auto_update"] = True
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
async def get_active_trades(request):
    """API endpoint للبرودكاست — يرجع الصفقات النشطة"""
    import json as _json
    if active_trades:
        return web.Response(
            text=_json.dumps(active_trades, ensure_ascii=False),
            content_type="application/json"
        )
    # إذا ما في صفقات نشطة، ارجع آخر صفقة مغلقة
    if closed_trades_today:
        last = closed_trades_today[-1]
        return web.Response(
            text=_json.dumps({"last_closed": last}, ensure_ascii=False),
            content_type="application/json"
        )
    return web.Response(text="{}", content_type="application/json")

async def get_closed_trades(request):
    """API endpoint — يرجع الصفقات المغلقة اليوم"""
    import json as _json
    return web.Response(
        text=_json.dumps(closed_trades_today, ensure_ascii=False),
        content_type="application/json"
    )

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
def make_stats_image(trades_history: list) -> io.BytesIO:
    """Generate daily stats table as image"""
    from PIL import Image, ImageDraw, ImageFont

    ROW_H  = 44
    HEADER = 60
    FOOTER = 50
    COLS   = [300, 130, 130, 160]  # widths
    W      = sum(COLS)
    H      = HEADER + ROW_H * max(len(trades_history), 1) + FOOTER

    BLACK  = (0, 0, 0)
    DARK   = (15, 15, 15)
    DARK2  = (22, 22, 22)
    WHITE  = (255, 255, 255)
    GRAY1  = (160, 163, 170)
    GRAY2  = (80, 83, 90)
    GREEN  = (0, 200, 100)
    RED    = (220, 50, 50)
    GOLD   = (220, 175, 0)
    DIV    = (40, 40, 44)

    img  = Image.new("RGB", (W, H), BLACK)
    draw = ImageDraw.Draw(img)

    bold = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    reg  = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    try:
        f_hdr  = ImageFont.truetype(bold, 20)
        f_col  = ImageFont.truetype(bold, 14)
        f_cell = ImageFont.truetype(reg,  14)
        f_foot = ImageFont.truetype(reg,  13)
    except:
        f_hdr = f_col = f_cell = f_foot = ImageFont.load_default()

    # Header
    draw.rectangle([0, 0, W, HEADER], fill=DARK)
    draw.text((W//2, HEADER//2), "📊 تقرير الصفقات اليومي", fill=GOLD, font=f_hdr, anchor="mm")
    draw.line([0, HEADER, W, HEADER], fill=DIV, width=1)

    # Column headers
    headers = ["Strike", "Entry", "High", "الربح/الخسارة"]
    x = 0
    for i, (h, cw) in enumerate(zip(headers, COLS)):
        draw.rectangle([x, HEADER, x+cw, HEADER+ROW_H], fill=DARK2)
        draw.text((x + cw//2, HEADER + ROW_H//2), h, fill=GRAY1, font=f_col, anchor="mm")
        if i < len(COLS)-1:
            draw.line([x+cw, HEADER, x+cw, HEADER+ROW_H], fill=DIV, width=1)
        x += cw
    draw.line([0, HEADER+ROW_H, W, HEADER+ROW_H], fill=DIV, width=2)

    # Rows
    total_pnl = 0
    for ri, t in enumerate(trades_history):
        y      = HEADER + ROW_H + ri * ROW_H
        bg     = (12, 12, 12) if ri % 2 == 0 else (18, 18, 18)
        draw.rectangle([0, y, W, y+ROW_H], fill=bg)

        entry     = float(t.get("entry", 0))
        max_price = float(t.get("max_price", entry))
        pnl       = (max_price - entry) * 100  # per contract
        total_pnl += pnl
        pnl_color = GREEN if pnl >= 0 else RED
        sign      = "+" if pnl >= 0 else ""

        contract = f"{t.get('symbol','')} ${t.get('strike','')} {t.get('type','')} {t.get('expiry','')}"
        cells = [
            (contract,                        WHITE),
            (f"${entry:.2f}",                 GRAY1),
            (f"${max_price:.2f}",             WHITE),
            (f"{sign}${pnl:.0f}",             pnl_color),
        ]

        x = 0
        for i, ((text, col), cw) in enumerate(zip(cells, COLS)):
            draw.text((x + cw//2, y + ROW_H//2), text, fill=col, font=f_cell, anchor="mm")
            if i < len(COLS)-1:
                draw.line([x+cw, y, x+cw, y+ROW_H], fill=DIV, width=1)
            x += cw
        draw.line([0, y+ROW_H, W, y+ROW_H], fill=DIV, width=1)

    # Footer
    fy = H - FOOTER
    draw.rectangle([0, fy, W, H], fill=DARK)
    draw.line([0, fy, W, fy], fill=DIV, width=1)
    total_color = GREEN if total_pnl >= 0 else RED
    sign = "+" if total_pnl >= 0 else ""
    draw.text((W//2, fy + FOOTER//2),
              f"إجمالي اليوم: {sign}${total_pnl:.0f}",
              fill=total_color, font=f_hdr, anchor="mm")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


async def send_daily_stats(app):
    """Send daily stats at 11 PM Saudi time"""
    try:
        if not active_trades and not closed_trades_today:
            return

        all_trades = list(closed_trades_today) + list(active_trades.values())
        if not all_trades:
            return

        img = make_stats_image(all_trades)
        await app.bot.send_photo(
            chat_id=PRIVATE_GROUP,
            photo=img,
            caption="📊 *تقرير نهاية اليوم*",
            parse_mode="Markdown"
        )
        # Reset daily closed trades
        closed_trades_today.clear()
    except Exception as e:
        logger.error(f"Daily stats error: {e}")


def make_stats_image(trades: list, label: str = "اليومي") -> io.BytesIO:
    from PIL import Image, ImageDraw, ImageFont
    from datetime import datetime as dt

    import PIL.ImageFont as _IFont
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        _config = arabic_reshaper.ArabicReshaper(configuration={
            'delete_harakat': True,
            'support_ligatures': True,
            'ALEF_MADDA': True,
            'ALEF_HAMZA_ABOVE': True,
            'ALEF_HAMZA_BELOW': True,
            'ALEF_WASLA': True,
        })
        def ar(text): return get_display(_config.reshape(str(text)))
    except:
        def ar(text): return text
    def _font(size):
        paths = [
            "Cairo-Bold.ttf",
            "/app/Cairo-Bold.ttf",
        ]
        for p in paths:
            try: return _IFont.truetype(p, size)
            except: pass
        return _IFont.load_default()
    BOLD_F = lambda s: _font(s)
    REG_F  = lambda s: _font(s)

    PAD    = 24
    ROW_H  = 52
    HEADER = 85
    COL_H  = 40
    FOOTER = 105
    COLS   = [140, 88, 118, 118, 136]
    W      = sum(COLS) + PAD*2
    H      = HEADER + COL_H + ROW_H * max(len(trades),1) + FOOTER

    try:
        bg_orig = Image.open("card_bg.png").convert("RGB")
        bg = bg_orig.resize((W, H)).convert("RGBA")
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 225))
        img = Image.alpha_composite(bg, overlay).convert("RGB")
    except:
        img = Image.new("RGB", (W, H), (0,0,0))

    f_title = BOLD_F(22)
    f_sub   = BOLD_F(13)
    f_head  = BOLD_F(13)
    f_data  = BOLD_F(14)
    f_sum_l = BOLD_F(13)
    f_sum_v = BOLD_F(13)

    WHITE = (255, 255, 255)
    GRAY2 = (88,  92, 108)
    GREEN = (0,   218, 102)
    RED   = (232,  48,  48)
    GOLD  = (222, 180,   0)
    DIV   = (55,  55,  62)

    def rect_alpha(base, xy, color):
        ov = Image.new("RGBA", base.size, (0,0,0,0))
        ImageDraw.Draw(ov).rectangle(xy, fill=color)
        return Image.alpha_composite(base.convert("RGBA"), ov).convert("RGB")

    # Header
    img = rect_alpha(img, [0,0,W,HEADER], (0,0,0,170))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 5, HEADER], fill=GOLD)
    draw.rectangle([0, 0, W, 3], fill=GOLD)

    now     = dt.now()

    draw.text((PAD+10, HEADER//2 - 12), f"SPX Report  -  {label}", fill=GOLD, font=f_title, anchor="lm")
    draw.text((PAD+10, HEADER//2 + 16), f"{now.strftime("%A")}  ·  {now.strftime('%d %b %Y')}", fill=WHITE, font=f_sub, anchor="lm")
    draw.line([0, HEADER, W, HEADER], fill=DIV, width=1)

    # Column headers
    img = rect_alpha(img, [0, HEADER, W, HEADER+COL_H], (6,6,10,220))
    draw = ImageDraw.Draw(img)
    headers = ["Strike", "Type", "Entry", "High", "P&L"]
    col_xs  = [PAD + sum(COLS[:i]) + COLS[i]//2 for i in range(5)]
    for i,(h,cx) in enumerate(zip(headers, col_xs)):
        draw.text((cx, HEADER + COL_H//2), h, fill=WHITE, font=f_head, anchor="mm")
    for i in range(1, 5):
        draw.line([PAD+sum(COLS[:i]), HEADER+8, PAD+sum(COLS[:i]), HEADER+COL_H-8], fill=DIV, width=1)
    draw.line([0, HEADER+COL_H, W, HEADER+COL_H], fill=GOLD, width=2)

    # Rows
    total_profit = 0
    total_loss   = 0

    for ri, t in enumerate(trades):
        y      = HEADER + COL_H + ri * ROW_H
        is_put = t.get("type","").upper() == "PUT"
        img    = rect_alpha(img, [0,y,W,y+ROW_H], (80,10,10,120) if is_put else (10,60,25,120))
        draw   = ImageDraw.Draw(img)

        entry     = float(t.get("entry", 0))
        max_price = float(t.get("max_price", entry))
        is_win    = max_price > entry
        pnl       = (max_price - entry) * 100 if is_win else -(entry * 100)
        if is_win: total_profit += pnl
        else:      total_loss   += pnl

        cy         = y + ROW_H//2
        type_color = RED if is_put else GREEN
        pnl_color  = GREEN if is_win else RED
        sign       = "+" if is_win else "−"

        draw.text((col_xs[0], cy), str(t.get("strike","")),  fill=WHITE,      font=f_data, anchor="mm")
        draw.text((col_xs[1], cy), t.get("type",""),          fill=type_color, font=f_data, anchor="mm")
        draw.text((col_xs[2], cy), f"${entry:.2f}",           fill=WHITE,      font=f_data, anchor="mm")
        draw.text((col_xs[3], cy), f"${max_price:.2f}",       fill=WHITE,      font=f_data, anchor="mm")
        draw.text((col_xs[4], cy), f"{sign}${abs(pnl):.0f}",  fill=pnl_color,  font=f_data, anchor="mm")

        for i in range(1, 5):
            draw.line([PAD+sum(COLS[:i]), y+10, PAD+sum(COLS[:i]), y+ROW_H-10], fill=DIV, width=1)
        draw.line([0, y+ROW_H, W, y+ROW_H], fill=(36,36,42), width=1)

    # Footer
    fy  = H - FOOTER
    img = rect_alpha(img, [0,fy,W,H], (0,0,0,210))
    draw = ImageDraw.Draw(img)
    draw.line([0, fy, W, fy], fill=GOLD, width=2)
    draw.rectangle([0, H-3, W, H], fill=GOLD)

    net   = total_profit + total_loss
    third = (W - PAD*2) // 3

    for i,(lbl,val,col,box_bg) in enumerate([
        ("Total Profit",   f"+${total_profit:.0f}", GREEN, (8,40,18)),
        ("Total Loss", f"-${abs(total_loss):.0f}", RED, (44,8,8)),
        ("Net P&L",     f"{'+'if net>=0 else''}${net:.0f}", GREEN if net>=0 else RED, (8,40,18) if net>=0 else (44,8,8)),
    ]):
        cx = PAD + i*third + third//2
        draw.rounded_rectangle([PAD+i*third+10, fy+14, PAD+(i+1)*third-10, fy+FOOTER-14], radius=12, fill=box_bg)
        draw.text((cx, fy+42), lbl, fill=WHITE, font=f_sum_l, anchor="mm")
        draw.text((cx, fy+68), val, fill=col,   font=f_sum_v, anchor="mm")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


async def main():
    saved = load_trades()
    for k, t in saved.items():
        active_trades[k] = t
    closed_trades_all.extend(load_history())

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
    web_app.router.add_post("/tg", tg_webhook)
    web_app.router.add_get("/", lambda r: web.Response(text="OK"))
    web_app.router.add_get("/active_trades", get_active_trades)
    web_app.router.add_get("/closed_trades", get_closed_trades)

    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    print(f"Bot running on port {PORT}")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
