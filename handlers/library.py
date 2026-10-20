"""Admin library-post workflow: /newpost and /editpost."""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from typing import Optional

from pyrogram import Client, enums, filters
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import database
from config import settings
from utils.channels import (
    require_channel_admin,
    resolve_channel,
    resolve_library_category,
)
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
    stage: str = "thumbnail"
    thumbnail_file_id: Optional[str] = None
    title: Optional[str] = None
    category: Optional[str] = None
    qualities: dict[str, QualityDraft] = field(default_factory=dict)


@dataclass
class EditPostDraft:
    post: dict
    category: str = "default"


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


def _is_admin_callback(_, __, query: CallbackQuery) -> bool:
    return (
        query.from_user is not None
        and query.from_user.id in settings.admin_ids
    )


ADMIN_CALLBACK = filters.create(_is_admin_callback)


def _category_keyboard(
    flow: str, post_id: Optional[int] = None
) -> InlineKeyboardMarkup:
    """Build one button per configured library category."""
    if flow == "edit" and post_id is not None:
        prefix = f"libcat:edit:{post_id}"
    else:
        prefix = f"libcat:{flow}"

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    category,
                    callback_data=f"{prefix}:{index}",
                )
            ]
            for index, category in enumerate(settings.library_channels)
        ]
    )


def _chat_label(chat: Chat) -> str:
    if chat.title:
        return chat.title
    if chat.username:
        return f"@{chat.username}"
    return str(chat.id)


async def _resolve_category_or_report(
    client: Client, chat_id: int, category: str
) -> Optional[Chat]:
    """Resolve a category and tell the admin exactly which one failed."""
    try:
        return await resolve_library_category(
            client, category, settings.library_channels
        )
    except RuntimeError as exc:
        await send_message(
            client,
            chat_id,
            f"❌ <b>Library category "
            f"{html.escape(category)} is unavailable.</b>\n\n"
            f"{html.escape(str(exc))}\n\n"
            "Choose another category or update <code>LIBRARY_CHANNELS</code> "
            "and restart the bot.",
        )
        return None
    except Exception:
        logger.exception(
            "Unexpected error resolving library category %r", category
        )
        await send_message(
            client,
            chat_id,
            f"❌ <b>Library category {html.escape(category)} could not be "
            "reached.</b>\n\n"
            "Check the bot's admin rights and network connection, then try "
            "again or choose another category.",
        )
        return None


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


async def _ensure_library_config(
    client: Client, chat_id: int
) -> Optional[Chat]:
    """Validate the shared LINKS_CHANNEL and require at least one category."""
    missing = []
    if not settings.links_channel:
        missing.append("LINKS_CHANNEL")
    if not settings.library_channels:
        missing.append("LIBRARY_CHANNELS")
    if missing:
        await send_message(
            client,
            chat_id,
            "❌ <b>Library posting is not configured.</b>\n\n"
            f"Missing: <code>{', '.join(missing)}</code>\n\n"
            "Set <code>LINKS_CHANNEL</code> and "
            "<code>LIBRARY_CHANNELS=category:@channel,...</code> in "
            "<code>.env</code>, then restart the bot. The old single-channel "
            "<code>LIBRARY_CHANNEL</code> setting is still accepted as the "
            "<code>default</code> category.",
        )
        return None

    try:
        links_chat = await resolve_channel(
            client, settings.links_channel, "LINKS_CHANNEL"
        )
        await require_channel_admin(
            client,
            links_chat,
            settings.links_channel,
            "LINKS_CHANNEL",
            require_post_permission=True,
        )
    except RuntimeError as exc:
        await send_message(client, chat_id, f"❌ {html.escape(str(exc))}")
        return None
    except Exception:
        logger.exception("Unexpected error resolving LINKS_CHANNEL")
        await send_message(
            client,
            chat_id,
            "❌ <b>LINKS_CHANNEL could not be reached.</b>\n\n"
            "Check the bot's admin rights and network connection, then retry.",
        )
        return None

    return links_chat


async def handle_newpost(client: Client, message: Message) -> None:
    """Start the guided /newpost flow."""
    if message.from_user is None:
        return

    links_chat = await _ensure_library_config(client, message.chat.id)
    if links_chat is None:
        return

    _editpost_sessions.pop(message.from_user.id, None)
    _newpost_sessions[message.from_user.id] = NewPostDraft(
        links_channel_id=links_chat.id,
    )

    await send_message(
        client,
        message.chat.id,
        "🎬 <b>New library post</b>\n\n"
        "<b>Step 1/4:</b> Send the thumbnail image (as a photo).\n\n"
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

    if await _ensure_library_config(client, message.chat.id) is None:
        return

    post = await database.get_library_post(post_id)
    if post is None:
        await send_message(
            client,
            message.chat.id,
            f"❌ Library post <code>{post_id}</code> was not found.",
        )
        return

    category = str(post.get("category") or "default")
    try:
        current_chat = await resolve_library_category(
            client, category, settings.library_channels
        )
        category_status = (
            f"🗂 <b>Category:</b> {html.escape(category)} "
            f"({html.escape(_chat_label(current_chat))})"
        )
    except RuntimeError as exc:
        category_status = (
            f"⚠️ <b>Stored category:</b> {html.escape(category)}\n"
            f"{html.escape(str(exc))}\n\n"
            "You can still replace a quality file, or choose a configured "
            "category below to move the post."
        )
    except Exception:
        logger.exception("Unexpected error resolving stored category")
        category_status = (
            f"⚠️ <b>Stored category:</b> {html.escape(category)}\n"
            "The category channel could not be checked. You can still replace "
            "a quality file, or choose a configured category below to move "
            "the post."
        )

    _newpost_sessions.pop(message.from_user.id, None)
    _editpost_sessions[message.from_user.id] = EditPostDraft(
        post=post, category=category
    )

    labels = ", ".join(
        f"<code>{html.escape(quality['label'])}</code>"
        for quality in post["qualities"]
    )
    await send_message(
        client,
        message.chat.id,
        f"✏️ <b>Editing library post #{post_id}</b>\n\n"
        f"{category_status}\n\n"
        f"Current qualities: {labels}\n\n"
        "Send the replacement video/document with the quality label as its "
        "caption (for example <code>1080p</code>).\n\n"
        "To move this post, tap a category button. The library post will be "
        "reposted there.\n\n"
        "Use /cancel to abort.",
        reply_markup=_category_keyboard("edit", post_id=post_id),
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

    if draft.stage == "category":
        await send_message(
            client,
            message.chat.id,
            "🗂 <b>Step 3/4:</b> Choose a library category using the buttons "
            "below, or use /cancel to abort.",
            reply_markup=_category_keyboard("new"),
        )
        return

    if draft.stage == "thumbnail":
        if not message.photo:
            await send_message(
                client,
                message.chat.id,
                "📷 <b>Step 1/4:</b> Send the thumbnail as a photo.",
            )
            return

        draft.thumbnail_file_id = message.photo.file_id
        draft.stage = "caption"
        await send_message(
            client,
            message.chat.id,
            "✅ Thumbnail saved.\n\n"
            "<b>Step 2/4:</b> Send the title/caption text. Multiple lines and "
            "Markdown formatting (<code>**bold**</code>, "
            "<code>__italic__</code>) are supported.",
        )
        return

    if draft.stage == "caption":
        if not message.text:
            await send_message(
                client,
                message.chat.id,
                "📝 <b>Step 2/4:</b> Send the title/caption as text.",
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
        if not settings.library_channels:
            await send_message(
                client,
                message.chat.id,
                "❌ <b>No library categories are configured.</b>\n\n"
                "Set <code>LIBRARY_CHANNELS=category:@channel,...</code> in "
                "<code>.env</code> and restart the bot, then run /newpost "
                "again.",
            )
            _newpost_sessions.pop(message.from_user.id, None)
            return

        draft.stage = "category"
        await send_message(
            client,
            message.chat.id,
            "✅ Caption saved.\n\n"
            "<b>Step 3/4:</b> Choose the library category for this post.",
            reply_markup=_category_keyboard("new"),
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
    if not draft.category:
        await send_message(
            client,
            message.chat.id,
            "❌ Choose a library category before using /done.",
        )
        return

    # Re-resolve at publish time so a channel removed while the draft was open
    # produces a category-specific error instead of a generic post failure.
    library_chat = await _resolve_category_or_report(
        client, message.chat.id, draft.category
    )
    if library_chat is None:
        # Keep the draft and let the admin pick another category instead of
        # throwing away all queued quality files.
        draft.stage = "category"
        await send_message(
            client,
            message.chat.id,
            "Choose another category to continue, or /cancel to abort.",
            reply_markup=_category_keyboard("new"),
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
            library_chat.id,
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
                f"Could not post to library category {draft.category!r} "
                f"({_chat_label(library_chat)}). Check the bot's admin and "
                "post-message rights."
            )

        post_id = await database.add_library_post(
            title=draft.title or "",
            category=draft.category,
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
        f"🗂 <b>Category:</b> {html.escape(draft.category)} "
        f"({html.escape(_chat_label(library_chat))})\n\n"
        f"🎞 <b>Qualities:</b> {labels}\n\n"
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
        "Created library post #%s in category %r (%s qualities) for admin %s",
        post_id,
        draft.category,
        len(stored_files),
        message.from_user.id,
    )


async def _select_newpost_category(
    client: Client, query: CallbackQuery, category: str
) -> None:
    if query.from_user is None:
        return

    chat_id = query.message.chat.id if query.message else query.from_user.id
    draft = _newpost_sessions.get(query.from_user.id)
    if draft is None or draft.stage != "category":
        await query.answer(
            "This category picker is no longer active.", show_alert=True
        )
        return

    library_chat = await _resolve_category_or_report(client, chat_id, category)
    if library_chat is None:
        await query.answer(
            f"Category {category!r} is unavailable.", show_alert=True
        )
        return

    draft.category = category
    draft.stage = "files"

    await query.answer(f"Selected {category}")
    if query.message is not None:
        await _delete_status(client, chat_id, query.message)

    await send_message(
        client,
        chat_id,
        f"✅ Category: <b>{html.escape(category)}</b> "
        f"({html.escape(_chat_label(library_chat))})\n\n"
        "<b>Step 4/4:</b> Send one or more video/document files. Put only the "
        "quality label in each file's caption, for example <code>1080p</code> "
        "or <code>720p</code>.\n\n"
        "Send /done when finished.",
    )


async def _move_library_post(
    client: Client,
    chat_id: int,
    draft: EditPostDraft,
    new_category: str,
    new_chat: Chat,
) -> bool:
    """Repost a library post to a new category and update its DB reference."""
    post = draft.post
    old_channel_id = int(post["library_channel_id"])
    old_message_id = int(post["library_message_id"])

    if new_chat.id == old_channel_id:
        try:
            updated = await database.update_library_post_location(
                post["id"],
                category=new_category,
                library_channel_id=old_channel_id,
                library_message_id=old_message_id,
                library_message_link=post.get("library_message_link"),
            )
        except Exception:
            logger.exception("Failed to update library post category")
            await send_message(
                client,
                chat_id,
                "❌ The category could not be saved. The post was not changed.",
            )
            return False
        if not updated:
            await send_message(
                client,
                chat_id,
                "❌ The library post no longer exists. The category was not "
                "changed.",
            )
            return False

        post["category"] = new_category
        draft.category = new_category
        return True

    new_message = await send_photo(
        client,
        new_chat.id,
        post["thumbnail_file_id"],
        caption=post["title"],
        parse_mode=enums.ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📥 Download Links", url=post["links_message_link"]
                    )
                ]
            ]
        ),
    )
    if new_message is None:
        await send_message(
            client,
            chat_id,
            f"❌ Could not post to category <b>{html.escape(new_category)}</b> "
            f"({html.escape(_chat_label(new_chat))}). Check the bot's admin "
            "and post-message rights; the category was not changed.",
        )
        return False

    try:
        updated = await database.update_library_post_location(
            post["id"],
            category=new_category,
            library_channel_id=new_message.chat.id,
            library_message_id=new_message.id,
            library_message_link=new_message.link,
        )
    except Exception:
        logger.exception("Failed to update library post location")
        await delete_messages(
            client, new_message.chat.id, [new_message.id]
        )
        await send_message(
            client,
            chat_id,
            "❌ The category could not be saved. The post was not changed.",
        )
        return False

    if not updated:
        await delete_messages(
            client, new_message.chat.id, [new_message.id]
        )
        await send_message(
            client,
            chat_id,
            "❌ The library post no longer exists. The category was not "
            "changed.",
        )
        return False

    old_deleted = await delete_messages(
        client, old_channel_id, [old_message_id]
    )
    if not old_deleted:
        logger.warning(
            "Moved library post #%s but could not delete old message %s in %s",
            post["id"],
            old_message_id,
            old_channel_id,
        )
        await send_message(
            client,
            chat_id,
            "⚠️ The post was moved, but the old library message in category "
            f"<b>{html.escape(str(post.get('category') or 'default'))}</b> "
            "could not be deleted. Remove it manually if it is still visible.",
        )

    post.update(
        {
            "category": new_category,
            "library_channel_id": new_message.chat.id,
            "library_message_id": new_message.id,
            "library_message_link": new_message.link,
        }
    )
    draft.category = new_category
    return True


async def _select_editpost_category(
    client: Client,
    query: CallbackQuery,
    category: str,
    post_id: int,
) -> None:
    if query.from_user is None:
        return

    chat_id = query.message.chat.id if query.message else query.from_user.id
    draft = _editpost_sessions.get(query.from_user.id)
    if draft is None or int(draft.post["id"]) != post_id:
        await query.answer(
            "This edit session is no longer active.", show_alert=True
        )
        return
    if category == draft.category:
        await query.answer(f"This post is already in {category}.")
        return

    new_chat = await _resolve_category_or_report(client, chat_id, category)
    if new_chat is None:
        await query.answer(
            f"Category {category!r} is unavailable.", show_alert=True
        )
        return

    await query.answer(f"Moving to {category}...")
    if not await _move_library_post(
        client, chat_id, draft, category, new_chat
    ):
        return

    post = draft.post
    link = post.get("library_message_link") or _chat_label(new_chat)
    await send_message(
        client,
        chat_id,
        f"✅ Library post #{post['id']} moved to category "
        f"<b>{html.escape(category)}</b> "
        f"({html.escape(_chat_label(new_chat))}).\n\n"
        f"📚 {html.escape(link)}\n\n"
        "Send a replacement file to update a quality, or /done to finish.",
        disable_web_page_preview=True,
    )


async def handle_library_category_callback(
    client: Client, query: CallbackQuery
) -> None:
    """Handle category buttons for /newpost and /editpost."""
    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 3 or parts[0] != "libcat":
        await query.answer()
        return

    flow = parts[1]
    post_id: Optional[int] = None
    try:
        if flow == "new" and len(parts) == 3:
            index = int(parts[2])
        elif flow == "edit" and len(parts) == 4:
            post_id = int(parts[2])
            index = int(parts[3])
        else:
            await query.answer("Invalid category selection.", show_alert=True)
            return
    except ValueError:
        await query.answer("Invalid category selection.", show_alert=True)
        return

    categories = list(settings.library_channels)
    if not 0 <= index < len(categories):
        await query.answer(
            "That category is no longer configured.", show_alert=True
        )
        return

    category = categories[index]
    if flow == "new":
        await _select_newpost_category(client, query, category)
    elif flow == "edit" and post_id is not None:
        await _select_editpost_category(
            client, query, category, post_id
        )
    else:
        await query.answer()


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
        logger.exception("Failed to update library post quality in Turso")
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
    app.add_handler(
        CallbackQueryHandler(
            handle_library_category_callback, ADMIN_CALLBACK
        ),
        group=-2,
    )
