"""Resolve configured channels and verify the bot's admin rights."""

from __future__ import annotations

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
