"""Tests for the legacy-post migration state and series bridge.

The tests use libSQL's local ``file:`` client, so they exercise the real SQL
used by :mod:`database` without needing a Turso token or network access.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

# Importing database imports config, which validates the environment at import
# time. Supply harmless defaults so the test does not depend on a developer's
# .env. Existing environment values still win.
os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("API_ID", "123456")
os.environ.setdefault("API_HASH", "test-api-hash")
os.environ.setdefault("VPLINK_API_KEY", "test-vplink-key")
os.environ.setdefault("BOT_USERNAME", "test_series_bot")
os.environ.setdefault("STORAGE_CHANNEL", "@test_storage")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("TURSO_DATABASE_URL", "file:test-migration.db")
os.environ.setdefault("TURSO_AUTH_TOKEN", "")
os.environ.setdefault("LIBRARY_CHANNELS", "")

import libsql_client

import database


class MigrationStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = Path(self._temp_dir.name) / "migration.db"
        self.client = libsql_client.create_client(
            f"file:{self._db_path.as_posix()}"
        )
        await self.client.batch(list(database._SCHEMA_STATEMENTS))
        await database._migrate_library_posts_category(self.client)
        await database._migrate_library_posts_migrated(self.client)
        database._client = self.client
        self._message_counter = 100

    async def asyncTearDown(self) -> None:
        database._client = None
        await self.client.close()
        self._temp_dir.cleanup()

    async def _add_legacy_post(
        self,
        title: str,
        *,
        category: str = "anime",
        quality: str = "1080p",
    ) -> int:
        self._message_counter += 2
        links_message_id = self._message_counter
        library_message_id = self._message_counter + 1
        return await database.add_library_post(
            title=title,
            category=category,
            thumbnail_file_id=f"thumb-{library_message_id}",
            links_channel_id=-1001,
            links_message_id=links_message_id,
            links_message_link=(
                f"https://t.me/c/1/{links_message_id}"
            ),
            library_channel_id=-1002,
            library_message_id=library_message_id,
            library_message_link=(
                f"https://t.me/c/2/{library_message_id}"
            ),
            qualities=[
                {
                    "label": quality,
                    "unique_id": f"uid-{library_message_id}",
                    "storage_message_id": library_message_id,
                    "direct_link": (
                        f"https://t.me/test_series_bot?start=uid-"
                        f"{library_message_id}"
                    ),
                    "short_link": None,
                    "button_url": (
                        f"https://t.me/test_series_bot?start=uid-"
                        f"{library_message_id}"
                    ),
                }
            ],
        )

    async def test_new_posts_start_pending_and_are_oldest_first(self) -> None:
        first = await self._add_legacy_post("First")
        second = await self._add_legacy_post("Second")

        status = await database.count_library_post_migration_status()
        self.assertEqual(
            status, {"total": 2, "migrated": 0, "pending": 2}
        )

        next_post = await database.get_next_unmigrated_post()
        self.assertIsNotNone(next_post)
        self.assertEqual(next_post["id"], first)

        after_first = await database.get_next_unmigrated_post(first)
        self.assertIsNotNone(after_first)
        self.assertEqual(after_first["id"], second)

    async def test_marking_is_idempotent_and_skips_migrated_rows(self) -> None:
        first = await self._add_legacy_post("First")
        second = await self._add_legacy_post("Second")

        self.assertTrue(await database.mark_library_post_migrated(first))
        self.assertTrue(await database.mark_library_post_migrated(first))

        status = await database.count_library_post_migration_status()
        self.assertEqual(
            status, {"total": 2, "migrated": 1, "pending": 1}
        )

        next_post = await database.get_next_unmigrated_post()
        self.assertIsNotNone(next_post)
        self.assertEqual(next_post["id"], second)

        self.assertTrue(await database.mark_library_post_migrated(second))
        status = await database.count_library_post_migration_status()
        self.assertEqual(
            status, {"total": 2, "migrated": 2, "pending": 0}
        )
        self.assertIsNone(await database.get_next_unmigrated_post())

    async def test_new_series_migration_reuses_legacy_message_ids(self) -> None:
        post_id = await self._add_legacy_post(
            "Existing Show", category="tamil", quality="720p"
        )
        legacy = await database.get_library_post(post_id)
        self.assertIsNotNone(legacy)

        series_id = await database.create_series_from_library_post(post_id)
        self.assertIsNotNone(series_id)

        series = await database.get_series(series_id)
        self.assertIsNotNone(series)
        self.assertEqual(series["title"], "Existing Show")
        self.assertEqual(series["category"], "tamil")
        self.assertEqual(
            series["caption"], "Existing Show"
        )
        self.assertEqual(
            series["thumbnail_file_id"], legacy["thumbnail_file_id"]
        )
        self.assertEqual(
            series["library_channel_message_id"],
            legacy["library_message_id"],
        )
        self.assertEqual(
            series["links_channel_message_id"],
            legacy["links_message_id"],
        )
        self.assertEqual(series["episode_count"], 1)
        self.assertEqual(series["link_count"], 1)
        self.assertEqual(
            series["episodes"][0]["episode_label"], "Episode 1"
        )
        self.assertEqual(
            series["episodes"][0]["links"][0]["quality_label"], "720p"
        )

        # Creating the series must not mark the legacy post migrated; the
        # caller marks it only after the Links Channel message was edited.
        status = await database.count_library_post_migration_status()
        self.assertEqual(status["migrated"], 0)
        self.assertEqual(status["pending"], 1)

        self.assertTrue(await database.set_library_post_migrated(post_id, True))
        status = await database.count_library_post_migration_status()
        self.assertEqual(
            status, {"total": 1, "migrated": 1, "pending": 0}
        )

    async def test_append_to_existing_series_copies_links(self) -> None:
        first_post = await self._add_legacy_post(
            "Pilot", quality="1080p"
        )
        series_id = await database.create_series_from_library_post(first_post)
        self.assertIsNotNone(series_id)
        self.assertTrue(
            await database.mark_library_post_migrated(first_post)
        )

        second_post = await self._add_legacy_post(
            "Episode Two", quality="480p"
        )
        episode_id = await database.append_library_post_to_series(
            second_post, series_id, "Episode 2"
        )
        self.assertIsNotNone(episode_id)

        series = await database.get_series(series_id)
        self.assertIsNotNone(series)
        self.assertEqual(series["episode_count"], 2)
        self.assertEqual(series["link_count"], 2)
        self.assertEqual(
            [episode["episode_label"] for episode in series["episodes"]],
            ["Episode 1", "Episode 2"],
        )
        self.assertEqual(
            series["episodes"][1]["links"][0]["quality_label"], "480p"
        )
        self.assertEqual(
            await database.count_series_links(series_id), 2
        )

        # The second legacy post remains pending until the caller marks it.
        status = await database.count_library_post_migration_status()
        self.assertEqual(status["pending"], 1)
        self.assertTrue(
            await database.set_library_post_migrated(second_post, True)
        )
        status = await database.count_library_post_migration_status()
        self.assertEqual(
            status, {"total": 2, "migrated": 2, "pending": 0}
        )

    async def test_migrated_column_is_added_to_an_old_table(self) -> None:
        old_temp = tempfile.TemporaryDirectory()
        self.addCleanup(old_temp.cleanup)
        old_path = Path(old_temp.name) / "old.db"
        old_client = libsql_client.create_client(
            f"file:{old_path.as_posix()}"
        )
        self.addAsyncCleanup(old_client.close)
        await old_client.execute(
            """
            CREATE TABLE library_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL
            )
            """
        )
        await old_client.execute(
            "INSERT INTO library_posts (title) VALUES ('legacy row')"
        )

        await database._migrate_library_posts_migrated(old_client)
        # Running it again must be a no-op, not a duplicate-column error.
        await database._migrate_library_posts_migrated(old_client)

        columns = await old_client.execute(
            """
            SELECT name FROM pragma_table_info('library_posts')
            WHERE name = 'migrated'
            """
        )
        self.assertEqual(len(columns.rows), 1)
        row = await old_client.execute(
            "SELECT migrated FROM library_posts"
        )
        self.assertEqual(int(row.rows[0][0]), 0)


if __name__ == "__main__":
    unittest.main()
