"""
main.py — Entry point: starts the Telegram bot and background APScheduler.
"""

import asyncio
import logging
import signal
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.ext import Application

import db
import scraper
import telegram_bot as tgbot
from config import MESSAGE_SEND_DELAY, SCRAPE_INTERVAL_MINUTES, TELEGRAM_BOT_TOKEN

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scrape cycle
# ---------------------------------------------------------------------------

async def run_scrape_cycle(app: Application, silent: bool = False) -> None:
    """
    Fetch positions for all active users, compare against seen_listings,
    and send notifications for new ones.

    When `silent=True` (first run on startup), populate seen_listings without
    sending notifications.
    """
    users = await db.get_active_users()
    logger.info("Scrape cycle starting — %d active user(s), silent=%s", len(users), silent)

    for user in users:
        chat_id: int = user["chat_id"]
        email: str = user["email"]
        password: str = user["password"]

        try:
            positions = await scraper.scrape_positions(chat_id, email, password)
        except RuntimeError as exc:
            logger.error("Auth failed for chat_id=%s: %s", chat_id, exc)
            await db.set_user_status(chat_id, "auth_failed")
            if not silent:
                try:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "⚠️ I can't log into your OnSinch account. "
                            "Your password may have changed. "
                            "Send /start to set up again."
                        ),
                    )
                except Exception as send_exc:
                    logger.error(
                        "Failed to send auth-failure notice to %s: %s", chat_id, send_exc
                    )
            continue
        except Exception as exc:
            logger.error("Unexpected scrape error for chat_id=%s: %s", chat_id, exc)
            continue

        seen_ids = await db.get_seen_listing_ids(chat_id)
        new_positions = [p for p in positions if str(p["id"]) not in seen_ids]

        if new_positions:
            all_new_ids = [str(p["id"]) for p in new_positions]
            await db.mark_listings_seen(chat_id, all_new_ids)
            logger.info(
                "%d new position(s) for chat_id=%s — silent=%s",
                len(new_positions),
                chat_id,
                silent,
            )

            if not silent:
                for pos in new_positions:
                    await tgbot.send_new_shift_notification(app.bot, chat_id, pos)
                    await asyncio.sleep(MESSAGE_SEND_DELAY)
        else:
            logger.debug("No new positions for chat_id=%s", chat_id)

        # Also mark all currently-visible positions as seen (not just new ones),
        # so we don't repeatedly try to "notify" about positions we've already
        # skipped due to filtering changes.  Only store IDs we actually processed.
        all_visible_ids = [str(p["id"]) for p in positions]
        await db.mark_listings_seen(chat_id, all_visible_ids)

    logger.info("Scrape cycle complete")


async def scheduled_scrape(app: Application) -> None:
    await run_scrape_cycle(app, silent=False)


async def cleanup_job() -> None:
    await db.cleanup_old_seen_listings(days=30)


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

async def main() -> None:
    # Init DB
    await db.init_db()

    # Build Telegram application
    app: Application = tgbot.build_application(TELEGRAM_BOT_TOKEN)

    # APScheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scheduled_scrape,
        "interval",
        minutes=SCRAPE_INTERVAL_MINUTES,
        args=[app],
        id="scrape_cycle",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        cleanup_job,
        "interval",
        hours=24,
        id="cleanup",
    )

    # Graceful shutdown handler
    shutdown_event = asyncio.Event()

    def _handle_signal(signum, frame):  # noqa: ANN001
        logger.info("Received signal %s — shutting down", signum)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Start scheduler and bot
    scheduler.start()
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("Bot started — polling for updates")

    # Silent first run: populate seen_listings so we don't flood users on restart
    logger.info("Running initial silent scrape to populate seen_listings")
    await run_scrape_cycle(app, silent=True)

    # Wait until shutdown signal
    await shutdown_event.wait()

    # Cleanup
    logger.info("Shutting down...")
    scheduler.shutdown(wait=False)
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
    await db.close_pool()
    logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
