"""Force-subscribe channel helpers.

The bot must be an administrator of the force-sub channel; otherwise Telegram
rejects get_chat_member / export_chat_invite_link calls. For private channels,
set FORCE_SUB_INVITE_LINK explicitly (recommended) or let the bot export one.
"""

from __future__ import annotations

import logging
from typing import Optional

from pyrogram import Client, enums
from pyrogram.errors import ChatAdminRequired, PeerIdInvalid, UserNotParticipant

from config import settings

logger = logging.getLogger(__name__)

_invite_link_cache: Optional[str] = None


async def get_force_sub_link(client: Client) -> Optional[str]:
    """Return a join URL for the force-sub channel, if one can be built."""
    if not settings.force_sub_channel:
        return None
    if settings.force_sub_invite_link:
        return settings.force_sub_invite_link

    channel = settings.force_sub_channel
    if isinstance(channel, str):
        # Public channel username.
        return f"https://t.me/{channel.lstrip('@')}"

    global _invite_link_cache
    if _invite_link_cache:
        return _invite_link_cache

    try:
        _invite_link_cache = await client.export_chat_invite_link(channel)
        return _invite_link_cache
    except Exception:
        logger.exception(
            "Could not export an invite link for FORCE_SUB_CHANNEL=%s. "
            "Set FORCE_SUB_INVITE_LINK in .env.",
            channel,
        )
        return None


async def is_user_joined(client: Client, user_id: int) -> bool:
    """Return True when the user is a member of the configured channel."""
    if not settings.force_sub_channel:
        return True

    try:
        member = await client.get_chat_member(settings.force_sub_channel, user_id)
    except UserNotParticipant:
        return False
    except (ChatAdminRequired, PeerIdInvalid):
        logger.exception(
            "Force-sub check failed. Add the bot as an administrator of "
            "FORCE_SUB_CHANNEL=%s.",
            settings.force_sub_channel,
        )
        return False
    except Exception:
        logger.exception("Unexpected error while checking force-sub membership")
        return False

    if member.status in (
        enums.ChatMemberStatus.OWNER,
        enums.ChatMemberStatus.ADMINISTRATOR,
        enums.ChatMemberStatus.MEMBER,
    ):
        return True
    if member.status == enums.ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "is_member", False))
    return False
