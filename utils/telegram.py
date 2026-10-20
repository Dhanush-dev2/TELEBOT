"""Telegram API wrappers with FloodWait retry handling.

Bulk operations (broadcasts) and user deliveries must never crash the bot when
Telegram rate-limits us. Every wrapper returns None on permanent failure so the
caller can count failures and carry on.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable, Optional

from pyrogram import Client
from pyrogram.errors import (
    ChatWriteForbidden,
    FloodWait,
    InputUserDeactivated,
    MessageDeleteForbidden,
    PeerIdInvalid,
    UserIsBlocked,
)
from pyrogram.types import InlineKeyboardMarkup, Message

logger = logging.getLogger(__name__)

MAX_FLOOD_RETRIES = 3


async def _call_with_flood_retry(
    func: Any,
    *args: Any,
    max_retries: int = MAX_FLOOD_RETRIES,
    **kwargs: Any,
) -> Optional[Any]:
    """Call an async Telegram method, sleeping through FloodWait errors."""
    for attempt in range(1, max_retries + 1):
        try:
            return await func(*args, **kwargs)
        except FloodWait as exc:
            wait_seconds = int(exc.value) + 1
            logger.warning(
                "FloodWait on %s: sleeping %ss (attempt %s/%s)",
                getattr(func, "__name__", func),
                wait_seconds,
                attempt,
                max_retries,
            )
            await asyncio.sleep(wait_seconds)
        except (
            UserIsBlocked,
            InputUserDeactivated,
            PeerIdInvalid,
            ChatWriteForbidden,
            MessageDeleteForbidden,
        ) as exc:
            # Expected during broadcasts: the user blocked the bot or the
            # account no longer exists. No need to retry or log a traceback.
            logger.debug(
                "Recipient unavailable (%s): %s", type(exc).__name__, exc
            )
            return None
        except Exception:
            logger.exception(
                "Telegram call %s failed", getattr(func, "__name__", func)
            )
            return None

    logger.error(
        "Giving up on %s after %s FloodWait retries",
        getattr(func, "__name__", func),
        max_retries,
    )
    return None


async def send_message(
    client: Client,
    chat_id: int,
    text: str,
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    disable_web_page_preview: bool = True,
    **kwargs: Any,
) -> Optional[Message]:
    return await _call_with_flood_retry(
        client.send_message,
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        disable_web_page_preview=disable_web_page_preview,
        **kwargs,
    )


async def copy_message(
    client: Client,
    chat_id: int,
    from_chat_id: Any,
    message_id: int,
    **kwargs: Any,
) -> Optional[Message]:
    return await _call_with_flood_retry(
        client.copy_message,
        chat_id=chat_id,
        from_chat_id=from_chat_id,
        message_id=message_id,
        **kwargs,
    )


async def send_photo(
    client: Client,
    chat_id: int,
    photo: Any,
    *,
    caption: Optional[str] = None,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    **kwargs: Any,
) -> Optional[Message]:
    return await _call_with_flood_retry(
        client.send_photo,
        chat_id=chat_id,
        photo=photo,
        caption=caption,
        reply_markup=reply_markup,
        **kwargs,
    )


async def edit_message_reply_markup(
    client: Client,
    chat_id: int,
    message_id: int,
    reply_markup: InlineKeyboardMarkup,
    **kwargs: Any,
) -> Optional[Message]:
    return await _call_with_flood_retry(
        client.edit_message_reply_markup,
        chat_id=chat_id,
        message_id=message_id,
        reply_markup=reply_markup,
        **kwargs,
    )


async def delete_messages(
    client: Client,
    chat_id: int,
    message_ids: Iterable[int],
) -> bool:
    result = await _call_with_flood_retry(
        client.delete_messages,
        chat_id=chat_id,
        message_ids=list(message_ids),
    )
    return result is not None
