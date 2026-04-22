import os
import json
import logging
from datetime import datetime
from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, ContextTypes
from telegram import Update

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN  = os.environ["SIGNALS_TOKEN"]
PRIVATE_GROUP   = -1003618409425
PUBLIC_CHANNEL  = -1001934800979
ADMIN_ID        = int(os.environ.get("ADMIN_ID", "0"))
PORT            = int(os.environ.get("PORT", "8080"))

# ─── Format Message ────────────────────────────────────────────────────────────
def format_signal(signal_type: str) -> str:
    emoji = "🔴" if signal_type.upper() == "PUT" else "🟢"
    return (
        f"⚡️ *تنبيه صفقة محتملة*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{emoji} {signal_type.upper()}\n"
        f"━━━━━━━━━━━━━━━━"
    )

# ─── Webhook Handler ───────────────────────────────────────────────────────────
async def handle_webhook(request):
    try:
        data = await request.json()
        logger.info(f"Received webhook: {data}")

        signal_type = data.get("signal", "").strip()
        if signal_type.upper() not in ("PUT", "CALL"):
            return web.Response(text="Invalid signal", status=400)

        msg = format_signal(signal_type)

        # Keyboard for admin to post to public channel
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📣 نشر في القناة العامة", callback_data=f"pub_{signal_type.upper()}_{msg}"),
            InlineKeyboardButton("❌ تجاهل", callback_data="ignore"),
        ]])

        # Post to private group
        app = request.app["bot_app"]
        await app.bot.send_message(
            chat_id=PRIVATE_GROUP,
            text=msg,
            parse_mode="Markdown",
            reply_markup=kb
        )

        # Send private notification to admin
        if ADMIN_ID:
            await app.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"⚡️ إشارة جديدة: *{signal_type.upper()}*\nتم النشر في المجموعة الخاصة ✅",
                parse_mode="Markdown"
            )

        return web.Response(text="OK")

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(text="Error", status=500)

# ─── Button Handler ────────────────────────────────────────────────────────────
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "ignore":
        await query.edit_message_reply_markup(reply_markup=None)
        return

    if data.startswith("pub_"):
        parts       = data.split("_", 2)
        signal_type = parts[1]
        msg         = parts[2]

        try:
            await context.bot.send_message(
                chat_id=PUBLIC_CHANNEL,
                text=msg,
                parse_mode="Markdown"
            )
            await query.edit_message_reply_markup(reply_markup=None)
            await query.edit_message_text(
                text=msg + "\n\n✅ *تم النشر في القناة العامة*",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Error posting to channel: {e}")
            await query.answer("⚠️ فشل النشر في القناة العامة", show_alert=True)

# ─── Main ──────────────────────────────────────────────────────────────────────
async def main():
    # Build telegram app
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CallbackQueryHandler(button_handler))
    await app.initialize()
    await app.start()

    # Build web server
    web_app = web.Application()
    web_app["bot_app"] = app
    web_app.router.add_post("/webhook", handle_webhook)
    web_app.router.add_get("/", lambda r: web.Response(text="Signals Bot Running ✅"))

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    logger.info(f"Signals bot running on port {PORT}")
    print(f"✅ Signals bot running on port {PORT}")

    # Keep running
    import asyncio
    await asyncio.Event().wait()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
