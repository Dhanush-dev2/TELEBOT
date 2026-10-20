"""Copy uploads into STORAGE_CHANNEL and build shareable deep links."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from pyrogram import Client

import database
from config import settings
from utils.encoding import encode_base62
from utils.shortener import shorten_url
from utils.telegram import copy_message

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredFile:
    unique_id: str
    message_id: int
    file_type: str
    file_name: str
    direct_link: str
    short_link: Optional[str]

    @property
    def button_url(self) -> str:
        return self.short_link or self.direct_link


async def store_message(
    client: Client,
    chat_id: int,
    message_id: int,
    *,
    uploaded_by: Optional[int] = None,
) -> Optional[StoredFile]:
    """Copy a message to storage, persist it, and return its shareable links."""
    copied = await copy_message(
        client,
        settings.storage_channel,
        chat_id,
        message_id,
    )
    if copied is None:
        return None

    if copied.video:
        file_type = "video"
        media = copied.video
    elif copied.document:
        file_type = "document"
        media = copied.document
    else:
        raise RuntimeError("The copied message is not a video or document")

    unique_id = encode_base62(copied.id)
    file_name = getattr(media, "file_name", None) or f"{file_type}_{copied.id}"
    await database.add_file(
        unique_id=unique_id,
        message_id=copied.id,
        file_type=file_type,
        file_name=file_name,
        uploaded_by=uploaded_by,
    )

    direct_link = f"https://t.me/{settings.bot_username}?start={unique_id}"
    try:
        short_link = await shorten_url(direct_link)
    except Exception:
        # The file is already stored; never lose the direct link because the
        # third-party shortener misbehaved.
        logger.exception("Unexpected shortener error")
        short_link = None

    return StoredFile(
        unique_id=unique_id,
        message_id=copied.id,
        file_type=file_type,
        file_name=file_name,
        direct_link=direct_link,
        short_link=short_link,
    )
