"""User handlers: /start, force-subscribe gate and auto-deleting delivery."""

from __future__ import annotations

import logging
import time
from typing import Optional

from pyrogram import Client, filters
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import database
from config import settings
from utils import force_sub, scheduler
from utils.telegram import copy_message, send_message

logger = logging.getLogger(__name__)

CHECK_SUB_PREFIX = "check_sub:"


def _format_duration(seconds: int) -> str:
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"
    return f"{seconds} second" if seconds == 1 else f"{seconds} seconds"


async def _send_join_prompt(
    client: Client, chat_id: int, unique_id: str
) -> None:
    link = await force_sub.get_force_sub_link(client)

    buttons: list[list[InlineKeyboardButton]] = []
    if link:
        buttons.append([InlineKeyboardButton("📢 Join Channel", url=link)])
    buttons.append(
        [
            InlineKeyboardButton(
                "🔄 Try Again",
                callback_data=f"{CHECK_SUB_PREFIX}{unique_id}",
            )
        ]
    )

    text = (
        "🔒 <b>Join required</b>\n\n"
        "To receive this file, join our channel first, then tap "
        "<b>Try Again</b>."
    )
    if not link:
        text += (
            "\n\n⚠️ The invite link could not be generated. Ask the admin to "
            "set FORCE_SUB_INVITE_LINK."
        )

    await send_message(
        client,
        chat_id,
        text,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _deliver_file(client: Client, chat_id: int, file_row: dict) -> bool:
    """Copy the stored file, warn the user, and schedule both for deletion."""
    sent = await copy_message(
        client,
        chat_id,
        settings.storage_channel,
        int(file_row["message_id"]),
    )
    if sent is None:
        await send_message(
            client,
            chat_id,
            "❌ This file is no longer available. Please ask the admin for a "
            "new link.",
        )
        return False

    duration = _format_duration(settings.auto_delete_seconds)
    warning = await send_message(
        client,
        chat_id,
        f"⚠️ This file will be auto-deleted in {duration}. Please save it now!",
    )

    delete_at = time.time() + settings.auto_delete_seconds
    try:
        await scheduler.schedule_deletion(
            chat_id=chat_id,
            file_message_id=sent.id,
            warning_message_id=warning.id if warning else None,
            delete_at=delete_at,
        )
    except Exception:
        logger.exception(
            "Failed to schedule auto-deletion for chat %s; file will remain",
            chat_id,
        )

    return True


async def handle_start(client: Client, message: Message) -> None:
    """Handle /start [deep_link_id]."""
    user = message.from_user
    if user is None:
        return

    try:
        await database.touch_user(user, started=True)

        payload: Optional[str] = (
            message.command[1] if len(message.command) > 1 else None
        )

        if not payload:
            if user.id in settings.admin_ids:
                text = (
                    "👋 <b>Admin ready.</b>\n\n"
                    "Send me a video or document and I will store it, then "
                    "give you a direct and shortened link."
                )
            else:
                text = (
                    "👋 <b>Welcome!</b>\n\n"
                    "Open the link you received from the admin to get your file."
                )
            await send_message(client, message.chat.id, text)
            return

        file_row = await database.get_file(payload)
        if file_row is None:
            await send_message(
                client,
                message.chat.id,
                "❌ This link is invalid or the file is no longer available.",
            )
            return

        if not await force_sub.is_user_joined(client, user.id):
            await _send_join_prompt(client, message.chat.id, payload)
            return

        await _deliver_file(client, message.chat.id, file_row)
    except Exception:
        logger.exception("Unhandled error in /start handler")
        await send_message(
            client,
            message.chat.id,
            "❌ Something went wrong while opening this file. Please try again.",
        )


async def handle_check_sub(client: Client, callback_query: CallbackQuery) -> None:
    """Handle the 'Try Again' button after the user joins the channel."""
    data = callback_query.data or ""
    unique_id = data[len(CHECK_SUB_PREFIX) :]
    user = callback_query.from_user

    try:
        if not await force_sub.is_user_joined(client, user.id):
            await callback_query.answer(
                "❌ You have not joined the channel yet.", show_alert=True
            )
            return

        file_row = await database.get_file(unique_id)
        if file_row is None:
            await callback_query.answer(
                "❌ This link is no longer valid.", show_alert=True
            )
            return

        await callback_query.answer("✅ Verified!")
        chat_id = (
            callback_query.message.chat.id
            if callback_query.message is not None
            else user.id
        )
        if callback_query.message is not None:
            try:
                await callback_query.message.delete()
            except Exception:
                logger.debug("Could not delete the force-sub prompt", exc_info=True)

        await _deliver_file(client, chat_id, file_row)
    except Exception:
        logger.exception("Unhandled error in force-sub callback")
        try:
            await callback_query.answer(
                "❌ Something went wrong. Please try again.", show_alert=True
            )
        except Exception:
            pass


async def log_interaction(client: Client, message: Message) -> None:
    """Record every user that talks to the bot (used by /stats and broadcast)."""
    try:
        if message.from_user and not message.from_user.is_bot:
            await database.touch_user(message.from_user)
    except Exception:
        logger.exception("Failed to log user interaction")


def _is_check_sub_filter(_, __, callback_query: CallbackQuery) -> bool:
    return bool(
        callback_query.data
        and callback_query.data.startswith(CHECK_SUB_PREFIX)
    )


CHECK_SUB_FILTER = filters.create(_is_check_sub_filter)


def register(app: Client) -> None:
    """Register user handlers. Called explicitly from bot.py."""
    # Group -1 runs first, so interactions are logged even when another handler
    # handles the same update. Returning None keeps propagation going.
    app.add_handler(MessageHandler(log_interaction, filters.private), group=-1)
    app.add_handler(
        MessageHandler(handle_start, filters.private & filters.command("start"))
    )
    # CallbackQuery has no .chat attribute; filter on callback_data instead.
    app.add_handler(
        CallbackQueryHandler(handle_check_sub, CHECK_SUB_FILTER)
    )
