"""Resolve configured channels and verify the bot's admin rights."""

from __future__ import annotations

import logging
from collections.abc import Mapping

from pyrogram import Client, enums
from pyrogram.errors import (
    ChannelInvalid,
    ChannelPrivate,
    ChatAdminRequired,
    ChatForbidden,
    ChatIdInvalid,
    ChatInvalid,
    PeerIdInvalid,
    UsernameInvalid,
    UsernameNotOccupied,
    UserNotParticipant,
)
from pyrogram.types import Chat

from config import ChatRef
from utils import pyrogram_compat  # noqa: F401 - widen peer-ID bounds first

logger = logging.getLogger(__name__)

_CHANNEL_LOOKUP_ERRORS = (
    PeerIdInvalid,
    ChannelInvalid,
    ChannelPrivate,
    ChatInvalid,
    ChatIdInvalid,
    ChatForbidden,
    UsernameInvalid,
    UsernameNotOccupied,
)


async def resolve_channel(
    app: Client, chat_ref: ChatRef, env_name: str
) -> Chat:
    """Resolve a configured channel, distinguishing bad IDs from other errors."""
    try:
        chat = await app.get_chat(chat_ref)
    except _CHANNEL_LOOKUP_ERRORS as exc:
        raise RuntimeError(
            f"Cannot resolve {env_name}={chat_ref!r}. Check that it is the "
            "channel's numeric -100... ID or public @username (not an invite "
            "link or channel title) and that the bot was added to it."
        ) from exc
    except Exception as exc:
        raise RuntimeError(
            f"Could not validate {env_name}={chat_ref!r}: {exc}"
        ) from exc

    if chat.type not in (enums.ChatType.CHANNEL, enums.ChatType.SUPERGROUP):
        chat_type = getattr(chat.type, "value", str(chat.type))
        raise RuntimeError(
            f"{env_name}={chat_ref!r} refers to a {chat_type}, not a "
            "channel or supergroup."
        )

    return chat


async def require_channel_admin(
    app: Client,
    chat: Chat,
    chat_ref: ChatRef,
    env_name: str,
    *,
    require_post_permission: bool = False,
) -> None:
    """Verify the bot is an admin, instead of treating every lookup failure as one."""
    try:
        member = await app.get_chat_member(chat.id, "me")
    except (ChatAdminRequired, UserNotParticipant) as exc:
        raise RuntimeError(
            f"The bot is not an administrator of {env_name}={chat_ref!r}. "
            "Add it as an administrator of that channel."
        ) from exc
    except _CHANNEL_LOOKUP_ERRORS as exc:
        raise RuntimeError(
            f"Cannot access {env_name}={chat_ref!r}. The channel ID may be "
            "wrong, or the bot may have been removed from the channel."
        ) from exc
    except Exception as exc:
        raise RuntimeError(
            f"Could not check administrator status for "
            f"{env_name}={chat_ref!r}: {exc}"
        ) from exc

    if member.status not in (
        enums.ChatMemberStatus.OWNER,
        enums.ChatMemberStatus.ADMINISTRATOR,
    ):
        status = getattr(member.status, "value", str(member.status))
        raise RuntimeError(
            f"The bot is not an administrator of {env_name}={chat_ref!r} "
            f"(current status: {status}). Add it as an administrator."
        )

    if require_post_permission and member.status != enums.ChatMemberStatus.OWNER:
        privileges = getattr(member, "privileges", None)
        if privileges is not None and not privileges.can_post_messages:
            raise RuntimeError(
                f"The bot is an administrator of {env_name}={chat_ref!r} but "
                "does not have permission to post messages. Enable the "
                "'Post Messages' admin permission for the bot."
            )


async def resolve_library_category(
    app: Client,
    category: str,
    channels: Mapping[str, ChatRef],
) -> Chat:
    """Resolve one configured library category and verify posting rights."""
    chat_ref = channels.get(category)
    if chat_ref is None:
        raise RuntimeError(
            f"Library category {category!r} is not configured in "
            "LIBRARY_CHANNELS."
        )

    env_name = f"LIBRARY_CHANNELS[{category}]"
    chat = await resolve_channel(app, chat_ref, env_name)
    await require_channel_admin(
        app,
        chat,
        chat_ref,
        env_name,
        require_post_permission=True,
    )
    return chat


async def validate_library_channels(
    app: Client,
    channels: Mapping[str, ChatRef],
) -> dict[str, Chat]:
    """Validate every category independently and log per-category failures.

    A bad category must not prevent the remaining categories (or the rest of
    the bot) from starting. The handler re-resolves the selected category when
    it is used, so a channel that disappears later still produces a clear
    category-specific error for the admin.
    """
    if not channels:
        logger.warning(
            "LIBRARY_CHANNELS is not configured; /newseries, /addepisode, "
            "/deletepost, /newpost and /editpost category selection are "
            "unavailable"
        )
        return {}

    resolved: dict[str, Chat] = {}
    for category in channels:
        try:
            chat = await resolve_library_category(app, category, channels)
        except RuntimeError as exc:
            logger.error(
                "Library category %r is unavailable: %s", category, exc
            )
        except Exception:
            logger.exception(
                "Unexpected error validating library category %r", category
            )
        else:
            resolved[category] = chat
            logger.info(
                "Library category %r: %s (id=%s)",
                category,
                chat.title or chat.username or str(chat.id),
                chat.id,
            )

    return resolved
