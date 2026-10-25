"""Regression tests for callback-query filters.

Pyrogram's ``filters.private`` expects a Message and reads ``update.chat``.
CallbackQuery objects do not have a ``.chat`` attribute, so applying
``filters.private`` to a CallbackQueryHandler raises AttributeError before the
handler runs. These tests pass query-like objects that deliberately have no
``chat`` attribute through every registered callback filter.
"""

from __future__ import annotations

import inspect
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

# Importing handlers imports config, which validates the environment. Supply
# harmless defaults so the test does not depend on a developer's .env.
os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("API_ID", "123456")
os.environ.setdefault("API_HASH", "test-api-hash")
os.environ.setdefault("VPLINK_API_KEY", "test-vplink-key")
os.environ.setdefault("BOT_USERNAME", "test_series_bot")
os.environ.setdefault("STORAGE_CHANNEL", "@test_storage")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("TURSO_DATABASE_URL", "file:test-callback-filters.db")
os.environ.setdefault("TURSO_AUTH_TOKEN", "")
os.environ.setdefault("LIBRARY_CHANNELS", "")

from pyrogram.handlers import CallbackQueryHandler

from config import settings
from handlers import library, user


def _callback_query(user_id: int, data: str) -> SimpleNamespace:
    """Build the subset of CallbackQuery that filters are allowed to inspect."""
    return SimpleNamespace(
        from_user=SimpleNamespace(
            id=user_id,
            username=None,
            is_self=False,
        ),
        data=data,
    )


async def _run_filter(filter_obj, query: SimpleNamespace) -> bool:
    """Evaluate a Pyrogram filter whether it is sync or async."""
    result = filter_obj(None, query)
    if inspect.isawaitable(result):
        result = await result
    return bool(result)


class CallbackFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_callback_accepts_query_without_chat(self) -> None:
        admin_id = next(iter(settings.admin_ids))
        query = _callback_query(admin_id, "mig:skip:1")

        self.assertFalse(hasattr(query, "chat"))
        self.assertTrue(await _run_filter(library.ADMIN_CALLBACK, query))

    async def test_admin_callback_rejects_non_admin_without_chat(self) -> None:
        query = _callback_query(0, "mig:skip:1")

        self.assertFalse(hasattr(query, "chat"))
        self.assertFalse(await _run_filter(library.ADMIN_CALLBACK, query))

    async def test_check_sub_callback_filter_does_not_require_chat(self) -> None:
        query = _callback_query(
            1, f"{user.CHECK_SUB_PREFIX}example-file"
        )

        self.assertFalse(hasattr(query, "chat"))
        self.assertTrue(await _run_filter(user.CHECK_SUB_FILTER, query))

    async def test_every_registered_callback_handler_filter_accepts_query(
        self,
    ) -> None:
        admin_id = next(iter(settings.admin_ids))
        callback_handlers: list[CallbackQueryHandler] = []
        callback_groups: list[int] = []

        for module in (library, user):
            app = MagicMock()
            module.register(app)
            for call in app.add_handler.call_args_list:
                if isinstance(call.args[0], CallbackQueryHandler):
                    callback_handlers.append(call.args[0])
                    callback_groups.append(call.kwargs.get("group", 0))

        self.assertEqual(len(callback_handlers), 5)
        # Pyrogram runs only the first matching handler in a group. These
        # handlers all share ADMIN_CALLBACK, so they must not share a group.
        self.assertEqual(
            len(set(callback_groups)),
            len(callback_handlers),
            msg=f"callback handlers share groups: {callback_groups}",
        )
        for handler in callback_handlers:
            data = (
                f"{user.CHECK_SUB_PREFIX}example-file"
                if handler.callback is user.handle_check_sub
                else "mig:skip:1"
            )
            query = _callback_query(admin_id, data)

            self.assertFalse(
                hasattr(query, "chat"),
                msg=f"{handler.callback.__name__} received a chat attribute",
            )
            self.assertIsNotNone(handler.filters)
            self.assertTrue(
                await _run_filter(handler.filters, query),
                msg=f"{handler.callback.__name__} rejected a valid callback",
            )


if __name__ == "__main__":
    unittest.main()
