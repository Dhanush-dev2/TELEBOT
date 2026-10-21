"""Admin library workflow.

Series commands: /newseries, /addepisode, /listseries and the one-time
/migrateposts bridge. Legacy commands: /newpost and /editpost.
"""

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
MAX_LINKS_BUTTONS = 90
MAX_BUTTON_TEXT_LENGTH = 60
MAX_SERIES_TITLE_LENGTH = 120
SERIES_PAGE_SIZE = 8

_MANAGEMENT_COMMANDS = {
    "start",
    "stats",
    "broadcast",
    "newpost",
    "editpost",
    "newseries",
    "addepisode",
    "listseries",
    "migrateposts",
    "cancel",
}


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


@dataclass
class SeriesDraft:
    """Draft for /newseries (mode="new") and /addepisode (mode="add")."""

    mode: str
    stage: str = "thumbnail"
    links_channel_id: Optional[int] = None
    series_id: Optional[int] = None
    series: Optional[dict] = None
    thumbnail_file_id: Optional[str] = None
    title: Optional[str] = None
    caption: Optional[str] = None
    category: Optional[str] = None
    episode_label: Optional[str] = None
    qualities: dict[str, QualityDraft] = field(default_factory=dict)


@dataclass
class MigrateDraft:
    """State for one legacy post in the /migrateposts flow."""

    post: dict
    stage: str = "review"
    series_id: Optional[int] = None
    episode_label: Optional[str] = None
    series_page: int = 0


_newpost_sessions: dict[int, NewPostDraft] = {}
_editpost_sessions: dict[int, EditPostDraft] = {}
_series_sessions: dict[int, SeriesDraft] = {}
_migrate_sessions: dict[int, MigrateDraft] = {}


def _command_name(message: Message) -> Optional[str]:
    if not message.text or not message.text.startswith("/"):
        return None
    command = message.text.split(maxsplit=1)[0][1:]
    return command.split("@", 1)[0].lower()


def _has_session(_, __, message: Message) -> bool:
    if message.from_user is None:
        return False
    user_id = message.from_user.id
    return (
        user_id in _newpost_sessions
        or user_id in _editpost_sessions
        or user_id in _series_sessions
        or user_id in _migrate_sessions
    )


SESSION_FILTER = filters.create(_has_session)


def _clear_sessions(user_id: int) -> None:
    """Drop every in-memory admin draft for a user before starting a new one."""
    _newpost_sessions.pop(user_id, None)
    _editpost_sessions.pop(user_id, None)
    _series_sessions.pop(user_id, None)
    _migrate_sessions.pop(user_id, None)


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

    _clear_sessions(message.from_user.id)
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

    if int(post.get("migrated") or 0):
        await send_message(
            client,
            message.chat.id,
            f"⚠️ Library post <code>{post_id}</code> was migrated into the "
            "series layout.\n\n"
            "<code>/editpost</code> only manages unmigrated legacy posts now. "
            "Use <code>/listseries</code> to find the series and "
            "<code>/addepisode</code> for new episodes. Replacing the file of "
            "a migrated episode is not supported by this command.",
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

    _clear_sessions(message.from_user.id)
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
    if cancelled is None:
        cancelled = _series_sessions.pop(message.from_user.id, None)
    if cancelled is None:
        cancelled = _migrate_sessions.pop(message.from_user.id, None)

    if cancelled is not None:
        await send_message(
            client, message.chat.id, "❌ Admin draft cancelled."
        )
    else:
        await send_message(
            client, message.chat.id, "There is no active admin draft."
        )


async def handle_session_message(client: Client, message: Message) -> None:
    """Route messages for admins who are inside a guided draft."""
    if message.from_user is None:
        return

    user_id = message.from_user.id
    command = _command_name(message)

    draft = _newpost_sessions.get(user_id)
    if draft is not None:
        await _handle_newpost_message(client, message, draft)
        return

    edit_draft = _editpost_sessions.get(user_id)
    if edit_draft is not None:
        await _handle_editpost_message(client, message, edit_draft)
        return

    series_draft = _series_sessions.get(user_id)
    if series_draft is not None:
        # Informational/admin commands are handled by their own group -2
        # handlers; do not also answer them with a draft reminder.
        if command in _MANAGEMENT_COMMANDS:
            return
        await _handle_series_message(client, message, series_draft)
        return

    migrate_draft = _migrate_sessions.get(user_id)
    if migrate_draft is not None:
        if command in _MANAGEMENT_COMMANDS:
            return
        await _handle_migrate_message(client, message, migrate_draft)


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
    """Handle category buttons for /newpost, /newseries and /editpost."""
    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 3 or parts[0] != "libcat":
        # Other callback handlers own the remaining prefixes. Do not answer
        # here, or the owner's answer() would be the second answer to the
        # same callback query.
        return

    flow = parts[1]
    post_id: Optional[int] = None
    try:
        if flow == "new" and len(parts) == 3:
            index = int(parts[2])
        elif flow == "newseries" and len(parts) == 3:
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
    elif flow == "newseries":
        await _select_newseries_category(client, query, category)
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


# ---------------------------------------------------------------------------
# Series workflows (/newseries, /addepisode, /listseries)
# ---------------------------------------------------------------------------


def _derive_title(caption: str) -> str:
    """Use the first meaningful caption line as the Links Channel header."""
    for line in caption.splitlines():
        cleaned = line.strip().lstrip("#>*_-`~ ")
        for marker in ("**", "__", "`", "*"):
            cleaned = cleaned.replace(marker, "")
        cleaned = cleaned.strip()
        if cleaned:
            return cleaned[:MAX_SERIES_TITLE_LENGTH].strip()
    return "Untitled series"


def _validate_episode_label(raw: str) -> tuple[Optional[str], Optional[str]]:
    label = raw.strip()
    if not label:
        return None, "❌ The episode label cannot be empty."
    if len(label) > MAX_LABEL_LENGTH or "\n" in label or "\r" in label:
        return (
            None,
            f"❌ Episode labels must be a single line, at most "
            f"{MAX_LABEL_LENGTH} characters.",
        )
    return label, None


def _validate_quality_label(
    message: Message,
) -> tuple[Optional[str], Optional[str]]:
    label = (message.caption or "").strip()
    if not label:
        return (
            None,
            "❌ This file has no caption. Send it again with only the quality "
            "label as the caption, for example <code>1080p</code>.",
        )
    if len(label) > MAX_LABEL_LENGTH or "\n" in label or "\r" in label:
        return (
            None,
            f"❌ Quality labels must be a single line, at most "
            f"{MAX_LABEL_LENGTH} characters.",
        )
    return label, None


def _episode_button_label(episode_label: str, quality_label: str) -> str:
    """Build a button label that keeps the episode and quality readable."""
    label = (episode_label or "Episode").strip() or "Episode"
    quality = (quality_label or "Link").strip() or "Link"
    suffix = f" · {quality}"
    available = MAX_BUTTON_TEXT_LENGTH - len(suffix)
    if available < 8:
        return f"Ep · {quality}"[:MAX_BUTTON_TEXT_LENGTH]
    if len(label) > available:
        label = label[: max(1, available - 1)].rstrip() + "…"
    return f"{label}{suffix}"


def _series_links_keyboard(series: dict) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for episode in series.get("episodes", []):
        row = []
        for link in episode.get("links", []):
            url = link.get("short_url") or link.get("direct_link")
            if not url:
                continue
            row.append(
                InlineKeyboardButton(
                    _episode_button_label(
                        episode.get("episode_label", "Episode"),
                        link.get("quality_label", "Link"),
                    ),
                    url=url,
                )
            )
        if row:
            rows.append(row)
    return InlineKeyboardMarkup(rows)


def _series_links_text(series: dict) -> str:
    return (
        f"🎬 <b>{html.escape(str(series.get('title') or 'Untitled series'))}"
        "</b>\n\nSelect an episode and quality below."
    )


def _series_button_count(series: dict) -> int:
    return sum(
        len(episode.get("links", []))
        for episode in series.get("episodes", [])
    )


def _button_limit_text(series: dict, extra_buttons: int) -> str:
    current = int(series.get("link_count") or _series_button_count(series))
    total = current + extra_buttons
    return (
        "⚠️ <b>Button limit reached</b>\n\n"
        f"Series <b>#{series.get('id')} "
        f"{html.escape(str(series.get('title') or ''))}</b> already has "
        f"<b>{current}</b> quality button(s). Adding <b>{extra_buttons}</b> "
        f"more would make <b>{total}</b>, above the safe limit of "
        f"{MAX_LINKS_BUTTONS}.\n\n"
        "Nothing was changed. Create a manual <b>Part 2</b> series with "
        "/newseries for the remaining episodes so each Links Channel post "
        "stays usable."
    )


def _next_episode_label(series: dict) -> str:
    count = int(series.get("episode_count") or 0)
    return f"Episode {count + 1}"


def _default_label_from_post(post: dict) -> str:
    title = (post.get("title") or "").strip()
    first_line = title.splitlines()[0].strip() if title else ""
    if not first_line:
        return "Episode 1"
    return first_line[:MAX_LABEL_LENGTH]


def _message_link(chat: Optional[Chat], message_id: Optional[int]) -> Optional[str]:
    """Build a t.me link for a stored message without fetching it."""
    if chat is None or not message_id:
        return None
    if chat.username:
        return f"https://t.me/{chat.username}/{int(message_id)}"
    raw_id = str(chat.id)
    if raw_id.startswith("-100"):
        return f"https://t.me/c/{raw_id[4:]}/{int(message_id)}"
    return None


def _series_button_text(series: dict) -> str:
    label = (
        f"#{series.get('id')} {series.get('title') or 'Untitled'} "
        f"· {int(series.get('episode_count') or 0)} ep"
    )
    return label[:MAX_BUTTON_TEXT_LENGTH]


async def _send_addepisode_list(
    client: Client,
    chat_id: int,
    *,
    page: int = 0,
    category: Optional[str] = None,
) -> None:
    series_list = await database.list_series(category)
    if not series_list:
        text = "📺 <b>No series found.</b>\n\n"
        if category:
            text += (
                f"There is no series in category "
                f"<b>{html.escape(category)}</b> yet. Create one with "
                "/newseries."
            )
        else:
            text += "Create your first series with /newseries."
        await send_message(client, chat_id, text)
        return

    total_pages = max(
        1, (len(series_list) + SERIES_PAGE_SIZE - 1) // SERIES_PAGE_SIZE
    )
    page = max(0, min(page, total_pages - 1))
    start = page * SERIES_PAGE_SIZE
    chunk = series_list[start : start + SERIES_PAGE_SIZE]

    rows = [
        [
            InlineKeyboardButton(
                _series_button_text(series),
                callback_data=f"adp:s:{series['id']}",
            )
        ]
        for series in chunk
    ]

    categories = list(settings.library_channels)
    category_index = (
        categories.index(category) if category in categories else -1
    )

    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "⬅️ Prev",
                callback_data=f"adp:l:{page - 1}:{category_index}",
            )
        )
    if page + 1 < total_pages:
        navigation.append(
            InlineKeyboardButton(
                "Next ➡️",
                callback_data=f"adp:l:{page + 1}:{category_index}",
            )
        )
    if navigation:
        rows.append(navigation)

    if categories:
        filters = [
            InlineKeyboardButton("🌐 All", callback_data="adp:l:0:-1")
        ]
        for index, name in enumerate(categories):
            filters.append(
                InlineKeyboardButton(
                    f"🗂 {name}"[:MAX_BUTTON_TEXT_LENGTH],
                    callback_data=f"adp:c:{index}:0",
                )
            )
        for index in range(0, len(filters), 2):
            rows.append(filters[index : index + 2])

    text = (
        "📺 <b>Choose a series</b>\n\n"
        f"Filter: <b>{html.escape(category) if category else 'all categories'}"
        f"</b>\n"
        f"Page {page + 1}/{total_pages} · {len(series_list)} series\n\n"
        "Tap a series to add a new episode."
    )
    await send_message(
        client, chat_id, text, reply_markup=InlineKeyboardMarkup(rows)
    )


async def _prompt_episode_label(
    client: Client,
    chat_id: int,
    draft: SeriesDraft,
    *,
    callback_data: str,
) -> None:
    draft.stage = "episode_label"
    default_label = draft.episode_label or "Episode 1"
    draft.episode_label = default_label

    if draft.mode == "new":
        heading = (
            "🎬 <b>New series</b>\n\n"
            "✅ Caption saved.\n\n"
            "<b>Step 4/5:</b> Send the label for the first episode, or keep "
            "the default."
        )
    else:
        series = draft.series or {}
        heading = (
            f"📺 <b>Adding an episode to #{series.get('id')} "
            f"{html.escape(str(series.get('title') or ''))}</b>\n\n"
            f"Current episodes: <b>{int(series.get('episode_count') or 0)}</b>\n\n"
            "<b>Step 1/2:</b> Send the episode label, or keep the suggested "
            "one."
        )

    await send_message(
        client,
        chat_id,
        f"{heading}\n\n"
        f"Suggested label: <b>{html.escape(default_label)}</b>\n\n"
        "Send a single-line label (max "
        f"{MAX_LABEL_LENGTH} characters) or tap the button below.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"✅ Use {default_label}"[:MAX_BUTTON_TEXT_LENGTH],
                        callback_data=callback_data,
                    )
                ]
            ]
        ),
    )


async def _ensure_episode_label_ready(
    client: Client, chat_id: int, draft: SeriesDraft
) -> None:
    draft.stage = "files"
    label = draft.episode_label or "Episode 1"
    if draft.mode == "new":
        step = "<b>Step 5/5:</b>"
        tail = "Send another file or /done to create the series."
    else:
        step = "<b>Step 2/2:</b>"
        tail = "Send another file or /done to append this episode."
    await send_message(
        client,
        chat_id,
        f"✅ Episode label: <b>{html.escape(label)}</b>\n\n"
        f"{step} Send one or more video/document files. Put only the "
        "quality label in each file's caption, for example <code>1080p</code> "
        "or <code>720p</code>.\n\n"
        f"{tail}",
    )


async def _queue_series_quality(
    client: Client, message: Message, draft: SeriesDraft
) -> None:
    label, error = _validate_quality_label(message)
    if error:
        await send_message(client, message.chat.id, error)
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
    draft.stage = "files"
    action = "Replaced" if replaced else "Added"

    if draft.mode == "new":
        tail = "Send another file or /done to create the series."
    else:
        tail = "Send another file or /done to append this episode."
    await send_message(
        client,
        message.chat.id,
        f"✅ {action} <b>{html.escape(label)}</b>. "
        f"{len(draft.qualities)} quality file(s) queued.\n\n{tail}",
    )


async def handle_newseries(client: Client, message: Message) -> None:
    """Start the guided /newseries flow."""
    if message.from_user is None:
        return

    links_chat = await _ensure_library_config(client, message.chat.id)
    if links_chat is None:
        return

    _clear_sessions(message.from_user.id)
    _series_sessions[message.from_user.id] = SeriesDraft(
        mode="new",
        stage="thumbnail",
        links_channel_id=links_chat.id,
    )
    await send_message(
        client,
        message.chat.id,
        "🎬 <b>New series</b>\n\n"
        "<b>Step 1/5:</b> Send the series thumbnail image (as a photo).\n\n"
        "Use /cancel at any time to abort.",
    )


async def handle_addepisode(client: Client, message: Message) -> None:
    """Show the series picker for /addepisode [category]."""
    if message.from_user is None:
        return

    if await _ensure_library_config(client, message.chat.id) is None:
        return

    _clear_sessions(message.from_user.id)
    args = message.command[1:] if message.command else []
    category = args[0].strip() if args else None
    if category and category not in settings.library_channels:
        choices = ", ".join(settings.library_channels) or "(none configured)"
        await send_message(
            client,
            message.chat.id,
            f"❌ Unknown category <b>{html.escape(category)}</b>.\n\n"
            f"Configured categories: {html.escape(choices)}\n\n"
            "Use <code>/addepisode</code> to see every series.",
        )
        return

    await _send_addepisode_list(
        client, message.chat.id, category=category
    )


async def handle_listseries(client: Client, message: Message) -> None:
    """List every series with category, episode count and channel links."""
    try:
        series_list = await database.list_series()
        pending_status = await database.count_library_post_migration_status()
    except Exception:
        logger.exception("Failed to list series")
        await send_message(
            client,
            message.chat.id,
            "❌ Could not read the series list from the database.",
        )
        return

    if not series_list:
        text = (
            "📺 <b>No series yet.</b>\n\n"
            "Create your first series with /newseries."
        )
        if pending_status["pending"]:
            text += (
                f"\n\nℹ️ <b>{pending_status['pending']}</b> legacy post(s) "
                "are waiting for /migrateposts."
            )
        await send_message(client, message.chat.id, text)
        return

    links_chat: Optional[Chat] = None
    if settings.links_channel:
        try:
            links_chat = await resolve_channel(
                client, settings.links_channel, "LINKS_CHANNEL"
            )
        except Exception:
            logger.warning(
                "Could not resolve LINKS_CHANNEL for /listseries",
                exc_info=True,
            )

    category_chats: dict[str, Optional[Chat]] = {}
    entries: list[str] = []
    for series in series_list:
        category = str(series.get("category") or "default")
        if category not in category_chats:
            try:
                category_chats[category] = await resolve_library_category(
                    client, category, settings.library_channels
                )
            except Exception:
                category_chats[category] = None
                logger.warning(
                    "Could not resolve category %r for /listseries",
                    category,
                    exc_info=True,
                )

        library_link = _message_link(
            category_chats.get(category),
            series.get("library_channel_message_id"),
        )
        links_link = _message_link(
            links_chat, series.get("links_channel_message_id")
        )

        entry = (
            f"<b>#{series['id']} · "
            f"{html.escape(str(series.get('title') or 'Untitled'))}</b>\n"
            f"🗂 {html.escape(category)} · "
            f"🎞 {int(series.get('episode_count') or 0)} episode(s) · "
            f"🔗 {int(series.get('link_count') or 0)} link(s)"
        )
        if library_link:
            entry += f"\n📚 {html.escape(library_link)}"
        if links_link:
            entry += f"\n🔗 {html.escape(links_link)}"
        entries.append(entry)

    header = (
        f"📺 <b>Series library</b> — {len(series_list)} series\n\n"
    )
    chunk = header
    for entry in entries:
        if len(chunk) + len(entry) + 2 > 3800:
            await send_message(
                client,
                message.chat.id,
                chunk,
                disable_web_page_preview=True,
            )
            chunk = ""
        chunk += entry + "\n\n"

    if pending_status["pending"]:
        chunk += (
            f"ℹ️ <b>{pending_status['pending']}</b> legacy post(s) are still "
            "waiting for /migrateposts."
        )
    await send_message(
        client, message.chat.id, chunk, disable_web_page_preview=True
    )


async def _handle_series_message(
    client: Client, message: Message, draft: SeriesDraft
) -> None:
    command = _command_name(message)
    if command == "done":
        if draft.mode == "new":
            await _finish_newseries(client, message, draft)
        else:
            await _finish_addepisode(client, message, draft)
        return
    if command is not None:
        tail = "Use /done to finish or /cancel to abort."
        await send_message(
            client, message.chat.id, tail
        )
        return

    if draft.mode == "new":
        if draft.stage == "thumbnail":
            if not message.photo:
                await send_message(
                    client,
                    message.chat.id,
                    "📷 <b>Step 1/5:</b> Send the series thumbnail as a photo.",
                )
                return
            draft.thumbnail_file_id = message.photo.file_id
            draft.stage = "caption"
            await send_message(
                client,
                message.chat.id,
                "✅ Thumbnail saved.\n\n"
                "<b>Step 2/5:</b> Send the series caption. Multiple lines and "
                "Markdown formatting (<code>**bold**</code>, "
                "<code>__italic__</code>) are supported.\n\n"
                "The first line is also used as the Links Channel title.",
            )
            return

        if draft.stage == "caption":
            if not message.text:
                await send_message(
                    client,
                    message.chat.id,
                    "📝 <b>Step 2/5:</b> Send the series caption as text.",
                )
                return
            caption = message.text.strip()
            if not caption:
                await send_message(
                    client, message.chat.id, "❌ The caption cannot be empty."
                )
                return
            if len(caption) > MAX_CAPTION_LENGTH:
                await send_message(
                    client,
                    message.chat.id,
                    f"❌ The caption is too long ({len(caption)} characters). "
                    f"Telegram allows at most {MAX_CAPTION_LENGTH}.",
                )
                return
            if not settings.library_channels:
                await send_message(
                    client,
                    message.chat.id,
                    "❌ <b>No library categories are configured.</b>\n\n"
                    "Set <code>LIBRARY_CHANNELS=category:@channel,...</code> "
                    "in <code>.env</code> and restart the bot.",
                )
                _series_sessions.pop(message.from_user.id, None)
                return
            draft.caption = caption
            draft.title = _derive_title(caption)
            draft.stage = "category"
            await send_message(
                client,
                message.chat.id,
                "✅ Caption saved.\n\n"
                "<b>Step 3/5:</b> Choose the library category for this "
                "series.",
                reply_markup=_category_keyboard("newseries"),
            )
            return

        if draft.stage == "category":
            await send_message(
                client,
                message.chat.id,
                "🗂 <b>Step 3/5:</b> Choose a library category using the "
                "buttons below.",
                reply_markup=_category_keyboard("newseries"),
            )
            return

    if draft.stage == "episode_label":
        if message.text:
            label, error = _validate_episode_label(message.text)
            if error:
                await send_message(client, message.chat.id, error)
                return
            draft.episode_label = label
            await _ensure_episode_label_ready(
                client, message.chat.id, draft
            )
            return
        if message.video or message.document:
            draft.stage = "files"
            await _queue_series_quality(client, message, draft)
            return
        await send_message(
            client,
            message.chat.id,
            "Send the episode label as text, a quality file, or tap the "
            "suggested-label button above.",
        )
        return

    if draft.stage == "files":
        if message.video or message.document:
            await _queue_series_quality(client, message, draft)
            return
        await send_message(
            client,
            message.chat.id,
            "Send a video/document with only the quality label as its caption, "
            "or /done to finish.",
        )
        return

    await send_message(
        client,
        message.chat.id,
        "Follow the step shown above, or use /cancel to abort.",
    )


async def _select_newseries_category(
    client: Client, query: CallbackQuery, category: str
) -> None:
    if query.from_user is None:
        return

    chat_id = query.message.chat.id if query.message else query.from_user.id
    draft = _series_sessions.get(query.from_user.id)
    if (
        draft is None
        or draft.mode != "new"
        or draft.stage != "category"
    ):
        await query.answer(
            "This series category picker is no longer active.",
            show_alert=True,
        )
        return

    library_chat = await _resolve_category_or_report(client, chat_id, category)
    if library_chat is None:
        await query.answer(
            f"Category {category!r} is unavailable.", show_alert=True
        )
        return

    draft.category = category
    await query.answer(f"Selected {category}")
    if query.message is not None:
        await _delete_status(client, chat_id, query.message)

    await _prompt_episode_label(
        client,
        chat_id,
        draft,
        callback_data="seriesnew:def",
    )


async def _finish_newseries(
    client: Client, message: Message, draft: SeriesDraft
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
    if not draft.category or not draft.thumbnail_file_id or not draft.caption:
        await send_message(
            client,
            message.chat.id,
            "❌ The series draft is incomplete. Use /cancel and start again.",
        )
        return

    label = draft.episode_label or "Episode 1"
    library_chat = await _resolve_category_or_report(
        client, message.chat.id, draft.category
    )
    if library_chat is None:
        draft.stage = "category"
        await send_message(
            client,
            message.chat.id,
            "Choose another category to continue, or /cancel to abort.",
            reply_markup=_category_keyboard("newseries"),
        )
        return

    links_chat = await _ensure_library_config(client, message.chat.id)
    if links_chat is None:
        return

    quality_drafts = list(draft.qualities.values())
    if len(quality_drafts) > MAX_LINKS_BUTTONS:
        await send_message(
            client,
            message.chat.id,
            "⚠️ <b>Too many quality files for one Links Channel post.</b>\n\n"
            f"This episode has <b>{len(quality_drafts)}</b> quality files; "
            f"the safe limit is {MAX_LINKS_BUTTONS} buttons per post.\n\n"
            "Nothing was changed. Split the qualities across multiple "
            "episodes or series.",
        )
        return

    _series_sessions.pop(message.from_user.id, None)
    chat_id = message.chat.id
    title = draft.title or _derive_title(draft.caption)
    status = await send_message(
        client,
        chat_id,
        f"⏳ Storing {len(quality_drafts)} file(s) and creating the series...",
    )

    links_post: Optional[Message] = None
    library_post: Optional[Message] = None
    stored_files: list[StoredFile] = []

    try:
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

        preview = {
            "title": title,
            "episodes": [
                {
                    "episode_label": label,
                    "links": [
                        {
                            "quality_label": quality.label,
                            "short_url": stored.short_link,
                            "direct_link": stored.direct_link,
                        }
                        for quality, stored in zip(
                            quality_drafts, stored_files
                        )
                    ],
                }
            ],
        }
        links_post = await send_message(
            client,
            links_chat.id,
            _series_links_text(preview),
            reply_markup=_series_links_keyboard(preview),
        )
        if links_post is None:
            raise RuntimeError(
                "Could not post to LINKS_CHANNEL. Check the bot's admin "
                "rights and the series title."
            )

        library_post = await send_photo(
            client,
            library_chat.id,
            draft.thumbnail_file_id,
            caption=draft.caption,
            parse_mode=enums.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📥 Download Links", url=links_post.link
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

        series_id, _episode_id = await database.add_series_with_episode(
            title=title,
            category=draft.category,
            thumbnail_file_id=draft.thumbnail_file_id,
            caption=draft.caption,
            library_channel_message_id=library_post.id,
            links_channel_message_id=links_post.id,
            episode_label=label,
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
        logger.exception("Failed to create series")
        if library_post is not None:
            await delete_messages(
                client, library_post.chat.id, [library_post.id]
            )
        if links_post is not None:
            await delete_messages(client, links_post.chat.id, [links_post.id])
        await send_message(
            client,
            chat_id,
            "❌ <b>Series creation failed.</b>\n\n"
            f"{html.escape(str(exc))}\n\n"
            "No series was saved. Fix the issue and run /newseries again.",
        )
        await _delete_status(client, chat_id, status)
        return

    quality_labels = ", ".join(
        html.escape(quality.label) for quality in quality_drafts
    )
    text = (
        f"✅ <b>Series #{series_id} created</b>\n\n"
        f"🎬 <b>Title:</b> {html.escape(title)}\n"
        f"🗂 <b>Category:</b> {html.escape(draft.category)} "
        f"({html.escape(_chat_label(library_chat))})\n"
        f"🎞 <b>Episode:</b> {html.escape(label)} — {quality_labels}\n\n"
        f"🔗 <b>Links channel post:</b>\n"
        f"{html.escape(links_post.link)}\n\n"
        f"📚 <b>Library channel post:</b>\n"
        f"{html.escape(library_post.link)}\n\n"
        "Use /addepisode to append more episodes."
    )
    if any(stored.short_link is None for stored in stored_files):
        text += (
            "\n\n⚠️ Some short links could not be generated; the direct "
            "Telegram links were used for those buttons."
        )
    await send_message(client, chat_id, text, disable_web_page_preview=True)
    await _delete_status(client, chat_id, status)
    logger.info(
        "Created series #%s with episode %r (%s qualities) for admin %s",
        series_id,
        label,
        len(stored_files),
        message.from_user.id,
    )


async def _finish_addepisode(
    client: Client, message: Message, draft: SeriesDraft
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
    if draft.series_id is None:
        await send_message(
            client,
            message.chat.id,
            "❌ The series draft is incomplete. Use /cancel and start again.",
        )
        return

    series = await database.get_series(draft.series_id)
    if series is None:
        await send_message(
            client,
            message.chat.id,
            f"❌ Series <code>{draft.series_id}</code> no longer exists.",
        )
        _series_sessions.pop(message.from_user.id, None)
        return

    quality_drafts = list(draft.qualities.values())
    label = draft.episode_label or _next_episode_label(series)
    current_buttons = int(series.get("link_count") or 0)
    if current_buttons + len(quality_drafts) > MAX_LINKS_BUTTONS:
        await send_message(
            client,
            message.chat.id,
            _button_limit_text(series, len(quality_drafts)),
        )
        return

    links_chat = await _ensure_library_config(client, message.chat.id)
    if links_chat is None:
        return

    _series_sessions.pop(message.from_user.id, None)
    chat_id = message.chat.id
    status = await send_message(
        client,
        chat_id,
        f"⏳ Storing {len(quality_drafts)} file(s) and appending "
        f"<b>{html.escape(label)}</b>...",
    )

    stored_files: list[StoredFile] = []
    episode_id: Optional[int] = None

    try:
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

        episode_id = await database.add_episode(
            draft.series_id,
            label,
            [
                {
                    "label": quality.label,
                    "direct_link": stored.direct_link,
                    "short_link": stored.short_link,
                }
                for quality, stored in zip(quality_drafts, stored_files)
            ],
        )

        refreshed = await database.get_series(draft.series_id)
        if refreshed is None:
            raise RuntimeError("The series disappeared while appending.")

        if int(refreshed.get("link_count") or 0) > MAX_LINKS_BUTTONS:
            await database.delete_episode(episode_id)
            episode_id = None
            await send_message(
                client,
                chat_id,
                _button_limit_text(refreshed, 0),
            )
            await _delete_status(client, chat_id, status)
            return

        links_message_id = refreshed.get("links_channel_message_id")
        if links_message_id is None:
            raise RuntimeError(
                "The series has no stored Links Channel message ID."
            )

        edited = await edit_message_reply_markup(
            client,
            links_chat.id,
            int(links_message_id),
            _series_links_keyboard(refreshed),
        )
        if edited is None:
            raise RuntimeError(
                "Could not edit the Links Channel message. Make sure the bot "
                "is still an administrator there and the message was not "
                "deleted."
            )
    except Exception as exc:
        logger.exception("Failed to append series episode")
        if episode_id is not None:
            try:
                await database.delete_episode(episode_id)
            except Exception:
                logger.exception("Failed to roll back appended episode")
        await send_message(
            client,
            chat_id,
            "❌ <b>Episode append failed.</b>\n\n"
            f"{html.escape(str(exc))}\n\n"
            "The database change was rolled back; the Links Channel message "
            "was not changed.",
        )
        await _delete_status(client, chat_id, status)
        return

    quality_labels = ", ".join(
        html.escape(quality.label) for quality in quality_drafts
    )
    links_link = _message_link(links_chat, links_message_id)
    text = (
        f"✅ <b>Episode appended to series #{draft.series_id}</b>\n\n"
        f"🎬 <b>Series:</b> {html.escape(str(series.get('title') or ''))}\n"
        f"🎞 <b>Episode:</b> {html.escape(label)} — {quality_labels}\n"
        f"🔗 <b>Total quality buttons:</b> "
        f"{int(refreshed.get('link_count') or 0)}/{MAX_LINKS_BUTTONS}\n\n"
        "The Library Channel post was left untouched."
    )
    if links_link:
        text += f"\n\n🔗 {html.escape(links_link)}"
    if any(stored.short_link is None for stored in stored_files):
        text += (
            "\n\n⚠️ Some short links could not be generated; the direct "
            "Telegram links were used for those buttons."
        )
    await send_message(client, chat_id, text, disable_web_page_preview=True)
    await _delete_status(client, chat_id, status)
    logger.info(
        "Appended episode %r (%s qualities) to series #%s for admin %s",
        label,
        len(stored_files),
        draft.series_id,
        message.from_user.id,
    )


async def _start_addepisode_for_series(
    client: Client, query: CallbackQuery, series_id: int
) -> None:
    if query.from_user is None:
        return

    chat_id = query.message.chat.id if query.message else query.from_user.id
    series = await database.get_series(series_id)
    if series is None:
        await query.answer("That series no longer exists.", show_alert=True)
        return

    _clear_sessions(query.from_user.id)
    draft = SeriesDraft(
        mode="add",
        stage="episode_label",
        series_id=series_id,
        series=series,
        category=str(series.get("category") or "default"),
        episode_label=_next_episode_label(series),
    )
    _series_sessions[query.from_user.id] = draft

    await query.answer(f"Selected #{series_id}")
    if query.message is not None:
        await _delete_status(client, chat_id, query.message)

    await _prompt_episode_label(
        client, chat_id, draft, callback_data="adp:def"
    )


async def handle_series_action_callback(
    client: Client, query: CallbackQuery
) -> None:
    """Handle /newseries and /addepisode inline buttons."""
    data = query.data or ""
    if not data.startswith(("seriesnew:", "adp:")):
        return
    if query.from_user is None:
        await query.answer()
        return

    chat_id = query.message.chat.id if query.message else query.from_user.id
    user_id = query.from_user.id

    if data == "seriesnew:def" or data == "adp:def":
        draft = _series_sessions.get(user_id)
        if draft is None or draft.stage != "episode_label":
            await query.answer(
                "This draft is no longer active.", show_alert=True
            )
            return
        await query.answer("Using the suggested label")
        if query.message is not None:
            await _delete_status(client, chat_id, query.message)
        await _ensure_episode_label_ready(client, chat_id, draft)
        return

    parts = data.split(":")
    if parts[0] == "adp" and len(parts) >= 2:
        action = parts[1]
        if action == "s" and len(parts) == 3:
            try:
                series_id = int(parts[2])
            except ValueError:
                await query.answer(
                    "Invalid series selection.", show_alert=True
                )
                return
            await _start_addepisode_for_series(client, query, series_id)
            return

        if action == "l" and len(parts) in (3, 4):
            try:
                page = int(parts[2])
                category_index = int(parts[3]) if len(parts) == 4 else -1
            except ValueError:
                await query.answer("Invalid page.", show_alert=True)
                return
            category: Optional[str] = None
            if category_index >= 0:
                categories = list(settings.library_channels)
                if category_index >= len(categories):
                    await query.answer(
                        "That category is no longer configured.",
                        show_alert=True,
                    )
                    return
                category = categories[category_index]
            await query.answer()
            if query.message is not None:
                await _delete_status(client, chat_id, query.message)
            await _send_addepisode_list(
                client, chat_id, page=page, category=category
            )
            return

        if action == "c" and len(parts) == 4:
            try:
                index = int(parts[2])
                page = int(parts[3])
            except ValueError:
                await query.answer(
                    "Invalid category selection.", show_alert=True
                )
                return
            categories = list(settings.library_channels)
            if not 0 <= index < len(categories):
                await query.answer(
                    "That category is no longer configured.",
                    show_alert=True,
                )
                return
            category = categories[index]
            await query.answer(f"Filtering by {category}")
            if query.message is not None:
                await _delete_status(client, chat_id, query.message)
            await _send_addepisode_list(
                client, chat_id, page=page, category=category
            )
            return

    await query.answer()


# ---------------------------------------------------------------------------
# One-time legacy migration (/migrateposts)
# ---------------------------------------------------------------------------


async def _send_migration_status(client: Client, chat_id: int) -> None:
    try:
        status = await database.count_library_post_migration_status()
        series_count = await database.count_series()
    except Exception:
        logger.exception("Failed to read migration status")
        await send_message(
            client,
            chat_id,
            "❌ Could not read migration status from the database.",
        )
        return
    text = (
        "📦 <b>Legacy post migration</b>\n\n"
        f"🗂 <b>Total legacy posts:</b> <code>{status['total']}</code>\n"
        f"✅ <b>Migrated:</b> <code>{status['migrated']}</code>\n"
        f"🕓 <b>Pending:</b> <code>{status['pending']}</code>\n"
        f"📺 <b>Series rows:</b> <code>{series_count}</code>"
    )
    if status["pending"]:
        text += "\n\nRun /migrateposts to continue the interactive flow."
    else:
        text += "\n\n🎉 Every legacy post has been migrated."
    await send_message(client, chat_id, text)


async def _present_migration_post(
    client: Client, chat_id: int, user_id: int, post: dict
) -> None:
    full = await database.get_library_post(int(post["id"]))
    if full is None:
        await _send_migration_status(client, chat_id)
        return

    if int(full.get("migrated") or 0):
        next_post = await database.get_next_unmigrated_post(int(full["id"]))
        if next_post is None:
            await _send_migration_status(client, chat_id)
        else:
            await _present_migration_post(
                client, chat_id, user_id, next_post
            )
        return

    _migrate_sessions[user_id] = MigrateDraft(post=full)

    if full.get("thumbnail_file_id"):
        thumbnail = await send_photo(
            client,
            chat_id,
            full["thumbnail_file_id"],
            caption=f"🖼 Legacy post #{full['id']} thumbnail",
        )
        if thumbnail is None:
            logger.warning(
                "Could not send thumbnail for legacy post #%s", full["id"]
            )

    qualities = ", ".join(
        f"<code>{html.escape(str(quality.get('label') or '?'))}</code>"
        for quality in full.get("qualities", [])
    ) or "<i>none</i>"

    text = (
        f"🗂 <b>Legacy post #{full['id']}</b>\n\n"
        f"🎬 <b>Title:</b> "
        f"{html.escape(str(full.get('title') or 'Untitled'))}\n"
        f"🗂 <b>Category:</b> "
        f"{html.escape(str(full.get('category') or 'default'))}\n"
        f"🎞 <b>Qualities:</b> {qualities}\n\n"
    )
    if full.get("library_message_link"):
        text += (
            f"📚 <b>Library message:</b>\n"
            f"{html.escape(str(full['library_message_link']))}\n"
        )
    if full.get("links_message_link"):
        text += (
            f"🔗 <b>Links message:</b>\n"
            f"{html.escape(str(full['links_message_link']))}\n"
        )
    text += (
        "\nChoose how to migrate this post. The Library Channel post is "
        "never reposted."
    )
    if not full.get("qualities"):
        text += (
            "\n\n⚠️ This post has no quality rows, so it cannot be migrated "
            "automatically."
        )

    await send_message(
        client,
        chat_id,
        text,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "➕ New Series",
                        callback_data=f"mig:new:{full['id']}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📎 Add to Existing Series",
                        callback_data=f"mig:add:{full['id']}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⏭ Skip for now",
                        callback_data=f"mig:skip:{full['id']}",
                    )
                ],
            ]
        ),
        disable_web_page_preview=True,
    )


async def _send_next_migration(
    client: Client, chat_id: int, user_id: int, after_id: int
) -> None:
    _migrate_sessions.pop(user_id, None)
    next_post = await database.get_next_unmigrated_post(after_id)
    if next_post is None:
        await _send_migration_status(client, chat_id)
        return
    await _present_migration_post(client, chat_id, user_id, next_post)


async def _send_migration_series_picker(
    client: Client, chat_id: int, post_id: int, page: int = 0
) -> None:
    series_list = await database.list_series()
    if not series_list:
        await send_message(
            client,
            chat_id,
            "📎 <b>No series exist yet.</b>\n\n"
            "Start with <b>➕ New Series</b> for this post, then use "
            "/addepisode for later ones.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "➕ New Series",
                            callback_data=f"mig:new:{post_id}",
                        )
                    ]
                ]
            ),
        )
        return

    total_pages = max(
        1, (len(series_list) + SERIES_PAGE_SIZE - 1) // SERIES_PAGE_SIZE
    )
    page = max(0, min(page, total_pages - 1))
    start = page * SERIES_PAGE_SIZE
    chunk = series_list[start : start + SERIES_PAGE_SIZE]

    rows = [
        [
            InlineKeyboardButton(
                _series_button_text(series),
                callback_data=f"mig:pick:{post_id}:{series['id']}",
            )
        ]
        for series in chunk
    ]
    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "⬅️ Prev", callback_data=f"mig:page:{post_id}:{page - 1}"
            )
        )
    if page + 1 < total_pages:
        navigation.append(
            InlineKeyboardButton(
                "Next ➡️",
                callback_data=f"mig:page:{post_id}:{page + 1}",
            )
        )
    if navigation:
        rows.append(navigation)

    await send_message(
        client,
        chat_id,
        f"📎 <b>Add legacy post #{post_id} to a series</b>\n\n"
        f"Page {page + 1}/{total_pages}. Choose the target series:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _send_migration_label_confirm(
    client: Client, chat_id: int, draft: MigrateDraft
) -> None:
    if draft.series_id is None:
        return
    series = await database.get_series(draft.series_id)
    if series is None:
        await send_message(
            client,
            chat_id,
            "❌ That series no longer exists. Choose another one.",
        )
        return

    label = draft.episode_label or _default_label_from_post(draft.post)
    draft.episode_label = label
    draft.stage = "label_confirm"
    qualities = ", ".join(
        html.escape(str(quality.get("label") or "?"))
        for quality in draft.post.get("qualities", [])
    ) or "(none)"

    await send_message(
        client,
        chat_id,
        f"📎 <b>Merge legacy post #{draft.post['id']}</b>\n\n"
        f"📺 <b>Series:</b> #{series['id']} "
        f"{html.escape(str(series.get('title') or ''))}\n"
        f"🗂 <b>Category:</b> "
        f"{html.escape(str(series.get('category') or 'default'))} · "
        f"🎞 {int(series.get('episode_count') or 0)} episode(s)\n\n"
        f"🏷 <b>New episode label:</b> {html.escape(label)}\n"
        f"🎞 <b>Qualities:</b> {qualities}\n\n"
        "Appending edits the series' Links Channel message only. The old "
        "standalone Library Channel message is kept until you confirm its "
        "deletion afterwards.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"✅ Append as {label}"[:MAX_BUTTON_TEXT_LENGTH],
                        callback_data=f"mig:confirm:{draft.post['id']}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "✏️ Edit label",
                        callback_data=f"mig:editlabel:{draft.post['id']}",
                    ),
                    InlineKeyboardButton(
                        "📎 Choose another series",
                        callback_data=f"mig:add:{draft.post['id']}",
                    ),
                ],
            ]
        ),
        disable_web_page_preview=True,
    )


async def _migration_new_series(
    client: Client, query: CallbackQuery, post_id: int
) -> None:
    if query.from_user is None:
        return
    user_id = query.from_user.id
    chat_id = query.message.chat.id if query.message else user_id
    draft = _migrate_sessions.get(user_id)
    if draft is None or int(draft.post["id"]) != post_id:
        await query.answer(
            "This migration step is no longer active.", show_alert=True
        )
        return

    post = await database.get_library_post(post_id)
    if post is None or int(post.get("migrated") or 0):
        await query.answer("This post is already migrated.", show_alert=True)
        return

    quality_count = len(post.get("qualities", []))
    if quality_count == 0:
        await query.answer(
            "This post has no quality links to migrate.", show_alert=True
        )
        return
    if quality_count > MAX_LINKS_BUTTONS:
        await query.answer(
            "The post has too many quality buttons to migrate.",
            show_alert=True,
        )
        return

    series_id: Optional[int] = None
    message_edited = False
    try:
        series_id = await database.create_series_from_library_post(post_id)
        if series_id is None:
            raise RuntimeError("The legacy post could not be read.")

        series = await database.get_series(series_id)
        if series is None:
            raise RuntimeError("The migrated series could not be read.")

        edited = await edit_message_reply_markup(
            client,
            int(post["links_channel_id"]),
            int(post["links_message_id"]),
            _series_links_keyboard(series),
        )
        if edited is None:
            raise RuntimeError(
                "Could not edit the existing Links Channel message. It may "
                "have been deleted, or the bot may have lost admin rights."
            )
        message_edited = True

        marked = await database.set_library_post_migrated(post_id, True)
        if not marked:
            raise RuntimeError(
                "The legacy post disappeared before it could be marked "
                "migrated."
            )
    except Exception as exc:
        logger.exception("Failed to migrate legacy post #%s", post_id)
        if series_id is not None:
            try:
                await database.delete_series(series_id)
            except Exception:
                logger.exception("Failed to roll back migrated series")
        if message_edited:
            try:
                await edit_message_reply_markup(
                    client,
                    int(post["links_channel_id"]),
                    int(post["links_message_id"]),
                    _quality_keyboard(
                        [
                            (quality["label"], quality["button_url"])
                            for quality in post.get("qualities", [])
                        ]
                    ),
                )
            except Exception:
                logger.exception("Failed to restore legacy links keyboard")
        await query.answer("Migration failed.", show_alert=True)
        await send_message(
            client,
            chat_id,
            f"❌ <b>Migration of post #{post_id} failed.</b>\n\n"
            f"{html.escape(str(exc))}\n\n"
            "No series was kept and the links message was restored. Nothing "
            "was marked as migrated.",
        )
        return

    await query.answer("Migrated")
    text = (
        f"✅ <b>Legacy post #{post_id} → series #{series_id}</b>\n\n"
        "Episode <b>1</b> reuses the old quality links. The existing Library "
        "Channel message was kept (not reposted) and the Links Channel message "
        "was reformatted with episode rows."
    )
    if post.get("library_message_link"):
        text += (
            f"\n\n📚 {html.escape(str(post['library_message_link']))}"
        )
    if post.get("links_message_link"):
        text += f"\n🔗 {html.escape(str(post['links_message_link']))}"
    await send_message(
        client, chat_id, text, disable_web_page_preview=True
    )
    logger.info(
        "Migrated legacy post #%s into series #%s (new series)",
        post_id,
        series_id,
    )
    await _send_next_migration(client, chat_id, user_id, 0)


async def _migration_append(
    client: Client, query: CallbackQuery, post_id: int
) -> None:
    if query.from_user is None:
        return
    user_id = query.from_user.id
    chat_id = query.message.chat.id if query.message else user_id
    draft = _migrate_sessions.get(user_id)
    if (
        draft is None
        or int(draft.post["id"]) != post_id
        or draft.series_id is None
    ):
        await query.answer(
            "This migration step is no longer active.", show_alert=True
        )
        return

    post = await database.get_library_post(post_id)
    if post is None or int(post.get("migrated") or 0):
        await query.answer("This post is already migrated.", show_alert=True)
        return
    if not post.get("qualities"):
        await query.answer(
            "This post has no quality links to append.", show_alert=True
        )
        return

    series = await database.get_series(draft.series_id)
    if series is None:
        await query.answer("That series no longer exists.", show_alert=True)
        return

    label = draft.episode_label or _default_label_from_post(post)
    extra = len(post["qualities"])
    if int(series.get("link_count") or 0) + extra > MAX_LINKS_BUTTONS:
        await query.answer("Button limit reached.", show_alert=True)
        await send_message(
            client,
            chat_id,
            _button_limit_text(series, extra),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📎 Choose another series",
                            callback_data=f"mig:add:{post_id}",
                        )
                    ]
                ]
            ),
        )
        return

    links_chat = await _ensure_library_config(client, chat_id)
    if links_chat is None:
        return

    episode_id: Optional[int] = None
    message_id = series.get("links_channel_message_id")
    message_edited = False
    try:
        if message_id is None:
            raise RuntimeError(
                "The series has no stored Links Channel message ID."
            )

        episode_id = await database.append_library_post_to_series(
            post_id, draft.series_id, label
        )
        if episode_id is None:
            raise RuntimeError("The episode could not be created.")

        refreshed = await database.get_series(draft.series_id)
        if refreshed is None:
            raise RuntimeError("The series disappeared while appending.")

        edited = await edit_message_reply_markup(
            client,
            links_chat.id,
            int(message_id),
            _series_links_keyboard(refreshed),
        )
        if edited is None:
            raise RuntimeError(
                "Could not edit the series Links Channel message. It may have "
                "been deleted, or the bot may have lost admin rights."
            )
        message_edited = True

        marked = await database.set_library_post_migrated(post_id, True)
        if not marked:
            raise RuntimeError(
                "The legacy post disappeared before it could be marked "
                "migrated."
            )
    except Exception as exc:
        logger.exception(
            "Failed to append legacy post #%s to series #%s",
            post_id,
            draft.series_id,
        )
        if episode_id is not None:
            try:
                await database.delete_episode(episode_id)
            except Exception:
                logger.exception("Failed to roll back merged episode")
        if message_edited and message_id is not None:
            try:
                await edit_message_reply_markup(
                    client,
                    links_chat.id,
                    int(message_id),
                    _series_links_keyboard(series),
                )
            except Exception:
                logger.exception("Failed to restore series keyboard")
        await query.answer("Append failed.", show_alert=True)
        await send_message(
            client,
            chat_id,
            f"❌ <b>Merging post #{post_id} failed.</b>\n\n"
            f"{html.escape(str(exc))}\n\n"
            "The episode was rolled back and the post remains pending.",
        )
        return

    await query.answer("Merged")
    draft.stage = "await_delete"
    text = (
        f"✅ <b>Legacy post #{post_id} merged into series "
        f"#{draft.series_id}</b>\n\n"
        f"🏷 Episode: <b>{html.escape(label)}</b> · "
        f"{extra} quality link(s)\n\n"
        "The old standalone Library Channel post is now redundant. Delete it?"
    )
    if post.get("library_message_link"):
        text += (
            f"\n\n📚 {html.escape(str(post['library_message_link']))}"
        )
    await send_message(
        client,
        chat_id,
        text,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🗑 Delete old library post",
                        callback_data=f"mig:delete:{post_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📌 Keep it",
                        callback_data=f"mig:keep:{post_id}",
                    )
                ],
            ]
        ),
        disable_web_page_preview=True,
    )
    logger.info(
        "Merged legacy post #%s into series #%s as episode %r",
        post_id,
        draft.series_id,
        label,
    )


async def _migration_delete_old_post(
    client: Client, query: CallbackQuery, post_id: int
) -> None:
    if query.from_user is None:
        return
    user_id = query.from_user.id
    chat_id = query.message.chat.id if query.message else user_id
    draft = _migrate_sessions.get(user_id)
    if (
        draft is None
        or int(draft.post["id"]) != post_id
        or draft.stage != "await_delete"
    ):
        await query.answer(
            "This deletion prompt is no longer active.", show_alert=True
        )
        return

    post = await database.get_library_post(post_id) or draft.post
    deleted = await delete_messages(
        client,
        int(post["library_channel_id"]),
        [int(post["library_message_id"])],
    )
    await query.answer("Deleted" if deleted else "Delete failed")

    if deleted:
        text = f"🗑 Deleted the old standalone library message for post #{post_id}."
    else:
        text = (
            f"⚠️ Could not delete the old library message for post #{post_id}. "
            "Remove it manually if it is still visible."
        )
        if post.get("library_message_link"):
            text += (
                f"\n\n📚 {html.escape(str(post['library_message_link']))}"
            )
    await send_message(
        client, chat_id, text, disable_web_page_preview=True
    )
    await _send_next_migration(client, chat_id, user_id, post_id)


async def _migration_keep_old_post(
    client: Client, query: CallbackQuery, post_id: int
) -> None:
    if query.from_user is None:
        return
    user_id = query.from_user.id
    chat_id = query.message.chat.id if query.message else user_id
    draft = _migrate_sessions.get(user_id)
    if (
        draft is None
        or int(draft.post["id"]) != post_id
        or draft.stage != "await_delete"
    ):
        await query.answer(
            "This deletion prompt is no longer active.", show_alert=True
        )
        return

    await query.answer("Kept")
    text = (
        f"📌 Kept the old library message for post #{post_id}. "
        "It will not be migrated again."
    )
    if draft.post.get("library_message_link"):
        text += (
            f"\n\n📚 {html.escape(str(draft.post['library_message_link']))}"
        )
    await send_message(
        client, chat_id, text, disable_web_page_preview=True
    )
    await _send_next_migration(client, chat_id, user_id, post_id)


async def handle_migration_callback(
    client: Client, query: CallbackQuery
) -> None:
    """Handle every inline button of the /migrateposts flow."""
    data = query.data or ""
    if not data.startswith("mig:"):
        return
    if query.from_user is None:
        await query.answer()
        return

    user_id = query.from_user.id
    chat_id = query.message.chat.id if query.message else user_id
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    try:
        if action == "new" and len(parts) == 3:
            await _migration_new_series(client, query, int(parts[2]))
            return
        if action == "add" and len(parts) == 3:
            post_id = int(parts[2])
            await query.answer()
            if query.message is not None:
                await _delete_status(client, chat_id, query.message)
            await _send_migration_series_picker(
                client, chat_id, post_id, page=0
            )
            return
        if action == "page" and len(parts) == 4:
            post_id = int(parts[2])
            page = int(parts[3])
            await query.answer()
            if query.message is not None:
                await _delete_status(client, chat_id, query.message)
            await _send_migration_series_picker(
                client, chat_id, post_id, page=page
            )
            return
        if action == "pick" and len(parts) == 4:
            post_id = int(parts[2])
            series_id = int(parts[3])
            draft = _migrate_sessions.get(user_id)
            if draft is None or int(draft.post["id"]) != post_id:
                await query.answer(
                    "This migration step is no longer active.",
                    show_alert=True,
                )
                return
            draft.series_id = series_id
            draft.episode_label = _default_label_from_post(draft.post)
            await query.answer("Series selected")
            if query.message is not None:
                await _delete_status(client, chat_id, query.message)
            await _send_migration_label_confirm(client, chat_id, draft)
            return
        if action == "editlabel" and len(parts) == 3:
            post_id = int(parts[2])
            draft = _migrate_sessions.get(user_id)
            if draft is None or int(draft.post["id"]) != post_id:
                await query.answer(
                    "This migration step is no longer active.",
                    show_alert=True,
                )
                return
            draft.stage = "edit_label"
            await query.answer()
            await send_message(
                client,
                chat_id,
                f"✏️ Send the new episode label for post #{post_id} "
                f"(single line, max {MAX_LABEL_LENGTH} characters).",
            )
            return
        if action == "confirm" and len(parts) == 3:
            await _migration_append(client, query, int(parts[2]))
            return
        if action == "delete" and len(parts) == 3:
            await _migration_delete_old_post(client, query, int(parts[2]))
            return
        if action == "keep" and len(parts) == 3:
            await _migration_keep_old_post(client, query, int(parts[2]))
            return
        if action == "skip" and len(parts) == 3:
            post_id = int(parts[2])
            _migrate_sessions.pop(user_id, None)
            await query.answer("Skipped")
            await send_message(
                client,
                chat_id,
                f"⏭ Skipped legacy post #{post_id}. It remains pending for a "
                "later /migrateposts run.",
            )
            await _send_next_migration(client, chat_id, user_id, post_id)
            return
    except (ValueError, IndexError):
        await query.answer("Invalid migration action.", show_alert=True)
        return

    await query.answer()


async def _handle_migrate_message(
    client: Client, message: Message, draft: MigrateDraft
) -> None:
    command = _command_name(message)
    if command is not None:
        await send_message(
            client,
            message.chat.id,
            "Use the buttons above, or /cancel to abort the migration.",
        )
        return

    if draft.stage != "edit_label":
        await send_message(
            client,
            message.chat.id,
            "Use the buttons above to choose how to migrate this post.",
        )
        return

    if not message.text:
        await send_message(
            client, message.chat.id, "Send the episode label as text."
        )
        return
    label, error = _validate_episode_label(message.text)
    if error:
        await send_message(client, message.chat.id, error)
        return

    draft.episode_label = label
    await _send_migration_label_confirm(client, message.chat.id, draft)


async def handle_migrateposts(client: Client, message: Message) -> None:
    """Start or report the one-time legacy post migration."""
    if message.from_user is None:
        return

    args = message.command[1:] if message.command else []
    if args and args[0].casefold() == "status":
        await _send_migration_status(client, message.chat.id)
        return

    if await _ensure_library_config(client, message.chat.id) is None:
        return

    _clear_sessions(message.from_user.id)

    after_id = 0
    if args:
        try:
            start_id = int(args[0])
        except ValueError:
            await send_message(
                client,
                message.chat.id,
                "Usage:\n"
                "• <code>/migrateposts</code> — start with the oldest pending "
                "post\n"
                "• <code>/migrateposts &lt;post_id&gt;</code> — start at a "
                "specific post\n"
                "• <code>/migrateposts status</code> — show progress",
            )
            return
        after_id = max(0, start_id - 1)

    post = await database.get_next_unmigrated_post(after_id)
    if post is None:
        if after_id:
            await send_message(
                client,
                message.chat.id,
                f"ℹ️ No pending posts at or after #{after_id + 1}.",
            )
        await _send_migration_status(client, message.chat.id)
        return

    await _present_migration_post(
        client, message.chat.id, message.from_user.id, post
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
            handle_newseries, ADMIN_PRIVATE & filters.command("newseries")
        ),
        group=-2,
    )
    app.add_handler(
        MessageHandler(
            handle_addepisode, ADMIN_PRIVATE & filters.command("addepisode")
        ),
        group=-2,
    )
    app.add_handler(
        MessageHandler(
            handle_listseries, ADMIN_PRIVATE & filters.command("listseries")
        ),
        group=-2,
    )
    app.add_handler(
        MessageHandler(
            handle_migrateposts,
            ADMIN_PRIVATE & filters.command("migrateposts"),
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
    app.add_handler(
        CallbackQueryHandler(handle_series_action_callback, ADMIN_CALLBACK),
        group=-2,
    )
    app.add_handler(
        CallbackQueryHandler(handle_migration_callback, ADMIN_CALLBACK),
        group=-2,
    )
