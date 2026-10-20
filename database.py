"""Turso/libSQL persistence for files, users, pending deletions and library posts.

Every function uses one shared libSQL client created by :func:`init_db`. The
client supports remote Turso databases (``libsql://...``) and local ``file:``
databases for development. Schema creation and multi-statement writes use
atomic HTTP batches so a partially-created library post cannot be committed.

Turso's current database endpoints do not accept Hrana WebSocket upgrades, so
remote URLs are normalized to HTTPS and libsql-client's HTTP transport is used.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlparse, urlunparse

import libsql_client
from libsql_client import Client
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
    category            TEXT NOT NULL DEFAULT 'default',
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

_SCHEMA_STATEMENTS = tuple(
    statement.strip() for statement in _SCHEMA.split(";") if statement.strip()
)

_client: Optional[Client] = None


def _get_client() -> Client:
    if _client is None:
        raise RuntimeError("Database is not initialized; call init_db() first")
    return _client


def _http_url(url: str) -> str:
    """Return the HTTPS/HTTP form used by libsql-client's HTTP transport.

    Turso's ``libsql://`` URL is the WebSocket endpoint. Current Turso
    endpoints reject the WebSocket upgrade, while the same host serves the
    HTTP pipeline API over ``https://``. A ``file:`` URL is left untouched for
    local development. Query strings are dropped because the auth token is
    passed separately to ``create_client``.
    """
    parsed = urlparse(url)
    if parsed.scheme == "file":
        return url

    scheme = {
        "libsql": "https",
        "wss": "https",
        "https": "https",
        "ws": "http",
        "http": "http",
    }.get(parsed.scheme, parsed.scheme)
    return urlunparse((scheme, parsed.netloc, parsed.path, "", "", ""))


async def _migrate_library_posts_category(client: Client) -> None:
    """Add the category column to databases created before multi-category posts."""
    result = await client.execute(
        """
        SELECT COUNT(*) AS count
        FROM pragma_table_info('library_posts')
        WHERE name = 'category'
        """
    )
    has_category = bool(result.rows and int(result.rows[0][0]))
    if has_category:
        return

    logger.info("Adding missing category column to library_posts")
    await client.execute(
        "ALTER TABLE library_posts "
        "ADD COLUMN category TEXT NOT NULL DEFAULT 'default'"
    )


async def init_db() -> None:
    """Create the Turso schema once at startup."""
    global _client
    if _client is not None:
        return

    client = libsql_client.create_client(
        _http_url(settings.turso_database_url),
        auth_token=settings.turso_auth_token,
    )
    try:
        await client.batch(list(_SCHEMA_STATEMENTS))
        await _migrate_library_posts_category(client)
    except Exception:
        await client.close()
        raise

    _client = client
    logger.info(
        "Turso/libSQL database ready via HTTP at %s",
        _http_url(settings.turso_database_url),
    )


async def close_db() -> None:
    """Close the shared libSQL client during shutdown."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None


async def add_file(
    unique_id: str,
    message_id: int,
    file_type: str,
    file_name: Optional[str],
    uploaded_by: Optional[int],
) -> None:
    """Store (or refresh) the unique_id -> storage message mapping."""
    await _get_client().execute(
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


async def get_file(unique_id: str) -> Optional[dict]:
    """Return the file row for a deep-link ID, or None if it is unknown."""
    result = await _get_client().execute(
        "SELECT * FROM files WHERE unique_id = ?", (unique_id,)
    )
    return result.rows[0].asdict() if result.rows else None


async def _scalar(query: str, params: tuple = ()) -> int:
    result = await _get_client().execute(query, params)
    if not result.rows:
        return 0
    return int(result.rows[0][0] or 0)


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
    await _get_client().execute(
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


async def get_all_user_ids() -> list[int]:
    result = await _get_client().execute(
        "SELECT user_id FROM users ORDER BY user_id"
    )
    return [int(row["user_id"]) for row in result.rows]


async def add_pending_deletion(
    chat_id: int,
    file_message_id: int,
    warning_message_id: Optional[int],
    delete_at: float,
) -> int:
    """Persist a deletion so it can be rescheduled after a restart."""
    result = await _get_client().execute(
        """
        INSERT INTO pending_deletions
            (chat_id, file_message_id, warning_message_id, delete_at)
        VALUES (?, ?, ?, ?)
        RETURNING id
        """,
        (chat_id, file_message_id, warning_message_id, delete_at),
    )
    if not result.rows:
        raise RuntimeError("Turso did not return an id for the pending deletion")
    return int(result.rows[0]["id"])


async def get_pending_deletions() -> list[dict]:
    result = await _get_client().execute(
        """
        SELECT id, chat_id, file_message_id, warning_message_id, delete_at
        FROM pending_deletions
        ORDER BY delete_at
        """
    )
    return [row.asdict() for row in result.rows]


async def delete_pending_deletion(row_id: int) -> None:
    await _get_client().execute(
        "DELETE FROM pending_deletions WHERE id = ?", (row_id,)
    )


async def add_library_post(
    *,
    title: str,
    category: str = "default",
    thumbnail_file_id: str,
    links_channel_id: int,
    links_message_id: int,
    links_message_link: str,
    library_channel_id: int,
    library_message_id: int,
    library_message_link: Optional[str],
    qualities: list[dict],
) -> int:
    """Insert a library post and its quality buttons in one atomic batch."""
    statements: list[tuple[str, tuple]] = [
        (
            """
            INSERT INTO library_posts
                (title, category, thumbnail_file_id, links_channel_id,
                 links_message_id, links_message_link, library_channel_id,
                 library_message_id, library_message_link)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                title,
                category,
                thumbnail_file_id,
                links_channel_id,
                links_message_id,
                links_message_link,
                library_channel_id,
                library_message_id,
                library_message_link,
            ),
        )
    ]

    for position, quality in enumerate(qualities):
        # The HTTP batch is one atomic transaction on Turso. The post has just
        # been inserted, so the highest id in library_posts is this post; no
        # other writer can insert between batch statements.
        statements.append(
            (
                """
                INSERT INTO library_post_qualities
                    (post_id, label, unique_id, storage_message_id, direct_link,
                     short_link, button_url, position)
                VALUES (
                    (SELECT id FROM library_posts ORDER BY id DESC LIMIT 1),
                    ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    quality["label"],
                    quality["unique_id"],
                    quality["storage_message_id"],
                    quality["direct_link"],
                    quality.get("short_link"),
                    quality["button_url"],
                    position,
                ),
            )
        )

    results = await _get_client().batch(statements)
    if not results or not results[0].rows:
        raise RuntimeError("Turso did not return an id for the library post")
    return int(results[0].rows[0]["id"])


async def get_library_post(post_id: int) -> Optional[dict]:
    """Return a library post (including its category) with its qualities."""
    result = await _get_client().execute(
        "SELECT * FROM library_posts WHERE id = ?", (post_id,)
    )
    if not result.rows:
        return None

    quality_result = await _get_client().execute(
        """
        SELECT * FROM library_post_qualities
        WHERE post_id = ?
        ORDER BY position, id
        """,
        (post_id,),
    )

    post = result.rows[0].asdict()
    post["qualities"] = [row.asdict() for row in quality_result.rows]
    return post


async def update_library_post_location(
    post_id: int,
    *,
    category: str,
    library_channel_id: int,
    library_message_id: int,
    library_message_link: Optional[str],
) -> bool:
    """Point a post at a new category/channel message after a repost."""
    result = await _get_client().execute(
        """
        UPDATE library_posts
        SET category = ?,
            library_channel_id = ?,
            library_message_id = ?,
            library_message_link = ?,
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (
            category,
            library_channel_id,
            library_message_id,
            library_message_link,
            post_id,
        ),
    )
    return bool(result.rows_affected)


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
    results = await _get_client().batch(
        [
            (
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
            ),
            (
                """
                UPDATE library_posts
                SET updated_at = datetime('now')
                WHERE id = ?
                  AND EXISTS (
                      SELECT 1 FROM library_post_qualities
                      WHERE post_id = ? AND label = ?
                  )
                """,
                (post_id, post_id, label),
            ),
        ]
    )

    return bool(results and results[0].rows_affected)
