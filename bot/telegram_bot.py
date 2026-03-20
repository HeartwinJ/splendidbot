import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import db
import scraper
from config import MESSAGE_SEND_DELAY, ONSINCH_BASE_URL

logger = logging.getLogger(__name__)

UK_TZ = pytz.timezone("Europe/London")
POSITIONS_URL = f"{ONSINCH_BASE_URL}/react/position?ignoreCapacity=false"

_SPECIAL_CHARS = r"\_*[]()~`>#+-=|{}.!"


def md_escape(text: str) -> str:
    return re.sub(r"([" + re.escape(_SPECIAL_CHARS) + r"])", r"\\\1", text)


async def _keep_typing(bot, chat_id: int) -> None:
    try:
        while True:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


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


def _group_positions_by_shift(positions: list[dict]) -> list[list[dict]]:
    from collections import OrderedDict
    grouped: OrderedDict[int, list[dict]] = OrderedDict()
    for pos in positions:
        sid = pos.get("shift_id", pos["id"])
        grouped.setdefault(sid, []).append(pos)
    return list(grouped.values())


def format_shift_message(shift_positions: list[dict]) -> str:
    first = shift_positions[0]
    lines: list[str] = []

    has_conflict = any(p["in_conflict"] for p in shift_positions)
    has_req_failed = any(p.get("requirements_failed") for p in shift_positions)

    if first["featured"]:
        lines.append(f"⭐ *{md_escape(first['shift_name'])}*")
    else:
        lines.append(f"📋 *{md_escape(first['shift_name'])}*")

    if first.get("organizer"):
        lines.append(f"🏢 {md_escape(first['organizer'])}")

    try:
        dt_str = _format_dt_range(first["start_time"], first["end_time"])
        lines.append(f"📅 {md_escape(dt_str)}")
    except Exception as exc:
        logger.warning("Failed to format datetime for shift %s: %s", first.get("shift_id"), exc)
        lines.append(f"📅 {md_escape(first['start_time'])}")

    lines.append(f"📍 {md_escape(first['location'])}")

    lines.append("")
    lines.append("*Positions:*")

    for pos in shift_positions:
        spots = pos["free_capacity"]
        spot_word = "spot" if spots == 1 else "spots"

        role_label = ""
        if pos["title"]:
            role_label = f" — {md_escape(pos['title'])}"
        if pos["role"] == 2:
            role_label += " 🎖️ TL"

        spot_str = f"{spots} {spot_word}" if spots > 0 else "0 spots \\(full\\)"

        tags = ""
        if pos.get("requirements_failed"):
            tags += " 🚫"
        if pos["in_conflict"]:
            tags += " ⚠️"

        lines.append(
            f"  • {md_escape(pos['profession'])}{role_label} "
            f"— {spot_str}{tags}"
        )

    if has_req_failed or has_conflict:
        lines.append("")
        if has_req_failed:
            lines.append("🚫 Requirements not met")
        if has_conflict:
            lines.append("⚠️ Conflicts with existing shift")

    lines.append("")
    lines.append(f"🔗 [View on OnSinch]({POSITIONS_URL})")

    return "\n".join(lines)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    try:
        user = await db.get_user(chat_id)
    except Exception as exc:
        logger.error("cmd_start db error for chat_id=%s: %s", chat_id, exc)
        await update.message.reply_text("❌ Something went wrong. Please try again.")
        return

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


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*Available commands*\n\n"
        "/start — Register or re\\-authenticate with your OnSinch credentials\n"
        "/stop — Pause shift notifications\n"
        "/check — Check for available shifts right now\n"
        "/status — Show your account status and tracking stats\n"
        "/settings — Configure shift filter preferences\n"
        "/help — Show this message",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    try:
        user = await db.get_user(chat_id)
        if not user:
            await update.message.reply_text("You're not registered yet. Use /start to set up.")
            return
        await db.set_user_status(chat_id, "inactive")
        await update.message.reply_text("Notifications paused. Send /start to resume.")
    except Exception as exc:
        logger.error("cmd_stop error for chat_id=%s: %s", chat_id, exc)
        await update.message.reply_text("❌ Something went wrong. Please try again.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    try:
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
            f"{status_emoji} Status: *{md_escape(status)}*\n"
            f"Account: `{md_escape(email)}`\n"
            f"Last updated: {md_escape(updated)}\n"
            f"Tracked listings: {len(seen_ids)}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as exc:
        logger.error("cmd_status error for chat_id=%s: %s", chat_id, exc)
        await update.message.reply_text("❌ Something went wrong. Please try again.")


FILTER_LABELS = {
    "show_standby": "Standby shifts",
    "show_at_capacity": "Full (0 spots) shifts",
    "show_in_conflict": "Conflicting shifts",
}


def _settings_keyboard(filters: dict) -> InlineKeyboardMarkup:
    buttons = []
    for col, label in FILTER_LABELS.items():
        enabled = filters.get(col, False)
        icon = "✅" if enabled else "❌"
        buttons.append(
            [InlineKeyboardButton(f"{icon} {label}", callback_data=f"toggle:{col}")]
        )
    return InlineKeyboardMarkup(buttons)


def _settings_text(filters: dict) -> str:
    return (
        "*Filter settings*\n\n"
        "Choose which shifts to include in notifications\\.\n"
        "Tap a button to toggle it on/off\\."
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    try:
        user = await db.get_user(chat_id)
    except Exception as exc:
        logger.error("cmd_settings db error for chat_id=%s: %s", chat_id, exc)
        await update.message.reply_text("❌ Something went wrong. Please try again.")
        return

    if not user:
        await update.message.reply_text("You're not registered yet. Use /start to set up.")
        return

    filters = await db.get_user_filters(chat_id)
    await update.message.reply_text(
        _settings_text(filters),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_settings_keyboard(filters),
    )


async def handle_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data or not data.startswith("toggle:"):
        return

    column = data.split(":", 1)[1]
    if column not in FILTER_LABELS:
        return

    chat_id = query.message.chat_id
    try:
        await db.toggle_user_filter(chat_id, column)
        filters = await db.get_user_filters(chat_id)
        await query.edit_message_text(
            _settings_text(filters),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_settings_keyboard(filters),
        )
    except Exception as exc:
        logger.error("handle_settings_callback error for chat_id=%s: %s", chat_id, exc)


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    try:
        user = await db.get_user(chat_id)
    except Exception as exc:
        logger.error("cmd_check db error for chat_id=%s: %s", chat_id, exc)
        await update.message.reply_text("❌ Something went wrong. Please try again.")
        return

    if not user:
        await update.message.reply_text("You're not registered yet. Use /start to set up.")
        return

    logger.info("Manual check triggered by chat_id=%s", chat_id)
    await update.message.reply_text("🔍 Checking for available shifts…")

    user_filters = await db.get_user_filters(chat_id)

    _typing = asyncio.create_task(_keep_typing(context.bot, chat_id))
    try:
        positions = await scraper.scrape_positions(
            chat_id, user["email"], user["password"], user_filters
        )
    except Exception as exc:
        logger.error("Manual check failed for chat_id=%s: %s", chat_id, exc)
        await update.message.reply_text(
            f"❌ Failed to check shifts: {exc}\n\n"
            "Your session may have expired — use /start to re-authenticate."
        )
        return
    finally:
        _typing.cancel()

    try:
        seen_ids = await db.get_seen_listing_ids(chat_id)
    except Exception as exc:
        logger.error("cmd_check seen_ids error for chat_id=%s: %s", chat_id, exc)
        await update.message.reply_text("❌ Something went wrong. Please try again.")
        return

    new_positions = [p for p in positions if str(p["id"]) not in seen_ids]

    if not new_positions:
        await update.message.reply_text("No new shifts found right now.")
        return

    shift_groups = _group_positions_by_shift(new_positions)
    await update.message.reply_text(
        f"Found *{len(shift_groups)}* new shift(s) with *{len(new_positions)}* position(s):",
        parse_mode=ParseMode.MARKDOWN,
    )
    for group in shift_groups:
        try:
            await update.message.reply_text(
                format_shift_message(group),
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.error("Failed to send shift %s: %s", group[0].get("shift_id"), exc)
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
        chat_id=chat_id, text="🔐 Validating your credentials…"
    )

    _typing = asyncio.create_task(_keep_typing(context.bot, chat_id))
    try:
        await scraper.login_and_validate(email, password, chat_id)
    except Exception as exc:
        logger.warning("Login failed for chat_id=%s: %s", chat_id, exc)
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=validating_msg.message_id,
            text="❌ Login failed. Please check your credentials and try again. Send them as email:password",
        )
        return
    finally:
        _typing.cancel()

    try:
        await db.upsert_user(chat_id, email, password)
    except Exception as exc:
        logger.error("Failed to save user for chat_id=%s: %s", chat_id, exc)
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=validating_msg.message_id,
            text="❌ Failed to save your credentials. Please try again.",
        )
        return

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
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(handle_settings_callback, pattern=r"^toggle:"))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_credentials)
    )

    return app


async def send_new_shift_notification(bot, chat_id: int, shift_positions: list[dict]) -> None:
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=format_shift_message(shift_positions),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.error(
            "Failed to send shift notification for shift %s to chat_id %s: %s",
            shift_positions[0].get("shift_id"),
            chat_id,
            exc,
        )
