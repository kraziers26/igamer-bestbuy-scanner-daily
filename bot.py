import os
import logging
from datetime import time as dtime, datetime
import pytz

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler
)

from bb_fetcher import BBFetcher, CATEGORIES, BRANDS, PRICE_RANGES
from report_builder import build_report

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN")
BESTBUY_API_KEY = os.environ.get("BESTBUY_API_KEY")
REPORT_CHAT_ID  = os.environ.get("REPORT_CHAT_ID")
ADMIN_IDS       = set(
    int(x.strip()) for x in os.environ.get("ADMIN_TELEGRAM_ID", "0").split(",")
    if x.strip().lstrip("-").isdigit()
)
REPORT_HOUR_EST = int(os.environ.get("REPORT_HOUR_EST", "8"))

EST     = pytz.timezone("US/Eastern")
fetcher = BBFetcher(BESTBUY_API_KEY)

# ConversationHandler states for /report flow
CAT_STEP, PRICE_STEP, BRAND_STEP, TYPE_STEP = range(4)

# Conversation timeout in seconds (2 minutes)
CONV_TIMEOUT = 120

FILTERS = {
    "trending": ("🆕 Fresh Deals",       "Newest price drops sorted by Fresh Deal Score"),
    "full":     ("📦 Established Deals", "Top products by BB popularity"),
    "on_sale":  ("💰 On Sale Only",      "Discounted products sorted by % off"),
    "selling":  ("🛒 Best Sellers",      "Sorted by BB global best seller rank"),
    "hot":      ("🔴 HOT BUYS Only",    "Fresh drop + deep discount — highest conviction"),
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def is_admin(update: Update) -> bool:
    return update.effective_user.id in ADMIN_IDS

def fmt_price(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except Exception:
        return str(v)

def offer_emoji(offer_type: str) -> str:
    return {
        "Deal of the Day": "🎯",
        "Clearance":       "🟣",
        "Weekly Ad":       "📰",
        "Special Offer":   "⭐",
    }.get(offer_type or "", "")


# ── Core report sender ─────────────────────────────────────────────────────────

async def send_report(app, chat_id, filter_key="trending", triggered_by="scheduled"):
    label, description = FILTERS.get(filter_key, FILTERS["trending"])
    source = "📅 Scheduled" if triggered_by == "scheduled" else "⚡ On-Demand"
    try:
        await app.bot.send_message(
            chat_id=chat_id,
            text=(
                f"📊 *iGamer Market Report — {label}*\n\n"
                f"_{description}_\n\n"
                f"⏳ Pulling live Best Buy data..."
            ),
            parse_mode="Markdown"
        )
        data = await fetcher.fetch_all()
        path = build_report(data, filter_key=filter_key)

        caption = (
            f"{source} | *iGamer Corp — BB Market Intelligence*\n\n"
            f"*Filter: {label}*\n\n"
            f"_Tap any 🛒 Buy Now link to go directly to Best Buy_"
        )
        with open(path, "rb") as f:
            await app.bot.send_document(
                chat_id=chat_id, document=f,
                filename=os.path.basename(path),
                caption=caption, parse_mode="Markdown"
            )
        try:
            os.remove(path)
        except Exception:
            pass
        logger.info(f"Report [{filter_key}] delivered to {chat_id}")
    except Exception as e:
        logger.error(f"Report failed [{filter_key}]: {e}")
        await app.bot.send_message(
            chat_id=chat_id,
            text=f"❌ *Report failed:* `{str(e)}`",
            parse_mode="Markdown"
        )


# ── Alert message formatting ───────────────────────────────────────────────────

def format_alert_message(p: dict) -> str:
    """Format a single product as a Telegram alert message."""
    name       = p.get("name", "—")[:60]
    brand      = p.get("manufacturer", "—")
    price      = fmt_price(p.get("salePrice"))
    was        = fmt_price(p.get("regularPrice") or p.get("salePrice"))
    save_d     = float(p.get("dollarSavings") or 0)
    save_pct   = float(p.get("percentSavings") or 0)
    score      = p.get("fresh_score", 0)
    category   = p.get("_category", "")
    freshness  = p.get("freshness_label", "—")
    offer_type = p.get("offer_type") or ""
    offer_note = p.get("offer_note") or ""
    offer_lbl  = p.get("offer_label") or ""
    oe         = offer_emoji(offer_type)

    # Score line — urgency language
    if score >= 13:
        score_line = f"🔥 Fresh Score {score}/13 — highest conviction"
    elif score >= 11:
        score_line = f"🔴 Fresh Score {score}/13 — act fast"
    elif score >= 9:
        score_line = f"🟠 Fresh Score {score}/13 — strong buy signal"
    else:
        score_line = f"🟡 Fresh Score {score}/13"

    # Build badge line
    badges = []
    if offer_lbl:
        badges.append(f"{oe} {offer_lbl}" if oe else offer_lbl)
    if score >= 9:
        badges.append("🔴 HOT BUY")
    badge_line = "  |  ".join(badges)

    lines = []
    if badge_line:
        lines.append(f"*{badge_line}*")
    lines.append(f"*{name}*")
    if brand and brand not in name:
        lines.append(f"Brand: {brand}   |   Category: {category}")
    lines.append(f"Price: *{price}*   |   Was: {was}")
    if save_d > 0:
        lines.append(f"Save: *${save_d:.0f} ({save_pct:.0f}%)*")
    lines.append(f"Drop: {freshness}")
    lines.append(score_line)
    if offer_note:
        lines.append(f"_{offer_note}_")

    return "\n".join(lines)


# ── Scheduled alert poll ───────────────────────────────────────────────────────

async def price_drop_alert_job(context: ContextTypes.DEFAULT_TYPE):
    """Runs every 2-3 hours. Sends Telegram messages for new qualifying deals."""
    chat_id = REPORT_CHAT_ID or next(iter(ADMIN_IDS), 0)
    if not chat_id:
        logger.error("Alert job: no chat_id configured")
        return

    logger.info("Running price drop alert poll...")
    try:
        candidates = await fetcher.fetch_for_alerts()
    except Exception as e:
        logger.error(f"Alert poll fetch failed: {e}")
        return

    if not candidates:
        logger.info("Alert poll: no qualifying deals found")
        return

    # Load today's seen-SKU cache
    seen_today: set = context.bot_data.get("seen_skus_today", set())
    last_reset: str = context.bot_data.get("seen_skus_reset_date", "")
    today_str = datetime.now(EST).strftime("%Y-%m-%d")

    # Reset cache daily at midnight
    if last_reset != today_str:
        seen_today = set()
        context.bot_data["seen_skus_reset_date"] = today_str
        logger.info("Alert cache reset for new day")

    new_deals = [p for p in candidates if str(p.get("sku")) not in seen_today]

    if not new_deals:
        logger.info(f"Alert poll: {len(candidates)} candidates, all already seen today")
        return

    logger.info(f"Alert poll: sending {len(new_deals)} new deal(s)")

    for p in new_deals:
        sku = str(p.get("sku"))
        url = p.get("url", "")
        msg = format_alert_message(p)

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("View on Best Buy", url=url) if url else
            InlineKeyboardButton("View on Best Buy", callback_data=f"noop"),
            InlineKeyboardButton("More like this", callback_data=f"similar_{sku}"),
        ]])

        try:
            await context.bot.send_message(
                chat_id=int(chat_id),
                text=msg,
                parse_mode="Markdown",
                reply_markup=keyboard
            )
            seen_today.add(sku)
        except Exception as e:
            logger.error(f"Failed to send alert for SKU {sku}: {e}")

    context.bot_data["seen_skus_today"] = seen_today


# ── alsoViewed callback (More like this) ───────────────────────────────────────

async def similar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    sku = query.data.replace("similar_", "")
    await query.message.reply_text(f"🔍 Finding similar products to SKU {sku}...")

    try:
        products = await fetcher.fetch_also_viewed(sku)
    except Exception as e:
        await query.message.reply_text(f"❌ Could not fetch similar products: `{e}`", parse_mode="Markdown")
        return

    if not products:
        await query.message.reply_text("No similar products found right now.")
        return

    lines = [f"*Similar products — also viewed by BB customers:*\n"]
    for i, p in enumerate(products[:8], 1):
        name      = (p.get("name") or "—")[:55]
        price     = fmt_price(p.get("salePrice"))
        save_pct  = float(p.get("percentSavings") or 0)
        score     = p.get("fresh_score", 0)
        offer_lbl = p.get("offer_label") or ""
        oe        = offer_emoji(p.get("offer_type") or "")
        url       = p.get("url", "")

        score_icon = "🔴" if score >= 9 else ("🟠" if score >= 6 else "🟡")
        offer_part = f" | {oe} {offer_lbl}" if offer_lbl else ""
        save_part  = f" | -{save_pct:.0f}%" if save_pct > 0 else ""
        link_part  = f" — [Buy]({url})" if url else ""

        lines.append(f"{i}. {score_icon} *{name}*")
        lines.append(f"   {price}{save_part} | Score {score}/13{offer_part}{link_part}\n")

    await query.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


# ── /report — 3-step ConversationHandler ──────────────────────────────────────

async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Admin only.")
        return ConversationHandler.END

    # Build category keyboard
    buttons = []
    for name, cat_id in CATEGORIES:
        buttons.append([InlineKeyboardButton(name, callback_data=f"rcat_{cat_id}_{name}")])
    buttons.append([InlineKeyboardButton("All categories", callback_data="rcat_ALL_All")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="rcat_CANCEL_cancel")])

    await update.message.reply_text(
        "📊 *Pull a Report — Step 1 of 3*\n\nWhich category?",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )
    return CAT_STEP


async def report_cat_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts  = query.data.split("_", 2)
    cat_id = parts[1]
    name   = parts[2] if len(parts) > 2 else "All"

    if cat_id == "CANCEL":
        await query.edit_message_text("❌ Cancelled.")
        return ConversationHandler.END

    context.user_data["rep_cat_id"]   = cat_id
    context.user_data["rep_cat_name"] = name

    # Price range keyboard
    buttons = []
    for label, pmin, pmax in PRICE_RANGES:
        buttons.append([InlineKeyboardButton(label, callback_data=f"rprice_{pmin}_{pmax}_{label}")])
    buttons.append([InlineKeyboardButton("Any price", callback_data="rprice_None_None_Any")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="rprice_CANCEL_CANCEL_cancel")])

    await query.edit_message_text(
        f"📊 *Step 2 of 3 — Price Range*\n\nCategory: *{name}*\n\nWhat price range?",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )
    return PRICE_STEP


async def report_price_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_", 3)
    pmin_s, pmax_s, label = parts[1], parts[2], parts[3]

    if pmin_s == "CANCEL":
        await query.edit_message_text("❌ Cancelled.")
        return ConversationHandler.END

    context.user_data["rep_price_min"]   = None if pmin_s == "None" else float(pmin_s)
    context.user_data["rep_price_max"]   = None if pmax_s == "None" else float(pmax_s)
    context.user_data["rep_price_label"] = label
    cat_name = context.user_data.get("rep_cat_name", "All")

    # Brand keyboard
    buttons = []
    row = []
    for brand in BRANDS:
        row.append(InlineKeyboardButton(brand, callback_data=f"rbrand_{brand}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("Any brand", callback_data="rbrand_ANY")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="rbrand_CANCEL")])

    await query.edit_message_text(
        f"📊 *Step 3 of 3 — Brand*\n\n"
        f"Category: *{cat_name}*   |   Price: *{label}*\n\nWhich brand?",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )
    return BRAND_STEP


async def report_brand_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    brand = query.data.replace("rbrand_", "")

    if brand == "CANCEL":
        await query.edit_message_text("❌ Cancelled.")
        return ConversationHandler.END

    context.user_data["rep_brand"] = None if brand == "ANY" else brand
    cat_name    = context.user_data.get("rep_cat_name", "All")
    price_label = context.user_data.get("rep_price_label", "Any")
    brand_label = brand if brand != "ANY" else "Any brand"

    # Report type keyboard
    buttons = [
        [InlineKeyboardButton("🆕 Fresh Deals (Telegram list)", callback_data="rtype_fresh_msg")],
        [InlineKeyboardButton("🔴 HOT BUYS Only (Telegram list)", callback_data="rtype_hot_msg")],
        [InlineKeyboardButton("📊 Full Excel Report", callback_data="rtype_full_excel")],
        [InlineKeyboardButton("❌ Cancel", callback_data="rtype_CANCEL")],
    ]

    await query.edit_message_text(
        f"📊 *Ready to pull*\n\n"
        f"Category: *{cat_name}*\n"
        f"Price: *{price_label}*\n"
        f"Brand: *{brand_label}*\n\n"
        f"How do you want the results?",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )
    return TYPE_STEP


async def report_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    rtype = query.data.replace("rtype_", "")

    if rtype == "CANCEL":
        await query.edit_message_text("❌ Cancelled.")
        return ConversationHandler.END

    cat_id      = context.user_data.get("rep_cat_id", "ALL")
    cat_name    = context.user_data.get("rep_cat_name", "All")
    price_min   = context.user_data.get("rep_price_min")
    price_max   = context.user_data.get("rep_price_max")
    price_label = context.user_data.get("rep_price_label", "Any")
    brand       = context.user_data.get("rep_brand")
    brand_label = brand or "Any brand"

    await query.edit_message_text(
        f"⏳ Pulling *{cat_name}* | *{price_label}* | *{brand_label}*...",
        parse_mode="Markdown"
    )

    try:
        if cat_id == "ALL":
            # Fetch all categories and merge
            all_products = []
            for name, cid in CATEGORIES:
                prods = await fetcher.fetch_filtered(cid, brand=brand, price_min=price_min, price_max=price_max)
                for p in prods:
                    p["_category"] = name
                all_products.extend(prods)
            all_products.sort(key=lambda p: p.get("fresh_score", 0), reverse=True)
        else:
            all_products = await fetcher.fetch_filtered(cat_id, brand=brand, price_min=price_min, price_max=price_max)
            for p in all_products:
                p["_category"] = cat_name

        if not all_products:
            await query.message.reply_text(
                f"😕 No results found for *{cat_name}* | *{price_label}* | *{brand_label}*.\n"
                f"Try widening your filters.",
                parse_mode="Markdown"
            )
            return ConversationHandler.END

        if rtype in ("fresh_msg", "hot_msg"):
            # Telegram message list — quick scan format
            if rtype == "hot_msg":
                products = [p for p in all_products if p.get("fresh_score", 0) >= 9]
                if not products:
                    await query.message.reply_text(
                        "No HOT BUYS found for that filter combination right now.",
                    )
                    return ConversationHandler.END
                header = f"🔴 *HOT BUYS — {cat_name} | {price_label} | {brand_label}*\n"
            else:
                products = all_products[:15]
                header = f"🆕 *Fresh Deals — {cat_name} | {price_label} | {brand_label}*\n"

            lines = [header]
            for i, p in enumerate(products[:15], 1):
                name_s    = (p.get("name") or "—")[:55]
                price_s   = fmt_price(p.get("salePrice"))
                save_pct  = float(p.get("percentSavings") or 0)
                score     = p.get("fresh_score", 0)
                freshness = p.get("freshness_label", "—")
                offer_lbl = p.get("offer_label") or ""
                oe        = offer_emoji(p.get("offer_type") or "")
                url       = p.get("url", "")
                cat_s     = p.get("_category", "")

                score_icon = "🔴" if score >= 9 else ("🟠" if score >= 6 else "🟡")
                offer_part = f" | {oe} {offer_lbl}" if offer_lbl else ""
                save_part  = f" | -{save_pct:.0f}%" if save_pct > 0 else ""
                link_part  = f" — [Buy]({url})" if url else ""
                cat_part   = f" | {cat_s}" if cat_id == "ALL" else ""

                lines.append(f"{i}. {score_icon} *{name_s}*")
                lines.append(f"   {price_s}{save_part} | {freshness}{offer_part}{cat_part}{link_part}\n")

            await query.message.reply_text(
                "\n".join(lines),
                parse_mode="Markdown",
                disable_web_page_preview=True
            )

        else:
            # Full Excel report — build filtered dataset structure
            data = {}
            if cat_id == "ALL":
                for name, cid in CATEGORIES:
                    cat_prods = [p for p in all_products if p.get("_category") == name]
                    data[name] = {"products": cat_prods[:10], "pool": cat_prods, "fresh_products": cat_prods}
            else:
                data[cat_name] = {
                    "products":       all_products[:10],
                    "pool":           all_products,
                    "fresh_products": all_products,
                }

            path = build_report(data, filter_key="trending")
            caption = (
                f"⚡ On-Demand | *{cat_name}* | *{price_label}* | *{brand_label}*\n"
                f"_Sorted by Fresh Deal Score_"
            )
            with open(path, "rb") as f:
                await query.message.reply_document(
                    document=f,
                    filename=os.path.basename(path),
                    caption=caption,
                    parse_mode="Markdown"
                )
            try:
                os.remove(path)
            except Exception:
                pass

    except Exception as e:
        logger.error(f"On-demand report failed: {e}")
        await query.message.reply_text(f"❌ Failed: `{e}`", parse_mode="Markdown")

    return ConversationHandler.END


async def report_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Called when /report flow times out."""
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "⏱ Report request timed out after 2 minutes. Use /report to start again."
        )
    return ConversationHandler.END


# ── Simple filter keyboard (for /setschedule and old-style /report fallback) ───

def filter_keyboard(prefix):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🆕 Fresh Deals",      callback_data=f"{prefix}_trending"),
            InlineKeyboardButton("🔴 HOT BUYS Only",    callback_data=f"{prefix}_hot"),
        ],
        [
            InlineKeyboardButton("💰 On Sale Only",     callback_data=f"{prefix}_on_sale"),
            InlineKeyboardButton("🛒 Best Sellers",     callback_data=f"{prefix}_selling"),
        ],
        [
            InlineKeyboardButton("📦 Established Deals", callback_data=f"{prefix}_full"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"{prefix}_cancel")],
    ])


# ── /setschedule ───────────────────────────────────────────────────────────────

async def setschedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Admin only.")
        return
    current_filter = context.bot_data.get("scheduled_filter", "trending")
    current_hour   = context.bot_data.get("scheduled_hour", REPORT_HOUR_EST)
    current_label  = FILTERS.get(current_filter, FILTERS["trending"])[0]
    display_hour   = f"{current_hour}:00 AM" if current_hour < 12 else ("12:00 PM" if current_hour == 12 else f"{current_hour-12}:00 PM")
    await update.message.reply_text(
        f"⚙️ *Configure Scheduled Report*\n\n"
        f"Current: *{display_hour} EST* — *{current_label}*\n\n"
        f"*Step 1 — Choose filter:*",
        reply_markup=filter_keyboard("sch"),
        parse_mode="Markdown"
    )


async def setschedule_filter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        await query.answer("⛔ Admin only.", show_alert=True)
        return

    filter_key = query.data.replace("sch_", "")
    if filter_key == "cancel":
        await query.edit_message_text("❌ Cancelled.")
        return

    context.bot_data["scheduled_filter"] = filter_key
    label, _ = FILTERS.get(filter_key, FILTERS["trending"])

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("6 AM",  callback_data="schtime_6"),
            InlineKeyboardButton("7 AM",  callback_data="schtime_7"),
            InlineKeyboardButton("8 AM",  callback_data="schtime_8"),
            InlineKeyboardButton("9 AM",  callback_data="schtime_9"),
        ],
        [
            InlineKeyboardButton("10 AM", callback_data="schtime_10"),
            InlineKeyboardButton("11 AM", callback_data="schtime_11"),
            InlineKeyboardButton("12 PM", callback_data="schtime_12"),
            InlineKeyboardButton("1 PM",  callback_data="schtime_13"),
        ],
    ])
    await query.edit_message_text(
        f"✅ Filter set: *{label}*\n\n*Step 2 — What time? (EST)*",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


async def setschedule_time_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        await query.answer("⛔ Admin only.", show_alert=True)
        return

    hour       = int(query.data.replace("schtime_", ""))
    filter_key = context.bot_data.get("scheduled_filter", "trending")
    label, _   = FILTERS.get(filter_key, FILTERS["trending"])

    for job in context.job_queue.get_jobs_by_name("daily_report"):
        job.schedule_removal()

    context.job_queue.run_daily(
        scheduled_report,
        time=dtime(hour=hour, minute=0, tzinfo=EST),
        name="daily_report",
        data={"filter_key": filter_key}
    )
    context.bot_data["scheduled_hour"] = hour
    display_hour = f"{hour}:00 AM" if hour < 12 else ("12:00 PM" if hour == 12 else f"{hour-12}:00 PM")

    await query.edit_message_text(
        f"✅ *Schedule Updated!*\n\n"
        f"⏰ Time: *{display_hour} EST* (daily)\n"
        f"📊 Filter: *{label}*\n\n"
        f"⚠️ To survive a Railway restart set `REPORT_HOUR_EST={hour}` in env vars.",
        parse_mode="Markdown"
    )


# ── Scheduled jobs ─────────────────────────────────────────────────────────────

async def scheduled_report(context: ContextTypes.DEFAULT_TYPE):
    chat_id = REPORT_CHAT_ID or next(iter(ADMIN_IDS), 0)
    if not chat_id:
        logger.error("No REPORT_CHAT_ID set")
        return
    filter_key = "trending"
    if context.job and context.job.data:
        filter_key = context.job.data.get("filter_key", "trending")
    else:
        filter_key = context.bot_data.get("scheduled_filter", "trending")
    logger.info(f"Scheduled report → chat {chat_id} | filter: {filter_key}")
    await send_report(context.application, int(chat_id), filter_key=filter_key, triggered_by="scheduled")


# ── /schedule ──────────────────────────────────────────────────────────────────

async def schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Admin only.")
        return
    chat_id      = REPORT_CHAT_ID or context.bot_data.get("override_chat_id") or next(iter(ADMIN_IDS), 0)
    filter_key   = context.bot_data.get("scheduled_filter", "trending")
    hour         = context.bot_data.get("scheduled_hour", REPORT_HOUR_EST)
    label, _     = FILTERS.get(filter_key, FILTERS["trending"])
    display_hour = f"{hour}:00 AM" if hour < 12 else ("12:00 PM" if hour == 12 else f"{hour-12}:00 PM")
    jobs         = context.job_queue.get_jobs_by_name("daily_report")
    alert_jobs   = context.job_queue.get_jobs_by_name("price_drop_alerts")
    job_line     = f"Next report: {jobs[0].next_t}" if jobs else "No report job active"
    alert_line   = f"Alert polls: active (every 3hrs)" if alert_jobs else "Alert polls: not running"
    await update.message.reply_text(
        f"⏰ *Schedule Status*\n\n"
        f"Daily report: *{display_hour} EST* — *{label}*\n"
        f"Destination: `{chat_id}`\n"
        f"{job_line}\n"
        f"{alert_line}\n\n"
        f"Use /setschedule to change.",
        parse_mode="Markdown"
    )


# ── /setchat ───────────────────────────────────────────────────────────────────

async def setchat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Admin only.")
        return
    chat_id    = update.effective_chat.id
    chat_title = update.effective_chat.title or "this chat"
    context.bot_data["override_chat_id"] = chat_id
    await update.message.reply_text(
        f"✅ *Report destination set!*\n\nChat: *{chat_title}*\nID: `{chat_id}`\n\n"
        f"⚠️ Make permanent: set `REPORT_CHAT_ID={chat_id}` in Railway.",
        parse_mode="Markdown"
    )


# ── /test ──────────────────────────────────────────────────────────────────────

async def test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Admin only.")
        return
    await update.message.reply_text("🔌 Testing Best Buy API connection...")
    try:
        ok, count, sample = await fetcher.test_connection()
        if ok:
            await update.message.reply_text(
                f"✅ *Best Buy API — Connected*\n\nReturned {count} products\nSample: _{sample}_",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(f"❌ API test failed: {count}")
    except Exception as e:
        await update.message.reply_text(f"❌ Exception: `{e}`", parse_mode="Markdown")


# ── /start ─────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_adm = is_admin(update)
    admin_block = (
        "\n\n*Admin commands:*\n"
        "/report — Pull on-demand report (category, price, brand filter)\n"
        "/setschedule — Set daily report time + filter\n"
        "/schedule — View current schedule + alert status\n"
        "/setchat — Set this chat as report destination\n"
        "/test — Quick API connectivity test"
    ) if is_adm else ""
    await update.message.reply_text(
        "👋 *iGamer Morning Market Report Bot*\n\n"
        "Daily Best Buy market intelligence — scored by deal freshness.\n\n"
        "*Daily 8am:* 🆕 Fresh Deals report (Excel)\n"
        "*Alerts:* 🔴 Price drop alerts sent throughout the day\n"
        "*On-demand:* /report → filter by category, price, brand\n\n"
        "*Offer badges:*\n"
        "🎯 Deal of the Day — 24hr BB editorial pick\n"
        "🟣 Clearance — extreme discount, limited units\n"
        "📰 Weekly Ad — in this week's BB circular\n"
        "⭐ Special Offer — bundle or promo"
        + admin_block,
        parse_mode="Markdown"
    )


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Daily 8am report
    app.job_queue.run_daily(
        scheduled_report,
        time=dtime(hour=REPORT_HOUR_EST, minute=0, tzinfo=EST),
        name="daily_report",
        data={"filter_key": "trending"}
    )
    logger.info(f"Daily report scheduled at {REPORT_HOUR_EST}:00 EST — Fresh Deals")

    # Price drop alert polling — every 3 hours
    app.job_queue.run_repeating(
        price_drop_alert_job,
        interval=3 * 60 * 60,   # 3 hours in seconds
        first=60,               # first run 60s after startup
        name="price_drop_alerts"
    )
    logger.info("Price drop alert polling started — every 3 hours")

    # /report as a ConversationHandler (3-step menu with timeout)
    report_conv = ConversationHandler(
        entry_points=[CommandHandler("report", report_cmd)],
        states={
            CAT_STEP:   [CallbackQueryHandler(report_cat_callback,   pattern="^rcat_")],
            PRICE_STEP: [CallbackQueryHandler(report_price_callback, pattern="^rprice_")],
            BRAND_STEP: [CallbackQueryHandler(report_brand_callback, pattern="^rbrand_")],
            TYPE_STEP:  [CallbackQueryHandler(report_type_callback,  pattern="^rtype_")],
        },
        fallbacks=[],
        conversation_timeout=CONV_TIMEOUT,
    )

    app.add_handler(report_conv)
    app.add_handler(CommandHandler("start",       start))
    app.add_handler(CommandHandler("schedule",    schedule_cmd))
    app.add_handler(CommandHandler("setschedule", setschedule_cmd))
    app.add_handler(CommandHandler("setchat",     setchat_cmd))
    app.add_handler(CommandHandler("test",        test_cmd))

    app.add_handler(CallbackQueryHandler(setschedule_filter_callback, pattern="^sch_"))
    app.add_handler(CallbackQueryHandler(setschedule_time_callback,   pattern="^schtime_"))
    app.add_handler(CallbackQueryHandler(similar_callback,            pattern="^similar_"))

    logger.info("iGamer Morning Report bot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
