import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import pytz
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import db
import scraper
from config import MESSAGE_SEND_DELAY, ONSINCH_BASE_URL

logger = logging.getLogger(__name__)

UK_TZ = pytz.timezone("Europe/London")
POSITIONS_URL = f"{ONSINCH_BASE_URL}/react/position?ignoreCapacity=false"

_SPECIAL_CHARS = r"\_*[]()~`>#+-=|{}.!"


def md_escape(text: str) -> str:
    return re.sub(r"([" + re.escape(_SPECIAL_CHARS) + r"])", r"\\\1", text)


def _format_dt_range(start_iso: str, end_iso: str) -> str:
    start_utc = datetime.fromisoformat(start_iso).replace(tzinfo=timezone.utc)
    end_utc = datetime.fromisoformat(end_iso).replace(tzinfo=timezone.utc)
    start_uk = start_utc.astimezone(UK_TZ)
    end_uk = end_utc.astimezone(UK_TZ)
    date_str = start_uk.strftime("%-d %b")
    day_str = start_uk.strftime("%a")
    start_t = start_uk.strftime("%H:%M")
    end_t = end_uk.strftime("%H:%M")
    return f"{day_str} {date_str}, {start_t} – {end_t}"


def format_position_message(pos: dict) -> str:
    lines: list[str] = []

    if pos["featured"] and not pos["in_conflict"]:
        lines.append(f"⭐ *Featured: {md_escape(pos['shift_name'])}*")
    elif pos["in_conflict"]:
        lines.append("⚠️ *CONFLICTS WITH EXISTING SHIFT*\n")
        lines.append(f"📋 *{md_escape(pos['shift_name'])}*")
    else:
        lines.append(f"📋 *{md_escape(pos['shift_name'])}*")

    lines.append("")

    profession_str = md_escape(pos["profession"])
    if pos["title"]:
        profession_str += f" — {md_escape(pos['title'])}"
    if pos["role"] == 2:
        profession_str += " — 🎖️ Team Leader"
    lines.append(f"👤 {profession_str}")

    try:
        dt_str = _format_dt_range(pos["start_time"], pos["end_time"])
        lines.append(f"📅 {md_escape(dt_str)}")
    except Exception:
        lines.append(f"📅 {md_escape(pos['start_time'])}")

    lines.append(f"📍 {md_escape(pos['location'])}")

    spots = pos["free_capacity"]
    spot_word = "spot" if spots == 1 else "spots"
    if spots == 0:
        lines.append(f"👥 0 spots \\(full\\)")
    else:
        lines.append(f"👥 {md_escape(str(spots))} {spot_word} available")

    if pos.get("requirements_failed"):
        lines.append("")
        lines.append("⚠️ You may not meet the requirements for this position")

    lines.append("")
    lines.append(f"🔗 [View on OnSinch]({POSITIONS_URL})")

    return "\n".join(lines)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = await db.get_user(chat_id)

    if user and user["status"] == "active":
        await update.message.reply_text(
            "You're already registered and receiving notifications.\n\n"
            "Send your credentials again as `email:password` to update them, "
            "or use /stop to pause notifications.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await update.message.reply_text(
        "👋 Welcome to the OnSinch Shift Notifier\\!\n\n"
        "I'll check Splendid's OnSinch platform every 15 minutes and notify you "
        "whenever a new shift becomes available\\.\n\n"
        "To get started, send me your OnSinch credentials in this format:\n"
        "`email:password`\n\n"
        "Your message will be deleted immediately after I read it\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = await db.get_user(chat_id)
    if not user:
        await update.message.reply_text("You're not registered yet. Use /start to set up.")
        return
    await db.set_user_status(chat_id, "inactive")
    await update.message.reply_text("Notifications paused. Send /start to resume.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = await db.get_user(chat_id)
    if not user:
        await update.message.reply_text("You're not registered yet. Use /start to set up.")
        return

    seen_ids = await db.get_seen_listing_ids(chat_id)
    status = user["status"]
    email = user["email"]
    updated = user["updated_at"].strftime("%Y-%m-%d %H:%M UTC") if user["updated_at"] else "—"
    status_emoji = {"active": "✅", "inactive": "⏸", "auth_failed": "❌"}.get(status, "❓")

    await update.message.reply_text(
        f"{status_emoji} Status: *{status}*\n"
        f"Account: `{email}`\n"
        f"Last updated: {updated}\n"
        f"Tracked listings: {len(seen_ids)}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = await db.get_user(chat_id)
    if not user:
        await update.message.reply_text("You're not registered yet. Use /start to set up.")
        return

    await update.message.reply_text("🔍 Checking for available shifts...")

    try:
        positions = await scraper.scrape_positions(
            chat_id, user["email"], user["password"]
        )
    except RuntimeError as exc:
        await update.message.reply_text(
            f"❌ Failed to check shifts: {exc}\n\n"
            "Your session may have expired — use /start to re-authenticate."
        )
        return

    if not positions:
        await update.message.reply_text("No available shifts found right now.")
        return

    await update.message.reply_text(
        f"Found *{len(positions)}* available shift(s):",
        parse_mode=ParseMode.MARKDOWN,
    )
    for pos in positions:
        try:
            await update.message.reply_text(
                format_position_message(pos),
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.error("Failed to send position %s: %s", pos["id"], exc)
        await asyncio.sleep(MESSAGE_SEND_DELAY)


async def handle_credentials(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    message = update.message
    text = (message.text or "").strip()

    if ":" not in text:
        return

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message.message_id)
    except Exception as exc:
        logger.warning("Could not delete credential message: %s", exc)

    email, _, password = text.partition(":")
    email = email.strip()
    password = password.strip()

    if not email or not password or "@" not in email:
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ Invalid format. Please send your credentials as `email:password`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    validating_msg = await context.bot.send_message(
        chat_id=chat_id, text="🔐 Validating your credentials..."
    )

    try:
        await scraper.login_and_validate(email, password, chat_id)
    except Exception as exc:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=validating_msg.message_id,
            text="❌ Login failed. Please check your credentials and try again. Send them as email:password",
        )
        logger.warning("Login failed for chat_id=%s: %s", chat_id, exc)
        return

    await db.upsert_user(chat_id, email, password)
    logger.info("User registered/updated: chat_id=%s email=%s", chat_id, email)

    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=validating_msg.message_id,
        text="✅ You're all set! I'll check for new shifts every 15 minutes.",
    )


def build_application(token: str) -> Application:
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_credentials)
    )

    return app


async def send_new_shift_notification(bot, chat_id: int, pos: dict) -> None:
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=format_position_message(pos),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.error(
            "Failed to send shift notification for position %s to chat_id %s: %s",
            pos["id"],
            chat_id,
            exc,
        )
