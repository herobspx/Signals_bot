import os
import json
import logging
import asyncio
from datetime import datetime
from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["SIGNALS_TOKEN"]
PRIVATE_GROUP  = -1003618409425
PUBLIC_CHANNEL = -1001934800979
ADMIN_ID       = int(os.environ.get("ADMIN_ID", "0"))
PORT           = int(os.environ.get("PORT", "8080"))

signals_store = {}

def format_signal(signal_type: str) -> str:
    emoji = "🔴" if signal_type.upper() == "PUT" else "🟢"
    return (
        f"⚡️ *تنبيه صفقة محتملة*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{emoji} {signal_type.upper()}\n"
        f"━━━━━━━━━━━━━━━━"
    )

async def handle_webhook(request):
    try:
        data = await request.json()
        logger.info(f"Received: {data}")

        signal_type = data.get("signal", "").strip().upper()
        if signal_type not in ("PUT", "CALL"):
            return web.Response(text="Invalid signal", status=400)

        msg = format_signal(signal_type)
        sig_id = str(int(datetime.now().timestamp()))
        signals_store[sig_id] = {"type": signal_type, "msg": msg}

        bot_app = request.app["bot_app"]

        # 1. نشر في المجموعة الخاصة بدون أزرار
        await bot_app.bot.send_message(
            chat_id=PRIVATE_GROUP,
            text=msg,
            parse_mode="Markdown"
        )

        # 2. إرسال رسالة خاصة للأدمن فقط مع الأزرار
        if ADMIN_ID:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("📣 نشر في القناة العامة", callback_data=f"pub_{sig_id}"),
                InlineKeyboardButton("❌ تجاهل", callback_data=f"ign_{sig_id}"),
            ]])
            await bot_app.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"⚡️ *إشارة جديدة — {signal_type}*\n\nهل تريد نشرها في القناة العامة؟",
                parse_mode="Markdown",
                reply_markup=kb
            )

        return web.Response(text="OK")

    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return web.Response(text="Error", status=500)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # تحقق إن الأدمن هو اللي يضغط
    if str(query.from_user.id) != str(ADMIN_ID):
        await query.answer("⛔ غير مصرح", show_alert=True)
        return

    if data.startswith("ign_"):
        await query.edit_message_text("❌ تم تجاهل الإشارة.")
        return

    if data.startswith("pub_"):
        sig_id = data.replace("pub_", "")
        signal = signals_store.get(sig_id)
        if not signal:
            await query.answer("⚠️ انتهت صلاحية الإشارة", show_alert=True)
            return
        try:
            await context.bot.send_message(
                chat_id=PUBLIC_CHANNEL,
                text=signal["msg"],
                parse_mode="Markdown"
            )
            await query.edit_message_text(
                text=f"✅ *تم نشر إشارة {signal['type']} في القناة العامة*",
                parse_mode="Markdown"
            )
            signals_store.pop(sig_id, None)
        except Exception as e:
            logger.error(f"Channel error: {e}", exc_info=True)
            await query.answer(f"⚠️ فشل النشر: {str(e)}", show_alert=True)

async def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CallbackQueryHandler(button_handler))
    await app.initialize()
    await app.start()

    web_app = web.Application()
    web_app["bot_app"] = app
    web_app.router.add_post("/webhook", handle_webhook)
    web_app.router.add_get("/", lambda r: web.Response(text="Signals Bot OK"))

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Bot running on port {PORT}")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
