"""SQLite persistence for files, users, pending deletions and library posts.

All functions open a short-lived connection. SQLite is configured with WAL and
a busy timeout so the handler updates and the scheduler jobstore can share the
data directory without spurious "database is locked" errors.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import aiosqlite
from pyrogram.types import User

from config import settings

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    unique_id     TEXT PRIMARY KEY,
    message_id    INTEGER NOT NULL,
    file_type     TEXT NOT NULL,
    file_name     TEXT,
    uploaded_by   INTEGER,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    username      TEXT,
    first_name    TEXT,
    last_name     TEXT,
    first_seen    TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen     TEXT NOT NULL DEFAULT (datetime('now')),
    start_count   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pending_deletions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id            INTEGER NOT NULL,
    file_message_id    INTEGER NOT NULL,
    warning_message_id INTEGER,
    delete_at          REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_deletions_delete_at
    ON pending_deletions (delete_at);

CREATE TABLE IF NOT EXISTS library_posts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    title               TEXT NOT NULL,
    thumbnail_file_id   TEXT NOT NULL,
    links_channel_id    INTEGER NOT NULL,
    links_message_id    INTEGER NOT NULL,
    links_message_link  TEXT NOT NULL,
    library_channel_id  INTEGER NOT NULL,
    library_message_id  INTEGER NOT NULL,
    library_message_link TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS library_post_qualities (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id            INTEGER NOT NULL,
    label              TEXT NOT NULL,
    unique_id          TEXT NOT NULL,
    storage_message_id INTEGER NOT NULL,
    direct_link        TEXT NOT NULL,
    short_link         TEXT,
    button_url         TEXT NOT NULL,
    position           INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (post_id) REFERENCES library_posts (id) ON DELETE CASCADE,
    UNIQUE (post_id, label)
);

CREATE INDEX IF NOT EXISTS idx_library_post_qualities_post_id
    ON library_post_qualities (post_id, position);
"""


@asynccontextmanager
async def _connect() -> AsyncIterator[aiosqlite.Connection]:
    db = await aiosqlite.connect(str(settings.database_path))
    db.row_factory = aiosqlite.Row
    try:
        await db.execute("PRAGMA busy_timeout = 5000")
        yield db
    finally:
        await db.close()


async def init_db() -> None:
    """Create tables if needed and enable WAL mode."""
    async with _connect() as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.executescript(_SCHEMA)
        await db.commit()
    logger.info("SQLite database ready at %s", settings.database_path)


async def add_file(
    unique_id: str,
    message_id: int,
    file_type: str,
    file_name: Optional[str],
    uploaded_by: Optional[int],
) -> None:
    """Store (or refresh) the unique_id -> storage message mapping."""
    async with _connect() as db:
        await db.execute(
            """
            INSERT INTO files (unique_id, message_id, file_type, file_name, uploaded_by)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(unique_id) DO UPDATE SET
                message_id  = excluded.message_id,
                file_type   = excluded.file_type,
                file_name   = excluded.file_name,
                uploaded_by = excluded.uploaded_by
            """,
            (unique_id, message_id, file_type, file_name, uploaded_by),
        )
        await db.commit()


async def get_file(unique_id: str) -> Optional[dict]:
    """Return the file row for a deep-link ID, or None if it is unknown."""
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT * FROM files WHERE unique_id = ?", (unique_id,)
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


async def _scalar(query: str, params: tuple = ()) -> int:
    async with _connect() as db:
        cursor = await db.execute(query, params)
        row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def count_files() -> int:
    return await _scalar("SELECT COUNT(*) FROM files")


async def count_known_users() -> int:
    return await _scalar("SELECT COUNT(*) FROM users")


async def count_started_users() -> int:
    """Unique users who have used /start at least once."""
    return await _scalar("SELECT COUNT(*) FROM users WHERE start_count > 0")


async def count_pending_deletions() -> int:
    return await _scalar("SELECT COUNT(*) FROM pending_deletions")


async def touch_user(user: User, *, started: bool = False) -> None:
    """Insert or update a user seen by the bot.

    ``started`` increments the /start counter so stats can report unique users
    who actually opened a deep link (or used /start directly).
    """
    async with _connect() as db:
        await db.execute(
            """
            INSERT INTO users (user_id, username, first_name, last_name, start_count)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username    = excluded.username,
                first_name  = excluded.first_name,
                last_name   = excluded.last_name,
                last_seen   = datetime('now'),
                start_count = users.start_count + excluded.start_count
            """,
            (
                user.id,
                user.username,
                user.first_name,
                user.last_name,
                1 if started else 0,
            ),
        )
        await db.commit()


async def get_all_user_ids() -> list[int]:
    async with _connect() as db:
        cursor = await db.execute("SELECT user_id FROM users ORDER BY user_id")
        rows = await cursor.fetchall()
    return [int(row["user_id"]) for row in rows]


async def add_pending_deletion(
    chat_id: int,
    file_message_id: int,
    warning_message_id: Optional[int],
    delete_at: float,
) -> int:
    """Persist a deletion so it can be rescheduled after a restart."""
    async with _connect() as db:
        cursor = await db.execute(
            """
            INSERT INTO pending_deletions
                (chat_id, file_message_id, warning_message_id, delete_at)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, file_message_id, warning_message_id, delete_at),
        )
        await db.commit()
        row_id = cursor.lastrowid
    if row_id is None:  # pragma: no cover - sqlite always returns a rowid here
        raise RuntimeError("SQLite did not return an id for the pending deletion")
    return int(row_id)


async def get_pending_deletions() -> list[dict]:
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT id, chat_id, file_message_id, warning_message_id, delete_at
            FROM pending_deletions
            ORDER BY delete_at
            """
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def delete_pending_deletion(row_id: int) -> None:
    async with _connect() as db:
        await db.execute("DELETE FROM pending_deletions WHERE id = ?", (row_id,))
        await db.commit()


async def add_library_post(
    *,
    title: str,
    thumbnail_file_id: str,
    links_channel_id: int,
    links_message_id: int,
    links_message_link: str,
    library_channel_id: int,
    library_message_id: int,
    library_message_link: Optional[str],
    qualities: list[dict],
) -> int:
    """Insert a library post and its quality buttons in one transaction."""
    async with _connect() as db:
        cursor = await db.execute(
            """
            INSERT INTO library_posts
                (title, thumbnail_file_id, links_channel_id, links_message_id,
                 links_message_link, library_channel_id, library_message_id,
                 library_message_link)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                thumbnail_file_id,
                links_channel_id,
                links_message_id,
                links_message_link,
                library_channel_id,
                library_message_id,
                library_message_link,
            ),
        )
        post_id = cursor.lastrowid
        if post_id is None:  # pragma: no cover - sqlite always returns a rowid
            raise RuntimeError("SQLite did not return an id for the library post")

        await db.executemany(
            """
            INSERT INTO library_post_qualities
                (post_id, label, unique_id, storage_message_id, direct_link,
                 short_link, button_url, position)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    post_id,
                    quality["label"],
                    quality["unique_id"],
                    quality["storage_message_id"],
                    quality["direct_link"],
                    quality.get("short_link"),
                    quality["button_url"],
                    position,
                )
                for position, quality in enumerate(qualities)
            ],
        )
        await db.commit()

    return int(post_id)


async def get_library_post(post_id: int) -> Optional[dict]:
    """Return a library post with its qualities, or None if it is unknown."""
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT * FROM library_posts WHERE id = ?", (post_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        quality_cursor = await db.execute(
            """
            SELECT * FROM library_post_qualities
            WHERE post_id = ?
            ORDER BY position, id
            """,
            (post_id,),
        )
        quality_rows = await quality_cursor.fetchall()

    post = dict(row)
    post["qualities"] = [dict(quality) for quality in quality_rows]
    return post


async def replace_library_post_quality(
    post_id: int,
    label: str,
    *,
    unique_id: str,
    storage_message_id: int,
    direct_link: str,
    short_link: Optional[str],
    button_url: str,
) -> bool:
    """Replace one quality's stored file/link metadata."""
    async with _connect() as db:
        cursor = await db.execute(
            """
            UPDATE library_post_qualities
            SET unique_id = ?,
                storage_message_id = ?,
                direct_link = ?,
                short_link = ?,
                button_url = ?,
                updated_at = datetime('now')
            WHERE post_id = ? AND label = ?
            """,
            (
                unique_id,
                storage_message_id,
                direct_link,
                short_link,
                button_url,
                post_id,
                label,
            ),
        )
        if cursor.rowcount == 0:
            return False

        await db.execute(
            "UPDATE library_posts SET updated_at = datetime('now') WHERE id = ?",
            (post_id,),
        )
        await db.commit()

    return True
