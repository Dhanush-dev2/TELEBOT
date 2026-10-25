"""Tests for /editepisode and the migration episode-label regression."""

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
os.environ.setdefault("TURSO_DATABASE_URL", "file:test-edit-episode.db")
os.environ.setdefault("TURSO_AUTH_TOKEN", "")
os.environ.setdefault("LIBRARY_CHANNELS", "")

import libsql_client

import database
from handlers import library


class EditEpisodeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = Path(self._temp_dir.name) / "edit-episode.db"
        self.client = libsql_client.create_client(
            f"file:{self._db_path.as_posix()}"
        )
        await self.client.batch(list(database._SCHEMA_STATEMENTS))
        await database._migrate_library_posts_category(self.client)
        await database._migrate_library_posts_migrated(self.client)
        database._client = self.client
        library._editepisode_sessions.clear()
        library._migrate_sessions.clear()
        self._message_counter = 100

    async def asyncTearDown(self) -> None:
        library._editepisode_sessions.clear()
        library._migrate_sessions.clear()
        database._client = None
        await self.client.close()
        self._temp_dir.cleanup()

    async def _add_series(
        self,
        title: str = "Edit Me",
        *,
        episodes: list[tuple[str, list[dict]]] | None = None,
    ) -> int:
        if episodes is None:
            episodes = [
                (
                    "Episode 1",
                    [
                        {
                            "label": "1080p",
                            "short_link": None,
                            "direct_link": "https://t.me/test/1",
                        }
                    ],
                )
            ]
        first_label, first_qualities = episodes[0]
        series_id, _ = await database.add_series_with_episode(
            title=title,
            category="anime",
            thumbnail_file_id="series-thumb",
            caption=f"{title}\n\nSeries caption",
            library_channel_message_id=501,
            links_channel_message_id=502,
            episode_label=first_label,
            qualities=first_qualities,
        )
        for label, qualities in episodes[1:]:
            await database.add_episode(series_id, label, qualities)
        return series_id

    async def _add_legacy_post(self, title: str) -> int:
        self._message_counter += 2
        links_message_id = self._message_counter
        library_message_id = self._message_counter + 1
        return await database.add_library_post(
            title=title,
            category="anime",
            thumbnail_file_id=f"thumb-{library_message_id}",
            links_channel_id=-1001,
            links_message_id=links_message_id,
            links_message_link=f"https://t.me/c/1/{links_message_id}",
            library_channel_id=-1002,
            library_message_id=library_message_id,
            library_message_link=f"https://t.me/c/2/{library_message_id}",
            qualities=[
                {
                    "label": "1080p",
                    "unique_id": f"uid-{library_message_id}",
                    "storage_message_id": library_message_id,
                    "direct_link": "https://t.me/test_series_bot?start=old",
                    "short_link": None,
                    "button_url": "https://t.me/test_series_bot?start=old",
                }
            ],
        )

    def _chat(self) -> SimpleNamespace:
        return SimpleNamespace(id=-100123, username=None)

    async def test_resolve_episode_by_number(self) -> None:
        series_id = await self._add_series(
            episodes=[
                (
                    "Episode 1",
                    [
                        {
                            "label": "1080p",
                            "short_link": None,
                            "direct_link": "https://t.me/test/1",
                        }
                    ],
                ),
                (
                    "Episode 2",
                    [
                        {
                            "label": "720p",
                            "short_link": None,
                            "direct_link": "https://t.me/test/2",
                        }
                    ],
                ),
            ]
        )
        series = await database.get_series(series_id)
        self.assertIsNotNone(series)

        matches = library._resolve_episode_candidates(series, "2")

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["id"], series["episodes"][1]["id"])
        self.assertEqual(matches[0]["episode_label"], "Episode 2")

    async def test_resolve_episode_by_label(self) -> None:
        series_id = await self._add_series(
            episodes=[
                (
                    "Episode 1",
                    [
                        {
                            "label": "1080p",
                            "short_link": None,
                            "direct_link": "https://t.me/test/1",
                        }
                    ],
                ),
                (
                    "Special",
                    [
                        {
                            "label": "720p",
                            "short_link": None,
                            "direct_link": "https://t.me/test/2",
                        }
                    ],
                ),
            ]
        )
        series = await database.get_series(series_id)
        self.assertIsNotNone(series)

        matches = library._resolve_episode_candidates(series, "special")

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["id"], series["episodes"][1]["id"])

    async def test_ambiguous_query_shows_picker(self) -> None:
        # "2" matches both the second position and the first episode's label.
        series_id = await self._add_series(
            episodes=[
                (
                    "2",
                    [
                        {
                            "label": "1080p",
                            "short_link": None,
                            "direct_link": "https://t.me/test/1",
                        }
                    ],
                ),
                (
                    "Episode 2",
                    [
                        {
                            "label": "720p",
                            "short_link": None,
                            "direct_link": "https://t.me/test/2",
                        }
                    ],
                ),
            ]
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            chat=SimpleNamespace(id=1),
            command=["editepisode", str(series_id), "2"],
        )

        with (
            patch.object(
                library,
                "_ensure_library_config",
                new=AsyncMock(return_value=self._chat()),
            ),
            patch.object(library, "send_message", new=AsyncMock()) as send,
        ):
            await library.handle_editepisode(SimpleNamespace(), message)

        text = send.await_args.args[2]
        self.assertIn("Multiple episodes match", text)
        keyboard = send.await_args.kwargs["reply_markup"]
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        ]
        self.assertEqual(len(callbacks), 2)
        self.assertTrue(
            all(callback.startswith("edep:pick:") for callback in callbacks)
        )
        self.assertIsNone(library._editepisode_sessions[1].episode)

    async def test_label_command_stages_without_database_change(self) -> None:
        series_id = await self._add_series()
        series = await database.get_series(series_id)
        self.assertIsNotNone(series)
        draft = library.EditEpisodeDraft(
            user_id=1,
            series=series,
            series_id=series_id,
            episode=series["episodes"][0],
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            chat=SimpleNamespace(id=1),
            command=["label", "Episode", "2"],
            text="/label Episode 2",
            caption=None,
            video=None,
            document=None,
        )

        with (
            patch.object(library, "send_message", new=AsyncMock()) as send,
            patch.object(
                library, "_send_edit_episode_menu", new=AsyncMock()
            ),
        ):
            await library._handle_editepisode_message(
                SimpleNamespace(), message, draft
            )

        self.assertEqual(draft.new_label, "Episode 2")
        self.assertIn("Episode label queued", send.await_args.args[2])
        unchanged = await database.get_series(series_id)
        self.assertIsNotNone(unchanged)
        self.assertEqual(
            unchanged["episodes"][0]["episode_label"], "Episode 1"
        )

    async def test_url_command_stages_direct_and_clears_short(self) -> None:
        series_id = await self._add_series()
        series = await database.get_series(series_id)
        self.assertIsNotNone(series)
        episode = series["episodes"][0]
        link = episode["links"][0]
        draft = library.EditEpisodeDraft(
            user_id=1,
            series=series,
            series_id=series_id,
            episode=episode,
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            chat=SimpleNamespace(id=1),
            command=["url", "1080p", "https://example.com/staged"],
            text="/url 1080p https://example.com/staged",
            caption=None,
            video=None,
            document=None,
        )

        with (
            patch.object(library, "send_message", new=AsyncMock()),
            patch.object(
                library, "_send_edit_episode_menu", new=AsyncMock()
            ),
        ):
            await library._handle_editepisode_message(
                SimpleNamespace(), message, draft
            )

        staged = draft.link_edits[int(link["id"])]
        self.assertEqual(staged.direct_link, "https://example.com/staged")
        self.assertIsNone(staged.short_link)
        self.assertTrue(staged.short_link_set)

    async def test_shorturl_and_qlabel_commands_stage_changes(self) -> None:
        series_id = await self._add_series()
        series = await database.get_series(series_id)
        self.assertIsNotNone(series)
        episode = series["episodes"][0]
        link = episode["links"][0]
        draft = library.EditEpisodeDraft(
            user_id=1,
            series=series,
            series_id=series_id,
            episode=episode,
        )

        short_message = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            chat=SimpleNamespace(id=1),
            command=["shorturl", "1080p", "https://short.example/new"],
            text="/shorturl 1080p https://short.example/new",
            caption=None,
            video=None,
            document=None,
        )
        label_message = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            chat=SimpleNamespace(id=1),
            command=["qlabel", "1080p", "1080p", "HEVC"],
            text="/qlabel 1080p 1080p HEVC",
            caption=None,
            video=None,
            document=None,
        )

        with (
            patch.object(library, "send_message", new=AsyncMock()),
            patch.object(
                library, "_send_edit_episode_menu", new=AsyncMock()
            ),
        ):
            await library._handle_editepisode_message(
                SimpleNamespace(), short_message, draft
            )
            await library._handle_editepisode_message(
                SimpleNamespace(), label_message, draft
            )

        staged = draft.link_edits[int(link["id"])]
        self.assertEqual(staged.short_link, "https://short.example/new")
        self.assertTrue(staged.short_link_set)
        self.assertEqual(staged.new_quality_label, "1080p HEVC")

    async def test_file_caption_stages_quality_replacement(self) -> None:
        series_id = await self._add_series()
        series = await database.get_series(series_id)
        self.assertIsNotNone(series)
        episode = series["episodes"][0]
        link = episode["links"][0]
        draft = library.EditEpisodeDraft(
            user_id=1,
            series=series,
            series_id=series_id,
            episode=episode,
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            chat=SimpleNamespace(id=1),
            id=777,
            command=None,
            text=None,
            caption="1080p",
            video=object(),
            document=None,
        )

        with (
            patch.object(library, "send_message", new=AsyncMock()),
            patch.object(
                library, "_send_edit_episode_menu", new=AsyncMock()
            ),
        ):
            await library._handle_editepisode_message(
                SimpleNamespace(), message, draft
            )

        staged = draft.link_edits[int(link["id"])]
        self.assertEqual(staged.file_chat_id, 1)
        self.assertEqual(staged.file_message_id, 777)
        self.assertEqual(staged.file_type, "video")

    async def test_callback_pick_selects_episode(self) -> None:
        series_id = await self._add_series()
        series = await database.get_series(series_id)
        self.assertIsNotNone(series)
        episode = series["episodes"][0]
        draft = library.EditEpisodeDraft(
            user_id=1, series=series, series_id=series_id
        )
        library._editepisode_sessions[1] = draft
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(chat=SimpleNamespace(id=1)),
            data=f"edep:pick:{series_id}:{episode['id']}",
            answer=AsyncMock(),
        )

        with (
            patch.object(library, "_delete_status", new=AsyncMock()),
            patch.object(
                library, "_send_edit_episode_menu", new=AsyncMock()
            ) as menu,
        ):
            await library.handle_edit_episode_callback(
                SimpleNamespace(), query
            )

        self.assertIsNotNone(draft.episode)
        self.assertEqual(draft.episode["id"], episode["id"])
        query.answer.assert_awaited_once_with("Episode selected")
        menu.assert_awaited_once()

    async def test_cancel_command_discards_changes(self) -> None:
        series_id = await self._add_series()
        series = await database.get_series(series_id)
        self.assertIsNotNone(series)
        episode = series["episodes"][0]
        draft = library.EditEpisodeDraft(
            user_id=1,
            series=series,
            series_id=series_id,
            episode=episode,
            new_label="Discard Me",
        )
        library._editepisode_sessions[1] = draft
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            chat=SimpleNamespace(id=1),
            command=["cancel"],
            text="/cancel",
        )

        with patch.object(library, "send_message", new=AsyncMock()) as send:
            await library.handle_cancel(SimpleNamespace(), message)

        self.assertNotIn(1, library._editepisode_sessions)
        self.assertIn("cancelled", send.await_args.args[2])
        unchanged = await database.get_series(series_id)
        self.assertIsNotNone(unchanged)
        self.assertEqual(
            unchanged["episodes"][0]["episode_label"], "Episode 1"
        )

    async def test_callback_cancel_discards_changes(self) -> None:
        series_id = await self._add_series()
        series = await database.get_series(series_id)
        self.assertIsNotNone(series)
        episode = series["episodes"][0]
        draft = library.EditEpisodeDraft(
            user_id=1,
            series=series,
            series_id=series_id,
            episode=episode,
            new_label="Discard Me",
        )
        library._editepisode_sessions[1] = draft
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(chat=SimpleNamespace(id=1)),
            data=f"edep:cancel:{series_id}:{episode['id']}",
            answer=AsyncMock(),
        )

        with patch.object(library, "send_message", new=AsyncMock()):
            await library.handle_edit_episode_callback(
                SimpleNamespace(), query
            )

        self.assertNotIn(1, library._editepisode_sessions)
        unchanged = await database.get_series(series_id)
        self.assertIsNotNone(unchanged)
        self.assertEqual(
            unchanged["episodes"][0]["episode_label"], "Episode 1"
        )

    async def test_replace_quality_url_updates_database_and_keyboard(
        self,
    ) -> None:
        series_id = await self._add_series()
        series = await database.get_series(series_id)
        self.assertIsNotNone(series)
        episode = series["episodes"][0]
        link = episode["links"][0]
        draft = library.EditEpisodeDraft(
            user_id=1,
            series=series,
            series_id=series_id,
            episode=episode,
            link_edits={
                int(link["id"]): library.EpisodeLinkEdit(
                    link_id=int(link["id"]),
                    quality_label="1080p",
                    direct_link="https://example.com/direct",
                    short_link=None,
                    short_link_set=True,
                )
            },
        )
        edit = AsyncMock(return_value=SimpleNamespace())

        with (
            patch.object(
                library,
                "_ensure_library_config",
                new=AsyncMock(return_value=self._chat()),
            ),
            patch.object(
                library, "edit_message_reply_markup", new=edit
            ),
            patch.object(library, "send_message", new=AsyncMock()),
        ):
            ok = await library._apply_edit_episode(
                SimpleNamespace(), 1, draft
            )

        self.assertTrue(ok)
        refreshed = await database.get_series(series_id)
        self.assertIsNotNone(refreshed)
        refreshed_link = refreshed["episodes"][0]["links"][0]
        self.assertEqual(
            refreshed_link["direct_link"], "https://example.com/direct"
        )
        self.assertIsNone(refreshed_link["short_url"])
        keyboard = edit.await_args.args[3]
        self.assertEqual(
            keyboard.inline_keyboard[0][0].url,
            "https://example.com/direct",
        )

    async def test_file_replacement_copies_file_and_updates_keyboard(
        self,
    ) -> None:
        series_id = await self._add_series()
        series = await database.get_series(series_id)
        self.assertIsNotNone(series)
        episode = series["episodes"][0]
        link = episode["links"][0]
        draft = library.EditEpisodeDraft(
            user_id=1,
            series=series,
            series_id=series_id,
            episode=episode,
            link_edits={
                int(link["id"]): library.EpisodeLinkEdit(
                    link_id=int(link["id"]),
                    quality_label="1080p",
                    file_chat_id=1,
                    file_message_id=777,
                    file_type="video",
                )
            },
        )
        stored = SimpleNamespace(
            direct_link="https://example.com/replaced",
            short_link=None,
        )
        edit = AsyncMock(return_value=SimpleNamespace())

        with (
            patch.object(
                library,
                "_ensure_library_config",
                new=AsyncMock(return_value=self._chat()),
            ),
            patch.object(
                library, "store_message", new=AsyncMock(return_value=stored)
            ) as store,
            patch.object(
                library, "edit_message_reply_markup", new=edit
            ),
            patch.object(library, "send_message", new=AsyncMock()),
        ):
            ok = await library._apply_edit_episode(
                SimpleNamespace(), 1, draft
            )

        self.assertTrue(ok)
        store.assert_awaited_once()
        self.assertIsNone(draft.link_edits[int(link["id"])].file_message_id)
        refreshed = await database.get_series(series_id)
        self.assertIsNotNone(refreshed)
        refreshed_link = refreshed["episodes"][0]["links"][0]
        self.assertEqual(
            refreshed_link["direct_link"],
            "https://example.com/replaced",
        )
        keyboard = edit.await_args.args[3]
        self.assertEqual(
            keyboard.inline_keyboard[0][0].url,
            "https://example.com/replaced",
        )

    async def test_update_episode_details_missing_episode_returns_false(
        self,
    ) -> None:
        updated = await database.update_episode_details(
            999999, "Episode X", []
        )

        self.assertFalse(updated)

    async def test_rename_label_updates_database_and_keyboard(self) -> None:
        series_id = await self._add_series()
        series = await database.get_series(series_id)
        self.assertIsNotNone(series)
        episode = series["episodes"][0]
        draft = library.EditEpisodeDraft(
            user_id=1,
            series=series,
            series_id=series_id,
            episode=episode,
            new_label="Episode 2 Renamed",
        )
        library._editepisode_sessions[1] = draft
        edit = AsyncMock(return_value=SimpleNamespace())

        with (
            patch.object(
                library,
                "_ensure_library_config",
                new=AsyncMock(return_value=self._chat()),
            ),
            patch.object(
                library, "edit_message_reply_markup", new=edit
            ),
            patch.object(library, "send_message", new=AsyncMock()),
        ):
            await library._finish_edit_episode(
                SimpleNamespace(), 1, draft
            )

        refreshed = await database.get_series(series_id)
        self.assertIsNotNone(refreshed)
        self.assertEqual(
            refreshed["episodes"][0]["episode_label"], "Episode 2 Renamed"
        )
        self.assertNotIn(1, library._editepisode_sessions)

        self.assertEqual(edit.await_count, 1)
        args = edit.await_args.args
        self.assertEqual(args[1], -100123)
        self.assertEqual(args[2], 502)
        keyboard = args[3]
        self.assertIn(
            "Episode 2 Renamed", keyboard.inline_keyboard[0][0].text
        )

    async def test_replace_quality_link_updates_database_and_keyboard(
        self,
    ) -> None:
        series_id = await self._add_series()
        series = await database.get_series(series_id)
        self.assertIsNotNone(series)
        episode = series["episodes"][0]
        link = episode["links"][0]
        draft = library.EditEpisodeDraft(
            user_id=1,
            series=series,
            series_id=series_id,
            episode=episode,
            link_edits={
                int(link["id"]): library.EpisodeLinkEdit(
                    link_id=int(link["id"]),
                    quality_label="1080p",
                    new_quality_label="1080p HEVC",
                    direct_link="https://example.com/new-link",
                    short_link=None,
                    short_link_set=True,
                )
            },
        )
        edit = AsyncMock(return_value=SimpleNamespace())

        with (
            patch.object(
                library,
                "_ensure_library_config",
                new=AsyncMock(return_value=self._chat()),
            ),
            patch.object(
                library, "edit_message_reply_markup", new=edit
            ),
            patch.object(library, "send_message", new=AsyncMock()),
        ):
            ok = await library._apply_edit_episode(
                SimpleNamespace(), 1, draft
            )

        self.assertTrue(ok)
        refreshed = await database.get_series(series_id)
        self.assertIsNotNone(refreshed)
        refreshed_link = refreshed["episodes"][0]["links"][0]
        self.assertEqual(refreshed_link["quality_label"], "1080p HEVC")
        self.assertEqual(
            refreshed_link["direct_link"], "https://example.com/new-link"
        )
        self.assertIsNone(refreshed_link["short_url"])

        keyboard = edit.await_args.args[3]
        button = keyboard.inline_keyboard[0][0]
        self.assertIn("1080p HEVC", button.text)
        self.assertEqual(button.url, "https://example.com/new-link")

    async def test_telegram_edit_failure_rolls_back_database(self) -> None:
        series_id = await self._add_series()
        series = await database.get_series(series_id)
        self.assertIsNotNone(series)
        episode = series["episodes"][0]
        draft = library.EditEpisodeDraft(
            user_id=1,
            series=series,
            series_id=series_id,
            episode=episode,
            new_label="Should Roll Back",
        )
        library._editepisode_sessions[1] = draft

        with (
            patch.object(
                library,
                "_ensure_library_config",
                new=AsyncMock(return_value=self._chat()),
            ),
            patch.object(
                library,
                "edit_message_reply_markup",
                new=AsyncMock(return_value=None),
            ),
            patch.object(library, "send_message", new=AsyncMock()) as send,
        ):
            ok = await library._apply_edit_episode(
                SimpleNamespace(), 1, draft
            )

        self.assertFalse(ok)
        refreshed = await database.get_series(series_id)
        self.assertIsNotNone(refreshed)
        self.assertEqual(
            refreshed["episodes"][0]["episode_label"], "Episode 1"
        )
        self.assertIn(1, library._editepisode_sessions)
        self.assertIn("rolled back", send.await_args.args[2])

    async def test_telegram_edit_failure_restores_quality_link(
        self,
    ) -> None:
        series_id = await self._add_series()
        series = await database.get_series(series_id)
        self.assertIsNotNone(series)
        episode = series["episodes"][0]
        link = episode["links"][0]
        draft = library.EditEpisodeDraft(
            user_id=1,
            series=series,
            series_id=series_id,
            episode=episode,
            link_edits={
                int(link["id"]): library.EpisodeLinkEdit(
                    link_id=int(link["id"]),
                    quality_label="1080p",
                    direct_link="https://example.com/not-applied",
                    short_link=None,
                    short_link_set=True,
                )
            },
        )
        library._editepisode_sessions[1] = draft

        with (
            patch.object(
                library,
                "_ensure_library_config",
                new=AsyncMock(return_value=self._chat()),
            ),
            patch.object(
                library,
                "edit_message_reply_markup",
                new=AsyncMock(return_value=None),
            ),
            patch.object(library, "send_message", new=AsyncMock()) as send,
        ):
            ok = await library._apply_edit_episode(
                SimpleNamespace(), 1, draft
            )

        self.assertFalse(ok)
        refreshed = await database.get_series(series_id)
        self.assertIsNotNone(refreshed)
        refreshed_link = refreshed["episodes"][0]["links"][0]
        self.assertEqual(
            refreshed_link["direct_link"], "https://t.me/test/1"
        )
        self.assertIn(1, library._editepisode_sessions)
        self.assertIn("rolled back", send.await_args.args[2])

    async def test_bare_editepisode_opens_series_picker(self) -> None:
        series_id = await self._add_series(title="Picker Series")
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            chat=SimpleNamespace(id=1),
            command=["editepisode"],
        )
        fake_settings = SimpleNamespace(
            library_channels={"anime": object(), "movies": object()}
        )

        with (
            patch.object(library, "settings", fake_settings),
            patch.object(library, "send_message", new=AsyncMock()) as send,
        ):
            await library.handle_editepisode(SimpleNamespace(), message)

        text = send.await_args.args[2]
        keyboard = send.await_args.kwargs["reply_markup"]
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        ]
        self.assertIn("Edit an episode", text)
        self.assertIn(f"edep:s:{series_id}", callbacks)
        self.assertIn("edep:c:-1:0", callbacks)
        self.assertIn("edep:c:0:0", callbacks)
        self.assertIn("edep:c:1:0", callbacks)
        series_button = next(
            button
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data == f"edep:s:{series_id}"
        )
        self.assertIn("Picker Series", series_button.text)
        self.assertIn(str(series_id), series_button.text)

    async def test_editepisode_with_series_id_opens_episode_picker(
        self,
    ) -> None:
        series_id = await self._add_series(
            episodes=[
                (
                    "Episode 1",
                    [
                        {
                            "label": "1080p",
                            "short_link": None,
                            "direct_link": "https://t.me/test/1",
                        }
                    ],
                ),
                (
                    "Episode 2",
                    [
                        {
                            "label": "720p",
                            "short_link": None,
                            "direct_link": "https://t.me/test/2",
                        }
                    ],
                ),
            ]
        )
        series = await database.get_series(series_id)
        self.assertIsNotNone(series)
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            chat=SimpleNamespace(id=1),
            command=["editepisode", str(series_id)],
        )

        with patch.object(library, "send_message", new=AsyncMock()) as send:
            await library.handle_editepisode(SimpleNamespace(), message)

        text = send.await_args.args[2]
        keyboard = send.await_args.kwargs["reply_markup"]
        buttons = [
            button
            for row in keyboard.inline_keyboard
            for button in row
        ]
        callbacks = [button.callback_data for button in buttons]
        self.assertIn("Choose an episode to edit", text)
        self.assertEqual(len(callbacks), 2)
        self.assertTrue(
            all(callback.startswith("edep:pick:") for callback in callbacks)
        )
        for episode in series["episodes"]:
            self.assertTrue(
                any(
                    episode["episode_label"] in button.text
                    and str(episode["id"]) in button.text
                    for button in buttons
                )
            )
        draft = library._editepisode_sessions[1]
        self.assertIsNone(draft.episode)
        self.assertEqual(draft.stage, "episode_pick")

    async def test_series_picker_callback_opens_episode_picker(self) -> None:
        series_id = await self._add_series(
            episodes=[
                (
                    "Episode 1",
                    [
                        {
                            "label": "1080p",
                            "short_link": None,
                            "direct_link": "https://t.me/test/1",
                        }
                    ],
                ),
                (
                    "Episode 2",
                    [
                        {
                            "label": "720p",
                            "short_link": None,
                            "direct_link": "https://t.me/test/2",
                        }
                    ],
                ),
            ]
        )
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(chat=SimpleNamespace(id=1), id=77),
            data=f"edep:s:{series_id}",
            answer=AsyncMock(),
        )

        with (
            patch.object(library, "_delete_status", new=AsyncMock()),
            patch.object(library, "send_message", new=AsyncMock()) as send,
        ):
            await library.handle_edit_episode_callback(
                SimpleNamespace(), query
            )

        self.assertIn(1, library._editepisode_sessions)
        draft = library._editepisode_sessions[1]
        self.assertEqual(draft.series_id, series_id)
        self.assertIsNone(draft.episode)
        self.assertEqual(draft.stage, "episode_pick")
        text = send.await_args.args[2]
        keyboard = send.await_args.kwargs["reply_markup"]
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        ]
        self.assertIn("Choose an episode to edit", text)
        self.assertEqual(len(callbacks), 2)
        self.assertTrue(
            all(callback.startswith("edep:pick:") for callback in callbacks)
        )
        query.answer.assert_awaited_once_with("Series selected")

    def test_resolve_episode_by_database_id(self) -> None:
        series = {
            "episodes": [
                {"id": 99, "episode_label": "Episode 1", "links": []},
                {"id": 100, "episode_label": "Episode 2", "links": []},
            ]
        }

        for query, expected_id in (
            ("99", 99),
            ("#100", 100),
            ("id 100", 100),
            ("ID:100", 100),
        ):
            matches = library._resolve_episode_candidates(series, query)
            self.assertEqual(len(matches), 1, msg=query)
            self.assertEqual(matches[0]["id"], expected_id, msg=query)

    async def test_direct_episode_selection_by_position_opens_menu(
        self,
    ) -> None:
        series_id = await self._add_series(
            episodes=[
                (
                    "Episode 1",
                    [
                        {
                            "label": "1080p",
                            "short_link": None,
                            "direct_link": "https://t.me/test/1",
                        }
                    ],
                ),
                (
                    "Episode 2",
                    [
                        {
                            "label": "720p",
                            "short_link": None,
                            "direct_link": "https://t.me/test/2",
                        }
                    ],
                ),
            ]
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            chat=SimpleNamespace(id=1),
            command=["editepisode", str(series_id), "2"],
        )

        with patch.object(library, "send_message", new=AsyncMock()) as send:
            await library.handle_editepisode(SimpleNamespace(), message)

        draft = library._editepisode_sessions[1]
        self.assertIsNotNone(draft.episode)
        self.assertEqual(draft.episode["episode_label"], "Episode 2")
        self.assertIn("Edit episode", send.await_args.args[2])
        self.assertTrue(
            any(
                "Rename Episode" in button.text
                for row in send.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
            )
        )

    async def test_direct_episode_selection_by_label_opens_menu(
        self,
    ) -> None:
        series_id = await self._add_series(
            episodes=[
                (
                    "Episode 1",
                    [
                        {
                            "label": "1080p",
                            "short_link": None,
                            "direct_link": "https://t.me/test/1",
                        }
                    ],
                ),
                (
                    "Special",
                    [
                        {
                            "label": "720p",
                            "short_link": None,
                            "direct_link": "https://t.me/test/2",
                        }
                    ],
                ),
            ]
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            chat=SimpleNamespace(id=1),
            command=["editepisode", str(series_id), "Special"],
        )

        with patch.object(library, "send_message", new=AsyncMock()) as send:
            await library.handle_editepisode(SimpleNamespace(), message)

        draft = library._editepisode_sessions[1]
        self.assertIsNotNone(draft.episode)
        self.assertEqual(draft.episode["episode_label"], "Special")
        self.assertIn("Edit episode", send.await_args.args[2])

    async def test_rename_updates_every_quality_button(self) -> None:
        series_id = await self._add_series(
            episodes=[
                (
                    "Episode 1",
                    [
                        {
                            "label": "1080p",
                            "short_link": None,
                            "direct_link": "https://t.me/test/1",
                        },
                        {
                            "label": "720p",
                            "short_link": None,
                            "direct_link": "https://t.me/test/2",
                        },
                    ],
                )
            ]
        )
        series = await database.get_series(series_id)
        self.assertIsNotNone(series)
        draft = library.EditEpisodeDraft(
            user_id=1,
            series=series,
            series_id=series_id,
            episode=series["episodes"][0],
            new_label="Renamed Episode",
        )
        edit = AsyncMock(return_value=SimpleNamespace())

        with (
            patch.object(
                library,
                "_ensure_library_config",
                new=AsyncMock(return_value=self._chat()),
            ),
            patch.object(
                library, "edit_message_reply_markup", new=edit
            ),
            patch.object(library, "send_message", new=AsyncMock()),
        ):
            ok = await library._apply_edit_episode(
                SimpleNamespace(), 1, draft
            )

        self.assertTrue(ok)
        keyboard = edit.await_args.args[3]
        buttons = keyboard.inline_keyboard[0]
        self.assertEqual(len(buttons), 2)
        self.assertTrue(
            all("Renamed Episode" in button.text for button in buttons)
        )
        self.assertIn("1080p", buttons[0].text)
        self.assertIn("720p", buttons[1].text)

    async def test_editepisode_rejects_placeholder_and_series_title(
        self,
    ) -> None:
        series_id = await self._add_series(title="Black Clover")
        series = await database.get_series(series_id)
        self.assertIsNotNone(series)
        draft = library.EditEpisodeDraft(
            user_id=1,
            series=series,
            series_id=series_id,
            episode=series["episodes"][0],
        )

        for raw, expected in (
            ("New episode label...", "prompt text"),
            ("Black Clover", "series title"),
            ("   ", "cannot be empty"),
        ):
            with patch.object(
                library, "send_message", new=AsyncMock()
            ) as send:
                await library._stage_episode_label(
                    SimpleNamespace(), 1, draft, raw
                )
            self.assertIsNone(draft.new_label)
            self.assertIn(expected, send.await_args.args[2])

    async def test_apply_rejects_series_title_label(self) -> None:
        series_id = await self._add_series(title="Black Clover")
        series = await database.get_series(series_id)
        self.assertIsNotNone(series)
        draft = library.EditEpisodeDraft(
            user_id=1,
            series=series,
            series_id=series_id,
            episode=series["episodes"][0],
            new_label="Black Clover",
        )
        edit = AsyncMock()

        with (
            patch.object(
                library,
                "_ensure_library_config",
                new=AsyncMock(return_value=self._chat()),
            ),
            patch.object(
                library, "edit_message_reply_markup", new=edit
            ),
            patch.object(library, "send_message", new=AsyncMock()) as send,
        ):
            ok = await library._apply_edit_episode(
                SimpleNamespace(), 1, draft
            )

        self.assertFalse(ok)
        edit.assert_not_awaited()
        self.assertIn("series title", send.await_args.args[2])
        refreshed = await database.get_series(series_id)
        self.assertIsNotNone(refreshed)
        self.assertEqual(
            refreshed["episodes"][0]["episode_label"], "Episode 1"
        )

    async def test_stale_episode_picker_cannot_replace_selected_episode(
        self,
    ) -> None:
        series_id = await self._add_series(
            episodes=[
                (
                    "Episode 1",
                    [
                        {
                            "label": "1080p",
                            "short_link": None,
                            "direct_link": "https://t.me/test/1",
                        }
                    ],
                ),
                (
                    "Episode 2",
                    [
                        {
                            "label": "720p",
                            "short_link": None,
                            "direct_link": "https://t.me/test/2",
                        }
                    ],
                ),
            ]
        )
        series = await database.get_series(series_id)
        self.assertIsNotNone(series)
        selected = series["episodes"][0]
        other = series["episodes"][1]
        draft = library.EditEpisodeDraft(
            user_id=1,
            series=series,
            series_id=series_id,
            episode=selected,
        )
        library._editepisode_sessions[1] = draft
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            message=SimpleNamespace(chat=SimpleNamespace(id=1), id=77),
            data=f"edep:pick:{series_id}:{other['id']}",
            answer=AsyncMock(),
        )

        with (
            patch.object(library, "_delete_status", new=AsyncMock()),
            patch.object(
                library, "_send_edit_episode_menu", new=AsyncMock()
            ) as menu,
        ):
            await library.handle_edit_episode_callback(
                SimpleNamespace(), query
            )

        self.assertEqual(draft.episode["id"], selected["id"])
        menu.assert_not_awaited()
        query.answer.assert_awaited_once()
        self.assertTrue(query.answer.await_args.kwargs.get("show_alert"))

    async def test_editpost_usage_points_to_editepisode(self) -> None:
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            chat=SimpleNamespace(id=1),
            command=["editpost"],
        )

        with patch.object(library, "send_message", new=AsyncMock()) as send:
            await library.handle_editpost(SimpleNamespace(), message)

        text = send.await_args.args[2]
        self.assertIn("Usage:", text)
        self.assertIn("legacy /newpost entries", text)
        self.assertIn("/editepisode", text)

    def test_migration_label_ignores_title_and_placeholder(self) -> None:
        series = {"episode_count": 2, "title": "Black Clover"}

        self.assertEqual(
            library._migration_episode_label(None, series), "Episode 3"
        )
        self.assertEqual(
            library._migration_episode_label(
                "Black Clover", series, customized=True
            ),
            "Episode 3",
        )
        self.assertEqual(
            library._migration_episode_label(
                "Black Clover Season 2: EP...", series
            ),
            "Episode 3",
        )
        self.assertEqual(
            library._migration_episode_label(
                "New episode label...", series, customized=True
            ),
            "Episode 3",
        )
        self.assertEqual(
            library._migration_episode_label(
                "Episode 3", series, customized=True
            ),
            "Episode 3",
        )
        label, error = library._validate_episode_label(
            "New episode label..."
        )
        self.assertIsNone(label)
        self.assertIsNotNone(error)

    async def test_migration_confirm_replaces_title_and_placeholder(
        self,
    ) -> None:
        post_id = await self._add_legacy_post(
            "Black Clover Season 2: EP..."
        )
        series_id = await self._add_series(title="Black Clover")
        post = await database.get_library_post(post_id)
        self.assertIsNotNone(post)

        for raw, customized in (
            ("Black Clover Season 2: EP...", False),
            ("New episode label...", True),
        ):
            draft = library.MigrateDraft(
                post=post,
                series_id=series_id,
                episode_label=raw,
                episode_label_customized=customized,
            )
            with patch.object(
                library, "send_message", new=AsyncMock()
            ) as send:
                await library._send_migration_label_confirm(
                    SimpleNamespace(), 1, draft
                )

            text = send.await_args.args[2]
            self.assertIn(
                "New episode label:</b> Episode 2", text
            )
            self.assertEqual(draft.episode_label, "Episode 2")

    async def test_migration_pick_uses_next_episode_label(self) -> None:
        post_id = await self._add_legacy_post(
            "Black Clover Season 2: EP..."
        )
        series_id = await self._add_series()
        post = await database.get_library_post(post_id)
        self.assertIsNotNone(post)
        draft = library.MigrateDraft(post=post)
        library._migrate_sessions[1] = draft
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            message=None,
            data=f"mig:pick:{post_id}:{series_id}",
            answer=AsyncMock(),
        )

        with patch.object(
            library,
            "_send_migration_label_confirm",
            new=AsyncMock(),
        ):
            await library.handle_migration_callback(
                SimpleNamespace(), query
            )

        self.assertEqual(draft.episode_label, "Episode 2")
        self.assertFalse(draft.episode_label_customized)
        query.answer.assert_awaited_once_with("Series selected")


if __name__ == "__main__":
    unittest.main()
