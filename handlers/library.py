"""Admin library workflow.

Series commands: /newseries, /addepisode, /editepisode, /listseries,
/deletepost and the one-time /migrateposts bridge. Legacy commands: /newpost
and /editpost.
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
MAX_URL_LENGTH = 2048
MAX_MESSAGE_TEXT_LENGTH = 4000
SERIES_PAGE_SIZE = 8

_MANAGEMENT_COMMANDS = {
    "start",
    "stats",
    "broadcast",
    "newpost",
    "editpost",
    "newseries",
    "addepisode",
    "editepisode",
    "listseries",
    "migrateposts",
    "deletepost",
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
    episode_label_customized: bool = False
    series_page: int = 0


@dataclass
class EpisodeLinkEdit:
    """One staged quality-link change for /editepisode."""

    link_id: int
    quality_label: str
    new_quality_label: Optional[str] = None
    direct_link: Optional[str] = None
    short_link: Optional[str] = None
    short_link_set: bool = False
    file_chat_id: Optional[int] = None
    file_message_id: Optional[int] = None
    file_type: Optional[str] = None


@dataclass
class EditEpisodeDraft:
    """Staged label/link changes for one /editepisode session."""

    user_id: int
    series: dict
    series_id: int
    episode: Optional[dict] = None
    stage: str = "menu"
    new_label: Optional[str] = None
    link_edits: dict[int, EpisodeLinkEdit] = field(default_factory=dict)
    selected_link_id: Optional[int] = None


@dataclass
class DeletePostDraft:
    """Pending /deletepost confirmation for one catalog entry."""

    entry_type: str
    entry_id: int
    title: str


@dataclass
class CatalogDeleteResult:
    """Outcome of a best-effort channel cleanup plus database deletion."""

    entry_type: str
    entry_id: int
    library_ok: bool = False
    library_error: Optional[str] = None
    links_ok: bool = False
    links_error: Optional[str] = None
    db_ok: bool = False
    db_error: Optional[str] = None
    episode_count: int = 0
    link_count: int = 0


_newpost_sessions: dict[int, NewPostDraft] = {}
_editpost_sessions: dict[int, EditPostDraft] = {}
_series_sessions: dict[int, SeriesDraft] = {}
_migrate_sessions: dict[int, MigrateDraft] = {}
_editepisode_sessions: dict[int, EditEpisodeDraft] = {}
_deletepost_sessions: dict[int, DeletePostDraft] = {}


def _command_name(message: Message) -> Optional[str]:
    text = getattr(message, "text", None)
    if not text or not text.startswith("/"):
        return None
    command = text.split(maxsplit=1)[0][1:]
    return command.split("@", 1)[0].lower()


def _has_session(_, __, message: Message) -> bool:
    if message.from_user is None:
        return False
    user_id = message.from_user.id
    return (
        user_id in _newpost_sessions
        or user_id in _editpost_sessions
        or user_id in _series_sessions
        or user_id in _editepisode_sessions
        or user_id in _migrate_sessions
    )


SESSION_FILTER = filters.create(_has_session)


def _clear_sessions(user_id: int) -> None:
    """Drop every in-memory admin draft for a user before starting a new one."""
    _newpost_sessions.pop(user_id, None)
    _editpost_sessions.pop(user_id, None)
    _series_sessions.pop(user_id, None)
    _editepisode_sessions.pop(user_id, None)
    _migrate_sessions.pop(user_id, None)
    _deletepost_sessions.pop(user_id, None)


# CallbackQuery has no .chat attribute, so filters.private cannot be used on
# callback handlers. filters.user only reads query.from_user.
ADMIN_CALLBACK = filters.user(list(settings.admin_ids))


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
    status_id = getattr(status, "id", None)
    if status is not None and status_id is not None:
        await delete_messages(client, chat_id, [status_id])


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
            "Usage: <code>/editpost &lt;post_id&gt;</code>\n"
            "This command edits legacy /newpost entries.\n"
            "To rename or edit a series episode, use "
            "<code>/editepisode</code>.",
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
            "Use <code>/listseries</code> to find the series, "
            "<code>/addepisode</code> for new episodes and "
            "<code>/editepisode</code> to fix an existing label or quality "
            "link. Replacing the file of a migrated episode is not supported "
            "by this command.",
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
    if cancelled is None:
        cancelled = _editepisode_sessions.pop(message.from_user.id, None)

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

    edit_episode_draft = _editepisode_sessions.get(user_id)
    if edit_episode_draft is not None:
        if command in _MANAGEMENT_COMMANDS:
            return
        await _handle_editepisode_message(
            client, message, edit_episode_draft
        )
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
# Series workflows (/newseries, /addepisode, /editepisode, /listseries)
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


def _is_placeholder_episode_label(label: str) -> bool:
    """Return True for the /migrateposts prompt text, never a real label."""
    normalized = " ".join(label.casefold().split()).strip(" .:;,…")
    return "new episode label" in normalized


def _validate_episode_label(raw: str) -> tuple[Optional[str], Optional[str]]:
    label = raw.strip()
    if not label:
        return None, "❌ The episode label cannot be empty."
    if _is_placeholder_episode_label(label):
        return (
            None,
            "❌ That is the prompt text, not an episode label. Send a real "
            "label such as <code>Episode 2</code>.",
        )
    if len(label) > MAX_LABEL_LENGTH or "\n" in label or "\r" in label:
        return (
            None,
            f"❌ Episode labels must be a single line, at most "
            f"{MAX_LABEL_LENGTH} characters.",
        )
    return label, None


def _validate_episode_label_for_series(
    raw: str, series: dict
) -> tuple[Optional[str], Optional[str]]:
    """Validate an episode label and reject the series title as a label."""
    label, error = _validate_episode_label(raw)
    if error:
        return None, error
    series_title = str(series.get("title") or "").strip()
    if (
        label is not None
        and series_title
        and label.casefold() == series_title.casefold()
    ):
        return (
            None,
            "❌ The episode label cannot be identical to the series title.",
        )
    return label, None


def _validate_quality_text(raw: str) -> tuple[Optional[str], Optional[str]]:
    label = raw.strip()
    if not label:
        return None, "❌ The quality label cannot be empty."
    if len(label) > MAX_LABEL_LENGTH or "\n" in label or "\r" in label:
        return (
            None,
            f"❌ Quality labels must be a single line, at most "
            f"{MAX_LABEL_LENGTH} characters.",
        )
    return label, None


def _validate_link_url(raw: str) -> tuple[Optional[str], Optional[str]]:
    url = raw.strip()
    if not url:
        return None, "❌ The URL cannot be empty."
    if len(url) > MAX_URL_LENGTH:
        return (
            None,
            f"❌ URLs must be at most {MAX_URL_LENGTH} characters.",
        )
    if any(character.isspace() for character in url):
        return None, "❌ The URL must not contain spaces."
    if not url.startswith(("https://", "http://", "tg://")):
        return (
            None,
            "❌ Send a full URL starting with <code>https://</code>, "
            "<code>http://</code> or <code>tg://</code>.",
        )
    return url, None


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
    count = len(series.get("episodes", []))
    raw_count = series.get("episode_count")
    if raw_count is not None:
        count = max(count, int(raw_count or 0))
    return f"Episode {count + 1}"


def _migration_episode_label(
    raw: Optional[str],
    series: dict,
    *,
    customized: bool = False,
) -> str:
    """Return a safe migration label, ignoring prompt/title defaults.

    Only an explicitly edited, valid label is accepted. The legacy post title,
    the prompt text and the series title are never used as the episode label.
    """
    if customized and raw:
        label, error = _validate_episode_label(raw)
        if label is not None and error is None:
            series_title = str(series.get("title") or "").strip().casefold()
            if not series_title or label.casefold() != series_title:
                return label
    return _next_episode_label(series)


def _message_link(chat: Optional[Chat], message_id: Optional[int]) -> Optional[str]:
    """Build a t.me link for a stored message without fetching it."""
    if chat is None or not message_id:
        return None
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}/{int(message_id)}"
    raw_id = str(getattr(chat, "id", ""))
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
            "Create your first series with /newseries.\n\n"
            "✏️ Use <code>/editepisode</code> to browse series, "
            "<code>/editepisode &lt;series_id&gt;</code> to pick an episode, "
            "or <code>/editepisode &lt;series_id&gt; &lt;episode&gt;</code> "
            "to edit directly.\n"
            "🗑 Use <code>/deletepost &lt;id&gt;</code> to remove a legacy "
            "post or series."
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
            "waiting for /migrateposts.\n\n"
        )
    chunk += (
        "✏️ Use <code>/editepisode</code> to browse series, "
        "<code>/editepisode &lt;series_id&gt;</code> to pick an episode, or "
        "<code>/editepisode &lt;series_id&gt; &lt;episode&gt;</code> to edit "
        "directly.\n"
        "🗑 Use <code>/deletepost &lt;id&gt;</code> to delete a series or "
        "legacy post."
    )
    await send_message(
        client, message.chat.id, chunk, disable_web_page_preview=True
    )


# ---------------------------------------------------------------------------
# Episode editing (/editepisode)
# ---------------------------------------------------------------------------


def _find_episode(series: dict, episode_id: int) -> Optional[dict]:
    for episode in series.get("episodes", []):
        if int(episode.get("id") or 0) == int(episode_id):
            return episode
    return None


def _episode_position(series: dict, episode_id: int) -> int:
    for position, episode in enumerate(series.get("episodes", []), start=1):
        if int(episode.get("id") or 0) == int(episode_id):
            return position
    return 0


def _find_episode_link(episode: dict, link_id: int) -> Optional[dict]:
    for link in episode.get("links", []):
        if int(link.get("id") or 0) == int(link_id):
            return link
    return None


def _match_episode_quality(episode: dict, label: str) -> Optional[dict]:
    target = label.strip().casefold()
    for link in episode.get("links", []):
        if (
            str(link.get("quality_label") or "").strip().casefold()
            == target
        ):
            return link
    return None


def _match_quality_for_draft(
    draft: EditEpisodeDraft, label: str
) -> Optional[dict]:
    """Match a current quality label or a staged replacement label."""
    if draft.episode is None:
        return None
    link = _match_episode_quality(draft.episode, label)
    if link is not None:
        return link

    target = label.strip().casefold()
    for candidate in draft.episode.get("links", []):
        edit = draft.link_edits.get(int(candidate["id"]))
        if (
            edit is not None
            and edit.new_quality_label
            and edit.new_quality_label.strip().casefold() == target
        ):
            return candidate
    return None


def _resolve_episode_candidates(series: dict, query: str) -> list[dict]:
    """Resolve an episode by position, database ID or exact current label."""
    raw = query.strip()
    if not raw:
        return []

    episodes = series.get("episodes", [])
    matches: list[dict] = []
    seen: set[int] = set()

    def add(episode: dict) -> None:
        episode_id = int(episode.get("id") or 0)
        if episode_id and episode_id not in seen:
            seen.add(episode_id)
            matches.append(episode)

    id_text = raw
    lowered = raw.casefold()
    if id_text.startswith("#"):
        id_text = id_text[1:].strip()
    elif lowered.startswith("id:"):
        id_text = id_text[3:].strip()
    elif lowered.startswith("id "):
        id_text = id_text[3:].strip()

    if id_text.isdigit():
        number = int(id_text)
        if 1 <= number <= len(episodes):
            add(episodes[number - 1])
        for episode in episodes:
            if int(episode.get("id") or 0) == number:
                add(episode)

    target = raw.casefold()
    for episode in episodes:
        if str(episode.get("episode_label") or "").strip().casefold() == target:
            add(episode)
    return matches


def _clip(value: str, limit: int = 140) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _episode_link_detail(link: dict) -> str:
    short = str(link.get("short_url") or "").strip()
    direct = str(link.get("direct_link") or "").strip()
    if short and direct and short != direct:
        return (
            f"short: {html.escape(_clip(short))} | "
            f"direct: {html.escape(_clip(direct))}"
        )
    return html.escape(_clip(short or direct or "—"))


def _edit_episode_menu_text(draft: EditEpisodeDraft) -> str:
    series = draft.series
    episode = draft.episode or {}
    episode_id = int(episode.get("id") or 0)
    position = _episode_position(series, episode_id)
    lines = [
        "✏️ <b>Edit episode</b>",
        "",
        f"📺 <b>Series:</b> #{series.get('id')} "
        f"{html.escape(str(series.get('title') or 'Untitled'))}",
        f"🏷 <b>Current label:</b> "
        f"{html.escape(str(episode.get('episode_label') or 'Episode'))}",
        f"🎞 <b>Episode ID:</b> {episode_id} · position {position}",
        "",
        "<b>Current quality links:</b>",
    ]

    links = episode.get("links", [])
    for link in links[:6]:
        lines.append(
            f"• <b>{html.escape(str(link.get('quality_label') or 'Link'))}"
            f"</b> — {_episode_link_detail(link)}"
        )
    if not links:
        lines.append("• <i>none</i>")
    elif len(links) > 6:
        lines.append(
            f"• …and {len(links) - 6} more (use the quality buttons)."
        )

    pending: list[str] = []
    if draft.new_label is not None:
        pending.append(
            f"🏷 Episode label → <b>{html.escape(draft.new_label)}</b>"
        )
    for edit in list(draft.link_edits.values())[:6]:
        parts: list[str] = []
        if edit.new_quality_label:
            parts.append(f"label → {html.escape(edit.new_quality_label)}")
        if edit.file_message_id is not None:
            parts.append("replacement file queued")
        else:
            if edit.direct_link:
                parts.append(f"direct → {html.escape(_clip(edit.direct_link))}")
            if edit.short_link_set:
                parts.append(
                    "short → "
                    + html.escape(_clip(edit.short_link or "(cleared)"))
                )
        if parts:
            pending.append(
                f"🔗 <b>{html.escape(edit.quality_label)}</b>: "
                + " · ".join(parts)
            )
    if len(draft.link_edits) > 6:
        pending.append(
            f"…and {len(draft.link_edits) - 6} more quality change(s)."
        )
    if pending:
        lines += ["", "<b>Pending changes:</b>", *pending]

    lines += [
        "",
        "Tap a quality button to edit it, or:",
        "• Send a video/document whose caption matches an existing quality "
        "label to replace that file.",
        "• Send <code>/url &lt;quality_label&gt; &lt;new_url&gt;</code> to "
        "replace a direct link.",
        "• Send <code>/shorturl &lt;quality_label&gt; &lt;new_url&gt;</code> "
        "to replace a short link.",
        "• Send <code>/qlabel &lt;old_quality_label&gt; "
        "&lt;new_quality_label&gt;</code> to rename a quality.",
        "• Send <code>/label &lt;new episode label&gt;</code> or tap "
        "✏️ Rename Episode.",
        "• <code>/done</code> to apply, <code>/cancel</code> to abort.",
    ]
    text = "\n".join(lines)
    if len(text) > MAX_MESSAGE_TEXT_LENGTH:
        text = text[:MAX_MESSAGE_TEXT_LENGTH].rsplit("\n", 1)[0]
        text += "\n… (shortened)"
    return text


def _edit_episode_keyboard(draft: EditEpisodeDraft) -> InlineKeyboardMarkup:
    episode = draft.episode or {}
    series_id = int(draft.series_id)
    episode_id = int(episode.get("id") or 0)
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                "✏️ Rename Episode",
                callback_data=f"edep:rename:{series_id}:{episode_id}",
            )
        ]
    ]
    for link in episode.get("links", []):
        label = str(link.get("quality_label") or "Link")
        rows.append(
            [
                InlineKeyboardButton(
                    f"🔗 {label}"[:MAX_BUTTON_TEXT_LENGTH],
                    callback_data=(
                        f"edep:q:{series_id}:{episode_id}:{int(link['id'])}"
                    ),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "✅ Apply changes",
                callback_data=f"edep:done:{series_id}:{episode_id}",
            ),
            InlineKeyboardButton(
                "❌ Cancel",
                callback_data=f"edep:cancel:{series_id}:{episode_id}",
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def _send_edit_episode_menu(
    client: Client, chat_id: int, draft: EditEpisodeDraft
) -> None:
    if draft.episode is None:
        return
    await send_message(
        client,
        chat_id,
        _edit_episode_menu_text(draft),
        reply_markup=_edit_episode_keyboard(draft),
        disable_web_page_preview=True,
    )


def _episode_picker_button_text(series: dict, episode: dict) -> str:
    """Show the current label, position and database ID in one button."""
    episode_id = int(episode.get("id") or 0)
    position = _episode_position(series, episode_id)
    label = (
        str(episode.get("episode_label") or "Episode").strip() or "Episode"
    )
    prefix = f"#{position} "
    suffix = f" · id {episode_id}"
    available = MAX_BUTTON_TEXT_LENGTH - len(prefix) - len(suffix)
    if available < 1:
        return f"#{position} id {episode_id}"[:MAX_BUTTON_TEXT_LENGTH]
    if len(label) > available:
        label = label[: max(1, available - 1)].rstrip() + "…"
    return f"{prefix}{label}{suffix}"[:MAX_BUTTON_TEXT_LENGTH]


async def _send_edit_episode_picker(
    client: Client,
    chat_id: int,
    draft: EditEpisodeDraft,
    candidates: list[dict],
    reason: str,
) -> None:
    rows = []
    for episode in candidates:
        rows.append(
            [
                InlineKeyboardButton(
                    _episode_picker_button_text(draft.series, episode),
                    callback_data=(
                        f"edep:pick:{draft.series_id}:{episode['id']}"
                    ),
                )
            ]
        )
    await send_message(
        client,
        chat_id,
        reason,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _send_edit_episode_quality_picker(
    client: Client, chat_id: int, draft: EditEpisodeDraft
) -> None:
    if draft.episode is None:
        return
    links = draft.episode.get("links", [])
    if not links:
        await send_message(
            client,
            chat_id,
            "❌ This episode has no quality links to edit. Use /label to "
            "rename the episode, or /cancel.",
        )
        return

    rows = []
    for link in links:
        label = str(link.get("quality_label") or "Link")
        rows.append(
            [
                InlineKeyboardButton(
                    f"🔗 {label}"[:MAX_BUTTON_TEXT_LENGTH],
                    callback_data=(
                        f"edep:q:{draft.series_id}:"
                        f"{int(draft.episode['id'])}:{int(link['id'])}"
                    ),
                )
            ]
        )
    await send_message(
        client,
        chat_id,
        f"🔗 Choose a quality to edit for "
        f"<b>{html.escape(str(draft.episode.get('episode_label') or 'Episode'))}"
        "</b>:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _send_edit_episode_quality_prompt(
    client: Client, chat_id: int, draft: EditEpisodeDraft, link: dict
) -> None:
    short = str(link.get("short_url") or "").strip()
    direct = str(link.get("direct_link") or "").strip()
    await send_message(
        client,
        chat_id,
        f"🔗 <b>Editing quality:</b> "
        f"{html.escape(str(link.get('quality_label') or 'Link'))}\n\n"
        f"Short link: {html.escape(_clip(short)) if short else '<i>none</i>'}\n"
        f"Direct link: "
        f"{html.escape(_clip(direct)) if direct else '<i>none</i>'}\n\n"
        "Send a video/document with this quality label as its caption to "
        "replace the file.\n"
        "Or send <code>/url &lt;new_url&gt;</code> for the direct link, "
        "<code>/shorturl &lt;new_url&gt;</code> for the short link, or "
        "<code>/qlabel &lt;new_quality_label&gt;</code> to rename it.\n\n"
        "Use <code>/done</code> to apply or <code>/cancel</code> to abort.",
        disable_web_page_preview=True,
    )


def _get_or_create_link_edit(
    draft: EditEpisodeDraft, link: dict
) -> EpisodeLinkEdit:
    link_id = int(link["id"])
    edit = draft.link_edits.get(link_id)
    if edit is None:
        edit = EpisodeLinkEdit(
            link_id=link_id,
            quality_label=str(link.get("quality_label") or "Link"),
        )
        draft.link_edits[link_id] = edit
    return edit


async def _stage_episode_label(
    client: Client, chat_id: int, draft: EditEpisodeDraft, raw: str
) -> None:
    label, error = _validate_episode_label_for_series(raw, draft.series)
    if error:
        await send_message(client, chat_id, error)
        return
    draft.new_label = label
    draft.stage = "menu"
    await send_message(
        client,
        chat_id,
        f"✅ Episode label queued: <b>{html.escape(label)}</b>",
    )
    await _send_edit_episode_menu(client, chat_id, draft)


async def _stage_quality_url(
    client: Client,
    chat_id: int,
    draft: EditEpisodeDraft,
    link: dict,
    raw_url: str,
    *,
    short: bool,
) -> None:
    url, error = _validate_link_url(raw_url)
    if error:
        await send_message(client, chat_id, error)
        return
    edit = _get_or_create_link_edit(draft, link)
    edit.file_chat_id = None
    edit.file_message_id = None
    edit.file_type = None
    if short:
        edit.short_link = url
        edit.short_link_set = True
    else:
        edit.direct_link = url
        edit.short_link = None
        edit.short_link_set = True
    draft.selected_link_id = int(link["id"])
    draft.stage = "menu"
    await send_message(
        client,
        chat_id,
        f"✅ Queued {'short' if short else 'direct'} link for "
        f"<b>{html.escape(str(link.get('quality_label') or 'Link'))}</b>: "
        f"{html.escape(url)}",
    )
    await _send_edit_episode_menu(client, chat_id, draft)


async def _stage_quality_label(
    client: Client,
    chat_id: int,
    draft: EditEpisodeDraft,
    link: dict,
    raw: str,
) -> None:
    label, error = _validate_quality_text(raw)
    if error:
        await send_message(client, chat_id, error)
        return
    edit = _get_or_create_link_edit(draft, link)
    edit.new_quality_label = label
    draft.selected_link_id = int(link["id"])
    draft.stage = "menu"
    await send_message(
        client,
        chat_id,
        f"✅ Quality label queued: "
        f"<b>{html.escape(str(link.get('quality_label') or 'Link'))}</b> → "
        f"<b>{html.escape(label)}</b>",
    )
    await _send_edit_episode_menu(client, chat_id, draft)


async def _stage_quality_file(
    client: Client, message: Message, draft: EditEpisodeDraft
) -> None:
    if draft.episode is None:
        return
    caption = (getattr(message, "caption", None) or "").strip()
    if not caption:
        await send_message(
            client,
            message.chat.id,
            "❌ Add the existing quality label as the file caption, for "
            "example <code>1080p</code>.",
        )
        return

    link = _match_quality_for_draft(draft, caption)
    if link is None:
        labels = ", ".join(
            f"<code>{html.escape(str(item.get('quality_label') or '?'))}</code>"
            for item in draft.episode.get("links", [])
        ) or "<i>none</i>"
        await send_message(
            client,
            message.chat.id,
            f"❌ No quality labelled <b>{html.escape(caption)}</b> exists "
            f"for this episode.\n\nExisting qualities: {labels}",
        )
        return

    edit = _get_or_create_link_edit(draft, link)
    edit.file_chat_id = message.chat.id
    edit.file_message_id = message.id
    edit.file_type = (
        "video" if getattr(message, "video", None) else "document"
    )
    edit.direct_link = None
    edit.short_link = None
    edit.short_link_set = False
    draft.selected_link_id = int(link["id"])
    draft.stage = "menu"
    await send_message(
        client,
        message.chat.id,
        f"✅ Replacement file queued for "
        f"<b>{html.escape(str(link.get('quality_label') or 'Link'))}</b>.",
    )
    await _send_edit_episode_menu(client, message.chat.id, draft)


def _editepisode_filter_rows(
    categories: list[str],
) -> list[list[InlineKeyboardButton]]:
    if not categories:
        return []
    buttons = [
        InlineKeyboardButton("🌐 All", callback_data="edep:c:-1:0")
    ]
    for index, name in enumerate(categories):
        buttons.append(
            InlineKeyboardButton(
                f"🗂 {name}"[:MAX_BUTTON_TEXT_LENGTH],
                callback_data=f"edep:c:{index}:0",
            )
        )
    return [buttons[index : index + 2] for index in range(0, len(buttons), 2)]


async def _send_editepisode_series_list(
    client: Client,
    chat_id: int,
    *,
    page: int = 0,
    category: Optional[str] = None,
) -> None:
    """Show the paginated series picker used by bare /editepisode."""
    categories = list(settings.library_channels)
    category_index = (
        categories.index(category) if category in categories else -1
    )
    try:
        series_list = await database.list_series(category)
    except Exception:
        logger.exception("Failed to list series for /editepisode")
        await send_message(
            client,
            chat_id,
            "❌ Could not read the series list from the database.",
        )
        return

    filter_rows = _editepisode_filter_rows(categories)
    if not series_list:
        text = "✏️ <b>Edit an episode</b>\n\n"
        if category:
            text += (
                f"No series found in category <b>{html.escape(category)}</b>. "
                "Choose another category or create one with /newseries."
            )
        else:
            text += "No series found. Create one with /newseries."
        await send_message(
            client,
            chat_id,
            text,
            reply_markup=(
                InlineKeyboardMarkup(filter_rows) if filter_rows else None
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
                callback_data=f"edep:s:{series['id']}",
            )
        ]
        for series in chunk
    ]

    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "⬅️ Prev",
                callback_data=f"edep:l:{page - 1}:{category_index}",
            )
        )
    if page + 1 < total_pages:
        navigation.append(
            InlineKeyboardButton(
                "Next ➡️",
                callback_data=f"edep:l:{page + 1}:{category_index}",
            )
        )
    if navigation:
        rows.append(navigation)
    rows.extend(filter_rows)

    text = (
        "✏️ <b>Edit an episode</b>\n\n"
        "Choose a series. The button shows its ID and title.\n"
        f"Filter: <b>{html.escape(category) if category else 'all categories'}"
        "</b>\n"
        f"Page {page + 1}/{total_pages} · {len(series_list)} series"
    )
    await send_message(
        client, chat_id, text, reply_markup=InlineKeyboardMarkup(rows)
    )


async def handle_editepisode(client: Client, message: Message) -> None:
    """Start an /editepisode session or show the interactive picker."""
    if message.from_user is None:
        return

    args = message.command[1:] if message.command else []
    user_id = message.from_user.id

    if not args:
        _clear_sessions(user_id)
        await _send_editepisode_series_list(client, message.chat.id)
        return

    try:
        series_id = int(args[0])
    except ValueError:
        await send_message(
            client,
            message.chat.id,
            "❌ The series ID must be a number.",
        )
        return

    series = await database.get_series(series_id)
    if series is None:
        await send_message(
            client,
            message.chat.id,
            f"❌ Series <code>{series_id}</code> was not found.",
        )
        return
    if not series.get("episodes"):
        await send_message(
            client,
            message.chat.id,
            f"❌ Series <code>{series_id}</code> has no episodes to edit.",
        )
        return

    _clear_sessions(user_id)
    draft = EditEpisodeDraft(
        user_id=user_id,
        series=series,
        series_id=series_id,
        stage="episode_pick",
    )
    _editepisode_sessions[user_id] = draft

    title = html.escape(str(series.get("title") or "Untitled"))
    if len(args) == 1:
        reason = (
            "🎞 <b>Choose an episode to edit</b>\n\n"
            f"Series: <b>#{series_id} {title}</b>\n\n"
            "Tap an episode:"
        )
        await _send_edit_episode_picker(
            client, message.chat.id, draft, series["episodes"], reason
        )
        return

    query = " ".join(args[1:]).strip()
    if not query:
        reason = (
            "🎞 <b>Choose an episode to edit</b>\n\n"
            f"Series: <b>#{series_id} {title}</b>\n\n"
            "Tap an episode:"
        )
        await _send_edit_episode_picker(
            client, message.chat.id, draft, series["episodes"], reason
        )
        return

    candidates = _resolve_episode_candidates(series, query)
    if len(candidates) == 1:
        draft.episode = candidates[0]
        draft.stage = "menu"
        await _send_edit_episode_menu(client, message.chat.id, draft)
        return

    if candidates:
        reason = (
            "❓ <b>Multiple episodes match.</b>\n\n"
            f"Query: <code>{html.escape(query)}</code>\n"
            "Choose the exact episode:"
        )
        await _send_edit_episode_picker(
            client, message.chat.id, draft, candidates, reason
        )
        return

    reason = (
        "❌ <b>No episode matched.</b>\n\n"
        f"Query: <code>{html.escape(query)}</code>\n"
        "Choose an episode:"
    )
    await _send_edit_episode_picker(
        client, message.chat.id, draft, series.get("episodes", []), reason
    )


async def _handle_editepisode_message(
    client: Client, message: Message, draft: EditEpisodeDraft
) -> None:
    command = _command_name(message)

    if command == "done":
        await _finish_edit_episode(client, message.chat.id, draft)
        return

    if draft.episode is None:
        await send_message(
            client,
            message.chat.id,
            "Choose an episode using the buttons above, or /cancel.",
        )
        return

    if command == "label":
        raw = " ".join(message.command[1:]).strip() if message.command else ""
        await _stage_episode_label(client, message.chat.id, draft, raw)
        return

    if command in {"url", "shorturl"}:
        args = message.command[1:] if message.command else []
        link: Optional[dict] = None
        raw_url = ""
        if len(args) >= 2:
            link = _match_quality_for_draft(draft, args[0])
            raw_url = " ".join(args[1:]).strip()
            if link is None:
                await send_message(
                    client,
                    message.chat.id,
                    f"❌ No quality labelled <b>{html.escape(args[0])}</b> "
                    "exists for this episode.",
                )
                return
        elif len(args) == 1 and draft.selected_link_id is not None:
            link = _find_episode_link(
                draft.episode, draft.selected_link_id
            )
            raw_url = args[0].strip()
        if link is None:
            await send_message(
                client,
                message.chat.id,
                "Usage: <code>/url &lt;quality_label&gt; &lt;new_url&gt;</code>"
                " or <code>/shorturl &lt;quality_label&gt; "
                "&lt;new_url&gt;</code>\n\n"
                "Tap a quality button first to use the one-argument form.",
            )
            return
        await _stage_quality_url(
            client,
            message.chat.id,
            draft,
            link,
            raw_url,
            short=command == "shorturl",
        )
        return

    if command == "qlabel":
        args = message.command[1:] if message.command else []
        link = None
        raw_label = ""
        selected = (
            _find_episode_link(draft.episode, draft.selected_link_id)
            if draft.selected_link_id is not None
            else None
        )
        if len(args) >= 2:
            link = _match_quality_for_draft(draft, args[0])
            if link is not None:
                raw_label = " ".join(args[1:]).strip()
            elif selected is not None:
                link = selected
                raw_label = " ".join(args).strip()
            else:
                await send_message(
                    client,
                    message.chat.id,
                    f"❌ No quality labelled <b>{html.escape(args[0])}</b> "
                    "exists for this episode.",
                )
                return
        elif len(args) == 1 and selected is not None:
            link = selected
            raw_label = args[0].strip()
        if link is None:
            await send_message(
                client,
                message.chat.id,
                "Usage: <code>/qlabel &lt;old_quality_label&gt; "
                "&lt;new_quality_label&gt;</code>\n\n"
                "Tap a quality button first to use the one-argument form.",
            )
            return
        await _stage_quality_label(
            client, message.chat.id, draft, link, raw_label
        )
        return

    if command is not None:
        await send_message(
            client,
            message.chat.id,
            "Use /label, /url, /shorturl, /qlabel, /done or /cancel. "
            "Send a quality file with an existing quality label as its "
            "caption to replace a file.",
        )
        return

    if draft.stage == "label":
        text = getattr(message, "text", None)
        if not text:
            await send_message(
                client, message.chat.id, "Send the new episode label as text."
            )
            return
        await _stage_episode_label(client, message.chat.id, draft, text)
        return

    if getattr(message, "video", None) or getattr(
        message, "document", None
    ):
        await _stage_quality_file(client, message, draft)
        return

    text = getattr(message, "text", None)
    if text:
        raw = text.strip()
        if raw.startswith(("https://", "http://", "tg://")):
            if draft.selected_link_id is None:
                await send_message(
                    client,
                    message.chat.id,
                    "Tap a quality button first, or use "
                    "<code>/url &lt;quality_label&gt; &lt;new_url&gt;</code>.",
                )
                return
            link = _find_episode_link(
                draft.episode, draft.selected_link_id
            )
            if link is None:
                await send_message(
                    client,
                    message.chat.id,
                    "❌ That quality link no longer exists. Use /cancel and "
                    "start again.",
                )
                return
            await _stage_quality_url(
                client,
                message.chat.id,
                draft,
                link,
                raw,
                short=False,
            )
            return
        if draft.stage == "quality":
            await send_message(
                client,
                message.chat.id,
                "Send a video/document with the quality label as its caption, "
                "or send a URL. You can also use /url, /shorturl or /qlabel.",
            )
            return

    await send_message(
        client,
        message.chat.id,
        "Send a quality file, /label, /url, /shorturl, /qlabel, /done or "
        "/cancel.",
    )


async def _restore_episode_details(
    episode_id: int, old_label: str, old_links: list[dict]
) -> bool:
    """Best-effort rollback used when the live keyboard cannot be edited."""
    try:
        return await database.update_episode_details(
            episode_id, old_label, old_links
        )
    except Exception:
        logger.exception("Failed to roll back episode #%s", episode_id)
        return False


async def _apply_edit_episode(
    client: Client, chat_id: int, draft: EditEpisodeDraft
) -> bool:
    """Apply staged changes, then update the live keyboard or roll back."""
    if draft.episode is None:
        await send_message(
            client, chat_id, "❌ No episode is selected. Use /cancel."
        )
        return False

    episode_id = int(draft.episode["id"])
    series = await database.get_series(draft.series_id)
    if series is None:
        _editepisode_sessions.pop(draft.user_id, None)
        await send_message(
            client,
            chat_id,
            f"❌ Series <code>{draft.series_id}</code> no longer exists.",
        )
        return False

    episode = _find_episode(series, episode_id)
    if episode is None:
        _editepisode_sessions.pop(draft.user_id, None)
        await send_message(
            client,
            chat_id,
            "❌ That episode no longer exists. Use /cancel.",
        )
        return False

    draft.series = series
    draft.episode = episode

    if draft.new_label is None and not draft.link_edits:
        await send_message(
            client, chat_id, "ℹ️ No changes were queued. Nothing to apply."
        )
        return True

    links_chat = await _ensure_library_config(client, chat_id)
    if links_chat is None:
        return False
    links_message_id = series.get("links_channel_message_id")
    if links_message_id is None:
        _editepisode_sessions.pop(draft.user_id, None)
        await send_message(
            client,
            chat_id,
            "❌ This series has no stored Links Channel message ID, so the "
            "keyboard cannot be updated.",
        )
        return False

    old_label = str(episode.get("episode_label") or "")
    old_links = [dict(link) for link in episode.get("links", [])]
    old_by_id = {int(link["id"]): link for link in old_links}

    missing = [
        link_id for link_id in draft.link_edits if link_id not in old_by_id
    ]
    if missing:
        await send_message(
            client,
            chat_id,
            "❌ One or more selected quality links no longer exist. Use "
            "/cancel and start again.",
        )
        return False

    new_label: Optional[str] = None
    if draft.new_label is not None:
        new_label, error = _validate_episode_label_for_series(
            draft.new_label, series
        )
        if error:
            await send_message(client, chat_id, error)
            return False

    updates: list[dict] = []
    for link_id, edit in draft.link_edits.items():
        current = old_by_id[link_id]
        quality_label = (
            edit.new_quality_label
            or str(current.get("quality_label") or "Link")
        )
        direct_link = str(current.get("direct_link") or "")
        short_link = current.get("short_url")

        if edit.file_message_id is not None:
            if edit.file_chat_id is None:
                await send_message(
                    client,
                    chat_id,
                    "❌ The queued replacement file is incomplete. Send it "
                    "again.",
                )
                return False
            try:
                stored = await store_message(
                    client,
                    edit.file_chat_id,
                    edit.file_message_id,
                    uploaded_by=draft.user_id,
                )
            except Exception as exc:
                logger.exception(
                    "Failed to store replacement file for episode #%s",
                    episode_id,
                )
                await send_message(
                    client,
                    chat_id,
                    f"❌ Could not copy the replacement file for "
                    f"<b>{html.escape(edit.quality_label)}</b>: "
                    f"{html.escape(str(exc) or type(exc).__name__)}\n\n"
                    "Nothing was changed. Check STORAGE_CHANNEL and try "
                    "/done again.",
                )
                return False
            if stored is None:
                await send_message(
                    client,
                    chat_id,
                    f"❌ Could not copy the replacement file for "
                    f"<b>{html.escape(edit.quality_label)}</b> to "
                    "STORAGE_CHANNEL. Check the bot's storage admin rights "
                    "and try /done again.",
                )
                return False
            direct_link = stored.direct_link
            short_link = getattr(stored, "short_link", None)
            # Keep the stored links staged so a retry after a Telegram edit
            # failure does not copy the file twice.
            edit.file_chat_id = None
            edit.file_message_id = None
            edit.file_type = None
            edit.direct_link = direct_link
            edit.short_link = short_link
            edit.short_link_set = True

        if edit.direct_link:
            direct_link = edit.direct_link
        if edit.short_link_set:
            short_link = edit.short_link
        if not direct_link:
            await send_message(
                client,
                chat_id,
                f"❌ The quality <b>{html.escape(quality_label)}</b> has no "
                "direct link. Send a replacement URL or file.",
            )
            return False

        updates.append(
            {
                "id": link_id,
                "quality_label": quality_label,
                "short_url": short_link,
                "direct_link": direct_link,
            }
        )

    try:
        updated = await database.update_episode_details(
            episode_id,
            new_label if new_label is not None else old_label,
            updates,
        )
    except Exception as exc:
        logger.exception("Failed to update episode #%s", episode_id)
        await send_message(
            client,
            chat_id,
            "❌ <b>Episode update failed.</b>\n\n"
            f"{html.escape(str(exc) or type(exc).__name__)}\n\n"
            "The database was not changed.",
        )
        return False
    if not updated:
        await send_message(
            client,
            chat_id,
            "❌ The episode disappeared before it could be updated.",
        )
        return False

    try:
        refreshed = await database.get_series(draft.series_id)
    except Exception as exc:
        logger.exception(
            "Failed to reload series #%s after editing episode #%s",
            draft.series_id,
            episode_id,
        )
        restored = await _restore_episode_details(
            episode_id, old_label, old_links
        )
        if restored:
            await send_message(
                client,
                chat_id,
                "❌ <b>Could not reload the updated series.</b>\n\n"
                f"{html.escape(str(exc) or type(exc).__name__)}\n\n"
                "The database change was rolled back so the live keyboard "
                "and Turso still match.",
            )
        else:
            _editepisode_sessions.pop(draft.user_id, None)
            await send_message(
                client,
                chat_id,
                "❌ <b>Could not reload the updated series and the database "
                "rollback also failed.</b>\n\n"
                "The database and the live keyboard may be out of sync. "
                "Inspect the episode and repair it manually.",
            )
        return False
    if refreshed is None:
        _editepisode_sessions.pop(draft.user_id, None)
        await send_message(
            client,
            chat_id,
            "❌ The series disappeared after the database update. The Links "
            "Channel message could not be updated; manual repair is needed.",
        )
        return False

    try:
        keyboard = _series_links_keyboard(refreshed)
        edited = await edit_message_reply_markup(
            client,
            links_chat.id,
            int(links_message_id),
            keyboard,
        )
    except Exception:
        logger.exception("Failed to edit the series Links Channel message")
        edited = None

    if edited is None:
        restored = await _restore_episode_details(
            episode_id, old_label, old_links
        )
        if restored:
            await send_message(
                client,
                chat_id,
                "❌ <b>Telegram rejected the Links Channel edit.</b>\n\n"
                "The database change was rolled back, so the live keyboard "
                "and Turso still match.\n\n"
                "Check the bot's admin rights and the message, then use "
                "/done to retry or /cancel.",
            )
        else:
            _editepisode_sessions.pop(draft.user_id, None)
            await send_message(
                client,
                chat_id,
                "❌ <b>Telegram rejected the Links Channel edit and the "
                "database rollback also failed.</b>\n\n"
                "The database and the live keyboard may be out of sync. "
                "Inspect the episode and repair it manually.",
            )
        return False

    lines = [
        f"✅ <b>Episode updated in series #{draft.series_id}</b>",
        "",
    ]
    if new_label is not None:
        lines.append(
            f"🏷 Label: <b>{html.escape(old_label or '(empty)')}</b> → "
            f"<b>{html.escape(new_label)}</b>"
        )
    else:
        lines.append(f"🎞 Episode: <b>{html.escape(old_label)}</b>")

    for update in updates:
        current = old_by_id[int(update["id"])]
        parts: list[str] = []
        if update["quality_label"] != current.get("quality_label"):
            parts.append(
                f"label → {html.escape(str(update['quality_label']))}"
            )
        if update["direct_link"] != current.get("direct_link"):
            parts.append(
                f"direct → {html.escape(_clip(str(update['direct_link'])))}"
            )
        if update["short_url"] != current.get("short_url"):
            parts.append(
                "short → "
                + html.escape(_clip(str(update["short_url"] or "(cleared)")))
            )
        if parts:
            lines.append(
                f"🔗 <b>{html.escape(str(current.get('quality_label') or 'Link'))}"
                f"</b>: " + " · ".join(parts)
            )

    links_link = _message_link(links_chat, int(links_message_id))
    if links_link:
        lines.append(f"\n🔗 {html.escape(links_link)}")
    lines.append("\nThe Links Channel keyboard was updated.")
    await send_message(
        client, chat_id, "\n".join(lines), disable_web_page_preview=True
    )
    logger.info(
        "Updated episode #%s in series #%s for admin %s",
        episode_id,
        draft.series_id,
        draft.user_id,
    )
    return True


async def _finish_edit_episode(
    client: Client, chat_id: int, draft: EditEpisodeDraft
) -> None:
    success = await _apply_edit_episode(client, chat_id, draft)
    if success:
        current = _editepisode_sessions.get(draft.user_id)
        if current is draft:
            _editepisode_sessions.pop(draft.user_id, None)


async def handle_edit_episode_callback(
    client: Client, query: CallbackQuery
) -> None:
    """Handle /editepisode inline buttons."""
    data = query.data or ""
    if not data.startswith("edep:"):
        return
    if query.from_user is None:
        await query.answer()
        return

    user_id = query.from_user.id
    chat_id = query.message.chat.id if query.message else user_id
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    # Accept compact callback payloads as well as the fully qualified form
    # generated by the keyboards below.
    session = _editepisode_sessions.get(user_id)
    if action == "cancel" and len(parts) == 2:
        if session is None:
            await query.answer(
                "This edit session is no longer active.", show_alert=True
            )
            return
        _editepisode_sessions.pop(user_id, None)
        await query.answer("Cancelled")
        await send_message(client, chat_id, "Edit cancelled.")
        return

    if session is not None:
        if action == "pick" and len(parts) == 3:
            parts = ["edep", "pick", str(session.series_id), parts[2]]
        elif (
            action
            in {
                "rename",
                "label",
                "quality",
                "done",
                "apply",
                "cancel",
            }
            and len(parts) == 2
            and session.episode is not None
        ):
            parts = [
                "edep",
                action,
                str(session.series_id),
                str(session.episode["id"]),
            ]
        elif (
            action == "q"
            and len(parts) == 3
            and session.episode is not None
        ):
            parts = [
                "edep",
                "q",
                str(session.series_id),
                str(session.episode["id"]),
                parts[2],
            ]
        action = parts[1] if len(parts) > 1 else ""

    try:
        if action == "l" and len(parts) in (3, 4):
            try:
                page = int(parts[2])
                category_index = int(parts[3]) if len(parts) == 4 else -1
            except ValueError:
                await query.answer("Invalid page.", show_alert=True)
                return
            categories = list(settings.library_channels)
            category: Optional[str] = None
            if category_index >= 0:
                if category_index >= len(categories):
                    await query.answer(
                        "That category is no longer configured.",
                        show_alert=True,
                    )
                    return
                category = categories[category_index]
            elif category_index < -1:
                await query.answer("Invalid page.", show_alert=True)
                return
            await query.answer()
            if query.message is not None:
                await _delete_status(client, chat_id, query.message)
            await _send_editepisode_series_list(
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
            if index == -1:
                category = None
            elif 0 <= index < len(categories):
                category = categories[index]
            else:
                await query.answer(
                    "That category is no longer configured.",
                    show_alert=True,
                )
                return
            await query.answer(
                "Showing all categories"
                if category is None
                else f"Filtering by {category}"
            )
            if query.message is not None:
                await _delete_status(client, chat_id, query.message)
            await _send_editepisode_series_list(
                client, chat_id, page=page, category=category
            )
            return

        if action == "s" and len(parts) == 3:
            series_id = int(parts[2])
            existing = _editepisode_sessions.get(user_id)
            if existing is not None and (
                existing.episode is not None
                or existing.new_label is not None
                or existing.link_edits
            ):
                await query.answer(
                    "Finish or /cancel the current edit before choosing "
                    "another series.",
                    show_alert=True,
                )
                return
            series = await database.get_series(series_id)
            if series is None:
                await query.answer(
                    "That series no longer exists.", show_alert=True
                )
                return
            if not series.get("episodes"):
                await query.answer(
                    "That series has no episodes to edit.", show_alert=True
                )
                return
            _clear_sessions(user_id)
            draft = EditEpisodeDraft(
                user_id=user_id,
                series=series,
                series_id=series_id,
                stage="episode_pick",
            )
            _editepisode_sessions[user_id] = draft
            await query.answer("Series selected")
            if query.message is not None:
                await _delete_status(client, chat_id, query.message)
            reason = (
                "🎞 <b>Choose an episode to edit</b>\n\n"
                f"Series: <b>#{series_id} "
                f"{html.escape(str(series.get('title') or 'Untitled'))}</b>\n\n"
                "Tap an episode:"
            )
            await _send_edit_episode_picker(
                client, chat_id, draft, series["episodes"], reason
            )
            return

        if action == "pick" and len(parts) == 4:
            series_id = int(parts[2])
            episode_id = int(parts[3])
            draft = _editepisode_sessions.get(user_id)
            if (
                draft is None
                or draft.series_id != series_id
                or draft.episode is not None
            ):
                await query.answer(
                    "This edit session is no longer active.",
                    show_alert=True,
                )
                return
            series = await database.get_series(series_id)
            if series is None:
                _editepisode_sessions.pop(user_id, None)
                await query.answer(
                    "That series no longer exists.", show_alert=True
                )
                return
            episode = _find_episode(series, episode_id)
            if episode is None:
                await query.answer(
                    "That episode no longer exists.", show_alert=True
                )
                return
            draft.series = series
            draft.episode = episode
            draft.stage = "menu"
            draft.selected_link_id = None
            draft.new_label = None
            draft.link_edits.clear()
            await query.answer("Episode selected")
            if query.message is not None:
                await _delete_status(client, chat_id, query.message)
            await _send_edit_episode_menu(client, chat_id, draft)
            return

        if action in {
            "rename",
            "label",
            "quality",
            "done",
            "apply",
            "cancel",
        } and len(parts) == 4:
            series_id = int(parts[2])
            episode_id = int(parts[3])
            draft = _editepisode_sessions.get(user_id)
            if (
                draft is None
                or draft.series_id != series_id
                or draft.episode is None
                or int(draft.episode["id"]) != episode_id
            ):
                await query.answer(
                    "This edit session is no longer active.",
                    show_alert=True,
                )
                return

            if action in {"rename", "label"}:
                draft.stage = "label"
                await query.answer()
                await send_message(
                    client,
                    chat_id,
                    "✏️ Send the new episode name as text, for example "
                    "<code>Episode 2</code>.\n\n"
                    "Single line, max "
                    f"{MAX_LABEL_LENGTH} characters. Use /cancel to abort.",
                )
                return

            if action == "quality":
                await query.answer()
                await _send_edit_episode_quality_picker(
                    client, chat_id, draft
                )
                return

            if action in {"done", "apply"}:
                await query.answer("Applying changes...")
                await _finish_edit_episode(client, chat_id, draft)
                return

            _editepisode_sessions.pop(user_id, None)
            await query.answer("Cancelled")
            await send_message(client, chat_id, "Edit cancelled.")
            return

        if action == "q" and len(parts) == 5:
            series_id = int(parts[2])
            episode_id = int(parts[3])
            link_id = int(parts[4])
            draft = _editepisode_sessions.get(user_id)
            if (
                draft is None
                or draft.series_id != series_id
                or draft.episode is None
                or int(draft.episode["id"]) != episode_id
            ):
                await query.answer(
                    "This edit session is no longer active.",
                    show_alert=True,
                )
                return
            link = _find_episode_link(draft.episode, link_id)
            if link is None:
                await query.answer(
                    "That quality link no longer exists.", show_alert=True
                )
                return
            draft.selected_link_id = link_id
            draft.stage = "quality"
            await query.answer(
                f"Selected {link.get('quality_label') or 'quality'}"
            )
            await _send_edit_episode_quality_prompt(
                client, chat_id, draft, link
            )
            return
    except (ValueError, IndexError):
        await query.answer("Invalid edit action.", show_alert=True)
        return

    await query.answer("Invalid edit action.", show_alert=True)


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
        "Use /addepisode to append more episodes or /editepisode to fix an "
        "existing episode."
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

    label = _migration_episode_label(
        draft.episode_label,
        series,
        customized=draft.episode_label_customized,
    )
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

    label = _migration_episode_label(
        draft.episode_label,
        series,
        customized=draft.episode_label_customized,
    )
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
            series = await database.get_series(series_id)
            if series is None:
                await query.answer(
                    "That series no longer exists.", show_alert=True
                )
                return
            draft.series_id = series_id
            draft.episode_label = _next_episode_label(series)
            draft.episode_label_customized = False
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
    draft.episode_label_customized = True
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


# ---------------------------------------------------------------------------
# Catalog deletion (/deletepost)
# ---------------------------------------------------------------------------


def _catalog_entry_label(entry_type: str) -> str:
    return "Legacy Post" if entry_type == "post" else "Series"


async def _resolve_series_delete_channels(
    client: Client, series: dict
) -> tuple[Optional[Chat], Optional[Chat]]:
    """Resolve the channels that hold a series' two channel messages."""
    category = str(series.get("category") or "default")
    library_chat: Optional[Chat] = None
    links_chat: Optional[Chat] = None

    try:
        library_chat = await resolve_library_category(
            client, category, settings.library_channels
        )
    except Exception as exc:
        logger.warning(
            "Could not resolve library category %r for deleting series #%s: %s",
            category,
            series.get("id"),
            exc,
        )

    if settings.links_channel:
        try:
            links_chat = await resolve_channel(
                client, settings.links_channel, "LINKS_CHANNEL"
            )
        except Exception as exc:
            logger.warning(
                "Could not resolve LINKS_CHANNEL for deleting series #%s: %s",
                series.get("id"),
                exc,
            )

    return library_chat, links_chat


async def _catalog_entry_links(
    client: Client, entry: dict
) -> tuple[Optional[str], Optional[str]]:
    """Build the Library/Links message links shown in the confirmation."""
    data = entry["data"]
    if entry["entry_type"] == "post":
        return (
            data.get("library_message_link"),
            data.get("links_message_link"),
        )

    library_chat, links_chat = await _resolve_series_delete_channels(
        client, data
    )
    return (
        _message_link(library_chat, data.get("library_channel_message_id")),
        _message_link(links_chat, data.get("links_channel_message_id")),
    )


async def handle_deletepost(client: Client, message: Message) -> None:
    """Start the /deletepost <id> confirmation flow."""
    if message.from_user is None:
        return

    args = message.command[1:] if message.command else []
    if len(args) != 1:
        await send_message(
            client,
            message.chat.id,
            "Usage: <code>/deletepost &lt;id&gt;</code>\n\n"
            "The ID can refer to a legacy post or a series.",
        )
        return

    try:
        entry_id = int(args[0])
    except ValueError:
        await send_message(
            client,
            message.chat.id,
            "❌ The ID must be a number.",
        )
        return

    try:
        entry = await database.resolve_catalog_entry(entry_id)
    except Exception:
        logger.exception("Failed to resolve catalog entry #%s", entry_id)
        await send_message(
            client,
            message.chat.id,
            "❌ Could not read the catalog from the database.",
        )
        return

    if entry is None:
        await send_message(
            client,
            message.chat.id,
            f"No legacy post or series found with ID {entry_id}.",
        )
        return

    entry_type = entry["entry_type"]
    data = entry["data"]
    title = str(data.get("title") or "Untitled")
    category = str(data.get("category") or "default")
    library_link, links_link = await _catalog_entry_links(client, entry)
    label = _catalog_entry_label(entry_type)

    _clear_sessions(message.from_user.id)
    _deletepost_sessions[message.from_user.id] = DeletePostDraft(
        entry_type=entry_type,
        entry_id=entry_id,
        title=title,
    )

    text = (
        f"🗑 <b>Delete {label} #{entry_id}</b>\n\n"
        f"🎬 <b>Title:</b> {html.escape(title)}\n"
        f"🗂 <b>Category:</b> {html.escape(category)}\n"
    )
    if entry_type == "series":
        text += (
            f"🎞 <b>Episodes:</b> "
            f"{int(data.get('episode_count') or 0)}\n"
        )

    text += (
        "\n📚 <b>Library Channel message:</b>\n"
        f"{html.escape(library_link) if library_link else '<i>unavailable</i>'}"
        "\n\n"
        "🔗 <b>Links Channel message:</b>\n"
        f"{html.escape(links_link) if links_link else '<i>unavailable</i>'}"
        "\n\n"
        "Channel message deletion is best effort. The database records are "
        "then removed. This cannot be undone."
    )

    await send_message(
        client,
        message.chat.id,
        text,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Confirm Delete",
                        callback_data=(
                            f"delpost:confirm:{entry_type}:{entry_id}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ Cancel",
                        callback_data="delpost:cancel",
                    )
                ],
            ]
        ),
        disable_web_page_preview=True,
    )


async def _delete_channel_message(
    client: Client,
    chat_id: Optional[int],
    message_id: Optional[int],
    label: str,
) -> tuple[bool, Optional[str]]:
    """Delete one channel message, reporting rather than raising failures."""
    if chat_id is None or message_id is None:
        return False, f"{label} message ID is not stored"

    try:
        await client.delete_messages(
            chat_id=int(chat_id),
            message_ids=[int(message_id)],
        )
    except Exception as exc:
        logger.warning(
            "Could not delete %s message %s in chat %s: %s",
            label,
            message_id,
            chat_id,
            exc,
            exc_info=True,
        )
        detail = str(exc).strip()
        error = (
            f"{type(exc).__name__}: {detail}"
            if detail
            else type(exc).__name__
        )
        return False, error

    return True, None


async def _delete_catalog_entry(
    client: Client, entry_type: str, entry_id: int
) -> Optional[CatalogDeleteResult]:
    """Delete both channel messages and the database rows for one entry."""
    entry = await database.resolve_catalog_entry(entry_id)
    if entry is None or entry["entry_type"] != entry_type:
        return None

    data = entry["data"]
    result = CatalogDeleteResult(entry_type=entry_type, entry_id=entry_id)

    if entry_type == "post":
        library_chat_id = data.get("library_channel_id")
        library_message_id = data.get("library_message_id")
        links_chat_id = data.get("links_channel_id")
        links_message_id = data.get("links_message_id")
    else:
        library_chat, links_chat = await _resolve_series_delete_channels(
            client, data
        )
        library_chat_id = library_chat.id if library_chat else None
        links_chat_id = links_chat.id if links_chat else None
        library_message_id = data.get("library_channel_message_id")
        links_message_id = data.get("links_channel_message_id")
        if library_chat is None:
            result.library_error = "the Library Channel could not be resolved"
        if links_chat is None:
            result.links_error = "the Links Channel could not be resolved"

    if result.library_error is None:
        result.library_ok, result.library_error = (
            await _delete_channel_message(
                client,
                library_chat_id,
                library_message_id,
                "Library Channel",
            )
        )
    if result.links_error is None:
        result.links_ok, result.links_error = await _delete_channel_message(
            client,
            links_chat_id,
            links_message_id,
            "Links Channel",
        )

    try:
        if entry_type == "post":
            result.db_ok = await database.delete_library_post(entry_id)
            if not result.db_ok:
                result.db_error = "the legacy post no longer exists"
        else:
            counts = await database.delete_series_with_counts(entry_id)
            if counts is None:
                result.db_error = "the series no longer exists"
            else:
                result.db_ok = True
                result.episode_count = int(counts["episode_count"])
                result.link_count = int(counts["link_count"])
    except Exception as exc:
        logger.exception(
            "Failed to delete %s #%s from the database",
            entry_type,
            entry_id,
        )
        result.db_error = str(exc) or type(exc).__name__

    return result


def _deletion_failure_line(label: str, error: Optional[str]) -> str:
    if not error:
        return f"⚠️ {label} message was skipped"

    lowered = error.casefold()
    if "not stored" in lowered:
        return f"⚠️ {label} message was skipped (no message ID stored)"
    if any(
        marker in lowered
        for marker in (
            "message_id_invalid",
            "messageidinvalid",
            "message id is invalid",
            "message to delete not found",
            "already deleted",
            "not found",
        )
    ):
        return f"⚠️ {label} message was already deleted (skipped)"
    return (
        f"⚠️ {label} message could not be deleted: "
        f"{html.escape(error)}"
    )


def _format_catalog_delete_report(result: CatalogDeleteResult) -> str:
    label = _catalog_entry_label(result.entry_type)
    lines = [f"🗑 <b>{label} #{result.entry_id}</b>", ""]

    if result.library_ok:
        lines.append("✅ Deleted Library Channel message")
    else:
        lines.append(
            _deletion_failure_line("Library Channel", result.library_error)
        )

    if result.links_ok:
        lines.append("✅ Deleted Links Channel message")
    else:
        lines.append(
            _deletion_failure_line("Links Channel", result.links_error)
        )

    if result.db_ok:
        lines.append("✅ Removed database records")
        if result.entry_type == "series":
            episode_word = (
                "episode" if result.episode_count == 1 else "episodes"
            )
            pronoun = "its" if result.episode_count == 1 else "their"
            lines.append(
                f"🎞 Removed {result.episode_count} {episode_word} and "
                f"{pronoun} links."
            )
    else:
        lines.append(
            "❌ Database deletion failed: "
            f"{html.escape(result.db_error or 'unknown error')}"
        )

    return "\n".join(lines)


async def handle_deletepost_callback(
    client: Client, query: CallbackQuery
) -> None:
    """Handle the confirm/cancel buttons from /deletepost."""
    data = query.data or ""
    if not data.startswith("delpost:"):
        return
    if query.from_user is None:
        await query.answer()
        return

    user_id = query.from_user.id
    chat_id = query.message.chat.id if query.message else user_id
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "cancel" and len(parts) == 2:
        _deletepost_sessions.pop(user_id, None)
        await query.answer("Cancelled")
        await send_message(client, chat_id, "Deletion cancelled.")
        return

    if action == "confirm" and len(parts) == 4:
        entry_type = parts[2]
        try:
            entry_id = int(parts[3])
        except ValueError:
            await query.answer(
                "Invalid deletion action.", show_alert=True
            )
            return
        if entry_type not in ("post", "series"):
            await query.answer(
                "Invalid deletion action.", show_alert=True
            )
            return

        draft = _deletepost_sessions.get(user_id)
        if (
            draft is None
            or draft.entry_type != entry_type
            or draft.entry_id != entry_id
        ):
            await query.answer(
                "This deletion request is no longer active.",
                show_alert=True,
            )
            return

        _deletepost_sessions.pop(user_id, None)
        await query.answer("Deleting...")
        try:
            result = await _delete_catalog_entry(
                client, entry_type, entry_id
            )
        except Exception as exc:
            logger.exception(
                "Unexpected failure deleting %s #%s",
                entry_type,
                entry_id,
            )
            await send_message(
                client,
                chat_id,
                "❌ Deletion failed: "
                f"{html.escape(str(exc) or type(exc).__name__)}",
            )
            return

        if result is None:
            await send_message(
                client,
                chat_id,
                f"No legacy post or series found with ID {entry_id}.",
            )
            return

        await send_message(
            client,
            chat_id,
            _format_catalog_delete_report(result),
            disable_web_page_preview=True,
        )
        return

    await query.answer("Invalid deletion action.", show_alert=True)


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
            handle_editepisode,
            ADMIN_PRIVATE & filters.command("editepisode"),
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
            handle_deletepost,
            ADMIN_PRIVATE & filters.command("deletepost"),
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
    # Pyrogram stops after the first matching handler in a group. Every admin
    # callback shares ADMIN_CALLBACK, so each data-prefix owner needs its own
    # group to receive the update.
    app.add_handler(
        CallbackQueryHandler(handle_series_action_callback, ADMIN_CALLBACK),
        group=-3,
    )
    app.add_handler(
        CallbackQueryHandler(handle_migration_callback, ADMIN_CALLBACK),
        group=-4,
    )
    app.add_handler(
        CallbackQueryHandler(handle_deletepost_callback, ADMIN_CALLBACK),
        group=-5,
    )
    app.add_handler(
        CallbackQueryHandler(handle_edit_episode_callback, ADMIN_CALLBACK),
        group=-6,
    )
