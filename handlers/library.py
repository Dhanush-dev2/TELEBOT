"""Admin library-post workflow: /newpost and /editpost."""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from typing import Optional

from pyrogram import Client, enums, filters
from pyrogram.handlers import MessageHandler
from pyrogram.types import (
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import database
from config import settings
from utils.channels import require_channel_admin, resolve_channel
from utils.storage import StoredFile, store_message
from utils.telegram import (
    delete_messages,
    edit_message_reply_markup,
    send_message,
    send_photo,
)

logger = logging.getLogger(__name__)

ADMIN_PRIVATE = filters.private & filters.user(list(settings.admin_ids))
MAX_CAPTION_LENGTH = 1024
MAX_LABEL_LENGTH = 64


@dataclass
class QualityDraft:
    label: str
    chat_id: int
    message_id: int
    file_type: str


@dataclass
class NewPostDraft:
    links_channel_id: int
    library_channel_id: int
    stage: str = "thumbnail"
    thumbnail_file_id: Optional[str] = None
    title: Optional[str] = None
    qualities: dict[str, QualityDraft] = field(default_factory=dict)


@dataclass
class EditPostDraft:
    post: dict


_newpost_sessions: dict[int, NewPostDraft] = {}
_editpost_sessions: dict[int, EditPostDraft] = {}


def _command_name(message: Message) -> Optional[str]:
    if not message.text or not message.text.startswith("/"):
        return None
    command = message.text.split(maxsplit=1)[0][1:]
    return command.split("@", 1)[0].lower()


def _has_session(_, __, message: Message) -> bool:
    if message.from_user is None:
        return False
    user_id = message.from_user.id
    return user_id in _newpost_sessions or user_id in _editpost_sessions


SESSION_FILTER = filters.create(_has_session)


def _quality_keyboard(buttons: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"🔗 {label}", url=url)]
            for label, url in buttons
        ]
    )


async def _delete_status(
    client: Client, chat_id: int, status: Optional[Message]
) -> None:
    if status is not None:
        await delete_messages(client, chat_id, [status.id])


async def _ensure_library_channels(
    client: Client, chat_id: int
) -> Optional[tuple[Chat, Chat]]:
    missing = []
    if not settings.links_channel:
        missing.append("LINKS_CHANNEL")
    if not settings.library_channel:
        missing.append("LIBRARY_CHANNEL")
    if missing:
        await send_message(
            client,
            chat_id,
            "❌ <b>Library posting is not configured.</b>\n\n"
            f"Missing: <code>{', '.join(missing)}</code>\n\n"
            "Set both <code>LINKS_CHANNEL</code> and "
            "<code>LIBRARY_CHANNEL</code> in <code>.env</code>, then restart "
            "the bot.",
        )
        return None

    try:
        links_chat = await resolve_channel(
            client, settings.links_channel, "LINKS_CHANNEL"
        )
        library_chat = await resolve_channel(
            client, settings.library_channel, "LIBRARY_CHANNEL"
        )
        await require_channel_admin(
            client,
            links_chat,
            settings.links_channel,
            "LINKS_CHANNEL",
            require_post_permission=True,
        )
        await require_channel_admin(
            client,
            library_chat,
            settings.library_channel,
            "LIBRARY_CHANNEL",
            require_post_permission=True,
        )
    except RuntimeError as exc:
        await send_message(client, chat_id, f"❌ {html.escape(str(exc))}")
        return None

    return links_chat, library_chat


async def handle_newpost(client: Client, message: Message) -> None:
    """Start the guided /newpost flow."""
    if message.from_user is None:
        return

    channels = await _ensure_library_channels(client, message.chat.id)
    if channels is None:
        return
    links_chat, library_chat = channels

    _editpost_sessions.pop(message.from_user.id, None)
    _newpost_sessions[message.from_user.id] = NewPostDraft(
        links_channel_id=links_chat.id,
        library_channel_id=library_chat.id,
    )

    await send_message(
        client,
        message.chat.id,
        "🎬 <b>New library post</b>\n\n"
        "<b>Step 1/3:</b> Send the thumbnail image (as a photo).\n\n"
        "Use /cancel at any time to abort.",
    )


async def handle_editpost(client: Client, message: Message) -> None:
    """Start an /editpost <post_id> session for replacing one quality file."""
    if message.from_user is None:
        return

    args = message.command[1:] if message.command else []
    if not args:
        await send_message(
            client,
            message.chat.id,
            "Usage: <code>/editpost &lt;post_id&gt;</code>\n\n"
            "The post ID is shown after a successful /newpost.",
        )
        return

    try:
        post_id = int(args[0])
    except ValueError:
        await send_message(
            client, message.chat.id, "❌ The post ID must be a number."
        )
        return

    if await _ensure_library_channels(client, message.chat.id) is None:
        return

    post = await database.get_library_post(post_id)
    if post is None:
        await send_message(
            client,
            message.chat.id,
            f"❌ Library post <code>{post_id}</code> was not found.",
        )
        return

    _newpost_sessions.pop(message.from_user.id, None)
    _editpost_sessions[message.from_user.id] = EditPostDraft(post=post)

    labels = ", ".join(
        f"<code>{html.escape(quality['label'])}</code>"
        for quality in post["qualities"]
    )
    await send_message(
        client,
        message.chat.id,
        f"✏️ <b>Editing library post #{post_id}</b>\n\n"
        f"Current qualities: {labels}\n\n"
        "Send the replacement video/document with the quality label as its "
        "caption (for example <code>1080p</code>).\n\n"
        "Use /cancel to abort.",
    )


async def handle_cancel(client: Client, message: Message) -> None:
    """Cancel an active library-post session, if any."""
    if message.from_user is None:
        return

    cancelled = _newpost_sessions.pop(message.from_user.id, None)
    if cancelled is None:
        cancelled = _editpost_sessions.pop(message.from_user.id, None)

    if cancelled is not None:
        await send_message(
            client, message.chat.id, "❌ Library post draft cancelled."
        )
    else:
        await send_message(
            client, message.chat.id, "There is no active library post draft."
        )


async def handle_session_message(client: Client, message: Message) -> None:
    """Route messages for admins who are inside /newpost or /editpost."""
    if message.from_user is None:
        return

    user_id = message.from_user.id
    draft = _newpost_sessions.get(user_id)
    if draft is not None:
        await _handle_newpost_message(client, message, draft)
        return

    edit_draft = _editpost_sessions.get(user_id)
    if edit_draft is not None:
        await _handle_editpost_message(client, message, edit_draft)


async def _handle_newpost_message(
    client: Client, message: Message, draft: NewPostDraft
) -> None:
    command = _command_name(message)
    if command == "done":
        await _finish_newpost(client, message, draft)
        return
    if command is not None:
        await send_message(
            client,
            message.chat.id,
            "Use /done to finish the post or /cancel to abort.",
        )
        return

    if draft.stage == "thumbnail":
        if not message.photo:
            await send_message(
                client,
                message.chat.id,
                "📷 <b>Step 1/3:</b> Send the thumbnail as a photo.",
            )
            return

        draft.thumbnail_file_id = message.photo.file_id
        draft.stage = "caption"
        await send_message(
            client,
            message.chat.id,
            "✅ Thumbnail saved.\n\n"
            "<b>Step 2/3:</b> Send the title/caption text. Multiple lines and "
            "Markdown formatting (<code>**bold**</code>, "
            "<code>__italic__</code>) are supported.",
        )
        return

    if draft.stage == "caption":
        if not message.text:
            await send_message(
                client,
                message.chat.id,
                "📝 <b>Step 2/3:</b> Send the title/caption as text.",
            )
            return

        title = message.text.strip()
        if not title:
            await send_message(
                client, message.chat.id, "❌ The caption cannot be empty."
            )
            return
        if len(title) > MAX_CAPTION_LENGTH:
            await send_message(
                client,
                message.chat.id,
                f"❌ The caption is too long ({len(title)} characters). "
                f"Telegram allows at most {MAX_CAPTION_LENGTH}.",
            )
            return

        draft.title = title
        draft.stage = "files"
        await send_message(
            client,
            message.chat.id,
            "✅ Caption saved.\n\n"
            "<b>Step 3/3:</b> Send one or more video/document files. Put only "
            "the quality label in each file's caption, for example "
            "<code>1080p</code> or <code>720p</code>.\n\n"
            "Send /done when finished.",
        )
        return

    if message.video or message.document:
        label = (message.caption or "").strip()
        if not label:
            await send_message(
                client,
                message.chat.id,
                "❌ This file has no caption. Send it again with only the "
                "quality label as the caption, for example <code>1080p</code>.",
            )
            return
        if len(label) > MAX_LABEL_LENGTH or "\n" in label or "\r" in label:
            await send_message(
                client,
                message.chat.id,
                f"❌ Quality labels must be a single line, at most "
                f"{MAX_LABEL_LENGTH} characters.",
            )
            return

        key = label.casefold()
        file_type = "video" if message.video else "document"
        replaced = key in draft.qualities
        draft.qualities[key] = QualityDraft(
            label=label,
            chat_id=message.chat.id,
            message_id=message.id,
            file_type=file_type,
        )
        action = "Replaced" if replaced else "Added"
        await send_message(
            client,
            message.chat.id,
            f"✅ {action} <b>{html.escape(label)}</b>. "
            f"{len(draft.qualities)} quality file(s) queued.\n\n"
            "Send another file or /done to publish.",
        )
        return

    await send_message(
        client,
        message.chat.id,
        "Send a video/document with only the quality label as its caption, "
        "or /done to publish.",
    )


async def _finish_newpost(
    client: Client, message: Message, draft: NewPostDraft
) -> None:
    if message.from_user is None:
        return

    if draft.stage != "files":
        await send_message(
            client,
            message.chat.id,
            "❌ Finish the current step before using /done.",
        )
        return
    if not draft.qualities:
        await send_message(
            client,
            message.chat.id,
            "❌ Add at least one quality-labeled file before /done.",
        )
        return

    # Remove the session first so a duplicate /done cannot publish twice.
    _newpost_sessions.pop(message.from_user.id, None)
    chat_id = message.chat.id
    status = await send_message(
        client,
        chat_id,
        f"⏳ Storing {len(draft.qualities)} file(s) and creating the posts...",
    )

    links_post: Optional[Message] = None
    library_post: Optional[Message] = None
    stored_files: list[StoredFile] = []

    try:
        quality_drafts = list(draft.qualities.values())
        for quality in quality_drafts:
            stored = await store_message(
                client,
                quality.chat_id,
                quality.message_id,
                uploaded_by=message.from_user.id,
            )
            if stored is None:
                raise RuntimeError(
                    f"Could not copy {quality.label!r} to STORAGE_CHANNEL. "
                    "Check the bot's storage admin rights and try again."
                )
            stored_files.append(stored)

        quality_buttons = _quality_keyboard(
            [
                (quality.label, stored.button_url)
                for quality, stored in zip(quality_drafts, stored_files)
            ]
        )

        links_post = await send_photo(
            client,
            draft.links_channel_id,
            draft.thumbnail_file_id,
            caption=draft.title,
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=quality_buttons,
        )
        if links_post is None:
            raise RuntimeError(
                "Could not post to LINKS_CHANNEL. Check the bot's admin and "
                "post-message rights, and make sure the caption is valid "
                "Markdown."
            )
        links_message_link = links_post.link

        library_post = await send_photo(
            client,
            draft.library_channel_id,
            draft.thumbnail_file_id,
            caption=draft.title,
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📥 Download Links", url=links_message_link
                        )
                    ]
                ]
            ),
        )
        if library_post is None:
            raise RuntimeError(
                "Could not post to LIBRARY_CHANNEL. Check the bot's admin and "
                "post-message rights."
            )

        post_id = await database.add_library_post(
            title=draft.title or "",
            thumbnail_file_id=draft.thumbnail_file_id or "",
            links_channel_id=links_post.chat.id,
            links_message_id=links_post.id,
            links_message_link=links_message_link,
            library_channel_id=library_post.chat.id,
            library_message_id=library_post.id,
            library_message_link=library_post.link,
            qualities=[
                {
                    "label": quality.label,
                    "unique_id": stored.unique_id,
                    "storage_message_id": stored.message_id,
                    "direct_link": stored.direct_link,
                    "short_link": stored.short_link,
                    "button_url": stored.button_url,
                }
                for quality, stored in zip(quality_drafts, stored_files)
            ],
        )
    except Exception as exc:
        logger.exception("Failed to create library post")
        # Keep the two channels consistent if only part of the flow succeeded.
        if library_post is not None:
            await delete_messages(
                client, library_post.chat.id, [library_post.id]
            )
        if links_post is not None:
            await delete_messages(client, links_post.chat.id, [links_post.id])

        await send_message(
            client,
            chat_id,
            "❌ <b>Post creation failed.</b>\n\n"
            f"{html.escape(str(exc))}\n\n"
            "No post was saved. Fix the issue and run /newpost again.",
        )
        await _delete_status(client, chat_id, status)
        return

    labels = ", ".join(
        html.escape(quality.label) for quality in draft.qualities.values()
    )
    text = (
        f"✅ <b>Library post #{post_id} created</b>\n\n"
        f"🗂 <b>Qualities:</b> {labels}\n\n"
        f"🔗 <b>Links channel post:</b>\n"
        f"{html.escape(links_message_link)}\n\n"
        f"📚 <b>Library channel post:</b>\n"
        f"{html.escape(library_post.link)}"
    )
    if any(stored.short_link is None for stored in stored_files):
        text += (
            "\n\n⚠️ Some short links could not be generated; the direct "
            "Telegram links were used for those buttons."
        )

    await send_message(client, chat_id, text, disable_web_page_preview=True)
    await _delete_status(client, chat_id, status)

    logger.info(
        "Created library post #%s (%s qualities) for admin %s",
        post_id,
        len(stored_files),
        message.from_user.id,
    )


async def _handle_editpost_message(
    client: Client, message: Message, draft: EditPostDraft
) -> None:
    command = _command_name(message)
    if command == "done":
        _editpost_sessions.pop(message.from_user.id, None)
        await send_message(
            client,
            message.chat.id,
            "Edit session finished. No further changes were made.",
        )
        return
    if command is not None:
        await send_message(
            client,
            message.chat.id,
            "Send a replacement file, /done to finish, or /cancel to abort.",
        )
        return

    post = draft.post
    labels = ", ".join(
        f"<code>{html.escape(quality['label'])}</code>"
        for quality in post["qualities"]
    )

    if not (message.video or message.document):
        await send_message(
            client,
            message.chat.id,
            f"Send a video/document with one of these quality labels as its "
            f"caption: {labels}",
        )
        return

    label = (message.caption or "").strip()
    if not label:
        await send_message(
            client,
            message.chat.id,
            f"❌ This file has no caption. Use one of: {labels}",
        )
        return

    matching = next(
        (
            quality
            for quality in post["qualities"]
            if quality["label"].casefold() == label.casefold()
        ),
        None,
    )
    if matching is None:
        await send_message(
            client,
            message.chat.id,
            f"❌ Unknown quality <b>{html.escape(label)}</b>. "
            f"Use one of: {labels}",
        )
        return

    status = await send_message(
        client,
        message.chat.id,
        f"⏳ Replacing <b>{html.escape(matching['label'])}</b>...",
    )

    try:
        stored = await store_message(
            client,
            message.chat.id,
            message.id,
            uploaded_by=message.from_user.id,
        )
    except Exception:
        logger.exception("Failed to store replacement file")
        stored = None
    if stored is None:
        await send_message(
            client,
            message.chat.id,
            "❌ Could not copy the replacement to STORAGE_CHANNEL. "
            "Check the bot's storage admin rights.",
        )
        await _delete_status(client, message.chat.id, status)
        return

    updated_qualities = []
    for quality in post["qualities"]:
        if quality["id"] == matching["id"]:
            updated = dict(quality)
            updated.update(
                {
                    "unique_id": stored.unique_id,
                    "storage_message_id": stored.message_id,
                    "direct_link": stored.direct_link,
                    "short_link": stored.short_link,
                    "button_url": stored.button_url,
                }
            )
            updated_qualities.append(updated)
        else:
            updated_qualities.append(quality)

    try:
        saved = await database.replace_library_post_quality(
            post["id"],
            matching["label"],
            unique_id=stored.unique_id,
            storage_message_id=stored.message_id,
            direct_link=stored.direct_link,
            short_link=stored.short_link,
            button_url=stored.button_url,
        )
    except Exception:
        logger.exception("Failed to update library post quality in SQLite")
        await send_message(
            client,
            message.chat.id,
            "❌ The file was stored, but the database update failed. "
            "The post was not changed.",
        )
        await _delete_status(client, message.chat.id, status)
        return

    if not saved:
        await send_message(
            client,
            message.chat.id,
            "❌ That quality no longer exists in the database. "
            "The post was not changed.",
        )
        await _delete_status(client, message.chat.id, status)
        return

    edited = await edit_message_reply_markup(
        client,
        post["links_channel_id"],
        post["links_message_id"],
        reply_markup=_quality_keyboard(
            [
                (
                    quality["label"],
                    quality["button_url"],
                )
                for quality in updated_qualities
            ]
        ),
    )
    if edited is None:
        # Roll the database back so it still matches the unchanged message.
        try:
            await database.replace_library_post_quality(
                post["id"],
                matching["label"],
                unique_id=matching["unique_id"],
                storage_message_id=matching["storage_message_id"],
                direct_link=matching["direct_link"],
                short_link=matching["short_link"],
                button_url=matching["button_url"],
            )
        except Exception:
            logger.exception("Failed to roll back library post quality")

        await send_message(
            client,
            message.chat.id,
            "❌ Could not edit the LINKS_CHANNEL message. Make sure the bot is "
            "still an administrator there and the message was not deleted. "
            "The database change was rolled back.",
        )
        await _delete_status(client, message.chat.id, status)
        return

    _editpost_sessions.pop(message.from_user.id, None)
    await send_message(
        client,
        message.chat.id,
        f"✅ Replaced <b>{html.escape(matching['label'])}</b> in library "
        f"post #{post['id']}.\n\n"
        f"🔗 {html.escape(stored.button_url)}",
        disable_web_page_preview=True,
    )
    await _delete_status(client, message.chat.id, status)
    logger.info(
        "Replaced quality %s in library post #%s for admin %s",
        matching["label"],
        post["id"],
        message.from_user.id,
    )


def register(app: Client) -> None:
    """Register library-post handlers. Called explicitly from bot.py."""
    # Group -2 captures the guided flow before the normal upload handler in
    # group 0 can treat quality files as ordinary uploads.
    app.add_handler(
        MessageHandler(
            handle_newpost, ADMIN_PRIVATE & filters.command("newpost")
        ),
        group=-2,
    )
    app.add_handler(
        MessageHandler(
            handle_editpost, ADMIN_PRIVATE & filters.command("editpost")
        ),
        group=-2,
    )
    app.add_handler(
        MessageHandler(
            handle_cancel, ADMIN_PRIVATE & filters.command("cancel")
        ),
        group=-2,
    )
    app.add_handler(
        MessageHandler(handle_session_message, ADMIN_PRIVATE & SESSION_FILTER),
        group=-2,
    )
