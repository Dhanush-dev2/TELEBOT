"""Admin handlers: file uploads, /stats and /broadcast."""

from __future__ import annotations

import asyncio
import html
import logging

from pyrogram import Client, enums, filters
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message

import database
from config import settings
from utils.storage import store_message
from utils.telegram import copy_message, delete_messages, send_message

logger = logging.getLogger(__name__)

ADMIN_PRIVATE = filters.private & filters.user(list(settings.admin_ids))
UPLOAD_FILTER = ADMIN_PRIVATE & (filters.video | filters.document)

BROADCAST_DELAY_SECONDS = 0.05
BROADCAST_PROGRESS_EVERY = 25


async def handle_upload(client: Client, message: Message) -> None:
    """Copy an admin's video/document to storage and return shareable links."""
    try:
        await _store_file(client, message)
    except Exception:
        logger.exception("Unhandled error in upload handler")
        await send_message(
            client,
            message.chat.id,
            "❌ An unexpected error occurred while storing this file.",
        )


async def _store_file(client: Client, message: Message) -> None:
    status = await send_message(
        client, message.chat.id, "⏳ Copying file to the storage channel..."
    )

    stored = await store_message(
        client,
        message.chat.id,
        message.id,
        uploaded_by=message.from_user.id if message.from_user else None,
    )
    if stored is None:
        await send_message(
            client,
            message.chat.id,
            "❌ Could not copy the file to STORAGE_CHANNEL.\n\n"
            "Check that the bot is an administrator of the channel and that "
            "STORAGE_CHANNEL is the numeric -100... channel ID (or a public "
            "@username).",
        )
        if status is not None:
            await delete_messages(client, message.chat.id, [status.id])
        return

    text = (
        "✅ <b>File stored successfully</b>\n\n"
        f"🔗 <b>Direct link:</b>\n{html.escape(stored.direct_link)}\n\n"
    )
    if stored.short_link:
        text += f"✂️ <b>Short link:</b>\n{html.escape(stored.short_link)}\n\n"
    else:
        text += (
            "⚠️ Short link could not be generated. The direct link still works.\n\n"
        )
    text += (
        f"🆔 <b>ID:</b> <code>{stored.unique_id}</code>\n"
        f"💾 <b>Storage message:</b> <code>{stored.message_id}</code>"
    )

    await send_message(
        client,
        message.chat.id,
        text,
        disable_web_page_preview=True,
    )
    if status is not None:
        await delete_messages(client, message.chat.id, [status.id])

    logger.info(
        "Stored file %s (storage message %s) for admin %s",
        stored.unique_id,
        stored.message_id,
        message.from_user.id if message.from_user else "?",
    )


async def handle_stats(client: Client, message: Message) -> None:
    """Show file and user counts (admin only)."""
    try:
        files = await database.count_files()
        started_users = await database.count_started_users()
        known_users = await database.count_known_users()
        pending = await database.count_pending_deletions()

        text = (
            "📊 <b>Bot statistics</b>\n\n"
            f"🗂 <b>Files stored:</b> <code>{files}</code>\n"
            f"👤 <b>Users who used /start:</b> <code>{started_users}</code>\n"
            f"👥 <b>Known users:</b> <code>{known_users}</code>\n"
            f"⏳ <b>Pending deletions:</b> <code>{pending}</code>"
        )
        await send_message(
            client,
            message.chat.id,
            text,
            reply_to_message_id=message.id,
        )
    except Exception:
        logger.exception("Failed to build stats")
        await send_message(
            client, message.chat.id, "❌ Could not read statistics from the database."
        )


async def handle_broadcast(client: Client, message: Message) -> None:
    """Copy a replied-to message (or /broadcast text) to every known user."""
    try:
        target = message.reply_to_message
        raw_text = message.text or ""
        parts = raw_text.split(maxsplit=1)
        text_arg = parts[1].strip() if len(parts) > 1 else ""

        if target is None and not text_arg:
            await send_message(
                client,
                message.chat.id,
                "Usage:\n"
                "• Reply to any message with <code>/broadcast</code>\n"
                "• Or send <code>/broadcast your text</code>",
            )
            return

        user_ids = await database.get_all_user_ids()
        if not user_ids:
            await send_message(
                client, message.chat.id, "No known users to broadcast to yet."
            )
            return

        status = await send_message(
            client,
            message.chat.id,
            f"📣 Broadcasting to <b>{len(user_ids)}</b> user(s)...",
        )

        sent = 0
        failed = 0
        for index, user_id in enumerate(user_ids, start=1):
            if target is not None:
                result = await copy_message(
                    client, user_id, target.chat.id, target.id
                )
            else:
                # Admin-provided text may contain characters that look like
                # HTML; send it verbatim instead of letting the global HTML
                # parse mode reject it.
                result = await send_message(
                    client,
                    user_id,
                    text_arg,
                    parse_mode=enums.ParseMode.DISABLED,
                )

            if result is None:
                failed += 1
            else:
                sent += 1

            if index % BROADCAST_PROGRESS_EVERY == 0:
                logger.info(
                    "Broadcast progress: %s/%s (%s failed)",
                    index,
                    len(user_ids),
                    failed,
                )
            await asyncio.sleep(BROADCAST_DELAY_SECONDS)

        summary = (
            "📣 <b>Broadcast finished</b>\n\n"
            f"✅ Delivered: <code>{sent}</code>\n"
            f"❌ Failed: <code>{failed}</code>"
        )
        await send_message(client, message.chat.id, summary)
        if status is not None:
            await delete_messages(client, message.chat.id, [status.id])

        logger.info("Broadcast complete: %s sent, %s failed", sent, failed)
    except Exception:
        logger.exception("Broadcast failed")
        await send_message(
            client,
            message.chat.id,
            "❌ Broadcast failed. Check the bot logs for details.",
        )


def register(app: Client) -> None:
    """Register admin handlers. Called explicitly from bot.py."""
    app.add_handler(MessageHandler(handle_upload, UPLOAD_FILTER))
    app.add_handler(MessageHandler(handle_stats, ADMIN_PRIVATE & filters.command("stats")))
    app.add_handler(
        MessageHandler(handle_broadcast, ADMIN_PRIVATE & filters.command("broadcast"))
    )
