import os
import logging
import asyncio
from datetime import time as dtime
import pytz

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, JobQueue
)

from bb_fetcher import BBFetcher
from report_builder import build_report

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
BESTBUY_API_KEY  = os.environ.get("BESTBUY_API_KEY")
REPORT_CHAT_ID   = os.environ.get("REPORT_CHAT_ID")      # group/channel chat ID to auto-send to
ADMIN_IDS        = set(
    int(x.strip()) for x in os.environ.get("ADMIN_TELEGRAM_ID", "0").split(",")
    if x.strip().lstrip("-").isdigit()
)
REPORT_HOUR_EST  = int(os.environ.get("REPORT_HOUR_EST", "8"))   # default 8am EST

EST = pytz.timezone("US/Eastern")

fetcher = BBFetcher(BESTBUY_API_KEY)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def send_report(app, chat_id: int | str):
    """Fetch fresh BB data, build the Excel, send to chat_id."""
    try:
        await app.bot.send_message(
            chat_id=chat_id,
            text="📊 *iGamer Morning Market Report*\n\n⏳ Pulling live Best Buy data... give me 30 seconds.",
            parse_mode="Markdown"
        )

        data = await fetcher.fetch_all()
        path = build_report(data)

        now_est = asyncio.get_event_loop().time()
        caption = (
            "☀️ *iGamer Corp — Daily BB Market Intelligence*\n\n"
            "Your morning snapshot is ready:\n"
            "• 📋 Summary tab — category overview + today's top deals\n"
            "• 🏆 Top Deals — ranked by signal score (on sale + trending + sold)\n"
            "• 🔴 HOT BUY rows = act today\n"
            "• 5 category tabs with live Buy links\n\n"
            "_Generated fresh from Best Buy API_"
        )

        with open(path, "rb") as f:
            await app.bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=os.path.basename(path),
                caption=caption,
                parse_mode="Markdown"
            )

        try:
            os.remove(path)
        except Exception:
            pass

        logger.info(f"Report delivered to chat {chat_id}")

    except Exception as e:
        logger.error(f"Report generation failed: {e}")
        await app.bot.send_message(
            chat_id=chat_id,
            text=f"❌ *Report failed:* `{str(e)}`\nCheck logs on Railway.",
            parse_mode="Markdown"
        )


# ── Scheduled job ─────────────────────────────────────────────────────────────

async def scheduled_report(context: ContextTypes.DEFAULT_TYPE):
    """Called by APScheduler every morning."""
    chat_id = REPORT_CHAT_ID or next(iter(ADMIN_IDS), 0)
    if not chat_id:
        logger.error("No REPORT_CHAT_ID or ADMIN_TELEGRAM_ID set — can't send scheduled report")
        return
    logger.info(f"Scheduled report firing → chat {chat_id}")
    await send_report(context.application, int(chat_id))


# ── /start ────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_admin = update.effective_user.id in ADMIN_IDS
    admin_block = (
        "\n\n*Admin commands:*\n"
        "/report — Send report right now\n"
        "/schedule — View current schedule\n"
        "/setchat — Set this chat as report destination\n"
        "/test — Quick API connectivity test"
    ) if is_admin else ""

    await update.message.reply_text(
        "👋 *iGamer Morning Market Report Bot*\n\n"
        "I send your team a daily Best Buy market intelligence report every morning.\n\n"
        "The report includes:\n"
        "• Today's top deals ranked by signal strength\n"
        "• 5 category sheets with live prices + Buy links\n"
        "• Market signals: trending, most viewed, best sellers\n"
        "• Discount highlights and hot buy alerts"
        + admin_block,
        parse_mode="Markdown"
    )


# ── /report (manual trigger) ──────────────────────────────────────────────────

async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return
    chat_id = REPORT_CHAT_ID or update.effective_chat.id
    await send_report(context.application, int(chat_id))


# ── /setchat ──────────────────────────────────────────────────────────────────

async def setchat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return
    chat_id = update.effective_chat.id
    chat_title = update.effective_chat.title or "this chat"
    await update.message.reply_text(
        f"✅ *Report destination set!*\n\n"
        f"Chat: *{chat_title}*\n"
        f"ID: `{chat_id}`\n\n"
        f"⚠️ To make this permanent, set `REPORT_CHAT_ID={chat_id}` in your Railway environment variables.\n\n"
        f"For now I'll use this chat for the next scheduled send.",
        parse_mode="Markdown"
    )
    # Store temporarily in bot_data until restart
    context.bot_data["override_chat_id"] = chat_id


# ── /schedule ─────────────────────────────────────────────────────────────────

async def schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return

    chat_id = REPORT_CHAT_ID or context.bot_data.get("override_chat_id") or next(iter(ADMIN_IDS), 0)
    jobs = context.job_queue.jobs()
    job_list = "\n".join([f"• {j.name} — next run: {j.next_t}" for j in jobs]) if jobs else "No jobs scheduled"

    await update.message.reply_text(
        f"⏰ *Schedule Status*\n\n"
        f"Daily report time: *{REPORT_HOUR_EST}:00 AM EST*\n"
        f"Report destination: `{chat_id}`\n\n"
        f"*Active jobs:*\n{job_list}",
        parse_mode="Markdown"
    )


# ── /test ─────────────────────────────────────────────────────────────────────

async def test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return

    await update.message.reply_text("🔌 Testing Best Buy API connection...")

    try:
        ok, count, sample = await fetcher.test_connection()
        if ok:
            await update.message.reply_text(
                f"✅ *Best Buy API — Connected*\n\n"
                f"Test category returned {count} products\n"
                f"Sample: _{sample}_",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(f"❌ API test failed: {count}")
    except Exception as e:
        await update.message.reply_text(f"❌ Exception: `{e}`", parse_mode="Markdown")


# ── /settime (admin: change schedule hour) ────────────────────────────────────

async def settime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return

    keyboard = [
        [
            InlineKeyboardButton("6 AM", callback_data="settime_6"),
            InlineKeyboardButton("7 AM", callback_data="settime_7"),
            InlineKeyboardButton("8 AM", callback_data="settime_8"),
        ],
        [
            InlineKeyboardButton("9 AM", callback_data="settime_9"),
            InlineKeyboardButton("10 AM", callback_data="settime_10"),
            InlineKeyboardButton("11 AM", callback_data="settime_11"),
        ],
    ]
    await update.message.reply_text(
        "⏰ *Set daily report time (EST)*\n\nWhen should the morning report send?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def settime_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if update.effective_user.id not in ADMIN_IDS:
        await query.answer("⛔ Admin only.", show_alert=True)
        return

    hour = int(query.data.replace("settime_", ""))

    # Remove existing daily job and re-add at new time
    for job in context.job_queue.get_jobs_by_name("daily_report"):
        job.schedule_removal()

    context.job_queue.run_daily(
        scheduled_report,
        time=dtime(hour=hour, minute=0, tzinfo=EST),
        name="daily_report"
    )

    await query.edit_message_text(
        f"✅ *Schedule updated!*\n\n"
        f"Morning report will now send at *{hour}:00 AM EST* daily.\n\n"
        f"⚠️ Update `REPORT_HOUR_EST={hour}` in Railway to make this permanent.",
        parse_mode="Markdown"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Schedule the daily report
    app.job_queue.run_daily(
        scheduled_report,
        time=dtime(hour=REPORT_HOUR_EST, minute=0, tzinfo=EST),
        name="daily_report"
    )
    logger.info(f"Daily report scheduled at {REPORT_HOUR_EST}:00 AM EST")

    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("report",   report_cmd))
    app.add_handler(CommandHandler("schedule", schedule_cmd))
    app.add_handler(CommandHandler("setchat",  setchat_cmd))
    app.add_handler(CommandHandler("test",     test_cmd))
    app.add_handler(CommandHandler("settime",  settime_cmd))
    app.add_handler(CallbackQueryHandler(settime_callback, pattern="^settime_"))

    logger.info("iGamer Morning Report bot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
