"""Unit tests for LIBRARY_CHANNELS parsing and format validation."""

from __future__ import annotations

import unittest

from utils.categories import (
    MAX_CATEGORY_NAME_LENGTH,
    parse_chat_ref,
    parse_library_channels,
)


class ParseChatRefTests(unittest.TestCase):
    def test_numeric_ids_become_ints(self) -> None:
        self.assertEqual(parse_chat_ref("-1001234567890"), -1001234567890)

    def test_usernames_stay_strings(self) -> None:
        self.assertEqual(parse_chat_ref("@my_library"), "@my_library")


class ParseLibraryChannelsTests(unittest.TestCase):
    def test_multiple_categories(self) -> None:
        categories, errors = parse_library_channels(
            "anime:@animelibrary_now, tamil:@tamilmovies_library,"
            "hollywood:-1001234567890"
        )

        self.assertEqual(errors, [])
        self.assertEqual(
            categories,
            {
                "anime": "@animelibrary_now",
                "tamil": "@tamilmovies_library",
                "hollywood": -1001234567890,
            },
        )

    def test_legacy_single_channel_becomes_default(self) -> None:
        categories, errors = parse_library_channels(None, "@old_library")

        self.assertEqual(errors, [])
        self.assertEqual(categories, {"default": "@old_library"})

    def test_new_setting_wins_over_legacy(self) -> None:
        categories, errors = parse_library_channels(
            "anime:@animelibrary_now", "@old_library"
        )

        self.assertEqual(errors, [])
        self.assertEqual(categories, {"anime": "@animelibrary_now"})

    def test_empty_configuration_is_allowed(self) -> None:
        self.assertEqual(parse_library_channels(None, None), ({}, []))

    def test_missing_colon_is_reported(self) -> None:
        categories, errors = parse_library_channels("anime")

        self.assertEqual(categories, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("must use category:@channel", errors[0])

    def test_empty_category_and_channel_are_reported(self) -> None:
        categories, errors = parse_library_channels(
            ":@channel,anime:"
        )

        self.assertEqual(categories, {})
        self.assertEqual(len(errors), 2)
        self.assertIn("empty category", errors[0])
        self.assertIn("no channel", errors[1])

    def test_duplicate_category_is_reported_case_insensitively(self) -> None:
        categories, errors = parse_library_channels(
            "anime:@first,Anime:@second"
        )

        self.assertEqual(categories, {"anime": "@first"})
        self.assertEqual(len(errors), 1)
        self.assertIn("duplicate category", errors[0])

    def test_overlong_category_is_reported(self) -> None:
        name = "x" * (MAX_CATEGORY_NAME_LENGTH + 1)
        categories, errors = parse_library_channels(f"{name}:@channel")

        self.assertEqual(categories, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("too long", errors[0])

    def test_empty_entry_is_reported(self) -> None:
        categories, errors = parse_library_channels(
            "anime:@first,,tamil:@second"
        )

        self.assertEqual(
            categories,
            {"anime": "@first", "tamil": "@second"},
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("entry 2 is empty", errors[0])


if __name__ == "__main__":
    unittest.main()
