"""Parsing and format validation for the LIBRARY_CHANNELS setting."""

from __future__ import annotations

from typing import Optional, Union

CategoryTarget = Union[int, str]
MAX_CATEGORY_NAME_LENGTH = 64


def parse_chat_ref(value: str) -> CategoryTarget:
    """Return an int for numeric Telegram IDs and a string otherwise."""
    raw = value.strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw


def parse_library_channels(
    raw: Optional[str], legacy_raw: Optional[str] = None
) -> tuple[dict[str, CategoryTarget], list[str]]:
    """Parse ``LIBRARY_CHANNELS`` and fall back to the legacy setting.

    The format is ``category:@channel,category2:-100...``. Category names are
    shown as button labels, so they must be short single-line strings. The
    returned errors are user-facing configuration errors rather than
    exceptions, which lets ``config.py`` report every bad entry at once.
    """
    categories: dict[str, CategoryTarget] = {}
    errors: list[str] = []
    seen: dict[str, str] = {}

    if raw:
        for entry_number, entry in enumerate(raw.split(","), 1):
            entry = entry.strip()
            if not entry:
                errors.append(
                    f"LIBRARY_CHANNELS entry {entry_number} is empty"
                )
                continue
            if ":" not in entry:
                errors.append(
                    f"LIBRARY_CHANNELS entry {entry_number} ({entry!r}) must "
                    "use category:@channel or category:-100..."
                )
                continue

            category, channel = entry.split(":", 1)
            category = category.strip()
            channel = channel.strip()

            if not category:
                errors.append(
                    f"LIBRARY_CHANNELS entry {entry_number} has an empty "
                    "category"
                )
                continue
            if len(category) > MAX_CATEGORY_NAME_LENGTH:
                errors.append(
                    f"LIBRARY_CHANNELS category {category!r} is too long "
                    f"(maximum {MAX_CATEGORY_NAME_LENGTH} characters)"
                )
                continue
            if any(ord(character) < 32 for character in category):
                errors.append(
                    f"LIBRARY_CHANNELS category {category!r} must be a "
                    "single line without control characters"
                )
                continue

            key = category.casefold()
            if key in seen:
                errors.append(
                    f"LIBRARY_CHANNELS contains duplicate category "
                    f"{category!r} (already defined as {seen[key]!r})"
                )
                continue
            if not channel:
                errors.append(
                    f"LIBRARY_CHANNELS category {category!r} has no channel"
                )
                continue

            seen[key] = category
            categories[category] = parse_chat_ref(channel)

        return categories, errors

    # Backward compatibility: old single-channel configs become "default".
    if legacy_raw:
        return {"default": parse_chat_ref(legacy_raw)}, []

    return {}, []
