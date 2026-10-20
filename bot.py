"""Entry point for the Telegram file-sharing bot.

Startup order matters:
1. Validate configuration (config.py does this on import).
2. Start the keep-alive HTTP server so Render sees an open port.
3. Create the Turso/libSQL schema.
4. Start Pyrogram and verify access to the storage/force-sub channels.
5. Start APScheduler and re-register pending auto-delete jobs from Turso.
6. Register handlers and idle until stopped.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from pyrogram import Client, enums, idle

import database
from config import settings
from utils import pyrogram_compat  # noqa: F401 - must run before handlers
from handlers import admin, library, user
from utils import http_server, scheduler
from utils.channels import require_channel_admin, resolve_channel

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        stream=sys.stdout,
    )
    # Pyrogram is very chatty at INFO; APScheduler logs every restored job.
    logging.getLogger("pyrogram").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


async def _validate_channels(app: Client) -> None:
    storage_chat = await resolve_channel(
        app, settings.storage_channel, "STORAGE_CHANNEL"
    )
    await require_channel_admin(
        app,
        storage_chat,
        settings.storage_channel,
        "STORAGE_CHANNEL",
        require_post_permission=True,
    )
    logger.info(
        "Storage channel: %s (id=%s)",
        storage_chat.title or storage_chat.username,
        storage_chat.id,
    )

    if settings.force_sub_channel:
        force_chat = await resolve_channel(
            app, settings.force_sub_channel, "FORCE_SUB_CHANNEL"
        )
        await require_channel_admin(
            app,
            force_chat,
            settings.force_sub_channel,
            "FORCE_SUB_CHANNEL",
        )
        logger.info(
            "Force-sub channel: %s (id=%s)",
            force_chat.title or force_chat.username,
            force_chat.id,
        )


async def main() -> None:
    _configure_logging()
    logger.info("Starting Telegram file-sharing bot")
    logger.info("Data directory: %s", settings.data_dir)

    # Start Render's health endpoint before the slower Telegram/Turso startup
    # so the platform sees an open port immediately.
    http_runner = await http_server.start_server(settings.port)
    app: Client | None = None

    try:
        await database.init_db()

        app = Client(
            name=settings.session_name,
            api_id=settings.api_id,
            api_hash=settings.api_hash,
            bot_token=settings.bot_token,
            workdir=str(settings.data_dir),
            parse_mode=enums.ParseMode.HTML,
        )

        await app.start()
        me = await app.get_me()
        if not me.username:
            raise RuntimeError(
                "The bot has no username. Create one with @BotFather and set "
                "BOT_USERNAME in .env."
            )
        if me.username.lower() != settings.bot_username.lower():
            logger.warning(
                "BOT_USERNAME=%s but Telegram reports @%s. Deep links will use "
                "the configured value; update .env if it is wrong.",
                settings.bot_username,
                me.username,
            )
        logger.info("Logged in as @%s (id=%s)", me.username, me.id)

        await _validate_channels(app)

        scheduler.init_scheduler(app)
        await scheduler.reload_pending_jobs()

        user.register(app)
        admin.register(app)
        library.register(app)

        logger.info("Bot is running. Press Ctrl+C to stop.")
        await idle()
    finally:
        scheduler.shutdown_scheduler()
        if app is not None and app.is_connected:
            await app.stop()
        await http_server.stop_server(http_runner)
        await database.close_db()
        logger.info("Bot stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except SystemExit:
        raise
    except Exception:
        logging.getLogger(__name__).exception("Fatal error during startup")
        sys.exit(1)
