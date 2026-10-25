"""Tests for the /deletepost catalog deletion flow."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# Importing handlers imports config, which validates the environment. Supply
# harmless defaults so the test does not depend on a developer's .env.
os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("API_ID", "123456")
os.environ.setdefault("API_HASH", "test-api-hash")
os.environ.setdefault("VPLINK_API_KEY", "test-vplink-key")
os.environ.setdefault("BOT_USERNAME", "test_series_bot")
os.environ.setdefault("STORAGE_CHANNEL", "@test_storage")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("TURSO_DATABASE_URL", "file:test-deletepost.db")
os.environ.setdefault("TURSO_AUTH_TOKEN", "")
os.environ.setdefault("LIBRARY_CHANNELS", "")

import libsql_client

import database
from handlers import library


class DeletePostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = Path(self._temp_dir.name) / "deletepost.db"
        self.client = libsql_client.create_client(
            f"file:{self._db_path.as_posix()}"
        )
        await self.client.batch(list(database._SCHEMA_STATEMENTS))
        await database._migrate_library_posts_category(self.client)
        await database._migrate_library_posts_migrated(self.client)
        database._client = self.client
        library._deletepost_sessions.clear()
        self._message_counter = 100

    async def asyncTearDown(self) -> None:
        library._deletepost_sessions.clear()
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
            links_message_link=f"https://t.me/c/1/{links_message_id}",
            library_channel_id=-1002,
            library_message_id=library_message_id,
            library_message_link=f"https://t.me/c/2/{library_message_id}",
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

    async def _add_series(
        self,
        title: str = "Delete Me",
        *,
        episodes: int = 1,
    ) -> int:
        series_id, _ = await database.add_series_with_episode(
            title=title,
            category="anime",
            thumbnail_file_id="series-thumb",
            caption=f"{title}\n\nSeries caption",
            library_channel_message_id=501,
            links_channel_message_id=502,
            episode_label="Episode 1",
            qualities=[
                {
                    "label": "1080p",
                    "short_link": None,
                    "direct_link": "https://t.me/test/1",
                }
            ],
        )
        for number in range(2, episodes + 1):
            await database.add_episode(
                series_id,
                f"Episode {number}",
                [
                    {
                        "label": "720p",
                        "short_link": None,
                        "direct_link": "https://t.me/test/2",
                    }
                ],
            )
        return series_id

    async def test_resolve_legacy_post_id(self) -> None:
        post_id = await self._add_legacy_post("Legacy One")

        entry = await database.resolve_catalog_entry(post_id)

        self.assertIsNotNone(entry)
        self.assertEqual(entry["entry_type"], "post")
        self.assertEqual(entry["data"]["id"], post_id)
        self.assertEqual(entry["data"]["title"], "Legacy One")

    async def test_resolve_series_id(self) -> None:
        series_id = await self._add_series("Series One")

        entry = await database.resolve_catalog_entry(series_id)

        self.assertIsNotNone(entry)
        self.assertEqual(entry["entry_type"], "series")
        self.assertEqual(entry["data"]["id"], series_id)
        self.assertEqual(entry["data"]["title"], "Series One")

    async def test_resolve_prefers_legacy_post_when_ids_collide(self) -> None:
        post_id = await self._add_legacy_post("Legacy Wins")
        series_id = await self._add_series("Series Loses")
        self.assertEqual(post_id, series_id)

        entry = await database.resolve_catalog_entry(post_id)

        self.assertIsNotNone(entry)
        self.assertEqual(entry["entry_type"], "post")
        self.assertEqual(entry["data"]["title"], "Legacy Wins")

    async def test_resolve_unknown_id_returns_none(self) -> None:
        self.assertIsNone(await database.resolve_catalog_entry(99999))

    async def test_deletepost_reports_missing_id(self) -> None:
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            chat=SimpleNamespace(id=1),
            command=["deletepost", "99999"],
        )

        with patch.object(library, "send_message", new=AsyncMock()) as send:
            await library.handle_deletepost(SimpleNamespace(), message)

        text = send.await_args.args[2]
        self.assertEqual(
            text,
            "No legacy post or series found with ID 99999.",
        )

    async def test_cancel_clears_pending_confirmation(self) -> None:
        library._deletepost_sessions[1] = library.DeletePostDraft(
            entry_type="post",
            entry_id=42,
            title="Cancel Me",
        )
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(chat=SimpleNamespace(id=1)),
            data="delpost:cancel",
            answer=AsyncMock(),
        )

        with patch.object(library, "send_message", new=AsyncMock()) as send:
            await library.handle_deletepost_callback(
                SimpleNamespace(), query
            )

        self.assertNotIn(1, library._deletepost_sessions)
        query.answer.assert_awaited_once_with("Cancelled")
        self.assertEqual(send.await_args.args[2], "Deletion cancelled.")

    async def test_stale_confirmation_is_rejected(self) -> None:
        library._deletepost_sessions[1] = library.DeletePostDraft(
            entry_type="post",
            entry_id=42,
            title="Still Pending",
        )
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(chat=SimpleNamespace(id=1)),
            data="delpost:confirm:series:42",
            answer=AsyncMock(),
        )

        with patch.object(library, "send_message", new=AsyncMock()) as send:
            await library.handle_deletepost_callback(
                SimpleNamespace(), query
            )

        query.answer.assert_awaited_once_with(
            "This deletion request is no longer active.",
            show_alert=True,
        )
        send.assert_not_awaited()
        self.assertIn(1, library._deletepost_sessions)

    async def test_legacy_delete_continues_after_message_failure(self) -> None:
        post_id = await self._add_legacy_post("Partial Delete")
        client = SimpleNamespace(
            delete_messages=AsyncMock(
                side_effect=[None, RuntimeError("MESSAGE_ID_INVALID")]
            )
        )

        with patch.object(library.logger, "warning"):
            result = await library._delete_catalog_entry(
                client, "post", post_id
            )

        self.assertIsNotNone(result)
        self.assertTrue(result.library_ok)
        self.assertFalse(result.links_ok)
        self.assertIn("MESSAGE_ID_INVALID", result.links_error or "")
        self.assertTrue(result.db_ok)
        self.assertIsNone(await database.get_library_post(post_id))

        quality_count = await self.client.execute(
            "SELECT COUNT(*) FROM library_post_qualities WHERE post_id = ?",
            (post_id,),
        )
        self.assertEqual(int(quality_count.rows[0][0]), 0)

        report = library._format_catalog_delete_report(result)
        self.assertIn("✅ Deleted Library Channel message", report)
        self.assertIn(
            "⚠️ Links Channel message was already deleted (skipped)",
            report,
        )
        self.assertIn("✅ Removed database records", report)

    async def test_series_delete_cascades_episodes_and_links(self) -> None:
        series_id = await self._add_series("Cascade Me", episodes=2)
        client = SimpleNamespace(delete_messages=AsyncMock())

        with patch.object(
            library,
            "_resolve_series_delete_channels",
            new=AsyncMock(
                return_value=(
                    SimpleNamespace(id=-100123456),
                    SimpleNamespace(id=-100654321),
                )
            ),
        ):
            result = await library._delete_catalog_entry(
                client, "series", series_id
            )

        self.assertIsNotNone(result)
        self.assertTrue(result.library_ok)
        self.assertTrue(result.links_ok)
        self.assertTrue(result.db_ok)
        self.assertEqual(result.episode_count, 2)
        self.assertEqual(result.link_count, 2)
        self.assertIsNone(await database.get_series(series_id))

        episode_count = await self.client.execute(
            "SELECT COUNT(*) FROM episodes WHERE series_id = ?",
            (series_id,),
        )
        self.assertEqual(int(episode_count.rows[0][0]), 0)
        link_count = await self.client.execute(
            """
            SELECT COUNT(*)
            FROM episode_links l
            JOIN episodes e ON e.id = l.episode_id
            WHERE e.series_id = ?
            """,
            (series_id,),
        )
        self.assertEqual(int(link_count.rows[0][0]), 0)

        report = library._format_catalog_delete_report(result)
        self.assertIn("✅ Deleted Library Channel message", report)
        self.assertIn("✅ Deleted Links Channel message", report)
        self.assertIn("✅ Removed database records", report)
        self.assertIn("Removed 2 episodes and their links", report)


if __name__ == "__main__":
    unittest.main()
