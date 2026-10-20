"""Unit tests for per-category channel validation and failure isolation."""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

# Importing utils.channels imports config, which validates the environment at
# module import time. Supply harmless defaults so the unit test does not depend
# on a developer's .env file. Existing environment values still win.
os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("API_ID", "123456")
os.environ.setdefault("API_HASH", "test-api-hash")
os.environ.setdefault("VPLINK_API_KEY", "test-vplink-key")
os.environ.setdefault("BOT_USERNAME", "test_library_bot")
os.environ.setdefault("STORAGE_CHANNEL", "@test_storage")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("TURSO_DATABASE_URL", "file:test-validation.db")
os.environ.setdefault("TURSO_AUTH_TOKEN", "")
os.environ.setdefault("LIBRARY_CHANNELS", "")

from utils.channels import resolve_library_category, validate_library_channels


class FakeChat:
    def __init__(self, chat_id: int, title: str) -> None:
        self.id = chat_id
        self.title = title
        self.username = None


async def fake_resolve_channel(app, chat_ref, env_name):
    if chat_ref == "bad":
        raise RuntimeError(f"Cannot resolve {env_name}={chat_ref!r}")
    return FakeChat(-1000 - len(str(chat_ref)), str(chat_ref))


class ValidateLibraryChannelsTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_bad_category_does_not_block_the_others(self) -> None:
        with patch(
            "utils.channels.resolve_channel", new=fake_resolve_channel
        ), patch(
            "utils.channels.require_channel_admin", new=AsyncMock()
        ), self.assertLogs("utils.channels", level="ERROR") as logs:
            resolved = await validate_library_channels(
                None, {"good": "@good", "bad": "bad"}
            )

        self.assertEqual(list(resolved), ["good"])
        self.assertTrue(
            any("'bad'" in message for message in logs.output),
            logs.output,
        )

    async def test_missing_category_has_a_clear_error(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "not configured"):
            await resolve_library_category(None, "missing", {"anime": "@a"})


if __name__ == "__main__":
    unittest.main()
